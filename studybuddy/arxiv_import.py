"""arXiv / DOI import + citation-graph logic (Worker B, #25).

Logic-only module: no Flask, no routes. The integrator registers routes and
passes metadata between here, the study_import page, and the study library.
All network calls use ``requests`` with ``timeout=15`` and a polite
User-Agent / mailto contact string.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import defusedxml.ElementTree as ET
from datetime import UTC, datetime

import requests

from studybuddy.safe_fetch import (  # SEC-02: SSRF-safe fetching
    SSRFError,
    safe_fetch_stream,
    validate_url,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------

TIMEOUT = 15
_MAX_RETRIES = 2  # max 2 retries -> up to 3 attempts per external call
# RAG-06: contact email for the User-Agent "mailto:" token comes from the
# environment; the default keeps existing behavior byte-identical.
MAILTO = os.environ.get("RESEARCH_CONTACT_EMAIL", "research@student.local").strip()
USER_AGENT = (
    f"ACRA-ResearchAssistant/1.0 (mailto:{MAILTO}; educational research assistant)"
)
# NOTE: plain HTTP to export.arxiv.org fails from some networks (connection
# reset); HTTPS works everywhere, so the API base is HTTPS-only.
ARXIV_API = "https://export.arxiv.org/api/query"
OPENALEX_API = "https://api.openalex.org"

_ARXIV_NEW_RE = re.compile(r"^(\d{2})(\d{2})\.(\d{4,5})$")
_ARXIV_OLD_RE = re.compile(r"^([a-z\-]+(?:\.[A-Z]{2})?)/(\d{2})(\d{2})(\d{3})$")
# Official DOI syntax: https://www.doi.org/doi_handbook/2_Numbering.html
_DOI_RE = re.compile(r"^10\.\d{4,9}/[-._;()/:A-Z0-9]+$", re.IGNORECASE)


def _valid_yymm(yy: str, mm: str) -> bool:
    """YYMM sanity: month must be 01-12. Any two-digit year is accepted —
    arXiv started in 1991 and keeps growing, so the year carries no strict
    upper bound."""
    try:
        month = int(mm)
        int(yy)
    except ValueError:
        return False
    return 1 <= month <= 12

_ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


def _headers() -> dict:
    return {"User-Agent": USER_AGENT, "Accept": "application/json, application/xml"}


def _get(url: str, params: dict | None = None) -> requests.Response:
    """GET with timeout=15, polite headers, and up to 2 retries on network
    failures (ConnectionError/Timeout). HTTP error statuses are NOT retried —
    they are returned to the caller for a clean error dict."""
    last_exc: requests.RequestException | None = None
    for _ in range(_MAX_RETRIES + 1):
        try:
            return requests.get(url, params=params, headers=_headers(), timeout=TIMEOUT)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
    raise last_exc  # type: ignore[misc]


def _get_stream(url: str):
    """Streaming GET with the same retry policy as _get."""
    last_exc: requests.RequestException | None = None
    for _ in range(_MAX_RETRIES + 1):
        try:
            return requests.get(url, headers=_headers(), timeout=TIMEOUT, stream=True)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
    raise last_exc  # type: ignore[misc]


# --------------------------------------------------------------------------
# 1. ID validation / normalization
# --------------------------------------------------------------------------

def validate_arxiv_id(s: str) -> str | None:
    """Normalize an arXiv identifier, stripping any version suffix.

    Accepts "arXiv:1706.03762v2", "1706.03762", "hep-th/9901001", or an
    https://arxiv.org/abs/… URL. Returns the bare id or None if invalid.
    """
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    s = re.sub(r"^https?://(export\.)?arxiv\.org/abs/", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^https?://(export\.)?arxiv\.org/pdf/", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^arxiv:", "", s, flags=re.IGNORECASE)
    s = s.rstrip("/")
    s = re.sub(r"\.pdf$", "", s, flags=re.IGNORECASE)
    # strip version suffix like v2, v10
    s = re.sub(r"v\d+$", "", s)
    m = _ARXIV_NEW_RE.match(s)
    if m:
        return s if _valid_yymm(m.group(1), m.group(2)) else None
    m = _ARXIV_OLD_RE.match(s)
    if m:
        return s if _valid_yymm(m.group(2), m.group(3)) else None
    return None


def validate_doi(s: str) -> str | None:
    """Normalize a DOI to its bare "10.xxxx/yyyy" form, or None if invalid."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^doi:\s*", "", s, flags=re.IGNORECASE)
    s = s.rstrip("/")
    if _DOI_RE.match(s):
        return s
    return None


# --------------------------------------------------------------------------
# 2. Metadata normalization
# --------------------------------------------------------------------------

_METADATA_KEYS = (
    "title",
    "authors",
    "abstract",
    "year",
    "venue",
    "doi",
    "arxiv_id",
    "pdf_url",
    "openalex_id",
    "url",
    "source",
    "fetched_at",
    "is_retracted",
    "oa_version",
)


