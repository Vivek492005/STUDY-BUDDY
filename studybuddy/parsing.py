"""
Hardened document parsing pipeline (ENG-05 + SEC-08).

ENG-05: robust resume parsing — pypdfium2 (Apache-2.0) as primary text
extractor, pdfplumber (MIT) for layout analysis, pypdf only as last resort.
DOCX: paragraphs + tables + headers/footers + text boxes (via raw XML).

SEC-08: hardened pipeline for untrusted files —
  1. content-type validation by magic bytes (not extension)
  2. hard limits: file size, page count, decompressed size, text length
  3. parsing runs in a sandboxed subprocess (rlimits + wall-clock timeout)
  4. originals deleted immediately after parsing (finally block)
  5. active content (JS/embedded files) ignored
  6. user-friendly errors, logging instead of print

License note: PyMuPDF is intentionally NOT used (AGPL-3.0).
"""
from __future__ import annotations

import io
import logging
import multiprocessing
import os
import re
import zipfile
from dataclasses import dataclass, field

log = logging.getLogger("acra.parsing")

# ---------------------------------------------------------------------------
# SEC-08 limits
# ---------------------------------------------------------------------------
# PERF-01 (audit Stage 4): these are the parsing-layer caps. The WEB
# ceiling is Flask's MAX_CONTENT_LENGTH (16 MB, unified_app.py), which
# rejects larger bodies before any handler runs — so the 30 MB paper
# cap binds only for direct/library calls, while the 5 MB resume cap
# (stricter than the ceiling) binds everywhere. The upload forms
# state the effective limits; keep all three places in sync.
MAX_RESUME_BYTES = 5 * 1024 * 1024        # 5 MB for resumes
MAX_PAPER_BYTES = 30 * 1024 * 1024        # 30 MB for research papers
MAX_RESUME_PAGES = 6
MAX_PAPER_PAGES = 200
MAX_DOCX_DECOMPRESSED = 50 * 1024 * 1024   # 50 MB zip-bomb cap
MAX_TEXT_CHARS = 500_000                  # extracted text cap
PARSE_TIMEOUT_S = 10                      # wall-clock sandbox timeout
PARSE_CPU_S = 15                          # rlimit CPU seconds in sandbox
PARSE_MAX_RSS = 512 * 1024 * 1024         # rlimit address space in sandbox

PDF_MAGIC = b"%PDF-"
DOCX_CONTENT_TYPES = b"[Content_Types].xml"
DOCX_DOCUMENT = b"word/document.xml"


# ---------------------------------------------------------------------------
# Result type (ENG-05)
# ---------------------------------------------------------------------------
@dataclass
class ParseResult:
    text: str = ""
    pages: int = 0
    layout: dict = field(default_factory=dict)    # columns, has_tables, ...
    quality: dict = field(default_factory=dict)   # chars_per_page, garbled_ratio
    warnings: list = field(default_factory=list)
    fields: dict = field(default_factory=dict)    # name, email, phone, links, ...
    ok: bool = True
    error: str = ""


class ParseError(Exception):
    """User-facing parse failure (message is safe to show)."""


