"""Source-grounded PDF Q&A (feature #21) — pure logic for the study library.

This module is intentionally framework-free (no Flask imports) so it can be
unit-tested in isolation and reused anywhere. Everything here is:

- **Deterministic** — same inputs always produce the same outputs. Retrieval
  breaks score ties by chunk index, never by set/dict iteration order.
- **Honest** — answers are extractive syntheses of retrieved passages only.
  Every factual claim carries a ``[doc, p.X]`` citation, and page numbers only
  ever come from the chunks that were actually retrieved. Citation excerpts
  are verbatim substrings of the real chunk text. No LLM, no invented facts,
  no invented page numbers, no hallucinated citations — when nothing relevant
  is found, the answer is exactly the abstention string below.
- **Defensive** — ``parse_pdf_pages`` never raises; scanned/image PDFs (no
  extractable text) yield empty page strings and flow into the honest
  abstention answer instead of crashing.

Retrieval is a hybrid of BM25-style keyword scoring and TF-IDF cosine
similarity (both implemented with the stdlib only — ``math``/``re``/
``collections``; no sklearn/numpy). Each retrieved chunk carries a ``debug``
dict explaining its score, so ranking decisions are auditable.

Public surface (exactly these five functions):

    validate_upload(file_storage) -> (ok, error)
    parse_pdf_pages(file_stream_or_path) -> list[str]
    chunk_pages(pages, chunk_chars=500, overlap=100) -> ChunkList[dict]
    retrieve(chunks, query, top_k=5) -> list[dict]
    answer_question(query, doc_title, chunks, top_k=5) -> dict
"""

from __future__ import annotations

import io
import math
import os
import re
import unicodedata
from collections import Counter
from typing import Any, BinaryIO

import pypdf

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Uploads larger than this are rejected by :func:`validate_upload`.
#: PERF-01 (audit Stage 4): like the parsing caps, this binds for
#: direct calls; web uploads hit Flask's 16 MB MAX_CONTENT_LENGTH
#: ceiling first, so the effective web limit for PDF QA is 16 MB.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB

#: Hard cap on chunks per document — protects memory on huge PDFs.
CHUNK_CAP = 2000

#: Minimum combined relevance score for a passage to count as "supporting".
RELEVANCE_THRESHOLD = 0.05

#: Maximum characters kept in a citation excerpt.
EXCERPT_MAX = 280

#: Honest abstention answer — returned verbatim when nothing relevant is found.
NO_RESULT_ANSWER = "I couldn't find this in your documents."

#: Blend weights for the hybrid score: 0.5 * BM25 + 0.5 * TF-IDF cosine.
HYBRID_BM25_WEIGHT = 0.5
HYBRID_TFIDF_WEIGHT = 0.5

#: ~40 common English stopwords stripped before scoring.
_STOPWORDS = frozenset(
    """
    a an the and or but if then else when at by for with about into through
    during before after above below to from up down in out on off over under
    again further once here there all any both each few more most other some
    such no nor not only own same so than too very can will just should now
    is are was were be been being have has had having do does did doing would
    could ought i me my we our you your he him his she her it its they them
    their this that these those am of as
    """.split()
)

#: Alphanumeric tokens on lowercased text.
_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Sentence splitter (keeps the trailing punctuation with the sentence).
_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]+|\S[^.!?]*$")

#: Sentence-boundary positions: punctuation followed by whitespace or end.
_SENT_END_RE = re.compile(r"[.!?](?=\s|$)")


# ---------------------------------------------------------------------------
# 1. Upload validation
# ---------------------------------------------------------------------------

def _upload_name_error(file_storage: Any) -> str | None:
    """Error string when the filename is missing/not a PDF, else None."""
    filename = getattr(file_storage, "filename", None) or ""
    filename = os.path.basename(filename).strip()
    if not filename:
        return "No file was uploaded."
    if not filename.lower().endswith(".pdf"):
        return "Only PDF files are accepted."
    return None


def _upload_mime_error(file_storage: Any) -> str | None:
    """Error string when a declared MIME type is not PDF-ish, else None."""
    mimetype = (
        getattr(file_storage, "mimetype", None)
        or getattr(file_storage, "content_type", "")
        or ""
    )
    mimetype = str(mimetype).split(";")[0].strip().lower()
    if mimetype and "pdf" not in mimetype and mimetype != "application/octet-stream":
        return "Expected a PDF file (declared content type: '%s')." % mimetype
    return None


def _upload_stream(file_storage: Any) -> BinaryIO | None:
    """Resolve the readable stream, or None when unreadable."""
    stream = getattr(file_storage, "stream", None)
    if stream is None and hasattr(file_storage, "read"):
        stream = file_storage
    return stream


def _upload_size_error(stream: BinaryIO) -> str | None:
    """Error string when the stream is empty/oversized/unreadable, else None."""
    try:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
    except (OSError, AttributeError, ValueError):
        return "Could not read the uploaded file."
    if size is None or size <= 0:
        return "The file is empty."
    if size > MAX_UPLOAD_BYTES:
        return "File exceeds the 25 MB limit (%s MB)." % round(
            size / (1024 * 1024), 1)
    return None


def _upload_magic_error(stream: BinaryIO, pos: int | None) -> str | None:
    """Error string when %PDF is missing from the first 1024 bytes.

    Restores the stream to ``pos`` (captured before the size check) on every
    path, exactly as the original monolithic version did.
    """
    try:
        try:
            stream.seek(0)
            header = stream.read(1024)
        except (OSError, AttributeError, ValueError):
            return "Could not read the uploaded file."
    finally:
        if pos is not None:
            try:
                stream.seek(pos)
            except (OSError, ValueError):
                pass
    if isinstance(header, str):  # pragma: no cover — uploads are binary
        header = header.encode("latin-1", "ignore")
    if not isinstance(header, (bytes, bytearray)) or b"%PDF" not in bytes(header):
        return "The file is not a valid PDF (missing %PDF header)."
    return None