def normalize_metadata(raw: dict | None) -> dict:
    """Force metadata into the exact canonical key set.

    Missing fields become None (or [] for authors). Authors are "First Last"
    strings. Provenance is always attached: ``source`` ("arxiv"|"openalex")
    and ``fetched_at`` (UTC ISO-8601 timestamp). Passes through
    {"error": ...} dicts unchanged.
    """
    if not isinstance(raw, dict):
        raw = {}
    if "error" in raw:
        return dict(raw)
    out: dict = {}
    for key in _METADATA_KEYS:
        out[key] = raw.get(key)
    out["authors"] = [str(a) for a in (raw.get("authors") or [])]
    year = raw.get("year")
    try:
        out["year"] = int(year) if year is not None and str(year).strip() != "" else None
    except (TypeError, ValueError):
        out["year"] = None
    if out["source"] not in ("arxiv", "openalex"):
        out["source"] = raw.get("source") or None
    # Retraction/version flags are additive provenance: coerce defensively
    # (raw dicts from older callers may carry them as 0/1 or strings).
    out["is_retracted"] = bool(raw.get("is_retracted"))
    out["oa_version"] = raw.get("oa_version") or None
    out["fetched_at"] = raw.get("fetched_at") or datetime.now(UTC).isoformat()
    title = out.get("title")
    out["title"] = re.sub(r"\s+", " ", str(title)).strip() if title else None
    return out


# --------------------------------------------------------------------------
# 3. arXiv fetch
# --------------------------------------------------------------------------

def _arxiv_entry_to_raw(entry: ET.Element) -> dict:
    def text(tag: str, ns: str = "atom") -> str | None:
        node = entry.find(f"{ns}:{tag}", _ATOM_NS)
        if node is not None and node.text:
            return node.text.strip()
        return None

    title = text("title")
    summary = text("summary")
    published = text("published")
    year = None
    if published:
        m = re.match(r"(\d{4})", published)
        year = int(m.group(1)) if m else None

    authors = []
    for a in entry.findall("atom:author", _ATOM_NS):
        name = a.find("atom:name", _ATOM_NS)
        if name is not None and name.text:
            authors.append(re.sub(r"\s+", " ", name.text).strip())

    abs_url = None
    pdf_url = None
    for link in entry.findall("atom:link", _ATOM_NS):
        rel = link.attrib.get("rel")
        href = link.attrib.get("href")
        ltype = link.attrib.get("type", "")
        title_attr = link.attrib.get("title")
        if title_attr == "pdf" or "application/pdf" in ltype:
            pdf_url = href
        elif rel == "alternate" or abs_url is None:
            abs_url = href

    doi = text("doi", ns="arxiv")
    raw_id = abs_url or ""
    arxiv_id = validate_arxiv_id(raw_id) or validate_arxiv_id(
        (entry.find("atom:id", _ATOM_NS).text or "") if entry.find("atom:id", _ATOM_NS) is not None else ""
    )

    venue = text("journal_ref", ns="arxiv")
    categories = [c.attrib.get("term") for c in entry.findall("atom:category", _ATOM_NS) if c.attrib.get("term")]
    if not venue and categories:
        venue = "arXiv:" + ", ".join(categories[:2])

    return {
        "title": title,
        "authors": authors,
        "abstract": summary,
        "year": year,
        "venue": venue,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "pdf_url": pdf_url or (f"https://arxiv.org/pdf/{arxiv_id}.pdf" if arxiv_id else None),
        "openalex_id": None,
        "url": abs_url,
        "source": "arxiv",
    }


def fetch_arxiv(arxiv_id: str) -> dict:
    """Fetch a paper from the arXiv Atom API. Never raises; {"error": ...} on failure."""
    norm = validate_arxiv_id(arxiv_id or "")
    if not norm:
        return {"error": f"Invalid arXiv id: {arxiv_id!r}"}
    try:
        resp = _get(ARXIV_API, params={"id_list": norm, "max_results": 1})
    except requests.RequestException as exc:
        return {"error": f"arXiv request failed: {exc}"}
    if resp.status_code != 200:
        return {"error": f"arXiv returned HTTP {resp.status_code}"}
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        return {"error": f"arXiv XML parse failed: {exc}"}
    entries = root.findall("atom:entry", _ATOM_NS)
    if not entries:
        return {"error": f"No arXiv record found for {norm}"}
    raw = _arxiv_entry_to_raw(entries[0])
    # Best-effort: resolve the OpenAlex id so the graph panel can open, and
    # cross-check titles between the two sources. On mismatch we keep the
    # arXiv record (the id the user supplied) but log a loud warning — wrong
    # metadata must never be stored silently.
    try:
        oa_id = _resolve_openalex_for_arxiv(norm)
        if oa_id:
            raw["openalex_id"] = oa_id
            oa_title = _openalex_title(oa_id)
            if oa_title and not _titles_match(raw.get("title"), oa_title):
                logger.warning(
                    "Title mismatch for arXiv %s (OpenAlex %s): arXiv title %r "
                    "vs OpenAlex title %r — keeping arXiv record, verify manually",
                    norm, oa_id, raw.get("title"), oa_title,
                )
            # Best-effort retraction + OA-version flags; a failure here must
            # never fail the import — the arXiv record stands on its own.
            flags = _openalex_flags(oa_id)
            if flags:
                raw["is_retracted"] = flags["is_retracted"]
                raw["oa_version"] = flags["oa_version"]
    except Exception:
        pass
    return raw


