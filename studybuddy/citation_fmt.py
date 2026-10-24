"""Citation formatter (study feature: "Cite this" / citation formatter).

Pure-logic module — no network, no dependencies, no Flask. Formats
academic references in five styles, converts to/from BibTeX, and ships
per-style usage guidance for the UI.

Metadata dict keys (all optional; missing fields degrade gracefully):
    title, authors (list of "First Last" or "Last, First" strings),
    year, venue (journal/conference), volume, issue, pages, doi,
    arxiv_id, url, pubtype ("article" | "inproceedings" | "misc").

Style rules are taken from the official guides (verified 2026-10-14):
  IEEE       — IEEE Editorial Style Manual: initials + surname, >6 authors
               -> first + "et al.", sentence-case title in quotes,
               ``vol. x, no. y, pp. z``, DOI as ``doi: 10.xxxx``.
  APA        — APA Publication Manual 7th ed.: <=20 authors all listed,
               21+ -> first 19 + ellipsis + last (no ampersand there),
               sentence-case title, DOI as https://doi.org/ URL, (n.d.)
               when the year is missing.
  MLA        — MLA Handbook 9th ed.: 3+ authors -> first + "et al.",
               headline-style title in quotes, container (venue) italic,
               ``vol. x, no. y``, year, ``pp. z``.
  Vancouver  — ICMJE / NLM: surname + initials (no periods), 1-6 listed,
               7+ -> first 6 + "et al.", sentence-case title, roman type,
               ``Year;Volume(Issue):Pages``, numbered reference list.
  Chicago    — Chicago Manual of Style 17th ed., author-date: first author
               inverted, up to 10 authors listed (11+ -> first 7 + "et al."),
               headline-style title in quotes, ``Venue Volume (Issue):
               Pages``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Iterator
from typing import Any

STYLES = ("ieee", "apa", "mla", "vancouver", "chicago")

_STYLE_ALIASES = {
    "ieee": "ieee",
    "apa": "apa", "apa7": "apa", "apa-7": "apa",
    "mla": "mla", "mla9": "mla", "mla-9": "mla",
    "vancouver": "vancouver", "nlm": "vancouver", "icmje": "vancouver",
    "chicago": "chicago", "chicago-author-date": "chicago",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _norm_style(style: str | None) -> str:
    key = str(style or "").strip().lower()
    if key in _STYLE_ALIASES:
        return _STYLE_ALIASES[key]
    raise ValueError(
        "Unknown citation style: %r. Supported styles: %s."
        % (style, ", ".join(STYLES))
    )


def _str(meta: dict[str, Any] | None, key: str) -> str:
    val = (meta or {}).get(key)
    if val is None:
        return ""
    return str(val).strip()


def _authors(meta: dict[str, Any] | None) -> list[str]:
    raw = (meta or {}).get("authors") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(a).strip() for a in raw if str(a).strip()]


def _split_name(name: str | None) -> tuple[str, str]:
    """Split an author string into (first_names, surname)."""
    name = (name or "").strip()
    if "," in name:
        last, _, first = name.partition(",")
        return first.strip(), last.strip()
    parts = name.split()
    if len(parts) <= 1:
        return "", name
    return " ".join(parts[:-1]), parts[-1]


def _initials(first: str | None) -> str:
    """'Aidan N.' -> 'A. N.'; 'Ai-Jun' -> 'A.-J.' (APA hyphen rule)."""
    out = []
    for token in (first or "").split():
        subs = [s[0].upper() + "." for s in token.split("-") if s]
        if subs:
            out.append("-".join(subs))
    return " ".join(out)


def _sentence_case(title: str | None) -> str:
    """APA/IEEE/Vancouver rule: first word (+ first word after a colon)
    capitalised, everything else lowercased."""
    title = (title or "").strip().rstrip(".")
    if not title:
        return ""
    chunks = []
    for chunk in title.split(": "):
        chunk = chunk.strip()
        chunks.append(chunk[:1].upper() + chunk[1:].lower() if chunk else "")
    return ": ".join(chunks)


_MINOR_WORDS = frozenset(
    "a an the and but or nor for so yet as at by in of on to up via vs v vs. per".split()
)


def _title_case(title: str | None) -> str:
    """Headline style for MLA/Chicago: capitalise major words."""
    title = (title or "").strip().rstrip(".")
    if not title:
        return ""

    def cap_word(word: str, first_or_last: bool) -> str:
        subs = []
        for i, sub in enumerate(word.split("-")):
            low = sub.lower()
            if not first_or_last and i > 0 and low in _MINOR_WORDS:
                subs.append(low)
            elif low in _MINOR_WORDS and not (first_or_last):
                subs.append(low)
            else:
                subs.append(sub[:1].upper() + sub[1:] if sub else "")
        return "-".join(subs)

    words = title.split()
    out = []
    for wi, word in enumerate(words):
        first_or_last = wi == 0 or wi == len(words) - 1
        after_colon = out and out[-1].endswith(":")
        out.append(cap_word(word, first_or_last or after_colon))
    return " ".join(out)


def _pages(raw: Any) -> str:
    p = _str({"p": raw}, "p").rstrip(".")
    p = re.sub(r"\s*--\s*", "\u2013", p)
    p = re.sub(r"(?<=\d)\s*-\s*(?=\d)", "\u2013", p)
    return p


def _doi(raw: Any) -> str:
    d = _str({"d": raw}, "d")
    d = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", d, flags=re.I).strip()
    return d


def _tidy(text: str | None) -> str:
    """Collapse whitespace and duplicated punctuation; never emits 'None'."""
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,.])\1+", r"\1", text)
    return text


def _is_preprint(meta: dict[str, Any] | None) -> bool:
    return bool(_str(meta, "arxiv_id")) and not _str(meta, "venue")


# ---------------------------------------------------------------------------
# Per-style author lists
# ---------------------------------------------------------------------------

def _ieee_authors(authors: list[str]) -> str:
    fmt = ["%s %s" % (_initials(f), l) if f else l
           for f, l in (_split_name(a) for a in authors)]
    if len(fmt) > 6:
        return fmt[0] + " et al."
    if len(fmt) == 2:
        return " and ".join(fmt)
    if len(fmt) > 2:
        return ", ".join(fmt[:-1]) + ", and " + fmt[-1]
    return ", ".join(fmt)


def _apa_authors(authors: list[str]) -> str:
    fmt = ["%s, %s" % (l, _initials(f)) if f else l
           for f, l in (_split_name(a) for a in authors)]
    if len(fmt) > 20:
        return ", ".join(fmt[:19]) + ", \u2026 " + fmt[-1]
    if len(fmt) >= 2:
        return ", ".join(fmt[:-1]) + ", & " + fmt[-1]
    return ", ".join(fmt)


def _mla_authors(authors: list[str]) -> str:
    if len(authors) >= 3:
        f, l = _split_name(authors[0])
        first = ("%s, %s" % (l, f)).strip(", ") if f else l
        return first + ", et al."
    if len(authors) == 2:
        f1, l1 = _split_name(authors[0])
        f2, l2 = _split_name(authors[1])
        a1 = ("%s, %s" % (l1, f1)).strip(", ") if f1 else l1
        a2 = ("%s %s" % (f2, l2)).strip() if f2 else l2
        return "%s, and %s" % (a1, a2)
    if authors:
        f, l = _split_name(authors[0])
        return ("%s, %s" % (l, f)).strip(", ") if f else l
    return ""


def _vancouver_authors(authors: list[str]) -> str:
    fmt = ["%s %s" % (l, _initials(f).replace(". ", "").replace(".", ""))
           for f, l in (_split_name(a) for a in authors)]
    # "A. N." -> "AN": strip periods and spaces from the initials block.
    if len(fmt) > 6:
        return ", ".join(fmt[:6]) + ", et al."
    return ", ".join(fmt)


def _chicago_authors(authors: list[str]) -> str:
    if len(authors) > 10:
        shown = authors[:7]
        tail = ", et al."
    else:
        shown, tail = authors, ""
    fmt = []
    for i, a in enumerate(shown):
        f, l = _split_name(a)
        if i == 0:
            fmt.append(("%s, %s" % (l, f)).strip(", ") if f else l)
        else:
            fmt.append(("%s %s" % (f, l)).strip() if f else l)
    if len(fmt) == 2:
        return fmt[0] + ", and " + fmt[1] + tail
    if len(fmt) > 2:
        return ", ".join(fmt[:-1]) + ", and " + fmt[-1] + tail
    return "".join(fmt) + tail


# ---------------------------------------------------------------------------
# Per-style formatters
# ---------------------------------------------------------------------------

def _fmt_ieee(meta: dict[str, Any] | None) -> str:
    authors = _ieee_authors(_authors(meta))
    title = _sentence_case(_str(meta, "title"))
    venue = _str(meta, "venue")
    arxiv = _str(meta, "arxiv_id")
    inproc = _str(meta, "pubtype") == "inproceedings"

    head = authors
    if title:
        head += (", " if head else "") + '"%s,"' % title

    segs = []
    if venue:
        v = "*%s*" % venue
        segs.append(("in " + v) if inproc else v)
    elif arxiv:
        segs.append("arXiv:%s" % arxiv)
    if _str(meta, "volume"):
        segs.append("vol. %s" % _str(meta, "volume"))
    if _str(meta, "issue"):
        segs.append("no. %s" % _str(meta, "issue"))
    pages = _pages(meta.get("pages"))
    if pages:
        segs.append("pp. %s" % pages)
    if _str(meta, "year"):
        segs.append(_str(meta, "year"))
    doi = _doi(meta.get("doi"))
    if doi:
        segs.append("doi: %s" % doi)

    s = head + (" " + ", ".join(segs) if segs else "")
    s = re.sub(r"[,;:\s]+$", "", s.strip())
    if s and not s.endswith((".", "?", "!")):
        s += "."
    return _tidy(s)


def _fmt_apa(meta: dict[str, Any] | None) -> str:
    authors = _apa_authors(_authors(meta))
    year = _str(meta, "year")
    title = _sentence_case(_str(meta, "title"))
    venue = _str(meta, "venue")
    arxiv = _str(meta, "arxiv_id")
    year_s = "(%s)" % year if year else "(n.d.)"
    if _is_preprint(meta) and title:
        title += " [Preprint]"

    if authors:
        s = "%s %s." % (authors, year_s)
        if title:
            s += " %s." % title
    elif title:
        # No author: the title moves to the author position (APA 9.12).
        s = "%s. %s." % (title, year_s)
    else:
        s = "%s." % year_s

    segs = []
    if venue:
        v = "*%s*" % venue
        if _str(meta, "volume"):
            v += ", *%s*" % _str(meta, "volume")
            if _str(meta, "issue"):
                v += "(%s)" % _str(meta, "issue")
        segs.append(v)
    elif arxiv:
        segs.append("*arXiv*")
    pages = _pages(meta.get("pages"))
    if pages:
        segs.append(pages)

    if segs:
        s += " " + ", ".join(segs) + "."
    doi = _doi(meta.get("doi"))
    if doi:
        s += " https://doi.org/%s" % doi
    elif arxiv and not venue:
        s += " https://doi.org/10.48550/arXiv.%s" % arxiv
    elif _str(meta, "url"):
        s += " %s" % _str(meta, "url")
    return _tidy(s)


def _fmt_mla(meta: dict[str, Any] | None) -> str:
    authors = _mla_authors(_authors(meta))
    title = _title_case(_str(meta, "title"))
    venue = _str(meta, "venue")
    arxiv = _str(meta, "arxiv_id")

    s = ""
    if authors:
        s += authors + ". "
    if title:
        s += '"%s." ' % title

    segs = []
    if venue:
        segs.append("*%s*" % venue)
    elif arxiv:
        segs.append("*arXiv*")
    if _str(meta, "volume"):
        segs.append("vol. %s" % _str(meta, "volume"))
    if _str(meta, "issue"):
        segs.append("no. %s" % _str(meta, "issue"))
    if _str(meta, "year"):
        segs.append(_str(meta, "year"))
    pages = _pages(meta.get("pages"))
    if pages:
        segs.append("pp. %s" % pages)

    s += ", ".join(segs)
    s = re.sub(r"[,;:\s]+$", "", s.strip())
    if s and not s.endswith((".", "?", "!")):
        s += "."

    # The DOI/URL is its own trailing sentence (MLA 9th).
    doi = _doi(meta.get("doi"))
    if doi:
        s += " doi:%s." % doi
    elif arxiv and not venue:
        s += " https://arxiv.org/abs/%s." % arxiv
    elif _str(meta, "url"):
        s += " %s." % _str(meta, "url").rstrip(".")
    return _tidy(s)


def _fmt_vancouver(meta: dict[str, Any] | None) -> str:
    authors = _vancouver_authors(_authors(meta))
    title = _sentence_case(_str(meta, "title"))
    venue = _str(meta, "venue")
    arxiv = _str(meta, "arxiv_id")
    inproc = _str(meta, "pubtype") == "inproceedings"

    s = ""
    if authors:
        s += authors + ". "
    if title:
        t = title + (" [Preprint]" if _is_preprint(meta) else "")
        s += "%s. " % t

    if venue:
        s += ("In: %s. " % venue) if inproc else ("%s. " % venue)

    dv = _str(meta, "year")
    vol = _str(meta, "volume")
    issue = _str(meta, "issue")
    pages = _pages(meta.get("pages"))
    if vol:
        dv = (dv + ";" if dv else "") + vol
        if issue:
            dv += "(%s)" % issue
    if pages:
        dv = (dv + ":" if dv else "") + pages
    if dv:
        s += "%s. " % dv

    doi = _doi(meta.get("doi"))
    if doi:
        s += "https://doi.org/%s " % doi
    elif arxiv and not venue:
        s += "Available from: https://arxiv.org/abs/%s " % arxiv
    elif _str(meta, "url"):
        s += "Available from: %s " % _str(meta, "url")

    s = re.sub(r"[,;:\s]+$", "", s.strip())
    if s and not s.endswith((".", "?", "!")):
        s += "."
    return _tidy(s)


def _fmt_chicago(meta: dict[str, Any] | None) -> str:
    authors = _chicago_authors(_authors(meta))
    year = _str(meta, "year") or "n.d."
    title = _title_case(_str(meta, "title"))
    venue = _str(meta, "venue")
    arxiv = _str(meta, "arxiv_id")
    inproc = _str(meta, "pubtype") == "inproceedings"

    s = ""
    if authors:
        s += authors + ". "
    s += "%s. " % year
    if title:
        s += '"%s." ' % title

    if venue:
        vp = "*%s*" % venue
        if _str(meta, "volume"):
            vp += " %s" % _str(meta, "volume")
            if _str(meta, "issue"):
                vp += " (%s)" % _str(meta, "issue")
        pages = _pages(meta.get("pages"))
        if pages:
            vp += ": %s" % pages
        s += ("In %s. " % vp) if inproc else ("%s. " % vp)
    elif arxiv:
        s += "*arXiv*. "

    doi = _doi(meta.get("doi"))
    if doi:
        s += "https://doi.org/%s." % doi
    elif arxiv and not venue:
        s += "https://arxiv.org/abs/%s." % arxiv
    elif _str(meta, "url"):
        s += "%s." % _str(meta, "url")

    return _tidy(s)


_FORMATTERS = {
    "ieee": _fmt_ieee,
    "apa": _fmt_apa,
    "mla": _fmt_mla,
    "vancouver": _fmt_vancouver,
    "chicago": _fmt_chicago,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _strip_html(text: str | None) -> str:
    """Remove HTML tags from citation fields — never legitimate in citations."""
    return re.sub(r"<[^>]*>", "", text or "")


def _clean_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    """Strip HTML from all string values in a citation metadata dict."""
    cleaned = {}
    for k, v in (meta or {}).items():
        if isinstance(v, str):
            cleaned[k] = _strip_html(v)
        elif isinstance(v, list):
            cleaned[k] = [_strip_html(i) if isinstance(i, str) else i for i in v]
        else:
            cleaned[k] = v
    return cleaned


def format_citation(meta: dict[str, Any] | None, style: str) -> str:
    """Format one metadata dict in the given style.

    Raises ValueError for an unknown style name.
    """
    style = _norm_style(style)
    return _FORMATTERS[style](_clean_meta(meta))


def _sort_key(meta: dict[str, Any] | None) -> str:
    authors = _authors(meta)
    if authors:
        return _split_name(authors[0])[1].lower()
    return _str(meta, "title").lower()


def format_bibliography(
    metas: list[dict[str, Any]] | None,
    style: str,
) -> str:
    """Format many metadata dicts.

    IEEE/Vancouver are numbered in the given order ("[1]", "1.");
    APA/MLA/Chicago are sorted alphabetically by first-author surname.
    """
    style = _norm_style(style)
    metas = list(metas or [])
    if style in ("ieee", "vancouver"):
        lines = []
        for i, meta in enumerate(metas, 1):
            prefix = "[%d]" % i if style == "ieee" else "%d." % i
            lines.append("%s %s" % (prefix, format_citation(meta, style)))
        return "\n".join(lines)
    ordered = sorted(metas, key=_sort_key)
    return "\n".join(format_citation(m, style) for m in ordered)


# ---------------------------------------------------------------------------
# BibTeX
# ---------------------------------------------------------------------------

def _bibtex_escape(value: str | None) -> str:
    return re.sub(r"([\\&%$#_{}])", r"\\\1", value or "")


def _bibtex_unescape(value: str | None) -> str:
    value = re.sub(r"(?<!\\)[{}]", "", value or "")
    return re.sub(r"\\([\\&%$#_{}])", r"\1", value)


def _protect_title_caps(title: str | None) -> str:
    """Brace-protect capitalised words so BibTeX styles don't downcase them."""
    return re.sub(r"(^|\s)([A-Z][^\s]*)", r"\1{\2}", title or "")