def validate_upload(file_storage: Any) -> tuple[bool, str]:
    """Validate an uploaded file before any PDF parsing is attempted.

    Accepts any object with a ``filename`` attribute and either a ``stream``
    attribute or a file-like interface (the shape Flask/Werkzeug
    ``FileStorage`` provides, but duck-typed so tests can use fakes).

    Rules: must exist, must be named ``*.pdf`` (case-insensitive), must
    declare a PDF-ish MIME type when one is declared, must be non-empty, must
    not exceed :data:`MAX_UPLOAD_BYTES` (25 MB), and must start with the PDF
    magic bytes (``%PDF`` within the first 1024 bytes — extension alone is
    not trusted).

    The stream position is restored after the checks so downstream code can
    read the upload from the start. Never raises: every failure mode returns
    ``(False, <helpful message>)``.
    """
    if file_storage is None:
        return False, "No file was uploaded."

    for check in (_upload_name_error, _upload_mime_error):
        err = check(file_storage)
        if err:
            return False, err

    stream = _upload_stream(file_storage)
    if stream is None:
        return False, "Could not read the uploaded file."

    try:
        pos = stream.tell()
    except (OSError, AttributeError, ValueError):
        pos = None

    err = _upload_size_error(stream)
    if err:
        return False, err
    err = _upload_magic_error(stream, pos)
    if err:
        return False, err
    return True, ""


# ---------------------------------------------------------------------------
# 2. PDF parsing
# ---------------------------------------------------------------------------

def parse_pdf_pages(file_stream_or_path: str | os.PathLike | BinaryIO | bytes) -> list[str]:
    """Extract text per PDF page.

    Accepts a filesystem path, raw ``bytes``, or a binary file-like object
    (including Flask ``FileStorage``). Returns one string per page, so
    ``pages[i]`` is always the text of page ``i`` (0-based). Pages with no
    extractable text (scanned/image PDFs, blank pages) come back as ``""`` —
    they are skipped later by :func:`chunk_pages`, and a document with *no*
    text at all flows into the honest abstention answer instead of an error.

    Never raises: any failure (corrupt PDF, unreadable stream, encrypted
    file) yields ``[]``.
    """
    try:
        if isinstance(file_stream_or_path, (bytes, bytearray)):
            reader = pypdf.PdfReader(io.BytesIO(bytes(file_stream_or_path)))
        elif isinstance(file_stream_or_path, (str, os.PathLike)):
            reader = pypdf.PdfReader(str(file_stream_or_path))
        else:
            stream = file_stream_or_path
            try:
                stream.seek(0)
            except (OSError, AttributeError, ValueError):
                pass
            reader = pypdf.PdfReader(stream)

        pages: list[str] = []
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception:
                # One bad page must not kill the whole document.
                text = ""
            # Unicode-normalize so combining characters can't split tokens.
            pages.append(unicodedata.normalize("NFC", text).strip())
        return pages
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 3. Chunking (500-char overlapping windows, sentence-boundary cuts, headings)
# ---------------------------------------------------------------------------

class ChunkList(list):
    """A list of chunk dicts with a ``truncated`` flag.

    ``chunk_pages`` caps output at :data:`CHUNK_CAP` (2000) chunks for huge
    documents; when the cap bites, ``truncated`` is ``True`` so the UI can
    tell the user only the first 2000 chunks were indexed.
    """

    def __init__(self, *args: Any, truncated: bool = False) -> None:
        super().__init__(*args)
        self.truncated = truncated


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _is_heading(line: str) -> str | None:
    """Best-effort section-heading detection. Returns the heading text or None.

    Recognizes, in order:
    - Markdown headings (``# Title``)
    - Numbered headings (``1. Introduction``, ``2.3 Methods``)
    - ALL-CAPS short lines (``RESULTS``, ``LITERATURE REVIEW``)
    - Short title-case lines ending in a colon (``Conclusion:``)
    """
    s = line.strip()
    if not s or len(s) > 80:
        return None

    m = re.match(r"^#{1,6}\s+(.+?)\s*$", s)
    if m and len(m.group(1)) >= 2:
        return m.group(1)

    m = re.match(r"^(\d+(?:\.\d+)*\.?)\s+([A-Z][^.!?]{1,70})$", s)
    if m:
        return s

    if s.endswith(":") and len(s) <= 60 and re.match(r"^[A-Z][^.!?]*:$", s):
        return s[:-1].strip() or None

    # ALL CAPS: at least 2 chars, mostly uppercase letters, no sentence end.
    if (
        2 <= len(s) <= 60
        and not s.endswith((".", "!", "?", ":", ";"))
        and re.search(r"[A-Z]", s)
        and not re.search(r"[a-z]", s)
    ):
        return s.title() if len(s) <= 40 else s

    return None


def _split_by_headings(raw_text: str) -> list[tuple[str, str]]:
    """Split page text into (heading, text) segments at heading boundaries.

    The heading line itself stays at the start of its segment's text so its
    words remain searchable. Text before the first detected heading gets
    ``heading == ""``.
    """
    segments: list[tuple[str, list[str]]] = []
    current_heading = ""
    current_lines: list[str] = []
    for line in raw_text.splitlines():
        heading = _is_heading(line)
        if heading is not None:
            if current_lines or current_heading:
                segments.append((current_heading, current_lines))
            current_heading = heading
            current_lines = [line.strip()]
        else:
            current_lines.append(line)
    if current_lines or current_heading:
        segments.append((current_heading, current_lines))
    return [(h, "\n".join(lines)) for h, lines in segments]