def _resolve_openalex_for_arxiv(arxiv_id: str) -> str | None:
    """Resolve an arXiv id to its OpenAlex id via the canonical URL form.

    OpenAlex resolves external ids in the path
    (GET /works/https://arxiv.org/abs/<id>); the filter=arxiv: form is not
    a documented filter and silently returned nothing, which left arXiv
    imports without openalex_id (breaking graph + citations for them).
    Never raises; None on any failure.
    """
    try:
        resp = _get(
            f"{OPENALEX_API}/works/https://arxiv.org/abs/{arxiv_id}",
            params={"mailto": MAILTO, "select": "id"},
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        work = resp.json()
    except ValueError:
        return None
    if not isinstance(work, dict):
        return None
    return _short_openalex_id(work.get("id"))


def _titles_match(a: str | None, b: str | None) -> bool:
    """Loose title comparison for cross-source sanity checks."""
    if not a or not b:
        return True  # nothing to compare; don't cry wolf
    def norm(t: str) -> str:
        return re.sub(r"[^a-z0-9]", "", t.lower())
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return True
    if na == nb:
        return True
    longer, shorter = (na, nb) if len(na) >= len(nb) else (nb, na)
    return len(shorter) >= 20 and shorter in longer


def _openalex_title(short_id: str) -> str | None:
    resp = _get(
        f"{OPENALEX_API}/works/{short_id}",
        params={"mailto": MAILTO, "select": "id,title"},
    )
    if resp.status_code != 200:
        return None
    return resp.json().get("title")


def _openalex_flags(short_id: str) -> dict | None:
    """Best-effort retraction + OA-version flags for an OpenAlex work.

    Same fetch pattern as _openalex_title. Never raises; None on any
    failure so callers can keep the arXiv record without these flags.
    """
    try:
        resp = _get(
            f"{OPENALEX_API}/works/{short_id}",
            params={"mailto": MAILTO, "select": "id,is_retracted,best_oa_location"},
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        work = resp.json()
    except ValueError:
        return None
    if not isinstance(work, dict):
        return None
    best = work.get("best_oa_location") or {}
    return {
        "is_retracted": bool(work.get("is_retracted")),
        "oa_version": best.get("version") or None,
    }


# --------------------------------------------------------------------------
# 4. OpenAlex fetch
# --------------------------------------------------------------------------

def _reconstruct_abstract(inverted: dict | None) -> str | None:
    if not inverted:
        return None
    positions: dict[int, str] = {}
    for word, idxs in inverted.items():
        for i in idxs:
            positions[i] = word
    if not positions:
        return None
    return " ".join(positions[i] for i in sorted(positions))


def _short_openalex_id(url_or_id: str | None) -> str | None:
    if not url_or_id:
        return None
    return re.sub(r"^https?://openalex\.org/", "", str(url_or_id).strip(), flags=re.IGNORECASE)


def _openalex_work_to_raw(work: dict) -> dict:
    authors = []
    for a in work.get("authorships") or []:
        author = (a or {}).get("author") or {}
        name = author.get("display_name")
        if name:
            authors.append(name)

    loc = work.get("primary_location") or {}
    source = loc.get("source") or {}
    venue = source.get("display_name") or (loc.get("display_name"))
    if not venue and work.get("type"):
        venue = str(work.get("type")).replace("-", " ").title()

    ids = work.get("ids") or {}
    doi_url = ids.get("doi") or work.get("doi")
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi_url, flags=re.IGNORECASE) if doi_url else None
    arxiv_url = ids.get("arxiv")
    arxiv_id = validate_arxiv_id(arxiv_url) if arxiv_url else None

    pdf_url = None
    best = work.get("best_oa_location") or {}
    if best.get("pdf_url"):
        pdf_url = best["pdf_url"]
    elif loc.get("pdf_url"):
        pdf_url = loc["pdf_url"]
    elif arxiv_id:
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"

    return {
        "title": work.get("title"),
        "authors": authors,
        "abstract": _reconstruct_abstract(work.get("abstract_inverted_index")),
        "year": work.get("publication_year"),
        "venue": venue,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "pdf_url": pdf_url,
        "openalex_id": _short_openalex_id(work.get("id")),
        "url": doi_url or (work.get("id")),
        "source": "openalex",
        # Retraction + OA-version provenance (additive; None/False when absent).
        "is_retracted": bool(work.get("is_retracted")),
        "oa_version": (best.get("version") or None),
    }


def fetch_doi(doi: str) -> dict:
    """Fetch a work by DOI from OpenAlex. Never raises; {"error": ...} on failure."""
    norm = validate_doi(doi or "")
    if not norm:
        return {"error": f"Invalid DOI: {doi!r}"}
    try:
        resp = _get(f"{OPENALEX_API}/works/https://doi.org/{norm}", params={"mailto": MAILTO})
    except requests.RequestException as exc:
        return {"error": f"OpenAlex request failed: {exc}"}
    if resp.status_code == 404:
        return {"error": f"No OpenAlex record for DOI {norm}"}
    if resp.status_code != 200:
        return {"error": f"OpenAlex returned HTTP {resp.status_code}"}
    try:
        work = resp.json()
    except ValueError as exc:
        return {"error": f"OpenAlex JSON parse failed: {exc}"}
    if not isinstance(work, dict) or not work.get("id"):
        return {"error": f"OpenAlex returned an unexpected payload for {norm}"}
    return _openalex_work_to_raw(work)


# --------------------------------------------------------------------------
# 5. Citation graph
# --------------------------------------------------------------------------

_SELECT = "id,title,authorships,publication_year,cited_by_count,doi"


def _node_from_work(work: dict, is_center: bool = False) -> dict:
    authors = [
        (a.get("author") or {}).get("display_name")
        for a in (work.get("authorships") or [])
        if (a.get("author") or {}).get("display_name")
    ]
    doi_url = ((work.get("ids") or {}).get("doi")) or work.get("doi") or ""
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi_url, flags=re.IGNORECASE) if doi_url else None
    return {
        "id": _short_openalex_id(work.get("id")),
        "title": work.get("title"),
        "authors": authors,
        "year": work.get("publication_year"),
        "cited_by_count": work.get("cited_by_count") or 0,
        "is_center": is_center,
        "doi": doi,
    }


