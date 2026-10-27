"""Internship / scholarship / exam / hackathon / competition opportunity radar.

Serves opportunities from data/opportunities.json. The dataset mixes:
- curated entries (curated=true): hand-maintained, NEVER touched by refresh
- auto entries (curated=false): refreshed automatically every STALE_AFTER_DAYS
  from free public sources (see opportunity_sources.py)

AUTO-REFRESH DESIGN (works on Render free tier — no external cron needed):
- load_dataset() checks meta.last_updated on every (cached) load. If the data
  is older than STALE_AFTER_DAYS and no refresh is already running (guarded by
  a lock file with 30-min TTL, so multi-worker gunicorn can't stampede), it
  spawns a daemon thread running refresh_opportunities() and returns the
  current (slightly stale) data immediately — requests are never blocked.
- refresh_opportunities() fetches all sources, merges, and ATOMICALLY rewrites
  data/opportunities.json (temp file + os.replace), then clears the lru_cache
  so the next request serves fresh data.
- Merge rules: dedupe by normalized link URL; curated entries are never
  deleted or modified; auto entries with deadline < today are pruned; auto
  entries are capped at MAX_AUTO_ENTRIES (soonest deadlines first); every
  merged entry is schema-validated (invalid ones are dropped with a warning,
  never crash the refresh).

OPPORTUNITY JSON SCHEMA (for extending data/opportunities.json):
{
    "meta": {
        "last_updated": "YYYY-MM-DD",   # required, ISO date
        "notice": "string",              # required, shown on the board
        "schema_doc": "string"           # required, pointer to this docstring
    },
    "opportunities": [                   # required, non-empty list
        {
            "id": "kebab-case-unique",   # required, unique across the file
            "title": "string",           # required, <= 120 chars
            "organization": "string",    # required, <= 120 chars
            "type": "internship|scholarship|exam|hackathon|competition",
            "deadline": "YYYY-MM-DD",    # required ISO date, or null = rolling
            "eligibility": "string",     # required, <= 300 chars
            "link": "https://...",       # required, http(s) URL
            "curated": true|false,       # optional; missing = curated (legacy)
            "source": "string",          # optional; e.g. "devpost"
            "tags": ["string"],          # optional; honest topic tags
            "status": "open|closed",    # optional; stamped by
                                          # scripts/refresh_opportunities.py
            "description": "string"       # optional; 1-2 factual sentences
        }
    ]
}
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)

DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "opportunities.json")
_LOCK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", ".refresh_lock")

TYPES = ("internship", "scholarship", "exam", "hackathon", "competition")
CLOSING_SOON_DAYS = 14

STALE_AFTER_DAYS = 2          # auto-refresh cadence
LOCK_TTL_SECONDS = 30 * 60    # refresh lock expiry (stuck-thread safety)
MAX_AUTO_ENTRIES = 60         # cap on auto (non-curated) entries

_refresh_lock = threading.Lock()  # guards merge+write within this process


class OpportunityDataError(Exception):
    """Raised when the opportunity dataset is missing or malformed."""


def _parse_deadline(value: str | None, opp_id: str) -> date | None:
    if value is None:
        return None
    try:
        year, month, day = (int(p) for p in str(value).split("-"))
        return date(year, month, day)
    except (ValueError, TypeError) as exc:
        raise OpportunityDataError(
            f"Opportunity {opp_id!r} has an invalid deadline.") from exc


def _validate_entry(
    o: dict[str, Any],
    seen_ids: set[str],
) -> dict[str, Any]:
    """Validate one raw entry dict. Returns normalized entry or raises."""
    for key in ("id", "title", "organization", "type", "deadline",
                "eligibility", "link"):
        if key not in o:
            raise OpportunityDataError(
                f"Opportunity {o.get('id', '?')!r} missing key: {key}")
    if o["id"] in seen_ids:
        raise OpportunityDataError(f"Duplicate opportunity id: {o['id']!r}")
    seen_ids.add(o["id"])
    if o["type"] not in TYPES:
        raise OpportunityDataError(
            f"Opportunity {o['id']!r} has invalid type: {o['type']!r}")
    if len(o["title"]) > 120 or len(o["organization"]) > 120:
        raise OpportunityDataError(
            f"Opportunity {o['id']!r} title/organization too long.")
    if len(o["eligibility"]) > 300:
        raise OpportunityDataError(
            f"Opportunity {o['id']!r} eligibility too long.")
    if not (str(o["link"]).startswith("https://")
            or str(o["link"]).startswith("http://")):
        raise OpportunityDataError(
            f"Opportunity {o['id']!r} link must be http(s).")
    _parse_deadline(o["deadline"], o["id"])
    return o


@lru_cache(maxsize=1)
def load_dataset() -> dict[str, Any]:
    """Load and validate the opportunity dataset (cached for process lifetime).

    If the data is stale (> STALE_AFTER_DAYS), kicks off a background refresh
    (once per LOCK_TTL_SECONDS across workers) and returns current data.
    """
    data = _load_from_disk()
    _maybe_trigger_refresh(data)
    return data


def _load_from_disk() -> dict[str, Any]:
    if not os.path.exists(DATA_PATH):
        raise OpportunityDataError("Opportunity dataset not found.")
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise OpportunityDataError("Opportunity dataset is unreadable.") from exc
    meta = data.get("meta") or {}
    for key in ("last_updated", "notice", "schema_doc"):
        if key not in meta:
            raise OpportunityDataError(f"Opportunity dataset meta missing: {key}")
    opps = data.get("opportunities")
    if not isinstance(opps, list) or not opps:
        raise OpportunityDataError("Opportunity dataset has no opportunities.")
    seen = set()
    for o in opps:
        _validate_entry(o, seen)
    return data


def _is_stale(meta: dict[str, Any], today: date | None = None) -> bool:
    """True if meta.last_updated is older than STALE_AFTER_DAYS."""
    today = today or date.today()
    try:
        y, m, d = (int(p) for p in str(meta.get("last_updated", "")).split("-"))
        updated = date(y, m, d)
    except (ValueError, TypeError, AttributeError):
        return True  # unparseable -> treat as stale, refresh will fix it
    return (today - updated).days >= STALE_AFTER_DAYS


def _refresh_already_running() -> bool:
    """Cross-process guard: True if a fresh lock file exists."""
    try:
        with open(_LOCK_PATH, encoding="utf-8") as f:
            ts = float(f.read().strip())
        return (time.time() - ts) < LOCK_TTL_SECONDS
    except (OSError, ValueError):
        return False


def _take_refresh_lock() -> None:
    try:
        with open(_LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError as exc:
        log.warning("Could not write refresh lock: %s", exc)


def _maybe_trigger_refresh(data: dict[str, Any]) -> None:
    """Spawn a background refresh if data is stale. Never raises."""
    try:
        # Tests and operators can disable auto-refresh entirely.
        if os.environ.get("OPP_NO_AUTO_REFRESH") == "1":
            return
        if not _is_stale(data.get("meta") or {}):
            return
        if _refresh_already_running():
            return
        _take_refresh_lock()
        t = threading.Thread(target=refresh_opportunities,
                             name="opp-refresh", daemon=True)
        t.start()
        log.info("Opportunity refresh triggered in background.")
    except Exception as exc:  # never break request handling
        log.warning("Failed to trigger opportunity refresh: %s", exc)


def _normalize_link(url: str) -> str:
    """Normalize a URL for dedupe: lowercase, strip trailing slash."""
    try:
        parts = urlsplit(str(url).strip())
        netloc = parts.netloc.lower()
        path = (parts.path.rstrip("/") or "/").lower()
        return urlunsplit((parts.scheme.lower(), netloc, path, "", ""))
    except Exception:
        return str(url).strip().lower()


def _is_curated(o: dict[str, Any]) -> bool:
    # Missing flag = curated (protects legacy entries).
    return o.get("curated", True) is True


def merge_opportunities(
    existing: list[dict[str, Any]],
    fetched: list[dict[str, Any]],
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Merge fetched auto entries into the existing list.

    Pure function (no IO) — easy to test. Returns the new list.
    Rules:
      * curated entries: kept verbatim, never modified or deleted
      * existing auto entries: re-validated; expired (deadline < today) or
        corrupt ones are pruned
      * dedupe by normalized link (existing entries win over fetched)
      * fetched entries are schema-validated; invalid ones dropped w/ warning
      * TOTAL auto entries capped at MAX_AUTO_ENTRIES (soonest deadlines
        first, null deadlines last) — the file never grows unboundedly
    """
    today = today or date.today()
    curated = []
    old_auto = []
    seen_ids = set()
    seen_links = set()

    for o in existing:
        seen_ids.add(o["id"])
        seen_links.add(_normalize_link(o["link"]))
        if _is_curated(o):
            curated.append(dict(o))
        else:
            try:
                _validate_entry(dict(o), set())
                dl = _parse_deadline(o["deadline"], o["id"])
                if dl is not None and dl < today:
                    continue  # expired auto entry
                old_auto.append(dict(o))
            except (OpportunityDataError, TypeError, KeyError):
                continue  # corrupt auto entry

    auto_new = []
    for raw in fetched:
        try:
            entry = {
                "id": str(raw["id"]),
                "title": str(raw["title"])[:120],
                "organization": str(raw["organization"])[:120],
                "type": str(raw["type"]),
                "deadline": raw["deadline"],
                "eligibility": str(raw["eligibility"])[:300],
                "link": str(raw["link"]),
                "curated": False,
                "source": str(raw.get("source", "auto")),
            }
            # Preserve extra informative keys (e.g. posted_days_ago) —
            # harmless for validation, useful for the UI.
            if "posted_days_ago" in raw:
                entry["posted_days_ago"] = raw["posted_days_ago"]
            # Validate fields only (fresh set): id collisions are resolved
            # by the uniquify pass below.
            _validate_entry(entry, set())
        except (KeyError, OpportunityDataError, TypeError) as exc:
            log.warning("Dropping invalid fetched opportunity: %s", exc)
            continue
        if _normalize_link(entry["link"]) in seen_links:
            continue  # already have it
        seen_links.add(_normalize_link(entry["link"]))
        auto_new.append(entry)

    # Ensure unique ids (existing ids are never renamed).
    for e in auto_new:
        base = e["id"]
        i = 2
        while e["id"] in seen_ids:
            e["id"] = f"{base}-{i}"
            i += 1
        seen_ids.add(e["id"])

    # Prune expired fetched entries, then cap the TOTAL auto pool.
    fresh_auto = []
    for e in auto_new:
        dl = _parse_deadline(e["deadline"], e["id"])
        if dl is not None and dl < today:
            continue
        fresh_auto.append(e)

    all_auto = old_auto + fresh_auto
    all_auto.sort(key=lambda e: (e["deadline"] is None, e["deadline"] or "9999"))
    all_auto = all_auto[:MAX_AUTO_ENTRIES]

    return curated + all_auto