def _split_text(text: str, chunk_chars: int, overlap: int) -> list[str]:
    """Sentence-aware sliding-window split. Never returns empty strings.

    The window edge prefers a sentence boundary (``.``/``!``/``?``) so chunks
    rarely cut mid-sentence; it falls back to a word boundary when no
    sentence end fits inside the window.
    """
    text = _normalize_whitespace(text)
    n = len(text)
    out: list[str] = []
    start = 0
    while start < n:
        end = min(start + chunk_chars, n)
        if end < n:
            # Prefer cutting at a sentence boundary ...
            sent_cut = None
            for m in _SENT_END_RE.finditer(text, start, end):
                sent_cut = m.end()
            if sent_cut is not None and sent_cut > start + 20:
                end = sent_cut
            else:
                # ... otherwise fall back to a word boundary.
                cut = text.rfind(" ", start, end)
                if cut > start:
                    end = cut
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        nxt = end - overlap
        if nxt <= start:
            nxt = end  # overlap too large to advance — step to end instead
        if nxt <= start:
            break  # cannot advance; avoids an infinite loop
        start = nxt
    return out


def chunk_pages(
    pages: list[str],
    chunk_chars: int = 500,
    overlap: int = 100,
) -> ChunkList:
    """Split per-page text into overlapping, page-attributed chunks.

    Each chunk is ``{"page": 1-based, "index": global chunk idx,
    "heading": best-effort section heading ("" if none detected),
    "text": str}``. Empty/whitespace-only pages are skipped, and no chunk is
    ever an empty string. Chunks from page *p* always carry ``"page": p`` so
    citations can never point at a wrong page.

    Output is capped at :data:`CHUNK_CAP` (2000) chunks; the returned
    :class:`ChunkList` sets ``truncated=True`` when the cap was hit.

    Never raises: ``None`` or malformed input yields an empty
    :class:`ChunkList`; invalid ``chunk_chars``/``overlap`` values are
    coerced to safe minimums.
    """
    try:
        chunk_chars = int(chunk_chars)
    except (TypeError, ValueError):
        chunk_chars = 500
    try:
        overlap = int(overlap)
    except (TypeError, ValueError):
        overlap = 100
    chunk_chars = max(1, chunk_chars)
    overlap = max(0, min(overlap, chunk_chars - 1)) if chunk_chars > 1 else 0

    chunks: list[dict] = []
    truncated = False
    index = 0
    for page_no, page_text in enumerate(pages or [], start=1):
        if not isinstance(page_text, str) or not page_text.strip():
            continue
        # RAG-04: sanitize prompt-injection attempts before chunking.
        # Documents are data, never instructions.
        page_text, _inj_report = sanitize_document_text(page_text)
        if not page_text.strip():
            continue
        for heading, segment in _split_by_headings(page_text.strip()):
            for piece in _split_text(segment, chunk_chars, overlap):
                if len(chunks) >= CHUNK_CAP:
                    truncated = True
                    break
                chunks.append(
                    {"page": page_no, "index": index, "heading": heading, "text": piece}
                )
                index += 1
            if truncated:
                break
        if truncated:
            break
    return ChunkList(chunks, truncated=truncated)


# ---------------------------------------------------------------------------
# 4. Hybrid retrieval: BM25-ish keyword scoring + TF-IDF cosine (stdlib only)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric unigrams with stopwords removed."""
    if not isinstance(text, str):
        return []
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def _smooth_idf(tokenized_docs: list[list[str]]) -> dict[str, float]:
    """Smoothed IDF for TF-IDF: log((N+1)/(df+1)) + 1, always positive."""
    n = len(tokenized_docs)
    df = Counter()
    for tokens in tokenized_docs:
        df.update(set(tokens))
    return {tok: math.log((n + 1) / (freq + 1)) + 1.0 for tok, freq in df.items()}


def _bm25_idf(tokenized_docs: list[list[str]]) -> dict[str, float]:
    """BM25 IDF: log((N - df + 0.5) / (df + 0.5) + 1), always non-negative."""
    n = len(tokenized_docs)
    df = Counter()
    for tokens in tokenized_docs:
        df.update(set(tokens))
    return {
        tok: math.log((n - freq + 0.5) / (freq + 0.5) + 1.0)
        for tok, freq in df.items()
    }