def _details_for_ids(openalex_ids: list[str], limit: int) -> list[dict]:
    """Bulk-fetch work summaries for a list of OpenAlex short ids."""
    if not openalex_ids or limit <= 0:
        return []
    chunk = "|".join(openalex_ids[:limit])
    try:
        resp = _get(
            f"{OPENALEX_API}/works",
            params={
                "filter": f"openalex:{chunk}",
                "per-page": min(limit, 50),
                "select": _SELECT,
                "mailto": MAILTO,
            },
        )
    except requests.RequestException:
        return []
    if resp.status_code != 200:
        return []
    try:
        results = resp.json().get("results") or []
    except ValueError:
        return []
    return results


def _graph_cache_fallback(cache_key: str, message: str) -> dict:
    """On API failure (timeout/429/5xx), serve a previously cached graph if one
    exists, flagged with from_cache=True. Otherwise return the error dict."""
    cached = cache_get(cache_key)
    if isinstance(cached, dict) and cached.get("nodes"):
        logger.warning("Citation graph degraded (%s); serving cached graph", message)
        out = dict(cached)
        out["from_cache"] = True
        return out
    return {"error": message}


def fetch_citation_graph(openalex_id: str, max_nodes: int = 25) -> dict:
    """Build a small citation graph around an OpenAlex work.

    Outgoing edges: works the center cites (referenced_works).
    Incoming edges: works that cite the center (filter=cites:<id>), sampled
    by cited_by_count desc. Total nodes capped at max_nodes; nodes deduped
    by OpenAlex id. Successful results are cached for 24h so a later
    timeout/429/5xx can degrade gracefully to the cached graph.
    Never raises; {"error": ...} on failure.
    """
    short = _short_openalex_id(openalex_id)
    if not short:
        return {"error": f"Invalid OpenAlex id: {openalex_id!r}"}
    cache_key = f"citation_graph:{short}"
    try:
        resp = _get(
            f"{OPENALEX_API}/works/{short}",
            params={"mailto": MAILTO, "select": f"{_SELECT},referenced_works"},
        )
    except requests.RequestException as exc:
        return _graph_cache_fallback(cache_key, f"OpenAlex request failed: {exc}")
    if resp.status_code == 404:
        return _graph_cache_fallback(cache_key, f"No OpenAlex record for {short}")
    if resp.status_code != 200:
        return _graph_cache_fallback(
            cache_key, f"OpenAlex returned HTTP {resp.status_code}"
        )
    try:
        work = resp.json()
    except ValueError as exc:
        return _graph_cache_fallback(cache_key, f"OpenAlex JSON parse failed: {exc}")
    if not isinstance(work, dict) or not work.get("id"):
        return _graph_cache_fallback(cache_key, "OpenAlex returned an unexpected payload")

    budget = max(int(max_nodes) - 1, 0)
    n_out = budget // 2
    n_in = budget - n_out

    referenced = [ _short_openalex_id(u) for u in (work.get("referenced_works") or []) ]
    referenced = [r for r in referenced if r]

    cited_by: list[dict] = []
    try:
        r2 = _get(
            f"{OPENALEX_API}/works",
            params={
                "filter": f"cites:{short}",
                "per-page": min(max(n_in, 1), 50),
                "select": _SELECT,
                "sort": "cited_by_count:desc",
                "mailto": MAILTO,
            },
        )
        if r2.status_code == 200:
            cited_by = r2.json().get("results") or []
    except (requests.RequestException, ValueError):
        cited_by = []

    nodes = [_node_from_work(work, is_center=True)]
    edges = []
    seen = {short}

    out_details = _details_for_ids(referenced, n_out)
    for det in out_details:
        sid = _short_openalex_id(det.get("id"))
        if not sid or sid in seen:
            continue
        seen.add(sid)
        nodes.append(_node_from_work(det))
        edges.append({"from": short, "to": sid})

    for det in cited_by:
        if len(nodes) >= max_nodes:
            break
        sid = _short_openalex_id(det.get("id"))
        if not sid or sid in seen:
            continue
        seen.add(sid)
        nodes.append(_node_from_work(det))
        edges.append({"from": sid, "to": short})

    graph = {"nodes": nodes[:max_nodes], "edges": edges}
    cache_set(cache_key, graph)  # 24h; enables degraded fallback on later failures
    return graph