# ---------------------------------------------------------------------------
# SEC-08.1 — content validation by magic bytes
# ---------------------------------------------------------------------------
def detect_type(data: bytes) -> str | None:
    """Return 'pdf' | 'docx' | None based on content, not extension."""
    if not data:
        return None
    head = data[:8]
    if head.startswith(PDF_MAGIC):
        return "pdf"
    # DOCX is a ZIP: PK\x03\x04, must contain the OOXML core parts
    if head[:4] == b"PK\x03\x04" or head[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
                if any(DOCX_CONTENT_TYPES.decode() in n for n in names) and \
                   any(DOCX_DOCUMENT.decode() in n for n in names):
                    return "docx"
        except zipfile.BadZipFile:
            return None
    return None


def validate_upload(data: bytes, filename: str = "", kind: str = "resume") -> str:
    """SEC-08: validate content + size. Returns 'pdf'|'docx' or raises ParseError."""
    max_bytes = MAX_RESUME_BYTES if kind == "resume" else MAX_PAPER_BYTES
    if len(data) > max_bytes:
        raise ParseError(
            f"File is too large ({len(data) // 1024} KB). "
            f"Maximum is {max_bytes // (1024 * 1024)} MB — try a shorter document.")
    if len(data) < 16:
        raise ParseError("File is empty or too small to be a valid document.")
    ftype = detect_type(data)
    if ftype is None:
        ext = os.path.splitext(filename)[1].lower()
        if ext == ".doc":
            raise ParseError(
                "Legacy .doc files are not supported. Please save/export as "
                ".docx or PDF and upload again.")
        raise ParseError(
            "Could not recognize this file. Please upload a text-based PDF "
            "or .docx file.")
    if ftype == "docx":
        # zip-bomb guard: cap total decompressed size before python-docx touches it
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                total = sum(i.file_size for i in zf.infolist())
                if total > MAX_DOCX_DECOMPRESSED:
                    raise ParseError(
                        "This .docx expands to an unexpectedly large size and "
                        "was rejected for safety. Try re-saving it.")
        except zipfile.BadZipFile:
            raise ParseError("This .docx file appears to be corrupted.")
    return ftype


# ---------------------------------------------------------------------------
# ENG-05 — field detection ("what a parser sees")
# ---------------------------------------------------------------------------
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}|\+\d[\d\s-]{7,}\d")
_URL_RE = re.compile(r"https?://[^\s)>\"]+|(?:www\.|linkedin\.com/|github\.com/)[^\s)>\"]*")
_SECTION_RES = [
    re.compile(r"^\s*(summary|objective|profile)\b", re.I),
    re.compile(r"^\s*(education|academic background|qualifications)\b", re.I),
    re.compile(r"^\s*(experience|work history|employment)\b", re.I),
    re.compile(r"^\s*(skills|technical skills|competencies)\b", re.I),
    re.compile(r"^\s*(projects|personal projects)\b", re.I),
    re.compile(r"^\s*(certifications|certificates)\b", re.I),
    re.compile(r"^\s*(achievements|awards|honors)\b", re.I),
]


def detect_fields(text: str) -> dict:
    """Extract name/email/phone/links/sections — 'what a parser sees' (ENG-05.5)."""
    fields: dict = {}
    emails = _EMAIL_RE.findall(text)
    if emails:
        fields["email"] = emails[0]
    phones = _PHONE_RE.findall(text)
    if phones:
        fields["phone"] = phones[0].strip()
    urls = _URL_RE.findall(text)
    if urls:
        fields["links"] = sorted(set(urls))[:10]
    # name heuristic: first non-empty line that isn't an email/phone/URL
    for line in text.splitlines():
        s = line.strip()
        if not s or len(s) > 60:
            continue
        if _EMAIL_RE.search(s) or _PHONE_RE.search(s) or _URL_RE.search(s):
            continue
        if re.match(r"^[A-Z][a-z]+(?: [A-Z][a-z'.-]+){1,3}$", s):
            fields["name"] = s
            break
    sections = []
    for line in text.splitlines():
        for rx in _SECTION_RES:
            if rx.match(line.strip()):
                sections.append(line.strip()[:40])
                break
    if sections:
        fields["sections"] = sections
    return fields


def _quality_metrics(text: str, pages: int) -> dict:
    chars = len(text)
    cpp = chars / max(pages, 1)
    # garbled ratio: share of non-printable / replacement chars
    weird = len(re.findall(r"[\ufffd\x00-\x08\x0b\x0c\x0e-\x1f]", text))
    garbled = weird / max(chars, 1)
    return {"chars_per_page": round(cpp, 1), "garbled_ratio": round(garbled, 4)}