def _tfidf_vector(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    """L2-normalized sparse TF-IDF vector as a token->weight dict."""
    if not tokens:
        return {}
    counts = Counter(tokens)
    length = len(tokens)
    vec = {tok: (cnt / length) * idf.get(tok, 0.0) for tok, cnt in counts.items()}
    norm = math.sqrt(sum(w * w for w in vec.values()))
    if norm == 0.0:
        return {}
    return {tok: w / norm for tok, w in vec.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return sum(w * b.get(tok, 0.0) for tok, w in a.items())


def _bm25_score(
    query_tokens: list[str],
    doc_tokens: list[str],
    avgdl: float,
    idf: dict[str, float],
    k1: float = 1.2,
    b: float = 0.75,
) -> float:
    """BM25-ish keyword score (no external deps)."""
    if not doc_tokens or not query_tokens:
        return 0.0
    tf = Counter(doc_tokens)
    dl = len(doc_tokens)
    score = 0.0
    for tok in set(query_tokens):
        freq = tf.get(tok, 0)
        if freq == 0:
            continue
        idf_t = idf.get(tok, 0.0)
        denom = freq + k1 * (1 - b + b * dl / avgdl)
        score += idf_t * (freq * (k1 + 1)) / denom
    return score


def _minmax_normalize(values: list[float]) -> list[float]:
    """Scale to [0, 1]; all-equal input maps to all zeros (no signal)."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi <= lo:
        return [0.0] * len(values)
    span = hi - lo
    return [(v - lo) / span for v in values]


def retrieve(chunks: list[dict], query: str, top_k: int = 5) -> list[dict]:
    """Rank chunks with a hybrid BM25 + TF-IDF-cosine score.

    ``score`` is ``0.5 * norm(BM25) + 0.5 * norm(TF-IDF cosine)`` where each
    component is min-max normalized over the corpus, so the final score is in
    ``[0, 1]``. Deterministic: ties break on chunk ``index`` ascending.

    Empty/blank queries, queries with no indexable tokens, and empty corpora
    all return ``[]``. Only chunks with a positive score are returned (up to
    ``top_k``). When min-max normalization erases all signal (uniform corpus,
    e.g. a single chunk), ranking falls back to raw hybrid scores.

    Each result is a copy of the chunk dict plus:

    - ``"score"`` — the combined hybrid score (float)
    - ``"debug"`` — ``{"bm25": raw BM25, "tfidf_cosine": raw cosine,
      "bm25_norm": ..., "tfidf_norm": ..., "combined": ...,
      "matched_terms": [query terms found in the chunk]}`` explaining the
      ranking decision.

    Never raises: bad input (``None`` chunks, non-string query, ``None``
    entries) yields ``[]``.
    """
    if (
        not chunks
        or not isinstance(query, str)
        or not query.strip()
        or not isinstance(top_k, int)
        or top_k <= 0
    ):
        return []

    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    corpus_tokens = [
        _tokenize(c.get("text", "") if isinstance(c, dict) else "") for c in chunks
    ]
    smooth_idf = _smooth_idf(corpus_tokens)
    bm25_idf = _bm25_idf(corpus_tokens)
    avgdl = sum(len(t) for t in corpus_tokens) / max(len(corpus_tokens), 1)

    query_vec = _tfidf_vector(query_tokens, smooth_idf)

    raw_bm25: list[float] = []
    raw_cosine: list[float] = []
    doc_vecs: list[dict[str, float]] = []
    for tokens in corpus_tokens:
        raw_bm25.append(_bm25_score(query_tokens, tokens, avgdl, bm25_idf))
        vec = _tfidf_vector(tokens, smooth_idf)
        doc_vecs.append(vec)
        raw_cosine.append(_cosine(query_vec, vec) if query_vec else 0.0)

    norm_bm25 = _minmax_normalize(raw_bm25)
    norm_cosine = _minmax_normalize(raw_cosine)

    query_set = set(query_tokens)
    norm_signal = any(v > 0.0 for v in norm_bm25) or any(v > 0.0 for v in norm_cosine)
    scored = []
    for i, chunk in enumerate(chunks):
        if norm_signal:
            combined = (
                HYBRID_BM25_WEIGHT * norm_bm25[i]
                + HYBRID_TFIDF_WEIGHT * norm_cosine[i]
            )
        else:
            # Uniform corpus (e.g. a single chunk): min-max normalization maps
            # every score to 0.0, erasing the signal. Fall back to raw scores
            # so a lone matching chunk is still retrievable. Chunks with no
            # term overlap still score exactly 0.0 and stay excluded.
            combined = (
                HYBRID_BM25_WEIGHT * raw_bm25[i]
                + HYBRID_TFIDF_WEIGHT * raw_cosine[i]
            )
        if combined > 0.0:
            matched = sorted(query_set.intersection(corpus_tokens[i]))
            scored.append(
                (
                    combined,
                    {
                        "bm25": raw_bm25[i],
                        "tfidf_cosine": raw_cosine[i],
                        "bm25_norm": norm_bm25[i],
                        "tfidf_norm": norm_cosine[i],
                        "combined": combined,
                        "matched_terms": matched,
                    },
                    chunk,
                )
            )

    # Sort by combined score descending, then chunk index ascending — fully
    # deterministic.
    def _sort_key(
        item: tuple[float, dict[str, Any], dict[str, Any]],
    ) -> tuple[float, int]:
        chunk = item[2]
        idx = chunk.get("index", 0) if isinstance(chunk, dict) else 0
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            idx = 0
        return (-item[0], idx)

    scored.sort(key=_sort_key)

    results = []
    for combined, debug, chunk in scored[:top_k]:
        result = dict(chunk)
        result["score"] = float(combined)
        result["debug"] = {k: (float(v) if isinstance(v, float) else v)
                           for k, v in debug.items()}
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# 5. Extractive, citation-honest answering
# ---------------------------------------------------------------------------

def _best_sentence(text: str, query_tokens: list[str]) -> str:
    """Pick the sentence in ``text`` with the most query-token overlap."""
    sentences = [s.strip() for s in _SENTENCE_RE.findall(text) if s.strip()]
    if not sentences:
        return _normalize_whitespace(text)[:200]
    query_set = set(query_tokens)
    best, best_overlap = sentences[0], -1
    for sent in sentences:
        overlap = len(query_set.intersection(_tokenize(sent)))
        if overlap > best_overlap:
            best, best_overlap = sent, overlap
    return best


def _excerpt(text: str, max_chars: int = EXCERPT_MAX) -> str:
    """Verbatim excerpt: a word-boundary prefix of the chunk text.

    Because the excerpt is a strict prefix (cut at a word boundary, no
    ellipsis appended), ``excerpt in chunk_text`` always holds — citation
    accuracy is verifiable by substring check.
    """
    text = _normalize_whitespace(text)
    if len(text) <= max_chars:
        return text
    cut = text.rfind(" ", 0, max_chars)
    if cut <= 0:
        cut = max_chars
    return text[:cut].rstrip()


def _citation(chunk: dict, doc_title: str) -> dict:
    return {
        "doc": doc_title,
        "page": int(chunk.get("page", 0)),
        "chunk": int(chunk.get("index", 0)),
        "excerpt": _excerpt(chunk.get("text", "")),
    }


def answer_question(
    query: str,
    doc_title: str,
    chunks: list[dict],
    top_k: int = 5,
) -> dict:
    """Answer ``query`` extractively from ``chunks``, with honest citations.

    The answer is a 2–4 sentence synthesis composed ONLY of key phrases quoted
    from the retrieved passages — every claim is immediately followed by a
    ``[doc, p.X]`` marker whose page number comes straight from the retrieved
    chunk. Nothing is invented: if no passage clears
    :data:`RELEVANCE_THRESHOLD`, or there are no chunks at all (e.g. a
    scanned PDF with no extractable text), the answer is exactly
    ``"I couldn't find this in your documents."`` with empty citations.

    Returns ``{"answer": str, "citations": [...], "passages": [...]}`` where
    ``passages`` mirrors ``citations`` (kept as a separate key for UI
    convenience — the expandable "supporting passages" panel).
    """
    retrieved = retrieve(chunks, query, top_k=top_k)
    if not retrieved or retrieved[0]["score"] < RELEVANCE_THRESHOLD:
        return {"answer": NO_RESULT_ANSWER, "citations": [], "passages": []}

    query_tokens = _tokenize(query)
    # Use up to 3 top passages so the synthesis stays focused and citable.
    selected = retrieved[:3]

    doc_label = doc_title if isinstance(doc_title, str) else ""

    claims: list[str] = []
    citations: list[dict] = []
    for chunk in selected:
        sentence = _normalize_whitespace(_best_sentence(chunk.get("text", ""), query_tokens))
        if len(sentence) > 400:
            sentence = sentence[:397].rstrip() + "…"
        claims.append('"%s" [doc, p.%d]' % (sentence, int(chunk.get("page", 0))))
        citations.append(_citation(chunk, doc_label))

    intro = "Here is what the document says:"
    answer = intro + " " + " ".join(claims)
    return {"answer": answer, "citations": citations, "passages": list(citations)}


# ================= Feature 18 Ultimate Enhancements =================
# 18.1 Multi-doc synthesis | 18.2 Citation verifier | 18.3 Export to notes

import re as _re12


def _clean12(text: str | None) -> str:
    return _re12.sub(r"<[^>]*>", "", text or "")


def answer_across_docs(
    query: str | None,
    docs: list[dict[str, Any]],
    top_k: int = 3,
) -> dict[str, Any]:
    """Ask one question across multiple documents.

    docs: list of {title, chunks}. Retrieves from the merged pool, then
    groups evidence per document. Returns {answer, per_doc: [...], citations}.
    """
    query = _clean12(query or "").strip()
    if not query:
        raise ValueError("Query is required.")
    if not isinstance(docs, (list, tuple)) or not docs:
        raise ValueError("At least one document is needed.")
    merged = []
    titles = {}
    for di, d in enumerate(docs):
        if not isinstance(d, dict):
            continue
        title = _clean12(d.get("title") or f"Doc {di+1}")[:80]
        titles[di] = title
        for c in (d.get("chunks") or []):
            if isinstance(c, dict):
                c2 = dict(c)
                c2["_doc_idx"] = di
                merged.append(c2)
    if not merged:
        return {"answer": NO_RESULT_ANSWER, "per_doc": [], "citations": []}

    retrieved = retrieve(merged, query, top_k=top_k * 2)
    if not retrieved or retrieved[0]["score"] < RELEVANCE_THRESHOLD:
        return {"answer": NO_RESULT_ANSWER, "per_doc": [], "citations": []}

    query_tokens = _tokenize(query)
    per_doc = {}
    citations = []
    for chunk in retrieved[:top_k]:
        di = chunk.get("_doc_idx", 0)
        title = titles.get(di, "Doc")
        sentence = _normalize_whitespace(_best_sentence(chunk.get("text", ""), query_tokens))
        if len(sentence) > 300:
            sentence = sentence[:297].rstrip() + "…"
        entry = per_doc.setdefault(title, {"doc": title, "claims": []})
        entry["claims"].append({"text": sentence, "page": int(chunk.get("page", 0))})
        citations.append({"doc": title, "page": int(chunk.get("page", 0)),
                          "excerpt": _excerpt(chunk.get("text", ""))})
    doc_list = list(per_doc.values())
    n_docs = len(doc_list)
    if n_docs > 1:
        answer = (f"Across {n_docs} documents: " +
                  " ".join(f"[{e['doc']}] " + " ".join(
                      f'"{c["text"]}" [p.{c["page"]}]' for c in e["claims"][:2])
                      for e in doc_list))
    else:
        e = doc_list[0]
        answer = (f"[{e['doc']}] " + " ".join(
            f'"{c["text"]}" [p.{c["page"]}]' for c in e["claims"][:3]))
    return {"answer": answer, "per_doc": doc_list, "citations": citations,
            "docs_consulted": n_docs}


def verify_citations(answer_dict: dict[str, Any]) -> dict[str, Any]:
    """Check that every claim in an answer carries a citation marker.

    Returns {claims, cited, uncited, ok}. A claim = one quoted segment.
    """
    if not isinstance(answer_dict, dict):
        raise ValueError("answer_dict must be a dict.")
    answer = _clean12(answer_dict.get("answer") or "")
    quotes = _re12.findall(r'"([^"]{10,})"', answer)
    cited = sum(1 for _ in _re12.finditer(r'"[^"]{10,}"\s*\[', answer))
    uncited = len(quotes) - cited
    return {"claims": len(quotes), "cited": cited, "uncited": max(0, uncited),
            "ok": bool(quotes) and uncited <= 0,
            "verdict": ("All claims cited." if quotes and uncited <= 0 else
                        "No verifiable claims found." if not quotes else
                        f"{uncited} claim(s) missing citations.")}


def grounding_report(answer_dict: dict[str, Any]) -> dict[str, Any]:
    """Faithfulness badge for an answer.

    Returns {claims, cited, uncited, score, verdict, abstained}.
    ``score`` is the share of quoted claims carrying a citation marker.
    An abstention (zero claims) scores 1.0: claiming nothing is
    perfectly grounded, and must never look like a failure.
    """
    v = verify_citations(answer_dict)
    claims, cited = v["claims"], v["cited"]
    if claims == 0:
        return {"claims": 0, "cited": 0, "uncited": 0, "score": 1.0,
                "verdict": "Abstained — nothing claimed.", "abstained": True}
    return {"claims": claims, "cited": cited, "uncited": v["uncited"],
            "score": round(cited / claims, 2), "verdict": v["verdict"],
            "abstained": False}


#: Direction words for the contrast heuristic below. A "contrast" here is
#: a keyword-level divergence flag, NOT a semantic verdict: two claims
#: that share real vocabulary but point in opposite directions (or one
#: negated, one not) are worth reading side by side.
_UP_MARKERS = frozenset(
    "increase increases increased increasing rise rises rising rose grow grows "
    "growing grew improve improves improved improving improvement higher better "
    "effective beneficial significant positive boost boosts enhance enhances gain "
    "gains".split())
_DOWN_MARKERS = frozenset(
    "decrease decreases decreased decreasing fall falls falling fell drop drops "
    "dropping decline declines declining worsen worsens worsening lower worse "
    "ineffective harmful negative reduce reduces reduced reducing loss fail "
    "fails failed failing".split())
_NEGATIONS = frozenset(
    "not no never none without fails failed lack lacks".split())


def _claim_content_tokens(text: str) -> set[str]:
    return {t for t in _tokenize(text or "") if t not in _STOPWORDS and len(t) >= 3}


def detect_contrasts(per_doc: list[dict[str, Any]], max_flags: int = 3) -> list[dict[str, Any]]:
    """Flag possible disagreements between documents' claims.

    ``per_doc`` is the answer_across_docs shape:
    [{doc, claims: [{text, page}]}]. A flag fires when two claims from
    DIFFERENT docs share real vocabulary (Jaccard >= 0.25 on content
    tokens) AND point in opposite directions (up-markers vs
    down-markers) or differ on negation. Every flag is labeled as a
    heuristic and points back to both passages — the reader verifies,
    the tool never declares a winner.
    """
    if not isinstance(per_doc, list) or len(per_doc) < 2:
        return []
    flat = []
    for entry in per_doc:
        doc = entry.get("doc") if isinstance(entry, dict) else None
        for claim in (entry.get("claims") or []):
            if isinstance(claim, dict) and claim.get("text"):
                flat.append((doc, claim))
    flags: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for i in range(len(flat)):
        if len(flags) >= max_flags:
            break
        doc_a, claim_a = flat[i]
        toks_a = _claim_content_tokens(claim_a["text"])
        if not toks_a:
            continue
        dir_a = ("up" if toks_a & _UP_MARKERS and not (toks_a & _DOWN_MARKERS)
                 else "down" if toks_a & _DOWN_MARKERS and not (toks_a & _UP_MARKERS)
                 else None)
        neg_a = bool(toks_a & _NEGATIONS)
        for j in range(i + 1, len(flat)):
            doc_b, claim_b = flat[j]
            if doc_b == doc_a:
                continue
            pair = tuple(sorted([str(doc_a), str(doc_b)]))
            if pair in seen_pairs:
                continue
            toks_b = _claim_content_tokens(claim_b["text"])
            if not toks_b:
                continue
            union = toks_a | toks_b
            jaccard = len(toks_a & toks_b) / len(union) if union else 0.0
            if jaccard < 0.25:
                continue
            dir_b = ("up" if toks_b & _UP_MARKERS and not (toks_b & _DOWN_MARKERS)
                     else "down" if toks_b & _DOWN_MARKERS and not (toks_b & _UP_MARKERS)
                     else None)
            neg_b = bool(toks_b & _NEGATIONS)
            opposed = (dir_a and dir_b and dir_a != dir_b) or (neg_a != neg_b and (dir_a or dir_b))
            if not opposed:
                continue
            signal = f"{dir_a or 'stated'} vs {dir_b or 'stated'}"
            if neg_a != neg_b:
                signal += " (negation differs)"
            flags.append({
                "docs": [doc_a, doc_b],
                "signal": signal,
                "shared_terms": sorted(toks_a & toks_b)[:5],
                "claims": [
                    {"doc": doc_a, "text": claim_a["text"], "page": claim_a.get("page")},
                    {"doc": doc_b, "text": claim_b["text"], "page": claim_b.get("page")},
                ],
                "note": ("Possible contrast (keyword heuristic) — read both "
                         "passages before concluding they disagree."),
            })
            seen_pairs.add(pair)
    return flags


def _citation_markers(text: str) -> list[str]:
    return _re12.findall(r"\[doc, p\.\d+\]|\[p\.\d+\]", text or "")


def fluent_answer(extractive_answer: str, generate_fn,
                  required_labels: list[str] | None = None) -> dict[str, Any]:
    """Rewrite stitched quotes into fluent prose WITHOUT losing proof.

    ``generate_fn`` is an injected callable(prompt) -> str (production
    passes the LLM gateway; tests pass a stub, so no network is needed).

    The rewrite must keep every claim in quotation marks with its exact
    citation marker directly after it, add no new marker, and keep every
    required doc label. Anything less — dropped quote, dropped or added
    marker, uncited claim, provider error, empty output — falls back to
    the extractive answer. Abstentions are never rewritten.

    Returns {answer, fluent, reason}.
    """
    original = extractive_answer or ""
    if original.strip() == NO_RESULT_ANSWER:
        return {"answer": original, "fluent": False, "reason": "abstained"}
    orig_verification = verify_citations({"answer": original})
    orig_markers = sorted(_citation_markers(original))
    if orig_verification["claims"] == 0 or not orig_markers:
        return {"answer": original, "fluent": False, "reason": "nothing citable"}
    claims = _re12.findall(r'"([^"]{10,})"', original)
    claims_block = "\n".join(
        f'"{c}" {m}' for c, m in zip(claims, orig_markers, strict=False))
    prompt = (
        "Rewrite the following quoted claims into fluent, readable prose "
        "(2-4 sentences). Connect the claims with your own transition words, "
        "but obey these rules exactly:\n"
        "- Keep EVERY claim in quotation marks with its exact citation marker "
        "directly after it.\n"
        "- Do not drop any claim, do not add any new claim or fact, and do "
        "not add, remove, or change any citation marker or page number.\n"
        "- Do not add any introductory sentence.\n\n"
        "Claims:\n" + claims_block)
    try:
        rewritten = (generate_fn(prompt) or "").strip()
    except Exception:
        return {"answer": original, "fluent": False,
                "reason": "provider unavailable"}
    if not rewritten:
        return {"answer": original, "fluent": False, "reason": "empty rewrite"}
    check = verify_citations({"answer": rewritten})
    if (check["claims"] != orig_verification["claims"]
            or check["uncited"] != 0
            or sorted(_citation_markers(rewritten)) != orig_markers):
        return {"answer": original, "fluent": False,
                "reason": "rewrite lost or added citations"}
    for label in (required_labels or []):
        if label and label not in rewritten:
            return {"answer": original, "fluent": False,
                    "reason": "rewrite dropped a document label"}
    return {"answer": rewritten, "fluent": True, "reason": None}


# --- Summary mode ------------------------------------------------------------
# A "summarize this document" question has almost no keyword overlap with
# any passage, so plain retrieval scores below threshold and the engine
# abstains — even though the document is fully indexed. Summary intent is
# therefore detected explicitly and answered with an extractive overview:
# the doc's highest TF-IDF sentences, quoted with page citations. Nothing
# is generated; every sentence comes from the document itself.
_SUMMARY_PATTERNS = [
    _re12.compile(r"\bsummar(y|ise|ize|ising|izing)\b", _re12.IGNORECASE),
    _re12.compile(r"\boverview\b", _re12.IGNORECASE),
    _re12.compile(r"\b(key|main)\s+points\b", _re12.IGNORECASE),
    _re12.compile(r"\bwhat is this (document|paper|about)\b", _re12.IGNORECASE),
    _re12.compile(r"\btell me about this\b", _re12.IGNORECASE),
    _re12.compile(r"\bwhat(?:'s| is) (this|it) about\b", _re12.IGNORECASE),
]


def is_summary_question(question: str | None) -> bool:
    """True when the question asks for a summary/overview, not a fact."""
    if not question:
        return False
    return any(p.search(question) for p in _SUMMARY_PATTERNS)


def _summary_sentences(chunks: list[dict], top_k: int) -> list[dict]:
    """Top TF-IDF sentences of a chunk list, at most 2 per page.

    Returns [{text, page, chunk}] in reading (page) order. Sentences
    shorter than 40 chars (headings, fragments) or longer than 400 chars
    are skipped; scoring is sum-of-IDF over content tokens, normalized
    by sqrt length so long sentences don't win by bulk alone.
    """
    import math as _math

    usable = [c for c in (chunks or []) if isinstance(c, dict)
              and (c.get("text") or "").strip()]
    if not usable:
        return []
    corpus = [[t for t in _tokenize(c.get("text", "")) if t not in _STOPWORDS]
              for c in usable]
    idf = _smooth_idf(corpus)
    scored = []
    for chunk in usable:
        for sent in _SENTENCE_RE.findall(chunk.get("text", "")):
            sent = _normalize_whitespace(sent.strip())
            if not (40 <= len(sent) <= 400):
                continue
            toks = [t for t in _tokenize(sent) if t not in _STOPWORDS]
            if len(toks) < 4:
                continue
            score = sum(idf.get(t, 0.0) for t in toks) / _math.sqrt(len(toks))
            scored.append({"text": sent, "page": int(chunk.get("page", 0) or 0),
                           "chunk": chunk, "score": score})
    scored.sort(key=lambda s: -s["score"])
    picked: list[dict] = []
    per_page: dict[int, int] = {}
    for sent in scored:
        if len(picked) >= top_k:
            break
        if per_page.get(sent["page"], 0) >= 2:
            continue
        per_page[sent["page"]] = per_page.get(sent["page"], 0) + 1
        picked.append(sent)
    picked.sort(key=lambda s: (s["page"],))
    return picked


def summarize_doc(doc_title: str, chunks: list[dict], top_k: int = 5) -> dict:
    """Extractive summary of one document, fully cited.

    Returns the answer_question shape {answer, citations, passages}.
    No chunks (e.g. scanned PDF) -> the honest abstention, unchanged.
    """
    picked = _summary_sentences(chunks, top_k)
    if not picked:
        return {"answer": NO_RESULT_ANSWER, "citations": [], "passages": []}
    doc_label = doc_title if isinstance(doc_title, str) else ""
    claims = ['"%s" [doc, p.%d]' % (s["text"], s["page"]) for s in picked]
    citations = [_citation(s["chunk"], doc_label) for s in picked]
    answer = "Summary of the document: " + " ".join(claims)
    return {"answer": answer, "citations": citations, "passages": list(citations)}


def summarize_across_docs(docs: list[dict[str, Any]], per_doc_k: int = 2,
                          max_docs: int = 4) -> dict[str, Any]:
    """Extractive summary across several documents, grouped per doc.

    Returns the answer_across_docs shape {answer, per_doc, citations,
    docs_consulted}. Each contributing doc gets its top sentences; docs
    with no usable sentences are skipped, not invented.
    """
    per_doc = []
    citations = []
    for d in (docs or [])[:max_docs]:
        if not isinstance(d, dict):
            continue
        title = d.get("title") or "Doc"
        picked = _summary_sentences(d.get("chunks") or [], per_doc_k)
        if not picked:
            continue
        entry = {"doc": title, "claims": [
            {"text": s["text"], "page": s["page"]} for s in picked]}
        per_doc.append(entry)
        citations.extend(_citation(s["chunk"], title) for s in picked)
    if not per_doc:
        return {"answer": NO_RESULT_ANSWER, "per_doc": [], "citations": [],
                "docs_consulted": 0}
    parts = []
    for e in per_doc:
        claims = " ".join('"%s" [p.%d]' % (c["text"], c["page"])
                          for c in e["claims"])
        parts.append("[%s] %s" % (e["doc"], claims))
    answer = "Summary across %d documents: " % len(per_doc) + " ".join(parts)
    return {"answer": answer, "per_doc": per_doc, "citations": citations,
            "docs_consulted": len(per_doc)}


def qa_to_notes(
    qa_pairs: list[dict[str, Any]],
    title: str = "Study Notes",
) -> dict[str, Any]:
    """Export Q&A pairs to structured markdown study notes.

    qa_pairs: list of {question, answer, citations?}. Returns {markdown, sections}.
    """
    if not isinstance(qa_pairs, (list, tuple)) or not qa_pairs:
        raise ValueError("At least one Q&A pair is needed.")
    title = _clean12(title or "Study Notes").strip()[:120] or "Study Notes"
    lines = [f"# {title}", ""]
    sections = 0
    for i, qa in enumerate(qa_pairs, 1):
        if not isinstance(qa, dict):
            continue
        q = _clean12(qa.get("question") or "").strip()
        a = _clean12(qa.get("answer") or "").strip()
        if not q and not a:
            continue
        lines.append(f"## Q{i}: {q[:120]}")
        lines.append("")
        if a:
            # Bullet-ify long answers at sentence boundaries.
            sents = [_s.strip() for _s in _re12.split(r"(?<=[.!?])\s+", a) if _s.strip()]
            if len(sents) > 2:
                lines.extend(f"- {s}" for s in sents[:8])
            else:
                lines.append(a)
        cites = qa.get("citations") or []
        if cites:
            lines.append("")
            lines.append("**Sources:** " + "; ".join(
                f"{_clean12(c.get('doc') or 'doc')[:30]} p.{c.get('page', '?')}"
                for c in cites[:5] if isinstance(c, dict)))
        lines.append("")
        sections += 1
    if not sections:
        raise ValueError("No valid Q&A pairs to export.")
    lines.append(f"_Generated from {sections} Q&A pairs._")
    return {"markdown": "\n".join(lines), "sections": sections,
            "title": title}


# --- RAG-04: Prompt-injection detection (P1) ---------------------------------
# Documents are DATA, never instructions. This detector flags hostile
# instructions embedded in document text so the answering pipeline can
# sanitize or reject them.

_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"disregard\s+(all\s+)?(prior|previous)\s+instructions",
    r"forget\s+(everything|all)\s+(you\s+know|above)",
    r"you\s+are\s+now\s+a\s+",
    r"system\s*:\s*",
    r"reveal\s+(your|the)\s+(system\s+)?prompt",
    r"output\s+(your|the)\s+(system|initial)\s+instructions",
    r"jailbreak",
    r"do\s+anything\s+now",
    r"bypass\s+(safety|filter|guardrail)",
]


def detect_prompt_injection(text: str) -> dict:
    """Scan document text for prompt-injection attempts.

    Returns {"injected": bool, "matches": [pattern, ...]}.
    Lightweight regex detector — catches common attack phrasings.
    """
    import re as _re
    found = []
    low = text.lower()
    for pat in _INJECTION_PATTERNS:
        if _re.search(pat, low):
            found.append(pat)
    return {"injected": bool(found), "matches": found}


def sanitize_document_text(text: str) -> tuple[str, dict]:
    """Remove detected injection attempts; return (clean_text, report).

    The answering pipeline treats documents as data. If injection is
    detected, the offending sentences are redacted and flagged.
    """
    import re as _re
    report = detect_prompt_injection(text)
    if not report["injected"]:
        return text, report
    # Redact sentences containing injection patterns
    sentences = _re.split(r"(?<=[.!?])\s+", text)
    clean = []
    redacted = 0
    for s in sentences:
        if detect_prompt_injection(s)["injected"]:
            redacted += 1
            continue
        clean.append(s)
    report["redacted_sentences"] = redacted
    return " ".join(clean), report