# --------------------------------------------------------------------------
# 6. File cache (data/cache/, 24h TTL)
# --------------------------------------------------------------------------

_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cache")


def _cache_path(key: str) -> str:
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
    return os.path.join(_CACHE_DIR, digest + ".json")


def cache_set(key: str, value, ttl: int = 86400) -> bool:
    """Store a JSON-serializable value under key. Returns True on success."""
    tmp = _cache_path(key) + ".tmp"
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        payload = {"exp": time.time() + ttl, "value": value}
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, _cache_path(key))
        return True
    except (OSError, TypeError, ValueError):
        return False
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def cache_get(key: str, default=None):
    """Return the cached value, or default if missing/expired/corrupt."""
    try:
        with open(_cache_path(key), encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return default
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        try:
            os.remove(_cache_path(key))
        except OSError:
            pass
        return default
    return payload.get("value", default)


# --------------------------------------------------------------------------
# 7. PDF download
# --------------------------------------------------------------------------

_MAX_PDF_BYTES = 30 * 1024 * 1024


def download_pdf(pdf_url: str, dest_path: str) -> bool:
    """Stream a PDF to dest_path (max 30MB, content-type sanity check).

    SEC-02: the URL goes through the SSRF-safe fetcher — private IPs,
    metadata endpoints and non-HTTP schemes are rejected before any
    connection is opened. Partial downloads are always cleaned up in a
    finally block. Never raises.
    """
    if not pdf_url or not dest_path:
        return False
    dest = os.path.abspath(dest_path)
    # Policy gate first: SSRF-blocked URLs fail fast with no retries.
    try:
        validate_url(pdf_url)
    except SSRFError:
        return False
    ok = False
    try:
        for _ in range(_MAX_RETRIES + 1):
            try:
                with safe_fetch_stream(pdf_url) as resp:
                    if resp.status_code != 200:
                        return False
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    if "pdf" not in ctype:
                        return False
                    total = 0
                    parent = os.path.dirname(dest)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    with open(dest, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=65536):
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > _MAX_PDF_BYTES:
                                return False
                            fh.write(chunk)
                ok = os.path.getsize(dest) > 0
                return ok
            except (SSRFError, requests.RequestException):
                continue  # transient network failure; retry
        return False
    except OSError:
        return False
    finally:
        if not ok:
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except OSError:
                pass


# ================= Feature 19 Ultimate Enhancements =================
# 19.1 Bulk import | 19.2 Related-paper discovery | 19.3 Reading queue


def _clean13(text):
    return re.sub(r"<[^>]*>", "", text or "")


def bulk_import(ids_text, max_ids=10):
    """Import up to N papers from pasted arXiv IDs / DOIs (one per line/comma).

    Returns {imported: [metadata], failed: [{input, reason}], total}.
    Never raises on hostile input; each ID is attempted independently.
    """
    if not isinstance(ids_text, str):
        raise ValueError("ids_text must be a string of IDs.")
    try:
        max_ids = int(max_ids)
    except (TypeError, ValueError):
        max_ids = 10
    max_ids = max(1, min(25, max_ids))
    raw_ids = [p.strip() for p in re.split(r"[,\s\n;]+", _clean13(ids_text)) if p.strip()]
    seen = set()
    ids = []
    for rid in raw_ids:
        if rid.lower() not in seen:
            seen.add(rid.lower())
            ids.append(rid)
        if len(ids) >= max_ids:
            break
    imported, failed = [], []
    for rid in ids:
        try:
            arxiv_id = validate_arxiv_id(rid)
            doi = validate_doi(rid) if not arxiv_id else None
            if arxiv_id:
                meta = normalize_metadata(fetch_arxiv(arxiv_id))
            elif doi:
                meta = normalize_metadata(fetch_doi(doi))
            else:
                failed.append({"input": rid[:60], "reason": "Not a valid arXiv ID or DOI."})
                continue
            if meta.get("error"):
                failed.append({"input": rid[:60], "reason": str(meta["error"])[:120]})
            else:
                imported.append(meta)
        except Exception as e:
            failed.append({"input": rid[:60], "reason": f"{type(e).__name__}: {str(e)[:80]}"})
    return {"imported": imported, "failed": failed,
            "total": len(imported), "attempted": len(ids)}


def related_papers(openalex_id, max_results=10):
    """Discover related papers via the citation graph.

    Returns {cited_by: [...], references: [...], total} — papers that cite
    this one and papers it cites, with titles + years. Uses cached graph data.
    """
    openalex_id = _clean13(openalex_id or "").strip()
    if not openalex_id:
        raise ValueError("openalex_id is required.")
    try:
        max_results = int(max_results)
    except (TypeError, ValueError):
        max_results = 10
    max_results = max(1, min(25, max_results))
    try:
        graph = fetch_citation_graph(openalex_id, max_nodes=50)
    except Exception as e:
        return {"cited_by": [], "references": [],
                "error": f"Could not load citation graph: {type(e).__name__}",
                "total": 0}
    nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
    cited_by, references = [], []
    for n in nodes:
        if not isinstance(n, dict) or n.get("id") == openalex_id:
            continue
        entry = {"id": n.get("id"), "title": (n.get("title") or "")[:120],
                 "year": n.get("year")}
        rel = str(n.get("relation") or "").lower()
        if "cited" in rel or "cites_this" in rel:
            cited_by.append(entry)
        else:
            references.append(entry)
    return {"cited_by": cited_by[:max_results],
            "references": references[:max_results],
            "total": len(cited_by) + len(references),
            "guidance": ("'Cited by' = newer work building on this. "
                         "'References' = foundations to read first.")}


def _normalize_interests(interests) -> list:
    """Clean interest keywords (max 10)."""
    return [_clean13(i).strip().lower() for i in (interests or [])
            if isinstance(i, str) and _clean13(i).strip()][:10]


def _clamp_minutes(minutes_per_paper) -> int:
    """Coerce to int and clamp to [10, 180]; default 45."""
    try:
        minutes_per_paper = int(minutes_per_paper)
    except (TypeError, ValueError):
        minutes_per_paper = 45
    return max(10, min(180, minutes_per_paper))


def _paper_year(p) -> int | None:
    """Coerce the year field; None when missing/unparseable."""
    try:
        year = int(p.get("year") or 0)
    except (TypeError, ValueError):
        year = 0
    return year or None


def _score_one_paper(p, interests, now_year, minutes_per_paper) -> dict | None:
    """Score a single paper dict; None when it is not a dict."""
    if not isinstance(p, dict):
        return None
    title = _clean13(p.get("title") or "Untitled")[:120]
    abstract = _clean13(p.get("abstract") or "").lower()
    blob = (title + " " + abstract).lower()
    hits = sum(1 for kw in interests if kw in blob)
    year = _paper_year(p)
    recency = max(0, 5 - (now_year - year)) if year else 0
    score = hits * 10 + recency * 2
    why = []
    if hits:
        why.append(f"Matches your interests ({hits} keyword(s))")
    if recency >= 3:
        why.append("Recent paper")
    if not why:
        why.append("General background reading")
    # Longer abstracts ≈ denser papers ≈ more time.
    est = minutes_per_paper + (15 if len(abstract) > 1500 else 0)
    return {"id": p.get("id"), "title": title, "year": year,
            "priority": score, "why": "; ".join(why),
            "est_minutes": est}


def reading_queue(papers, interests=None, minutes_per_paper=45):
    """Prioritized reading queue with time estimates.

    papers: list of metadata dicts {title, year, abstract?, id}.
    interests: list of keyword strings to boost relevance.
    Returns {queue: [{title, year, priority, why, est_minutes}], total_minutes}.
    """
    if not isinstance(papers, (list, tuple)) or not papers:
        raise ValueError("papers must be a non-empty list.")
    interests = _normalize_interests(interests)
    minutes_per_paper = _clamp_minutes(minutes_per_paper)

    now_year = datetime.now(UTC).year
    queue = []
    for p in papers:
        entry = _score_one_paper(p, interests, now_year, minutes_per_paper)
        if entry is not None:
            queue.append(entry)
    queue.sort(key=lambda x: (-x["priority"], -(x["year"] or 0)))
    total = sum(q["est_minutes"] for q in queue)
    return {"queue": queue, "total_papers": len(queue),
            "total_minutes": total,
            "total_hours": round(total / 60, 1),
            "guidance": f"~{round(total/60, 1)}h of reading. Do 1-2 papers per day."}


# --------------------------------------------------------------------------
# 8. Cross-source paper search (import page: search by keyword, not just ID)
# --------------------------------------------------------------------------
# Every search function never raises: any network/parse/status failure
# returns [] so a merged search still works with the sources that did
# answer. HTTP error statuses are NOT retried (_get only retries network
# failures), so a Semantic Scholar 429 (shared-IP rate limit) returns []
# immediately with no retry and no API key.

S2_API = "https://api.semanticscholar.org/graph/v1/paper/search"
_OA_SEARCH_SELECT = (
    _SELECT
    + ",primary_location,best_oa_location,abstract_inverted_index,ids,"
    "is_retracted,type"
)


def _clamp_search_limit(n, default=10) -> int:
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = default
    return max(1, min(25, n))


def _norm_title_key(title) -> str:
    return re.sub(r"[^a-z0-9]", "", (title or "").lower())


def _norm_doi_key(doi) -> str:
    return (doi or "").strip().lower()


def search_openalex(query, max_results=10) -> list:
    """Search OpenAlex works by keyword. Never raises; [] on failure."""
    q = (query or "").strip()
    if not q:
        return []
    try:
        resp = _get(
            f"{OPENALEX_API}/works",
            params={"search": q, "per-page": _clamp_search_limit(max_results),
                    "select": _OA_SEARCH_SELECT, "mailto": MAILTO},
        )
    except requests.RequestException:
        return []
    if resp.status_code != 200:
        return []
    try:
        results = resp.json().get("results") or []
    except ValueError:
        return []
    out = []
    for w in results:
        if not isinstance(w, dict):
            continue
        raw = _openalex_work_to_raw(w)
        raw["cited_by_count"] = w.get("cited_by_count")
        out.append(raw)
    return out


def search_arxiv(query, max_results=10) -> list:
    """Search arXiv by keyword (all fields). Never raises; [] on failure.

    OpenAlex id resolution is deliberately skipped here for speed — the
    merged result fills openalex_id from another source when available.
    """
    q = (query or "").strip()
    if not q:
        return []
    safe = q.replace('"', " ").strip()
    if not safe:
        return []
    try:
        resp = _get(
            ARXIV_API,
            params={"search_query": f'all:"{safe}"',
                    "max_results": _clamp_search_limit(max_results)},
        )
    except requests.RequestException:
        return []
    if resp.status_code != 200:
        return []
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        return []
    out = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        try:
            raw = _arxiv_entry_to_raw(entry)
        except Exception:
            continue
        raw["cited_by_count"] = None
        out.append(raw)
    return out


def search_semanticscholar(query, max_results=10) -> list:
    """Search Semantic Scholar by keyword. Never raises; [] on failure.

    Unauthenticated S2 requests may 429 on shared IPs — that is treated
    exactly like any other failure: silent [], no retry, no API key.
    """
    q = (query or "").strip()
    if not q:
        return []
    try:
        resp = _get(
            S2_API,
            params={"query": q,
                    "fields": "title,authors,year,venue,externalIds,abstract,"
                              "openAccessPdf,citationCount",
                    "limit": _clamp_search_limit(max_results)},
        )
    except requests.RequestException:
        return []
    if resp.status_code != 200:
        return []
    try:
        data = resp.json().get("data") or []
    except ValueError:
        return []
    out = []
    for p in data:
        if not isinstance(p, dict):
            continue
        ext = p.get("externalIds") or {}
        arxiv_id = None
        if ext.get("ArXiv"):
            arxiv_id = validate_arxiv_id(ext.get("ArXiv"))
        doi = ext.get("DOI") or None
        oa = p.get("openAccessPdf") or {}
        pdf_url = oa.get("url") or (
            f"https://arxiv.org/pdf/{arxiv_id}.pdf" if arxiv_id else None)
        authors = [a.get("name") for a in (p.get("authors") or [])
                   if isinstance(a, dict) and a.get("name")]
        out.append({
            "title": p.get("title"),
            "authors": authors,
            "abstract": p.get("abstract"),
            "year": p.get("year"),
            "venue": p.get("venue"),
            "doi": doi,
            "arxiv_id": arxiv_id,
            "pdf_url": pdf_url,
            "openalex_id": None,
            "url": None,
            "source": "semanticscholar",
            "cited_by_count": p.get("citationCount"),
        })
    return out


def _merge_key(raw: dict):
    """Dedupe key: DOI, else arXiv ID, else normalized title."""
    doi = _norm_doi_key(raw.get("doi"))
    if doi:
        return ("doi", doi)
    arxiv_id = (raw.get("arxiv_id") or "").strip()
    if arxiv_id:
        return ("arxiv", arxiv_id)
    title = _norm_title_key(raw.get("title"))
    if title:
        return ("title", title)
    return None


def search_papers(query, max_results=10) -> list:
    """Merged keyword search across OpenAlex + arXiv + Semantic Scholar.

    Dedupe by DOI, else arXiv ID, else normalized title. Merged entries
    carry sources=[...] and source="merged" when >1 source matched, else
    the single source. Preference: arXiv title/abstract when present,
    OpenAlex venue/year/cited_by_count when present; other fields take
    the first non-empty value. Never raises; [] on empty query or when
    every source fails. Results capped at max_results (max 25).
    """
    q = (query or "").strip()
    if not q:
        return []
    limit = _clamp_search_limit(max_results)
    merged: list[dict] = []
    by_key: dict = {}
    for fn in (search_openalex, search_arxiv, search_semanticscholar):
        try:
            results = fn(q, limit)
        except Exception:
            results = []
        for raw in results or []:
            if not isinstance(raw, dict):
                continue
            key = _merge_key(raw)
            if key is None:
                continue
            entry = by_key.get(key)
            src = raw.get("source")
            if entry is None:
                entry = {
                    "title": raw.get("title"),
                    "authors": list(raw.get("authors") or []),
                    "abstract": raw.get("abstract"),
                    "year": raw.get("year"),
                    "venue": raw.get("venue"),
                    "doi": raw.get("doi"),
                    "arxiv_id": raw.get("arxiv_id"),
                    "pdf_url": raw.get("pdf_url"),
                    "openalex_id": raw.get("openalex_id"),
                    "cited_by_count": raw.get("cited_by_count"),
                    "sources": [src] if src else [],
                    "_by_source": {src: raw} if src else {},
                }
                by_key[key] = entry
                merged.append(entry)
            else:
                if src and src not in entry["sources"]:
                    entry["sources"].append(src)
                if src:
                    entry["_by_source"][src] = raw
                for field in ("doi", "arxiv_id", "pdf_url", "openalex_id"):
                    if not entry.get(field) and raw.get(field):
                        entry[field] = raw.get(field)
                if not entry.get("authors") and raw.get("authors"):
                    entry["authors"] = list(raw.get("authors"))
    out = []
    for entry in merged[:limit]:
        by_source = entry.pop("_by_source", {})
        arxiv_raw = by_source.get("arxiv") or {}
        if arxiv_raw.get("title"):
            entry["title"] = arxiv_raw.get("title")
        if arxiv_raw.get("abstract"):
            entry["abstract"] = arxiv_raw.get("abstract")
        oa_raw = by_source.get("openalex") or {}
        for field in ("venue", "year", "cited_by_count"):
            if oa_raw.get(field) is not None:
                entry[field] = oa_raw.get(field)
        sources = entry["sources"]
        entry["source"] = "merged" if len(sources) > 1 else (
            sources[0] if sources else None)
        out.append(entry)
    return out


# --------------------------------------------------------------------------
# 9. New citations + bridge papers for the user's imports
# --------------------------------------------------------------------------

def _recent_citations_uncached(ids: list, per_paper: int) -> dict:
    """Fetch citing works per import id (no cache, no raise on per-id failure)."""
    select = _SELECT + ",publication_date"
    titles: dict[str, str | None] = {}
    for sid in ids:
        try:
            titles[sid] = _openalex_title(sid)
        except Exception:
            titles[sid] = None
    recent: list[dict] = []
    by_citing: dict[str, dict] = {}
    for sid in ids:
        try:
            resp = _get(
                f"{OPENALEX_API}/works",
                params={"filter": f"cites:{sid}",
                        "sort": "publication_date:desc",
                        "per-page": per_paper,
                        "select": select,
                        "mailto": MAILTO},
            )
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        try:
            works = resp.json().get("results") or []
        except ValueError:
            continue
        for w in works:
            if not isinstance(w, dict):
                continue
            cid = _short_openalex_id(w.get("id"))
            if not cid:
                continue
            authors = [
                (a.get("author") or {}).get("display_name")
                for a in (w.get("authorships") or [])
                if (a.get("author") or {}).get("display_name")
            ]
            doi_url = ((w.get("ids") or {}).get("doi")) or w.get("doi") or ""
            doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi_url,
                         flags=re.IGNORECASE) if doi_url else None
            recent.append({
                "title": w.get("title"),
                "authors": authors,
                "year": w.get("publication_year"),
                "doi": doi,
                "openalex_id": cid,
                "cited_paper_title": titles.get(sid),
                "cited_paper_openalex_id": sid,
            })
            slot = by_citing.get(cid)
            if slot is None:
                slot = {
                    "entry": {
                        "title": w.get("title"),
                        "authors": authors,
                        "year": w.get("publication_year"),
                        "doi": doi,
                        "openalex_id": cid,
                    },
                    "cited": [],
                }
                by_citing[cid] = slot
            if sid not in slot["cited"]:
                slot["cited"].append(sid)
    bridges = []
    for slot in by_citing.values():
        if len(slot["cited"]) >= 2:
            bridges.append({
                **slot["entry"],
                "cites": [titles.get(s) or s for s in slot["cited"]],
            })
    return {"recent": recent, "bridges": bridges}