def _atomic_write(data: dict[str, Any]) -> None:
    """Write JSON atomically: temp file + os.replace. Never leaves partial."""
    tmp_path = DATA_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, DATA_PATH)


def refresh_opportunities() -> bool:
    """Fetch sources, merge, atomically rewrite the dataset. Never raises."""
    try:
        from studybuddy.opportunity_sources import fetch_all  # local import: no cycle
        fetched = fetch_all()
    except Exception as exc:
        log.warning("Opportunity refresh: source fetch failed: %s", exc)
        return False
    try:
        with _refresh_lock:
            try:
                current = _load_from_disk()
            except OpportunityDataError as exc:
                log.warning("Opportunity refresh: current data unreadable: %s",
                            exc)
                return False
            today = date.today()
            merged = merge_opportunities(current["opportunities"], fetched,
                                         today=today)
            new_data = {
                "meta": {
                    **current.get("meta", {}),
                    "last_updated": today.isoformat(),
                },
                "opportunities": merged,
            }
            # Validate the final dataset before writing.
            seen = set()
            for o in merged:
                _validate_entry(o, seen)
            _atomic_write(new_data)
        load_dataset.cache_clear()
        log.info("Opportunity refresh complete: %d total (%d auto).",
                 len(merged),
                 sum(1 for o in merged if not _is_curated(o)))
        return True
    except Exception as exc:
        log.warning("Opportunity refresh failed: %s", exc)
        return False