# ---------------------------------------------------------------------------
# ENG-05.1 — PDF extraction: pypdfium2 -> pdfplumber layout -> pypdf fallback
# ---------------------------------------------------------------------------
def _extract_pdf_pdfium(data: bytes) -> tuple[str, dict]:
    """Primary extractor (Apache-2.0). Returns (text, layout_info)."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(io.BytesIO(data))
    try:
        n = len(doc)
        texts = []
        col_counts = []
        for i in range(n):
            page = doc[i]
            try:
                tp = page.get_textpage()
                try:
                    t = tp.get_text_range() or ""
                finally:
                    tp.close()
                texts.append(t)
                # column estimate via x-clustering of char boxes
                try:
                    chars = [c for c in tp.get_text_bounded()] if hasattr(tp, "get_text_bounded") else []
                except Exception:
                    chars = []
                col_counts.append(_estimate_columns(chars))
            finally:
                page.close()
        columns = max(col_counts) if col_counts else 1
        return "\n".join(texts), {"columns": columns, "extractor": "pypdfium2"}
    finally:
        doc.close()


def _estimate_columns(chars) -> int:
    """Crude column count: cluster char x-centers into bands."""
    if not chars:
        return 1
    try:
        xs = sorted((c[0] + c[2]) / 2 for c in chars[:2000])
    except Exception:
        return 1
    if not xs:
        return 1
    # split where gaps exceed 15% of page width
    width = xs[-1] - xs[0] or 1
    bands, start = 1, xs[0]
    for x in xs[1:]:
        if x - start > width * 0.15 and x - xs[xs.index(x) - 1] > width * 0.08:
            bands += 1
            start = x
    return min(bands, 4)


def _extract_pdf_plumber(data: bytes) -> tuple[str, dict]:
    """Layout-aware fallback (MIT): char sorting + table detection."""
    import pdfplumber
    texts, tables_found, col_counts = [], 0, []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            t = page.extract_text(x_tolerance=2, y_tolerance=2) or ""
            texts.append(t)
            try:
                if page.find_tables():
                    tables_found += 1
            except Exception:
                pass
            col_counts.append(1)
    return "\n".join(texts), {"columns": 1, "has_tables": tables_found > 0,
                              "extractor": "pdfplumber"}


def _extract_pdf_pypdf(data: bytes) -> tuple[str, dict]:
    """Last-resort fallback (pinned pypdf)."""
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(data))
    texts = [(p.extract_text() or "") for p in reader.pages]
    return "\n".join(texts), {"columns": 1, "extractor": "pypdf"}


def _pdf_page_count(data: bytes) -> int:
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(io.BytesIO(data))
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception:
        return 0


def extract_pdf(data: bytes, max_pages: int) -> ParseResult:
    res = ParseResult()
    n_pages = _pdf_page_count(data)
    if n_pages == 0:
        raise ParseError(
            "Could not read this PDF. Try exporting again as a text-based PDF.")
    if n_pages > max_pages:
        raise ParseError(
            f"This PDF has {n_pages} pages (limit {max_pages}). "
            "Please upload a shorter document.")
    res.pages = n_pages

    text, layout = "", {}
    errors = []
    for fn in (_extract_pdf_pdfium, _extract_pdf_plumber, _extract_pdf_pypdf):
        try:
            text, layout = fn(data)
            if text.strip():
                break
        except Exception as e:  # never swallow silently — record and continue
            log.warning("PDF extractor %s failed: %s", fn.__name__, e)
            errors.append(f"{fn.__name__}: {e}")
    if not text.strip():
        raise ParseError(
            "Could not extract any text from this PDF. "
            + ("It looks like a scanned image — " if _looks_scanned(data, n_pages)
               else "")
            + "Try exporting again as a text-based PDF.")
    res.text = text[:MAX_TEXT_CHARS]
    res.layout = layout
    res.quality = _quality_metrics(res.text, n_pages)
    if res.quality["chars_per_page"] < 100:
        res.warnings.append(
            "Scanned PDF detected: very little selectable text. "
            "OCR is not enabled on this host, so results may be incomplete.")
    if res.quality["garbled_ratio"] > 0.05:
        res.warnings.append("Text looks garbled — the PDF may use unusual fonts.")
    if errors:
        res.warnings.append(f"Primary extractor failed, used fallback ({layout.get('extractor')}).")
    res.fields = detect_fields(res.text)
    return res


def _looks_scanned(data: bytes, n_pages: int) -> bool:
    """Heuristic: many pages but pdfium finds almost no text."""
    try:
        text, _ = _extract_pdf_pdfium(data)
        return len(text.strip()) < 50 * n_pages
    except Exception:
        return False


# ---------------------------------------------------------------------------
# ENG-05.4 — DOCX: paragraphs + tables + headers/footers + text boxes
# ---------------------------------------------------------------------------
def extract_docx(data: bytes) -> ParseResult:
    from docx import Document
    res = ParseResult()
    doc = Document(io.BytesIO(data))

    parts = []
    # paragraphs (body)
    for p in doc.paragraphs:
        if p.text.strip():
            parts.append(p.text)
    # tables (python-docx misses text boxes; tables are explicit here)
    n_tables = 0
    for tbl in doc.tables:
        n_tables += 1
        for row in tbl.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    # headers / footers
    for section in doc.sections:
        for part in (section.header, section.footer):
            for p in part.paragraphs:
                if p.text.strip():
                    parts.append(p.text)
    # text boxes via raw XML (w:txbxContent) — ENG-05.4
    try:
        txbx = doc.element.findall(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}txbxContent")
        for tb in txbx:
            t = "".join(n.text or "" for n in tb.iter() if n.tag.endswith("}t"))
            if t.strip():
                parts.append(t)
        if txbx:
            res.warnings.append(
                "Text boxes detected — some parsers miss these; content was recovered from raw XML.")
    except Exception as e:
        log.warning("DOCX textbox scan failed: %s", e)

    text = "\n".join(parts)
    if not text.strip():
        raise ParseError("Could not extract any text from this .docx file.")
    res.text = text[:MAX_TEXT_CHARS]
    res.pages = 1
    res.layout = {"columns": 1, "has_tables": n_tables > 0, "extractor": "python-docx"}
    res.quality = _quality_metrics(res.text, 1)
    if n_tables:
        res.warnings.append(
            f"{n_tables} table(s) found — tables can break real-world parsers; "
            "verify the extracted order below.")
    res.fields = detect_fields(res.text)
    return res


# ---------------------------------------------------------------------------
# SEC-08.3 — sandboxed subprocess parsing
# ---------------------------------------------------------------------------
def _sandbox_target(data: bytes, ftype: str, max_pages: int, queue):
    """Runs in the child: apply rlimits, parse, send result back via queue."""
    try:
        import resource
        # CPU seconds (hard kill) and address space cap
        resource.setrlimit(resource.RLIMIT_CPU, (PARSE_CPU_S, PARSE_CPU_S))
        resource.setrlimit(resource.RLIMIT_AS, (PARSE_MAX_RSS, PARSE_MAX_RSS))
    except Exception as e:
        log.warning("Could not set rlimits in parse sandbox: %s", e)
    try:
        if ftype == "pdf":
            res = extract_pdf(data, max_pages)
        else:
            res = extract_docx(data)
        queue.put(("ok", res))
    except ParseError as e:
        queue.put(("parse_error", str(e)))
    except MemoryError:
        queue.put(("error", "File was too complex to parse within memory limits."))
    except Exception:
        log.exception("Sandbox parse failed")
        queue.put(("error", "Could not read this file. Try exporting again."))


def parse_document(data: bytes, filename: str = "", kind: str = "resume") -> ParseResult:
    """SEC-08 entry point: validate -> sandbox-parse -> ParseResult.

    The caller's bytes are never written to disk; the child parses purely
    in memory and the result crosses the process boundary pickled.

    Set STUDYBUDDY_NO_PARSE_SANDBOX=1 to parse in-process (tests only).
    """
    ftype = validate_upload(data, filename, kind)
    max_pages = MAX_RESUME_PAGES if kind == "resume" else MAX_PAPER_PAGES

    if os.environ.get("STUDYBUDDY_NO_PARSE_SANDBOX") == "1":
        # Test mode: parse in-process (no multiprocessing overhead)
        try:
            if ftype == "pdf":
                return extract_pdf(data, max_pages)
            return extract_docx(data)
        except ParseError:
            raise
        except Exception as e:
            raise ParseError(f"Could not parse this file: {e}")

    # Use spawn (not fork): safe in multi-threaded processes like gunicorn
    # threaded workers or pytest-xdist. Queue carries the pickled result.
    try:
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        proc = ctx.Process(
            target=_sandbox_target, args=(data, ftype, max_pages, queue),
            daemon=True)
        proc.start()
        try:
            try:
                status, payload = queue.get(timeout=PARSE_TIMEOUT_S)
            except Exception:
                proc.terminate()
                proc.join(timeout=2)
                raise ParseError(
                    "Parsing timed out — this file is too complex. "
                    "Try a simpler export.")
            if status == "ok":
                return payload
            raise ParseError(payload)
        finally:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=2)
    except ParseError:
        raise
    except Exception as e:
        # lxml can raise non-Exception subclasses or have races under load;
        # normalize everything to ParseError for a clean API contract.
        raise ParseError(f"Could not parse this file: {e}")


# ---------------------------------------------------------------------------
# SEC-08.4 — upload helper: parse then delete the temp file immediately
# ---------------------------------------------------------------------------
def parse_upload_file(path: str, filename: str = "", kind: str = "resume") -> ParseResult:
    """Read file, parse in sandbox, delete original in a finally block."""
    try:
        with open(path, "rb") as f:
            data = f.read()
        return parse_document(data, filename=filename, kind=kind)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
