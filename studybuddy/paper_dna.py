"""
Paper DNA — structured facts extracted from a document's chunks.

Every field is either a {text, page} citation back to a real passage or
None. Nothing is invented: when a paper does not state its dataset, the
dataset field is None and the UI says so. Extraction is heuristic
(section headings + keyword patterns over the same chunks the Q&A
engine uses), so DNA is a reading aid, not a peer review.

Sections come from chunk headings when the chunker recorded them;
otherwise the whole document is treated as one body of text.
"""

import re

_SECTION_PATTERNS = {
    "abstract": re.compile(r"abstract", re.IGNORECASE),
    "introduction": re.compile(r"introduction", re.IGNORECASE),
    "method": re.compile(r"method|methodology|materials and methods|experimental setup|approach", re.IGNORECASE),
    "results": re.compile(r"results|findings|experiments", re.IGNORECASE),
    "discussion": re.compile(r"discussion", re.IGNORECASE),
    "limitations": re.compile(r"limitations?|future work", re.IGNORECASE),
    "conclusion": re.compile(r"conclusion|concluding remarks", re.IGNORECASE),
}

_QUESTION_RES = [
    re.compile(r"\b(we investigate|we examine|we explore|we study|we ask|this paper investigates|this study investigates)\b", re.IGNORECASE),
    re.compile(r"\b(research question|aims? to|objective is to|goal is to)\b", re.IGNORECASE),
]
_METHOD_RES = [
    re.compile(r"\b(experiment|survey|model|algorithm|regression|trial|simulation|interview|trained|implemented|propose[sd]?)\b", re.IGNORECASE),
]
_DATASET_RES = [
    re.compile(r"\bdataset\b", re.IGNORECASE),
    re.compile(r"\bdata (was|were) collected\b", re.IGNORECASE),
]
_RESULT_RES = [
    re.compile(r"\b(significantly|increased|improved|achieved|outperform|accuracy|reduced|showed that|demonstrat\w+|achieves?)\b", re.IGNORECASE),
]
_LIMITATION_RES = [
    re.compile(r"\blimitation", re.IGNORECASE),
]
_SAMPLE_RES = [
    re.compile(r"\bn\s*=\s*([\d,]+)", re.IGNORECASE),
    re.compile(r"\b([\d,]+)\s+(participants|subjects|patients|students|respondents|samples|images|records)\b", re.IGNORECASE),
]

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_MAX_TEXT = 400


def _sentences(text):
    return [s.strip() for s in _SENT_SPLIT.split((text or "").replace("\n", " ")) if s.strip()]


def _section_of(chunk):
    heading = (chunk.get("heading") or "").strip()
    if not heading:
        return "body"
    for name, pattern in _SECTION_PATTERNS.items():
        if pattern.search(heading):
            return name
    return "body"


def _first_matching(chunks_by_section, sections, patterns, max_len=_MAX_TEXT):
    """First sentence (in reading order) matching any pattern, from the
    given sections in priority order. Returns {text, page} or None."""
    for section in sections:
        for chunk in chunks_by_section.get(section, []):
            for sentence in _sentences(chunk.get("text", "")):
                if any(p.search(sentence) for p in patterns):
                    text = sentence[:max_len].rstrip()
                    if len(sentence) > max_len:
                        text += "…"
                    return {"text": text, "page": int(chunk.get("page", 0) or 0)}
    return None


def _sample_size(chunks_by_section):
    """First explicit sample size (n = X, or 'N participants' style)."""
    for section in ["method", "abstract", "results", "introduction", "body"]:
        for chunk in chunks_by_section.get(section, []):
            for sentence in _sentences(chunk.get("text", "")):
                for pattern in _SAMPLE_RES:
                    m = pattern.search(sentence)
                    if m:
                        value = m.group(1).replace(",", "")
                        text = sentence[:_MAX_TEXT].rstrip()
                        if len(sentence) > _MAX_TEXT:
                            text += "…"
                        return {"value": value, "text": text,
                                "page": int(chunk.get("page", 0) or 0)}
    return None


def extract_dna(chunks):
    """Extract Paper DNA from a document's chunk list.

    Args:
        chunks: list of {page, index, text, heading?} (StudyDoc.data chunks).

    Returns a dict with one entry per field ({text, page} or None),
    plus sections_found and chunks_used.
    """
    if not isinstance(chunks, list):
        chunks = []
    by_section: dict[str, list] = {}
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        by_section.setdefault(_section_of(chunk), []).append(chunk)
    # Reading order within a section is chunk order (page, index).
    for section_chunks in by_section.values():
        section_chunks.sort(key=lambda c: (c.get("page", 0), c.get("index", 0)))

    all_sections = ["abstract", "introduction", "method", "results",
                    "discussion", "limitations", "conclusion", "body"]
    dna = {
        "research_question": _first_matching(
            by_section, ["abstract", "introduction", "body"], _QUESTION_RES),
        "method": _first_matching(
            by_section, ["method", "abstract", "body"], _METHOD_RES),
        "dataset": _first_matching(
            by_section, ["method", "abstract", "body"], _DATASET_RES),
        "sample_size": _sample_size(by_section),
        "key_result": _first_matching(
            by_section, ["results", "conclusion", "abstract", "body"], _RESULT_RES),
        "limitation": _first_matching(
            by_section, ["limitations", "discussion", "conclusion", "body"],
            _LIMITATION_RES),
        "sections_found": [s for s in all_sections if s in by_section],
        "chunks_used": sum(len(v) for v in by_section.values()),
    }
    return dna