def _ascii_fold(text: str | None) -> str:
    return unicodedata.normalize("NFKD", text or "").encode(
        "ascii", "ignore").decode("ascii")


def _bibtex_key(meta: dict[str, Any] | None) -> str:
    authors = _authors(meta)
    if authors:
        surname = _split_name(authors[0])[1] or "anon"
    else:
        surname = "anon"
    stem = re.sub(r"[^a-z0-9]", "", _ascii_fold(surname).lower()) or "anon"
    year = _str(meta, "year") or "xxxx"
    return "%s%s" % (stem, year)


def to_bibtex(
    meta: dict[str, Any] | None,
    existing_keys: Iterable[str] | None = None,
) -> str:
    """Export one metadata dict as a BibTeX entry string.

    existing_keys: iterable of keys already taken; on collision the key
    gains an a/b/c suffix (vaswani2017, vaswani2017a, ...).
    """
    meta = meta or {}
    existing = set(existing_keys or [])
    pubtype = _str(meta, "pubtype").lower()
    entry = {"article": "article",
             "inproceedings": "inproceedings",
             "conference": "inproceedings"}.get(pubtype, "misc")

    base = _bibtex_key(meta)
    key = base
    n = 0
    while key in existing:
        key = "%s%s" % (base, chr(ord("a") + n))
        n += 1

    fields = []
    authors = _authors(meta)
    if authors:
        fields.append(("author",
                       " and ".join(_bibtex_escape(a) for a in authors)))
    title = _str(meta, "title")
    if title:
        fields.append(("title",
                       _protect_title_caps(_bibtex_escape(title))))
    venue = _str(meta, "venue")
    if venue:
        vkey = "journal" if entry == "article" else "booktitle"
        fields.append((vkey, _bibtex_escape(venue)))
    if _str(meta, "volume"):
        fields.append(("volume", _bibtex_escape(_str(meta, "volume"))))
    if _str(meta, "issue"):
        fields.append(("number", _bibtex_escape(_str(meta, "issue"))))
    pages = _pages(meta.get("pages"))
    if pages:
        fields.append(("pages", pages.replace("\u2013", "--")))
    if _str(meta, "year"):
        fields.append(("year", _bibtex_escape(_str(meta, "year"))))
    doi = _doi(meta.get("doi"))
    if doi:
        fields.append(("doi", _bibtex_escape(doi)))
    arxiv = _str(meta, "arxiv_id")
    if entry == "misc" and arxiv and not venue:
        fields.append(("howpublished", "arXiv preprint"))
    if arxiv:
        fields.append(("eprint", _bibtex_escape(arxiv)))
    url = _str(meta, "url")
    if url:
        fields.append(("url", _bibtex_escape(url)))
    elif entry == "misc" and arxiv and not venue:
        fields.append(("url", "https://arxiv.org/abs/%s" % arxiv))

    lines = ["@%s{%s," % (entry, key)]
    for name, value in fields:
        lines.append("  %s = {%s}," % (name, value))
    if len(lines) > 1:
        lines[-1] = lines[-1].rstrip(",")
    lines.append("}")
    return "\n".join(lines)