def recent_citations(openalex_ids, per_paper=5, max_papers=8) -> dict:
    """Recent citing works + bridge papers for the given OpenAlex ids.

    A citing work that cites >=2 of MY imports is a bridge (it connects
    two of my topics). Result cached 24h under a key hashing the sorted
    id list. Never raises: on total failure returns empty lists.
    """
    ids: list[str] = []
    for raw in (openalex_ids or []):
        sid = _short_openalex_id(raw)
        if sid and sid not in ids:
            ids.append(sid)
    try:
        max_n = int(max_papers) if max_papers is not None else 8
    except (TypeError, ValueError):
        max_n = 8
    ids = ids[:max(0, max_n)]
    if not ids:
        return {"recent": [], "bridges": []}
    try:
        per = int(per_paper) if per_paper is not None else 5
    except (TypeError, ValueError):
        per = 5
    per = max(1, min(50, per))
    cache_key = (
        "recent_citations:"
        + hashlib.sha256(",".join(sorted(ids)).encode("utf-8")).hexdigest()
        + f":{per}"
    )
    cached = cache_get(cache_key)
    if isinstance(cached, dict):
        return cached
    try:
        result = _recent_citations_uncached(ids, per)
    except Exception:
        return {"recent": [], "bridges": []}
    cache_set(cache_key, result)
    return result