def get_meta() -> dict[str, Any]:
    return dict(load_dataset()["meta"])


def freshness_label(
    meta: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> str:
    """Human label like 'Updated 5 hours ago · auto-refreshes every 2 days'."""
    meta = meta or get_meta()
    now = now or datetime.now()
    try:
        updated = datetime.strptime(str(meta.get("last_updated", "")),
                                    "%Y-%m-%d")
    except (ValueError, TypeError):
        return "Update time unknown · auto-refreshes every 2 days"
    delta = now - updated
    total_seconds = max(0, int(delta.total_seconds()))
    if total_seconds < 3600:
        ago = "just now" if total_seconds < 60 else \
            f"{total_seconds // 60} minutes ago"
    elif total_seconds < 86400:
        hrs = total_seconds // 3600
        ago = f"{hrs} hour{'s' if hrs != 1 else ''} ago"
    else:
        days = total_seconds // 86400
        ago = f"{days} day{'s' if days != 1 else ''} ago"
    return f"Updated {ago} · auto-refreshes every {STALE_AFTER_DAYS} days"


def _enrich(o: dict[str, Any], today: date) -> dict[str, Any]:
    """Return the public dict for one opportunity with countdown fields."""
    deadline = _parse_deadline(o["deadline"], o["id"])
    days_left = (deadline - today).days if deadline else None
    return {
        "id": o["id"],
        "title": o["title"],
        "organization": o["organization"],
        "type": o["type"],
        "deadline": o["deadline"],          # ISO string or None (rolling)
        "days_left": days_left,             # int or None
        "closing_soon": days_left is not None and 0 <= days_left <= CLOSING_SOON_DAYS,
        "is_past": days_left is not None and days_left < 0,
        "eligibility": o["eligibility"],
        "link": o["link"],
        "posted_days_ago": o.get("posted_days_ago"),
        "tags": o.get("tags") or [],
        "status": o.get("status"),
        "description": o.get("description"),
    }


def list_opportunities(
    opp_type: str | None = None,
    today: date | None = None,
    include_past: bool = False,
) -> list[dict[str, Any]]:
    """List opportunities, optionally filtered by type, sorted by deadline.

    Rolling-deadline (null) entries sort last. Unknown type -> [].
    """
    if opp_type is not None and opp_type not in TYPES:
        return []
    today = today or date.today()
    out = []
    for o in load_dataset()["opportunities"]:
        if opp_type is not None and o["type"] != opp_type:
            continue
        e = _enrich(o, today)
        if e["is_past"] and not include_past:
            continue
        out.append(e)
    out.sort(key=lambda e: (e["deadline"] is None, e["deadline"] or "9999"))
    return out


def get_opportunity(opp_id: str) -> dict[str, Any] | None:
    """Return the enriched opportunity dict, or None if unknown."""
    for o in load_dataset()["opportunities"]:
        if o["id"] == opp_id:
            return _enrich(o, date.today())
    return None


def opp_exists(opp_id: str) -> bool:
    return any(o["id"] == opp_id for o in load_dataset()["opportunities"])


# ================= Feature 25 Ultimate Enhancements =================
# 25.1 Deadline alerts | 25.2 Fit scoring | 25.3 One-click tracker add

import re as _re19


def _clean19(text: str | None) -> str:
    return _re19.sub(r"<[^>]*>", "", text or "")


def deadline_alerts(
    opportunities: list[dict[str, Any]],
    today: date | None = None,
) -> dict[str, Any]:
    """Bucket opportunities by deadline urgency: 1/3/7-day alerts.

    opportunities: list of enriched dicts (with days_left). Returns
    {critical (<=1d), soon (<=3d), upcoming (<=7d), expired}.
    """
    if not isinstance(opportunities, (list, tuple)):
        raise ValueError("opportunities must be a list.")
    buckets = {"critical": [], "soon": [], "upcoming": [], "expired": []}
    for o in opportunities:
        if not isinstance(o, dict):
            continue
        try:
            days = o.get("days_left")
            days = int(days) if days is not None else None
        except (TypeError, ValueError):
            continue
        if days is None:
            continue
        entry = {"id": o.get("id"), "title": _clean19(o.get("title") or "")[:80],
                 "organization": _clean19(o.get("organization") or "")[:60],
                 "days_left": days, "link": o.get("link")}
        if days < 0:
            buckets["expired"].append(entry)
        elif days <= 1:
            buckets["critical"].append(entry)
        elif days <= 3:
            buckets["soon"].append(entry)
        elif days <= 7:
            buckets["upcoming"].append(entry)
    for k in buckets:
        buckets[k].sort(key=lambda x: x["days_left"])
    n_alert = sum(len(buckets[k]) for k in ("critical", "soon", "upcoming"))
    return {**buckets,
            "alert_count": n_alert,
            "message": (f"{n_alert} deadline(s) within 7 days — act now."
                        if n_alert else "No urgent deadlines. Browse freely.")}


def fit_score(
    opportunity: dict[str, Any],
    skills: list[str],
) -> dict[str, Any]:
    """Score how well an opportunity matches the user's skills (0-100).

    Returns {score, matched, missing_hint, verdict}.
    """
    if not isinstance(opportunity, dict):
        raise ValueError("opportunity must be a dict.")
    skills = [_clean19(s).strip().lower() for s in (skills or [])
              if isinstance(s, str) and _clean19(s).strip()][:30]
    if not skills:
        raise ValueError("At least one skill is needed for fit scoring.")
    blob = (_clean19(opportunity.get("title") or "") + " " +
            _clean19(opportunity.get("eligibility") or "") + " " +
            _clean19(opportunity.get("type") or "")).lower()
    matched = [s for s in skills if s in blob]
    # Partial credit for related terms.
    score = round(len(matched) / len(skills) * 100, 1)
    if matched:
        score = min(100.0, score + 10)  # bonus for any match
    verdict = ("Strong match — apply first." if score >= 60 else
               "Decent match — worth a look." if score >= 30 else
               "Stretch — apply only if the role excites you.")
    return {"score": min(100.0, score), "matched": matched,
            "matched_count": len(matched), "total_skills": len(skills),
            "verdict": verdict}


def to_tracker_payload(opportunity: dict[str, Any]) -> dict[str, Any]:
    """Convert an opportunity to a job-tracker add payload (one-click apply tracking).

    Returns {company, role, jd_url, notes, stage}.
    """
    if not isinstance(opportunity, dict):
        raise ValueError("opportunity must be a dict.")
    org = _clean19(opportunity.get("organization") or "").strip()[:200]
    title = _clean19(opportunity.get("title") or "").strip()[:200]
    if not org or not title:
        raise ValueError("Opportunity needs an organization and title.")
    link = (opportunity.get("link") or "").strip()[:500]
    deadline = opportunity.get("deadline")
    notes = f"From Opportunity Radar ({opportunity.get('type', 'opportunity')})"
    if deadline:
        notes += f". Deadline: {deadline}"
    return {"company": org, "role": title,
            "jd_url": link if _re19.match(r"^https?://", link, _re19.I) else "",
            "notes": notes[:500], "stage": "wishlist",
            "opportunity_id": opportunity.get("id")}