_ENTRY_RE = re.compile(r"@(\w+)")


def _extract_bibtex_entries(text: str) -> Iterator[tuple[str, str]]:
    """Yield (entry_type, inner_text) for each balanced @entry{...}."""
    i, n = 0, len(text)
    while True:
        at = text.find("@", i)
        if at == -1:
            return
        m = _ENTRY_RE.match(text, at)
        if not m:
            i = at + 1
            continue
        j = m.end()
        while j < n and text[j].isspace():
            j += 1
        if j >= n or text[j] not in "{(":
            i = j
            continue
        open_c = text[j]
        close_c = "}" if open_c == "{" else ")"
        depth, k = 0, j
        in_quote, escaped = False, False
        while k < n:
            ch = text[k]
            if in_quote:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_quote = False
            elif ch == '"':
                in_quote = True
            elif ch == open_c:
                depth += 1
            elif ch == close_c:
                depth -= 1
                if depth == 0:
                    break
            k += 1
        if depth != 0:  # unbalanced -> skip this entry, keep scanning
            i = j + 1
            continue
        yield m.group(1).lower(), text[j + 1:k]
        i = k + 1


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    parts, depth, cur = [], 0, []
    in_quote, escaped = False, False
    for ch in text:
        if in_quote:
            cur.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_quote = False
        elif ch == '"':
            in_quote = True
            cur.append(ch)
        elif ch == "{":
            depth += 1
            cur.append(ch)
        elif ch == "}":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _clean_bibtex_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value.startswith("{") and value.endswith("}"):
        value = value[1:-1]
    elif len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1].replace('\\"', '"')
    return _bibtex_unescape(value).strip()


