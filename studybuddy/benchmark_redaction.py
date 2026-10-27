"""GFI #5: PII redaction for opt-in benchmark contributions.

A contributed resume is stored ONLY in redacted form, and only after the
contributor has seen exactly what will be stored (editable preview) and
checked explicit consent. This module is the redaction half of that
guarantee; the storage half lives in studybuddy/benchmark_collection.py
(`store_contribution` refuses to persist anything without consent).

What is redacted (pure functions, no app state, no unified_app import):
  - names: the header line's leading name segment (e.g. "John Doe" in
    "John Doe | john@example.com"), plus any "Name:" labelled line.
  - emails, phones, URLs: reused from studybuddy/pii_redaction (its
    mapping, which contains the original values, is DISCARDED here —
    it is never returned, logged, or stored).
  - postal addresses: "Address:"-labelled lines (value replaced, label kept).
  - dates of birth: "Date of Birth:"/"DOB:"-labelled lines.
  - IDs: PAN, Aadhaar-style 12-digit groups, and 6-digit PIN codes.

Honest limits (documented, not hidden):
  - Automatic name detection is heuristic: it covers the header line and
    "Name:" labels. A name appearing mid-body (e.g. in a reference) is
    NOT auto-detected. That is why the preview textarea is EDITABLE:
    the contributor can strike anything else before confirming, and the
    server re-redacts the confirmed text before storing. The stored text
    is therefore always the output of this function, never raw input.
  - Over-redaction is possible and accepted: a 6-digit number that is
    not a PIN (e.g. "100000 users") becomes [PIN]. For aggregate
    benchmark statistics this is negligible, and it errs toward privacy.
  - The report contains COUNTS ONLY — never the removed values.

The return value is (redacted_text, report) where report is
{"counts": {category: int}, "redacted_any": bool}.
"""

from __future__ import annotations

import re

from studybuddy.pii_redaction import redact as _pii_redact

# Words that mark a header line as a section title, not a person's name.
_NON_NAME_WORDS = {
    "summary", "experience", "education", "skills", "projects", "resume",
    "curriculum", "vitae", "cv", "contact", "profile", "objective",
    "work", "professional", "employment", "certifications", "languages",
}

# Words that mark a header line as a job title, not a person's name
# (many resumes open with a title line instead of a name line).
_JOB_WORDS = {
    "engineer", "developer", "manager", "analyst", "designer",
    "scientist", "consultant", "architect", "lead", "senior", "junior",
    "intern", "executive", "director", "specialist", "administrator",
    "programmer", "researcher", "software", "data", "frontend", "backend",
    "fullstack", "full-stack", "student", "graduate", "aspiring",
}

# A name segment: 1-4 words of letters, apostrophes, dots, hyphens.
_NAME_SEGMENT_RE = re.compile(r"^[A-Za-z][A-Za-z'’.\-]*(?: [A-Za-z][A-Za-z'’.\-]*){0,3}$")

# Header separators: the name usually precedes the first of these.
_HEADER_SEP_RE = re.compile(r"[|•·]")

_PAN_RE = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")
_AADHAAR_RE = re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b")
_PIN_RE = re.compile(r"\b[1-9]\d{5}\b")

# Labelled lines: keep the label (it is not PII), replace the value.
_LABEL_RE = re.compile(
    r"^(?P<label>\s*(?:permanent\s+address|current\s+address|residential\s+address"
    r"|mailing\s+address|address|date\s+of\s+birth|\bdob\b|name)\s*:).*$",
    re.IGNORECASE | re.MULTILINE,
)


def _is_name_segment(segment: str) -> bool:
    segment = segment.strip()
    if not segment or not _NAME_SEGMENT_RE.match(segment):
        return False
    words = {w.strip(".'’").lower() for w in segment.split()}
    if words & _NON_NAME_WORDS:
        return False
    if words & _JOB_WORDS:
        return False
    return True


def _redact_header_name(text: str) -> tuple[str, int]:
    """Redact the leading name segment of the first non-empty line."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        stripped = line.strip()
        # Whole line is a name (e.g. "John Doe").
        if _is_name_segment(stripped):
            lines[i] = line.replace(stripped, "[NAME]", 1)
            return "\n".join(lines), 1
        # Header with separators (e.g. "John Doe | john@x.com | +91 ..."):
        # only the leading segment can be the name.
        m = _HEADER_SEP_RE.search(line)
        if m:
            head = line[:m.start()].strip()
            if _is_name_segment(head):
                lines[i] = line.replace(head, "[NAME]", 1)
                return "\n".join(lines), 1
        # First non-empty line is not a name header; stop looking.
        return text, 0
    return text, 0


def _redact_labelled_lines(text: str) -> tuple[str, dict[str, int]]:
    counts = {"address": 0, "dob": 0, "name": 0}

    def repl(m: re.Match) -> str:
        label = m.group("label")
        key = label.strip().lower().rstrip(":")
        if "address" in key:
            counts["address"] += 1
            ph = "[ADDRESS]"
        elif key in ("dob", "date of birth"):
            counts["dob"] += 1
            ph = "[DOB]"
        else:
            counts["name"] += 1
            ph = "[NAME]"
        return f"{label} {ph}"

    return _LABEL_RE.sub(repl, text), counts


def redact_resume_text(text: str) -> tuple[str, dict]:
    """Redact PII from resume text. Returns (redacted_text, report).

    The report holds counts only — never removed values — so it is safe
    to show in the preview and to log.
    """
    if not text or not text.strip():
        return "", {"counts": {}, "redacted_any": False}

    counts: dict[str, int] = {}

    # 1. Header name (before other passes alter the first line).
    text, n = _redact_header_name(text)
    if n:
        counts["name"] = counts.get("name", 0) + n

    # 2. Labelled address / DOB / name lines.
    text, labelled = _redact_labelled_lines(text)
    for kind, n in labelled.items():
        if n:
            counts[kind] = counts.get(kind, 0) + n

    # 3. IDs: PAN, Aadhaar-style groups, PIN codes.
    for pattern, kind, placeholder in (
        (_PAN_RE, "pan", "[PAN]"),
        (_AADHAAR_RE, "aadhaar", "[AADHAAR]"),
        (_PIN_RE, "pin", "[PIN]"),
    ):
        found = pattern.findall(text)
        if found:
            counts[kind] = counts.get(kind, 0) + len(found)
            text = pattern.sub(placeholder, text)

    # 4. Emails, phones, URLs via the shared redactor. Its mapping holds
    # the original values, so it is discarded immediately — never
    # returned, logged, or stored.
    text, mapping = _pii_redact(text)
    for key in mapping:
        if key.startswith("[EMAIL_"):
            counts["email"] = counts.get("email", 0) + 1
        elif key.startswith("[PHONE_"):
            counts["phone"] = counts.get("phone", 0) + 1
        elif key.startswith("[URL_"):
            counts["url"] = counts.get("url", 0) + 1

    counts = {k: v for k, v in counts.items() if v}
    return text, {"counts": counts, "redacted_any": bool(counts)}