def _parse_bibtex_entry(
    entry_type: str,
    inner: str,
) -> dict[str, Any] | None:
    chunks = _split_top_level(inner)
    if not chunks:
        return None
    fields = {}
    for chunk in chunks[1:]:  # chunk[0] is the citation key
        if "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name = name.strip().lower()
        if not name:
            continue
        fields[name] = _clean_bibtex_value(value)

    pubtype = {"article": "article",
               "inproceedings": "inproceedings",
               "conference": "inproceedings"}.get(entry_type, "misc")

    meta = {"pubtype": pubtype}
    if fields.get("title"):
        meta["title"] = fields["title"]
    if fields.get("author"):
        authors = [a.strip() for a in
                   re.split(r"\s+and\s+", fields["author"]) if a.strip()]
        if authors:
            meta["authors"] = authors
    venue = fields.get("journal") or fields.get("booktitle")
    if venue:
        meta["venue"] = venue
    for src, dst in (("volume", "volume"), ("number", "issue"),
                     ("pages", "pages"), ("year", "year"),
                     ("doi", "doi"), ("url", "url"),
                     ("eprint", "arxiv_id")):
        if fields.get(src):
            meta[dst] = fields[src]
    if not any(k in meta for k in ("title", "authors", "venue")):
        return None
    return meta


def from_bibtex(text: str | None) -> list[dict[str, Any]]:
    """Parse BibTeX source into a list of metadata dicts.

    Handles @article/@inproceedings/@conference/@misc/@book (others map to
    "misc"), braced and quoted values, "and"-separated authors. Garbage or
    malformed input yields [] — never raises.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    results = []
    try:
        entries = list(_extract_bibtex_entries(text))
    except Exception:
        return []
    for entry_type, inner in entries:
        try:
            meta = _parse_bibtex_entry(entry_type, inner)
        except Exception:
            continue
        if meta:
            results.append(meta)
    return results


# ---------------------------------------------------------------------------
# Style guidance for the UI
# ---------------------------------------------------------------------------

_GUIDES = {
    "ieee": {
        "in_text": "Numbered citations in square brackets, in order of first "
                   "appearance: [1], [2]. The same source keeps its original "
                   "number every time it is cited.",
        "reference_rules": [
            "Authors as initials + surname: A. B. Smith. List up to 6 "
            "authors; 7 or more -> first author + et al.",
            "Article title in quotation marks, sentence case: only the "
            "first word capitalised.",
            "Journal article: \"Title,\" *Journal*, vol. x, no. y, "
            "pp. xxx-xxx, Year.",
            "Conference paper: \"Title,\" in *Proc. Conference*, City, "
            "Year, pp. xxx-xxx.",
            "DOI goes last as doi: 10.xxxx/yyyy (no URL form).",
            "Reference list is numbered [1], [2], ... in citation order, "
            "not alphabetical.",
        ],
        "notes": "Engineering and computer-science standard. In-text, use "
                 "et al. for 3+ authors (Smith et al. [4]). Months are "
                 "abbreviated (Jan., Feb., Mar.) when given.",
    },
    "apa": {
        "in_text": "Author-date: (Smith, 2020). Two authors: (Smith & "
                   "Jones, 2020) — ampersand in parentheses, 'and' in "
                   "narrative text. Three or more: (Smith et al., 2020) "
                   "from the first citation.",
        "reference_rules": [
            "Authors as Surname, Initials: Smith, J. A. Use & before the "
            "last author.",
            "List up to 20 authors; 21+ -> first 19, ellipsis, then the "
            "final author (no ampersand).",
            "Article title in sentence case, not italicised, not in "
            "quotes.",
            "Journal name in title case and italics; volume italicised, "
            "issue in parentheses, not italicised: *15*(3).",
            "DOI as a full URL with no trailing period: "
            "https://doi.org/10.xxxx/yyyy.",
            "No year -> (n.d.). Reference list is alphabetical with a "
            "hanging indent.",
        ],
        "notes": "Social-sciences standard (APA Publication Manual, 7th "
                 "ed.). Preprints: add [Preprint] after the title and cite "
                 "the arXiv DOI https://doi.org/10.48550/arXiv.1706.03762.",
    },
    "mla": {
        "in_text": "Author surname + page, no comma, no year: (Smith 42). "
                   "Two authors: (Smith and Jones 42). Three or more: "
                   "(Smith et al. 42).",
        "reference_rules": [
            "First author inverted (Smith, John); later authors in "
            "normal order. 3+ authors -> first author + et al.",
            "Article title in quotation marks, headline (title) case.",
            "The venue is a 'container' and is italicised: the journal "
            "an article appears in, the site hosting a page.",
            "Journal article: \"Title.\" *Journal*, vol. x, no. y, Year, "
            "pp. xxx-xxx. DOI or URL last (DOI preferred).",
            "List is titled 'Works Cited', alphabetical by surname (or "
            "title when there is no author).",
        ],
        "notes": "Humanities standard (MLA Handbook, 9th ed.). The year "
                 "never appears in the in-text citation. Months are "
                 "abbreviated (Jan., Feb.) in web sources.",
    },
    "vancouver": {
        "in_text": "Arabic numerals for each source in order of first "
                   "appearance: (1) or [1] depending on the journal. The "
                   "number is re-used, never renumbered.",
        "reference_rules": [
            "Authors as surname + initials, no periods or commas: Smith "
            "JA. List 1-6 authors; 7+ -> first 6 + et al.",
            "Article title in sentence case, no quotes, no italics.",
            "Journal titles abbreviated per the NLM Catalog (Index "
            "Medicus), roman type.",
            "Journal article: Title. *J Abbrev.* Year;Volume(Issue):"
            "Pages — no spaces, e.g. 2002;347(4):284-7.",
            "Reference list follows citation order, not alphabetical.",
        ],
        "notes": "Biomedical standard (ICMJE / NLM). Some journals list "
                 "all authors instead of et al. — check the target "
                 "journal's instructions. Preprints: add [Preprint] "
                 "after the title and 'Available from:' + URL.",
    },
    "chicago": {
        "in_text": "Author-date: (Smith 2020); with a page: (Smith 2020, "
                   "42). Two authors: (Smith and Jones 2020). Four or "
                   "more: (Smith et al. 2020).",
        "reference_rules": [
            "Only the first author is inverted: Smith, John, and Jane "
            "Jones. Year comes right after the authors.",
            "List up to 10 authors; 11+ -> first 7 + et al.",
            "Article title in quotation marks, headline (title) case; "
            "journal title italicised, title case.",
            "Journal article: *Journal* Volume (Issue): Pages — e.g. "
            "*Journal* 15 (3): 45-67.",
            "DOI as a full URL: https://doi.org/10.xxxx/yyyy. Reference "
            "list is alphabetical.",
        ],
        "notes": "Chicago Manual of Style, 17th ed., author-date system "
                 "(used in sciences and social sciences). The other "
                 "Chicago system — notes and bibliography with footnotes "
                 "— is a different style and is not covered here.",
    },
}


def style_guide(style: str) -> dict[str, Any]:
    """Return usage guidance for a style.

    {"in_text": str, "reference_rules": [str, ...], "notes": str}.
    Raises ValueError for an unknown style name.
    """
    style = _norm_style(style)
    guide = _GUIDES[style]
    return {"in_text": guide["in_text"],
            "reference_rules": list(guide["reference_rules"]),
            "notes": guide["notes"]}


# ================= Feature 20 Ultimate Enhancements =================
# 20.1 In-text citation inserter | 20.2 Journal style matcher | 20.3 Duplicate detector

import re as _re14


def _clean14(text: str | None) -> str:
    return _re14.sub(r"<[^>]*>", "", text or "")


def _intext_bibtex_key(meta: dict[str, Any] | None) -> str:
    authors = _authors(meta)
    first = _split_name(authors[0])[1] if authors else "unknown"
    year = _str(meta, "year") or "nodate"
    title_word = (_str(meta, "title") or "untitled").split()
    tw = _re14.sub(r"\W", "", title_word[0]).lower() if title_word else "untitled"
    # NOTE: the regex sub is hoisted out of the f-string because Python < 3.12
    # forbids backslashes inside f-string expression parts (Render runs 3.10).
    first_clean = _re14.sub(r"\W", "", first).lower()
    key = f"{first_clean}{year}{tw}"[:40]
    return key or "ref"


def in_text_citation(
    meta: dict[str, Any],
    style: str = "apa",
) -> dict[str, Any]:
    """Copy-paste-ready in-text citation snippets.

    Returns {bibtex_key, snippets: {latex, apa, mla, ieee, numeric}}.
    meta: {authors (list), year, title}. Never raises on hostile input.
    """
    if not isinstance(meta, dict):
        raise ValueError("meta must be a dict.")
    style = _norm_style(style) if isinstance(style, str) else "apa"
    authors = _authors(meta)
    year = _clean14(_str(meta, "year") or "n.d.")
    surnames = [_split_name(a)[1] or a for a in authors]
    surnames = [s for s in surnames if s][:3]
    key = _intext_bibtex_key(meta)

    if len(surnames) >= 3:
        apa_auth = f"{surnames[0]} et al."
    elif len(surnames) == 2:
        apa_auth = f"{surnames[0]} & {surnames[1]}"
    elif surnames:
        apa_auth = surnames[0]
    else:
        apa_auth = "Unknown"
    snippets = {
        "latex": f"\\cite{{{key}}}",
        "apa": f"({apa_auth}, {year})",
        "mla": f"({surnames[0] if surnames else 'Unknown'} {year})" if surnames else f"({year})",
        "ieee": "[1]  % replace with your reference number",
        "numeric": "[1]  % replace with your reference number",
        "narrative": f"{apa_auth} ({year})",
    }
    return {"bibtex_key": key, "snippets": snippets,
            "default_for_style": snippets.get(style, snippets["apa"]),
            "tip": "Paste the snippet, then add the full reference to your bibliography."}


# Heuristic fingerprints for common journal styles.
_STYLE_FINGERPRINTS = [
    ("apa", [_re14.compile(r"\(\d{4}\)"), _re14.compile(r"&"),
             _re14.compile(r"https?://|doi:", _re14.I)],
     "APA: (Year), & between authors, DOI at end."),
    ("mla", [_re14.compile(r'"[^"]+"'), _re14.compile(r"\d{4},\s*pp\.")],
     "MLA: \"Quoted Title,\" pp. pages."),
    ("ieee", [_re14.compile(r"\[\d+\]"), _re14.compile(r"pp\.\s*\d+\s*-\s*\d+"),
             _re14.compile(r"\bin\b\s+(Proc\.|IEEE|Trans\.)", _re14.I)],
     "IEEE: [1] numbered, in Proc./Trans., pp. xx-xx."),
    ("chicago", [_re14.compile(r"\d{4}\."), _re14.compile(r"University Press|Press,")],
     "Chicago: Year. Publisher location: Press."),
    ("harvard", [_re14.compile(r"\d{4}[a-z]?\s+[A-Z]"), _re14.compile(r"Available at:")],
     "Harvard: Year + Available at: URL."),
    ("vancouver", [_re14.compile(r"\d+\.\s+[A-Z][a-z]+ [A-Z]"),
                   _re14.compile(r";\d+(\(\d+\))?:\d+-\d+")],
     "Vancouver: numbered, journalabbrev;vol:pages."),
]


def match_journal_style(sample_citation: str | None) -> dict[str, Any]:
    """Guess which citation style a sample reference follows.

    Returns {guesses: [{style, confidence, why}], best}.
    """
    sample = _clean14(sample_citation or "")
    if len(sample.strip()) < 20:
        raise ValueError("Sample citation is too short to analyze.")
    scored = []
    for style, patterns, why in _STYLE_FINGERPRINTS:
        hits = sum(1 for p in patterns if p.search(sample))
        if hits:
            scored.append({"style": style, "hits": hits,
                           "confidence": round(hits / len(patterns) * 100),
                           "why": why})
    scored.sort(key=lambda x: (-x["hits"], -x["confidence"]))
    if not scored:
        return {"guesses": [], "best": None,
                "verdict": "No clear style fingerprint — check the journal's author guidelines."}
    return {"guesses": scored[:3], "best": scored[0]["style"],
            "verdict": f"Closest match: {scored[0]['style'].upper()} ({scored[0]['why']})"}


def find_duplicates(
    references: list[Any],
    threshold: float = 0.85,
) -> dict[str, Any]:
    """Flag near-duplicate references in a bibliography.

    references: list of formatted reference strings (or dicts with title).
    Returns {duplicates: [{a_index, b_index, similarity, a, b}], count}.
    """
    if not isinstance(references, (list, tuple)):
        raise ValueError("references must be a list.")
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = 0.85
    threshold = max(0.5, min(1.0, threshold))

    def _norm(ref: dict[str, Any] | str | None) -> set[str]:
        if isinstance(ref, dict):
            text = str(ref.get("title") or "") + " " + str(ref.get("doi") or "")
        else:
            text = str(ref or "")
        text = _clean14(text).lower()
        return set(_re14.findall(r"[a-z0-9]{3,}", text))

    normed = [_norm(r) for r in references]
    dupes = []
    for i in range(len(normed)):
        for j in range(i + 1, len(normed)):
            a, b = normed[i], normed[j]
            if not a or not b:
                continue
            # DOI exact match = definite duplicate.
            doi_a = _re14.search(r"10\.\d{4,}/[^\s]+", _clean14(str(references[i] if not isinstance(references[i], dict) else references[i].get("doi") or "")).lower())
            doi_b = _re14.search(r"10\.\d{4,}/[^\s]+", _clean14(str(references[j] if not isinstance(references[j], dict) else references[j].get("doi") or "")).lower())
            if doi_a and doi_b and doi_a.group(0) == doi_b.group(0):
                sim = 1.0
            else:
                inter = len(a & b)
                union = len(a | b)
                sim = inter / union if union else 0
            if sim >= threshold:
                dupes.append({
                    "a_index": i, "b_index": j,
                    "similarity": round(sim, 2),
                    "a": _clean14(str(references[i]))[:100],
                    "b": _clean14(str(references[j]))[:100],
                })
    return {"duplicates": dupes, "count": len(dupes),
            "verdict": (f"{len(dupes)} duplicate pair(s) found — merge them."
                        if dupes else "No duplicates detected.")}
