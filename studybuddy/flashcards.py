"""Flashcards & paper-quiz generation (study feature: flashcards, Wave 4 #26).

Pure-logic module — no Flask, no network, no LLM, no numpy/sklearn. It turns
a StudyDoc's extracted chunks into three kinds of deterministic study cards:

- ``qa``    — definition-style cards ("What is X?" -> the full source
              sentence as the answer).
- ``cloze`` — fill-in-the-blank: a key sentence with a top key term blanked
              out as "_____".
- ``mcq``   — multiple-choice: the same blanked sentence as the stem, the
              key term as the answer, and 3 distractors drawn from the SAME
              document's terminology.

Design guarantees (the user's 1000%-quality bar):

- **Deterministic** — identical inputs always produce identical decks.
  Ties break alphabetically; MCQ option order is shuffled with a seed
  derived from the card id (SHA-256, never Python's salted ``hash()``).
- **Traceable** — every card carries ``chunk`` (the chunk's own index),
  ``page``, and ``doc_title``. The card's answer (or blanked term) is always
  a substring of the referenced chunk's text, so provenance is verifiable by
  substring check.
- **Honest** — distractors are real terms from the same document that do NOT
  appear in the stem sentence and are not substrings of the answer (or of
  each other), so exactly one option is defensible from the source sentence.
- **Defensive** — no public function ever raises. Bad input (``None`` docs,
  empty chunks, garbage answers) yields an empty deck / empty grade instead
  of an exception.

Data-shape contracts (these mirror the SQLAlchemy models in unified_app.py —
do not drift from them):

    FlashcardSet.data =
        {"cards": [ {"id": "qa-0", "kind": "qa"|"cloze"|"mcq",
                     "question": str, "answer": str,
                     "options": [str x4] (mcq) | [] (qa/cloze),
                     "correct_index": int (mcq only),
                     "chunk": int, "page": int, "doc_title": str}, ... ],
         "progress": {card_id: {"known": bool, "reviews": int}}}

    PaperQuizAttempt.data =
        {"answers": {card_id: chosen_index},
         "details": [ {"card_id": str, "correct": bool,
                       "chosen": str, "answer": str}, ... ],
         "score": int, "total": int}

Public surface (exactly these seven functions):

    key_terms(chunks, top_n=40) -> List[(term, score)]
    make_qa_cards(chunks_with_meta, max_n=12) -> List[card]
    make_cloze_cards(chunks_with_meta, terms, max_n=12) -> List[card]
    make_mcq_cards(chunks_with_meta, terms, max_n=12) -> List[card]
    generate_deck(doc, max_per_kind=10) -> FlashcardSet.data dict
    grade_mcq(cards, answers) -> PaperQuizAttempt.data dict
    mastery_stats(set_data) -> {"total","reviewed","known","unknown","mastery_pct"}

``chunks_with_meta`` is a list of ``(chunk, doc_title)`` tuples (what
:func:`generate_deck` builds); plain chunk dicts are also accepted, in which
case ``doc_title`` falls back to ``chunk.get("doc_title", "")``. Chunk dicts
have the pdf_qa shape: ``{"page": int, "index": int, "text": str,
"heading": str|None}``.
"""

from __future__ import annotations

import hashlib
import math
import random
import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Default cap on extracted key terms per document.
DEFAULT_TOP_TERMS = 40

#: Default cap on cards per kind.
DEFAULT_MAX_PER_KIND = 10

#: Sentence length bounds (chars) for card-worthy sentences.
MIN_SENTENCE_LEN = 25
MAX_QA_SENTENCE_LEN = 500
MAX_BLANK_SENTENCE_LEN = 400
MAX_MCQ_SENTENCE_LEN = 350

#: The blank marker used in cloze stems and MCQ stems.
BLANK = "_____"

# ---------------------------------------------------------------------------
# Stoplist (~150 words): function words + generic academic filler that must
# never become key terms on their own. Multi-word phrases may still contain
# these words only if every component word is a content word (enforced in
# _phrase_ok), so "the process" can never be a term but "light energy" can.
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset(
    """
    a an the and or but if then else when at by for with about into through
    during before after above below to from up down in out on off over under
    again further once here there all any both each few more most other some
    such no nor not only own same so than too very can will just should shall
    now is are was were be been being have has had having do does did doing
    would could ought may might must i me my we our you your he him his she
    her it its they them their this that these those am of as per via within
    without between among along across behind beyond plus except until while
    whereas although though however therefore thus hence furthermore moreover
    nevertheless meanwhile accordingly instead otherwise also well even still
    already yet ever never often usually generally mainly mostly largely
    particularly especially specifically relatively extremely highly widely
    closely directly clearly simply basically essentially virtually nearly
    almost quite rather somewhat enough indeed let etc eg ie vs versus
    one two three first second third new novel proposed present previous
    related recent current future following paper papers study studies
    research result results finding findings conclusion conclusions
    introduction abstract method methods methodology approach approaches
    section sections chapter figure fig figs table tables data dataset
    example examples work works based using used use uses show shows shown
    demonstrate demonstrates demonstrated provide provides provided include
    includes including propose proposes perform obtain obtained achieve
    achieved consider describe described discuss discussed known called
    termed defined referred make makes made take takes give gives get form
    part way kind type types number amount level levels case cases set order
    system process processes model models framework feature features term
    terms
    """.split()
)

#: Case-preserving word tokens (kept cased so surface forms like "DNA" survive).
_WORD_RE = re.compile(r"[A-Za-z0-9]+")

#: Sentence splitter (same shape as pdf_qa's: keeps trailing punctuation).
_SENT_RE = re.compile(r"[^.!?]+[.!?]+|\S[^.!?]*$")

#: Definition patterns, tried in this order. Groups: x = the defined term.
_DEF_DEFINED_RE = re.compile(
    r"^(?P<x>.+?)\s+is\s+defined\s+as\s+(?P<y>.+?)[.!?]*$", re.IGNORECASE
)
_DEF_REFER_RE = re.compile(
    r"^(?P<x>.+?)\s+refers?\s+to\s+(?P<y>.+?)[.!?]*$", re.IGNORECASE
)
_DEF_IS_RE = re.compile(
    r"^(?P<x>.+?)\s+(?:is|are|was|were)\s+(?P<y>.+?)[.!?]*$", re.IGNORECASE
)

#: Subjects that can never be a quiz term (pronouns / demonstratives).
_PRONOUN_STARTS = frozenset(
    "this that these those it they he she we you there here which who what "
    "one ones such there".split()
)

#: Leading articles stripped from the defined term for a clean question.
_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)

#: Page hint stripped when normalizing questions for near-duplicate detection.
_PAGE_HINT_RE = re.compile(r"\(from p\. \d+\)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Small helpers (all defensive)
# ---------------------------------------------------------------------------

def _sentences(text: str) -> list[str]:
    """Split text into stripped, non-empty sentences."""
    if not isinstance(text, str):
        return []
    return [s.strip() for s in _SENT_RE.findall(text) if s.strip()]


def _unpack(
    chunks_with_meta: Iterable[tuple[dict[str, Any], str] | dict[str, Any]] | None,
) -> list[tuple[dict, str, int]]:
    """Normalize ``chunks_with_meta`` to (chunk, doc_title, position) triples.

    Accepts ``(chunk, doc_title)`` tuples (preferred) or bare chunk dicts.
    Anything else is skipped silently.
    """
    out: list[tuple[dict, str, int]] = []
    try:
        items = list(chunks_with_meta or [])
    except TypeError:
        return []
    for pos, item in enumerate(items):
        chunk = None
        title = ""
        if isinstance(item, (tuple, list)) and len(item) == 2 and isinstance(item[0], dict):
            chunk, title = item[0], item[1]
        elif isinstance(item, dict):
            chunk = item
            title = chunk.get("doc_title", "")
        if chunk is None:
            continue
        out.append((chunk, str(title or ""), pos))
    return out


def _chunk_index(chunk: dict, fallback: int) -> int:
    try:
        return int(chunk.get("index", fallback))
    except (TypeError, ValueError):
        return fallback


def _chunk_page(chunk: dict) -> int:
    try:
        return int(chunk.get("page", 0))
    except (TypeError, ValueError):
        return 0


def _chunk_text(chunk: dict) -> str:
    text = chunk.get("text", "")
    return text if isinstance(text, str) else ""


def _assign_ids(cards: list[dict], kind: str) -> None:
    """Give cards deterministic ``"<kind>-N"`` ids in generation order."""
    for n, card in enumerate(cards):
        card["id"] = "%s-%d" % (kind, n)


def _qkey(question: str | None) -> str:
    """Normalized question for near-duplicate detection."""
    q = str(question or "").lower().replace(BLANK, " ")
    q = _PAGE_HINT_RE.sub(" ", q)
    return re.sub(r"[^a-z0-9]+", " ", q).strip()


def _valid_index(value: Any, n: int) -> int | None:
    """Return ``value`` as an int index into ``range(n)``, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value < n:
        return value
    return None


# ---------------------------------------------------------------------------
# 1. Key-term extraction: TF-IDF over chunks, multi-word phrases preferred
# ---------------------------------------------------------------------------

def _phrase_ok(words: list[str]) -> bool:
    """A candidate phrase is 1-3 content words, none a stopword/digit."""
    if not (1 <= len(words) <= 3):
        return False
    if len(words) > 1 and len(set(w.lower() for w in words)) == 1:
        return False  # "cell cell" is not a term
    for w in words:
        if len(w) < 2 or w.isdigit():
            return False
        if w.lower() in _STOPWORDS:
            return False
    return True


def _unigram_qualified(norm: str, tf: int, surfaces: Counter) -> bool:
    """Single words need slightly stronger evidence than phrases: seen at
    least twice overall, or capitalized in at least two occurrences (proper
    nouns / acronyms like "DNA")."""
    if tf >= 2:
        return True
    capitalized = sum(c for s, c in surfaces.items() if s[:1].isupper())
    return capitalized >= 2


def key_terms(
    chunks: Iterable[dict[str, Any]] | None,
    top_n: int = DEFAULT_TOP_TERMS,
) -> list[tuple[str, float]]:
    """Extract the document's key terms with TF-IDF scores.

    Candidate terms are uni/bi/trigrams of content words (nothing from the
    ~150-word stoplist, nothing shorter than 2 chars, no pure digits).
    Multi-word phrases score double so terminology like "light energy"
    outranks lone frequent words; single words additionally need either
    3+ occurrences or 2+ capitalized occurrences to qualify. The returned
    term string is the most frequent surface form ("DNA", not "dna").

    Scores are ``tf * idf * phrase_boost`` with smoothed
    ``idf = log((N+1)/(df+1)) + 1``. Ordering is deterministic:
    score descending, then term ascending. Never raises.
    """
    try:
        return _key_terms(chunks, top_n)
    except Exception:
        return []


def _key_terms(
    chunks: Iterable[dict[str, Any]] | None,
    top_n: int,
) -> list[tuple[str, float]]:
    try:
        top_n = int(top_n)
    except (TypeError, ValueError):
        top_n = DEFAULT_TOP_TERMS
    if top_n <= 0:
        return []
    try:
        chunk_list = list(chunks or [])
    except TypeError:
        return []
    texts = [
        _chunk_text(c) for c in chunk_list if isinstance(c, dict)
    ]
    texts = [t for t in texts if t.strip()]
    n_docs = len(texts)
    if n_docs == 0:
        return []

    tf: Counter = Counter()
    df: Counter = Counter()
    surfaces: dict[str, Counter] = {}
    for text in texts:
        # N-grams never cross sentence boundaries: "of the cell. Cellular
        # respiration" must not yield the phantom phrase "cell Cellular".
        seen = set()
        for sent in _sentences(text):
            words = _WORD_RE.findall(sent)
            for n in (1, 2, 3):
                for i in range(len(words) - n + 1):
                    grp = words[i:i + n]
                    if not _phrase_ok(grp):
                        continue
                    norm = " ".join(w.lower() for w in grp)
                    tf[norm] += 1
                    seen.add(norm)
                    surfaces.setdefault(norm, Counter())[" ".join(grp)] += 1
        for norm in seen:
            df[norm] += 1

    scored: list[tuple[str, float]] = []
    for norm, count in tf.items():
        n_words = norm.count(" ") + 1
        if n_words == 1 and not _unigram_qualified(norm, count, surfaces[norm]):
            continue
        idf = math.log((n_docs + 1) / (df[norm] + 1)) + 1.0
        boost = 2.0 if n_words > 1 else 1.0
        scored.append((norm, count * idf * boost))

    # Deterministic: score desc, term asc. (dict/Counter iteration order is
    # insertion order, but the explicit tie-break makes this fully stable.)
    scored.sort(key=lambda item: (-item[1], item[0]))

    out = []
    for norm, score in scored[:top_n]:
        surface = surfaces[norm].most_common(1)[0][0]
        out.append((surface, round(score, 4)))
    return out


# ---------------------------------------------------------------------------
# 2. Definition (QA) cards
# ---------------------------------------------------------------------------

def _clean_defined_term(raw_x: str) -> str | None:
    """Validate/clean the subject of a definition sentence; None if unusable."""
    x = (raw_x or "").strip().strip(",;:")
    x = _LEADING_ARTICLE_RE.sub("", x).strip()
    words = x.split()
    if not (1 <= len(words) <= 7):
        return None
    if not (2 <= len(x) <= 80):
        return None
    if not re.search(r"[A-Za-z]", x):
        return None
    first = words[0].lower()
    if first in _PRONOUN_STARTS or first in _STOPWORDS:
        return None
    return x


def _match_definition(sentence: str) -> tuple[str, str] | None:
    """Return ("is", X) / ("refer", X) for definition sentences, else None."""
    s = (sentence or "").strip()
    for pattern, kind in (
        (_DEF_DEFINED_RE, "is"),
        (_DEF_REFER_RE, "refer"),
        (_DEF_IS_RE, "is"),
    ):
        m = pattern.match(s)
        if not m:
            continue
        x = _clean_defined_term(m.group("x"))
        y = (m.group("y") or "").strip()
        if x and len(y) >= 3:
            return kind, x
    return None


def make_qa_cards(
    chunks_with_meta: Iterable[tuple[dict[str, Any], str] | dict[str, Any]] | None,
    max_n: int = 12,
) -> list[dict]:
    """Build definition-style QA cards from definition sentences.

    Matches "X is/are/was/were Y", "X refers to Y", "X is defined as Y".
    Question is "What is X?" (or "What does X refer to?"); the answer is the
    full source sentence, so every card is traceable to its chunk. Cards are
    generated in chunk order; duplicate defined terms are skipped. Never
    raises.
    """
    try:
        return _make_qa_cards(chunks_with_meta, max_n)
    except Exception:
        return []


def _make_qa_cards(
    chunks_with_meta: Iterable[tuple[dict[str, Any], str] | dict[str, Any]] | None,
    max_n: int,
) -> list[dict]:
    try:
        max_n = int(max_n)
    except (TypeError, ValueError):
        max_n = 12
    max_n = max(0, max_n)
    cards: list[dict] = []
    seen_terms = set()
    for chunk, title, pos in _unpack(chunks_with_meta):
        text = _chunk_text(chunk)
        page = _chunk_page(chunk)
        cidx = _chunk_index(chunk, pos)
        for sent in _sentences(text):
            if len(cards) >= max_n:
                break
            if not (MIN_SENTENCE_LEN <= len(sent) <= MAX_QA_SENTENCE_LEN):
                continue
            hit = _match_definition(sent)
            if not hit:
                continue
            kind, x = hit
            key = re.sub(r"\W+", "", x.lower())
            if not key or key in seen_terms:
                continue
            seen_terms.add(key)
            question = (
                "What does %s refer to?" % x if kind == "refer"
                else "What is %s?" % x
            )
            cards.append({
                "id": None,
                "kind": "qa",
                "question": question,
                "answer": sent,
                "options": [],
                "chunk": cidx,
                "page": page,
                "doc_title": title,
            })
        if len(cards) >= max_n:
            break
    _assign_ids(cards, "qa")
    return cards


# ---------------------------------------------------------------------------
# 3. Cloze (fill-in-the-blank) cards
# ---------------------------------------------------------------------------

def _usable_terms(
    terms: Iterable[tuple[str, float] | str] | str | None,
) -> list[str]:
    """Normalize the ``terms`` arg to a deduped list of usable term strings.

    Accepts ``key_terms`` output ``[(term, score), ...]`` or plain strings.
    Only terms with ``len >= 4`` are usable for blanking.
    """
    if isinstance(terms, str):
        terms = [terms]
    try:
        items = list(terms or [])
    except TypeError:
        return []
    out: list[str] = []
    seen = set()
    for t in items:
        if isinstance(t, (tuple, list)) and t:
            t = t[0]
        if not isinstance(t, str):
            continue
        term = t.strip()
        if len(term) < 4:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(term)
    return out


def _blank_term(
    sentence: str,
    term: str,
    other_terms: Iterable[str] = (),
) -> str | None:
    """Blank the FIRST whole-word occurrence of ``term``; None if absent.

    Word boundaries (``\\b``) guarantee the term is never blanked as a
    substring of a longer word ("cell" never matches "cellular"). Matching is
    case-insensitive; the original casing of the rest of the sentence is
    preserved. When the matched occurrence sits strictly inside a LONGER
    term's match with phrase words to its LEFT (e.g. "energy" inside "light
    energy" — blanking would leave the dangling fragment "light _____"),
    the sentence is rejected (None) so the blank always covers the most
    specific phrase available. A longer phrase extending only to the right
    ("Cellular respiration" inside "Cellular respiration refers") is fine:
    "_____ refers to ..." is still a clean card.
    """
    try:
        pattern = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
    except re.error:
        return None
    m = pattern.search(sentence or "")
    if not m:
        return None
    for other in other_terms or ():
        if not isinstance(other, str) or len(other) <= len(term):
            continue
        try:
            op = re.compile(r"\b" + re.escape(other) + r"\b", re.IGNORECASE)
        except re.error:
            continue
        for om in op.finditer(sentence):
            if om.start() < m.end() and m.start() < om.end() \
                    and om.start() < m.start():
                return None  # a longer phrase owns the words before the blank
    return sentence[:m.start()] + BLANK + sentence[m.end():]


def _first_placement(
    items: list[tuple[dict, str, int]],
    usable: list[str],
    term: str,
    used: set[tuple[int, int]],
) -> tuple[int, int, str, dict, str, int, int, str] | None:
    """First free sentence containing ``term`` as a blankable whole word.

    Returns ``(pos, si, sent, chunk, title, page, cidx, blanked)`` or None.
    ``used`` holds ``(chunk_pos, sent_idx)`` pairs already claimed.
    """
    for pos, (chunk, title, _) in enumerate(items):
        text = _chunk_text(chunk)
        page = _chunk_page(chunk)
        cidx = _chunk_index(chunk, pos)
        for si, sent in enumerate(_sentences(text)):
            if (pos, si) in used:
                continue
            if not (MIN_SENTENCE_LEN <= len(sent) <= MAX_MCQ_SENTENCE_LEN):
                continue
            blanked = _blank_term(sent, term, usable)
            if blanked is None:
                continue
            return (pos, si, sent, chunk, title, page, cidx, blanked)
    return None


def make_cloze_cards(
    chunks_with_meta: Iterable[tuple[dict[str, Any], str] | dict[str, Any]] | None,
    terms: Iterable[tuple[str, float] | str] | str | None,
    max_n: int = 12,
) -> list[dict]:
    """Build fill-in-the-blank cards from sentences containing top key terms.

    For each usable term (in term order), the first sentence containing it as
    a whole word gets its first occurrence blanked. The question is the
    blanked sentence plus a "(from p. N)" hint; the answer is the term. Each
    source sentence is used at most once. Never raises.
    """
    try:
        cards, _ = _make_cloze_cards(chunks_with_meta, terms, max_n)
        return cards
    except Exception:
        return []


def _make_cloze_cards(
    chunks_with_meta: Iterable[tuple[dict[str, Any], str] | dict[str, Any]] | None,
    terms: Iterable[tuple[str, float] | str] | str | None,
    max_n: int,
    skip: Iterable[tuple[int, int]] = frozenset(),
) -> tuple[list[dict], set[tuple[int, int]]]:
    """Inner cloze builder. Also returns the used (chunk_pos, sent_idx) set
    so a caller can keep a second card kind off the same sentences."""
    try:
        max_n = int(max_n)
    except (TypeError, ValueError):
        max_n = 12
    max_n = max(0, max_n)
    usable = _usable_terms(terms)
    items = _unpack(chunks_with_meta)
    try:
        skip = set(skip or ())
    except TypeError:
        skip = set()
    cards: list[dict] = []
    used_sentences = set()
    for term in usable:
        if len(cards) >= max_n:
            break
        placed = False
        for pos, (chunk, title, _) in enumerate(items):
            if placed:
                break
            text = _chunk_text(chunk)
            page = _chunk_page(chunk)
            cidx = _chunk_index(chunk, pos)
            for si, sent in enumerate(_sentences(text)):
                if (pos, si) in used_sentences or (pos, si) in skip:
                    continue
                if not (MIN_SENTENCE_LEN <= len(sent) <= MAX_BLANK_SENTENCE_LEN):
                    continue
                blanked = _blank_term(sent, term, usable)
                if blanked is None:
                    continue
                cards.append({
                    "id": None,
                    "kind": "cloze",
                    "question": "%s (from p. %d)" % (blanked, page),
                    "answer": term,
                    "options": [],
                    "chunk": cidx,
                    "page": page,
                    "doc_title": title,
                })
                used_sentences.add((pos, si))
                placed = True
                break
    _assign_ids(cards, "cloze")
    return cards, used_sentences


# ---------------------------------------------------------------------------
# 4. MCQ cards (paper quiz)
# ---------------------------------------------------------------------------

def _pick_distractors(answer: str, stem: str, terms: list[str]) -> list[str]:
    """Pick 3 distractors from the doc's own terms.

    A distractor must: (a) differ from the answer, (b) not occur in the stem
    sentence, (c) not be a substring of the answer nor contain the answer as
    a substring, and (d) not be a substring of / contain another chosen
    distractor. First-come in term order — fully deterministic.
    """
    ans = answer.lower()
    stem_l = stem.lower()
    picked: list[str] = []
    for t in terms:
        if len(picked) >= 3:
            break
        tl = t.lower()
        if tl == ans:
            continue
        if any(tl == p.lower() for p in picked):
            continue
        if tl in stem_l:
            continue
        if tl in ans or ans in tl:
            continue
        if any(tl in p.lower() or p.lower() in tl for p in picked):
            continue
        picked.append(t)
    return picked


def _shuffle_seed(card_id: str) -> int:
    """Stable integer seed from a card id (SHA-256, not salted hash())."""
    digest = hashlib.sha256(str(card_id).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def make_mcq_cards(
    chunks_with_meta: Iterable[tuple[dict[str, Any], str] | dict[str, Any]] | None,
    terms: Iterable[tuple[str, float] | str] | str | None,
    max_n: int = 12,
) -> list[dict]:
    """Build multiple-choice cards: blanked key sentence + 4 options.

    The stem is a key sentence with the key term blanked; the correct answer
    is the term and the 3 distractors are other top terms from the same
    document (see :func:`_pick_distractors`). Options are shuffled
    deterministically, seeded by the card id, and ``correct_index`` records
    where the answer landed. Cards that cannot get 3 valid distractors are
    skipped rather than emitted half-formed. Never raises.
    """
    try:
        cards, _ = _make_mcq_cards(chunks_with_meta, terms, max_n)
        return cards
    except Exception:
        return []


def _make_mcq_cards(
    chunks_with_meta: Iterable[tuple[dict[str, Any], str] | dict[str, Any]] | None,
    terms: Iterable[tuple[str, float] | str] | str | None,
    max_n: int,
    skip: Iterable[tuple[int, int]] = frozenset(),
) -> tuple[list[dict], set[tuple[int, int]]]:
    """Inner MCQ builder. Also returns the used (chunk_pos, sent_idx) set."""
    try:
        max_n = int(max_n)
    except (TypeError, ValueError):
        max_n = 12
    max_n = max(0, max_n)
    usable = _usable_terms(terms)
    if len(usable) < 4:
        return [], set()
    items = _unpack(chunks_with_meta)
    try:
        skip = set(skip or ())
    except TypeError:
        skip = set()
    cards: list[dict] = []
    used_sentences = set()
    for term in usable:
        if len(cards) >= max_n:
            break
        placed = False
        for pos, (chunk, title, _) in enumerate(items):
            if placed:
                break
            text = _chunk_text(chunk)
            page = _chunk_page(chunk)
            cidx = _chunk_index(chunk, pos)
            for si, sent in enumerate(_sentences(text)):
                if (pos, si) in used_sentences or (pos, si) in skip:
                    continue
                if not (MIN_SENTENCE_LEN <= len(sent) <= MAX_MCQ_SENTENCE_LEN):
                    continue
                stem = _blank_term(sent, term, usable)
                if stem is None:
                    continue
                distractors = _pick_distractors(term, stem, usable)
                if len(distractors) < 3:
                    continue
                card_id = "mcq-%d" % len(cards)
                options = [term] + distractors
                rng = random.Random(_shuffle_seed(card_id))
                rng.shuffle(options)
                cards.append({
                    "id": card_id,
                    "kind": "mcq",
                    "question": stem,
                    "answer": term,
                    "options": list(options),
                    "correct_index": options.index(term),
                    "chunk": cidx,
                    "page": page,
                    "doc_title": title,
                })
                used_sentences.add((pos, si))
                placed = True
                break
    return cards, used_sentences


# ---------------------------------------------------------------------------
# 5. Deck generation from a StudyDoc
# ---------------------------------------------------------------------------

def generate_deck(doc: Any, max_per_kind: int = DEFAULT_MAX_PER_KIND) -> dict:
    """Generate a full flashcard deck from a StudyDoc.

    ``doc`` is duck-typed: it needs ``.data["chunks"]`` (pdf_qa chunk dicts)
    and ``.title``. Builds up to ``max_per_kind`` cards of each kind, then
    dedupes near-identical questions across kinds (qa first, then cloze,
    then mcq) and assigns final deterministic ids (``"qa-0"``,
    ``"cloze-3"``, ``"mcq-1"``, ...).

    Cloze and MCQ terms are dealt out alternating by rank (term 0 -> cloze,
    term 1 -> mcq, ...) sharing one claimed-sentence set, so both kinds
    survive even on tiny documents and never blank the same sentence.

    Returns the FlashcardSet.data shape: ``{"cards": [...], "progress":
    {card_id: {"known": False, "reviews": 0}}}``. A ``None`` doc, missing
    chunks, or a document with nothing extractable yields the empty deck
    ``{"cards": [], "progress": {}}``. Never raises.
    """
    try:
        return _generate_deck(doc, max_per_kind)
    except Exception:
        return {"cards": [], "progress": {}}


def _generate_deck(doc: Any, max_per_kind: int) -> dict:
    try:
        max_per_kind = int(max_per_kind)
    except (TypeError, ValueError):
        max_per_kind = DEFAULT_MAX_PER_KIND
    max_per_kind = max(0, max_per_kind)
    if doc is None or max_per_kind == 0:
        return {"cards": [], "progress": {}}
    data = getattr(doc, "data", None)
    if not isinstance(data, dict):
        return {"cards": [], "progress": {}}
    raw_chunks = data.get("chunks") or []
    try:
        raw_chunks = list(raw_chunks)
    except TypeError:
        return {"cards": [], "progress": {}}
    chunks = [
        c for c in raw_chunks
        if isinstance(c, dict) and isinstance(c.get("text"), str) and c["text"].strip()
    ]
    if not chunks:
        return {"cards": [], "progress": {}}
    title = getattr(doc, "title", "") or ""
    title = str(title)

    meta = [(c, title) for c in chunks]
    terms = key_terms(chunks, top_n=DEFAULT_TOP_TERMS)

    cards: list[dict] = []
    cards.extend(make_qa_cards(meta, max_n=max_per_kind))
    # Cloze and MCQ share one sentence pool: ranked terms are dealt out
    # alternating kinds (term 0 -> cloze, term 1 -> mcq, ...) and both draw
    # from the same claimed-sentence set. Dealing this way (instead of
    # generating one kind fully first) guarantees both kinds survive even
    # on tiny documents, and neither blanks a sentence the other claimed.
    usable = _usable_terms(terms)
    items = _unpack(meta)
    used = set()
    mcq_seq = 0
    cloze_seq = 0
    for ti, term in enumerate(usable):
        if cloze_seq >= max_per_kind and mcq_seq >= max_per_kind:
            break
        want_mcq = (ti % 2 == 1)
        if want_mcq and mcq_seq >= max_per_kind:
            want_mcq = False
        if not want_mcq and cloze_seq >= max_per_kind:
            want_mcq = True
        placement = _first_placement(items, usable, term, used)
        if placement is None:
            continue
        pos, si, _sent, _chunk, ptitle, page, cidx, blanked = placement
        if want_mcq:
            distractors = _pick_distractors(term, blanked, usable)
            if len(distractors) < 3:
                continue
            card_id = "mcq-%d" % mcq_seq
            options = [term] + distractors
            rng = random.Random(_shuffle_seed(card_id))
            rng.shuffle(options)
            cards.append({
                "id": card_id,
                "kind": "mcq",
                "question": blanked,
                "answer": term,
                "options": list(options),
                "correct_index": options.index(term),
                "chunk": cidx,
                "page": page,
                "doc_title": ptitle,
            })
            mcq_seq += 1
        else:
            cards.append({
                "id": None,  # final ids assigned after cross-kind dedupe
                "kind": "cloze",
                "question": "%s (from p. %d)" % (blanked, page),
                "answer": term,
                "options": [],
                "chunk": cidx,
                "page": page,
                "doc_title": ptitle,
            })
            cloze_seq += 1
        used.add((pos, si))

    # Cross-kind near-duplicate detection (a cloze and an mcq built from the
    # same sentence must not both survive).
    seen_questions = set()
    final: list[dict] = []
    counters = {"qa": 0, "cloze": 0, "mcq": 0}
    for card in cards:
        qk = _qkey(card.get("question", ""))
        if not qk or qk in seen_questions:
            continue
        seen_questions.add(qk)
        kind = card.get("kind")
        if kind not in counters:
            continue
        card["id"] = "%s-%d" % (kind, counters[kind])
        counters[kind] += 1
        final.append(card)

    progress = {c["id"]: {"known": False, "reviews": 0} for c in final}
    return {"cards": final, "progress": progress}


# ---------------------------------------------------------------------------
# 6. MCQ grading -> PaperQuizAttempt.data
# ---------------------------------------------------------------------------

def grade_mcq(cards: list[dict] | None, answers: dict) -> dict:
    """Grade an MCQ attempt; returns the PaperQuizAttempt.data shape.

    Only ``kind == "mcq"`` cards are scored. ``answers`` maps card id ->
    chosen option index. Unknown card ids are ignored; non-integer,
    boolean, negative, or out-of-range indices count as wrong. The echoed
    ``"answers"`` map contains only entries for known MCQ card ids.
    Never raises.
    """
    try:
        return _grade_mcq(cards, answers)
    except Exception:
        return {"answers": {}, "details": [], "score": 0, "total": 0}


def _grade_mcq(cards: list[dict] | None, answers: dict | None) -> dict:
    try:
        card_list = list(cards or [])
    except TypeError:
        card_list = []
    mcq_cards = [
        c for c in card_list
        if isinstance(c, dict) and c.get("kind") == "mcq"
    ]
    ans = answers if isinstance(answers, dict) else {}

    echoed: dict = {}
    details: list[dict] = []
    score = 0
    for card in mcq_cards:
        cid = card.get("id")
        options = card.get("options")
        if not isinstance(options, list):
            options = []
        n = len(options)
        correct_idx = _valid_index(card.get("correct_index"), n)
        if correct_idx is None:
            try:
                correct_idx = options.index(card.get("answer"))
            except (ValueError, AttributeError):
                correct_idx = None

        raw = ans.get(cid) if isinstance(cid, str) and cid in ans else None
        if isinstance(cid, str) and cid in ans:
            echoed[cid] = raw
        chosen_idx = _valid_index(raw, n)
        if chosen_idx is not None:
            chosen_text = str(options[chosen_idx])
        elif raw is None:
            chosen_text = ""
        else:
            chosen_text = str(raw)
        correct = (
            chosen_idx is not None
            and correct_idx is not None
            and chosen_idx == correct_idx
        )
        if correct:
            score += 1
        details.append({
            "card_id": cid,
            "correct": bool(correct),
            "chosen": chosen_text,
            "answer": str(card.get("answer") or ""),
        })
    return {
        "answers": echoed,
        "details": details,
        "score": score,
        "total": len(mcq_cards),
    }


# ---------------------------------------------------------------------------
# 7. Mastery statistics from the progress map
# ---------------------------------------------------------------------------

def mastery_stats(set_data: dict | None) -> dict:
    """Summarize review progress: total/reviewed/known/unknown/mastery_pct.

    A card counts as "reviewed" once its progress entry has ``reviews >= 1``;
    "known" requires ``known is True`` on a reviewed card. ``mastery_pct``
    is the rounded percent of reviewed cards marked known (0 when nothing
    was reviewed). Never raises.
    """
    try:
        return _mastery_stats(set_data)
    except Exception:
        return {"total": 0, "reviewed": 0, "known": 0,
                "unknown": 0, "mastery_pct": 0, "due": 0}


# ---------------------------------------------------------------------------
# 8. Spaced-repetition scheduling (SM-2-lite)
# ---------------------------------------------------------------------------
# Until 2026-10-24 progress was only {known, reviews} — nothing was
# actually *spaced*. Each progress entry now also carries:
#   ease     float, starts 2.5, floor 1.3, +0.05 per success (cap 2.8),
#            -0.2 per lapse
#   interval int days until the card is due again
#   due      ISO date (YYYY-MM-DD) the card becomes due
#   lapses   count of "still learning" marks after first success
# Legacy entries without these fields behave as brand-new cards.

from datetime import date as _date
from datetime import timedelta as _timedelta

SRS_DEFAULT_EASE = 2.5
SRS_MIN_EASE = 1.3
SRS_MAX_EASE = 2.8


def _today_str(today: _date | None = None) -> str:
    return (today or _date.today()).isoformat()


def srs_update(
    entry: dict | None,
    known: bool,
    today: _date | None = None,
) -> dict:
    """Advance one card's schedule after a review. Mutates and returns
    the entry. ``today`` is a datetime.date (defaults to today)."""
    today = today or _date.today()
    entry = entry if isinstance(entry, dict) else {}
    try:
        ease = float(entry.get("ease") or SRS_DEFAULT_EASE)
    except (TypeError, ValueError):
        ease = SRS_DEFAULT_EASE
    try:
        interval = int(entry.get("interval") or 0)
    except (TypeError, ValueError):
        interval = 0
    lapses = entry.get("lapses") or 0
    if known:
        if interval <= 0:
            interval = 1
        elif interval == 1:
            interval = 3
        else:
            interval = max(2, round(interval * ease))
        ease = min(SRS_MAX_EASE, ease + 0.05)
    else:
        if interval > 0:
            lapses = int(lapses) + 1
        interval = 1
        ease = max(SRS_MIN_EASE, ease - 0.2)
    entry["ease"] = round(ease, 2)
    entry["interval"] = interval
    entry["lapses"] = int(lapses)
    entry["due"] = (today + _timedelta(days=interval)).isoformat()
    return entry


def card_due_state(entry: dict | None, today: _date | None = None) -> str:
    """'new' (never reviewed), 'due' (due today or overdue), or
    'scheduled' (due in the future) for one progress entry."""
    if not isinstance(entry, dict) or not entry.get("reviews"):
        return "new"
    due = entry.get("due")
    if not due:
        return "due"  # legacy reviewed card without a schedule
    return "due" if str(due) <= _today_str(today) else "scheduled"


def due_count(set_data: dict | None, today: _date | None = None) -> int:
    """Cards in a deck that are new or due today."""
    data = set_data if isinstance(set_data, dict) else {}
    cards = data.get("cards") if isinstance(data.get("cards"), list) else []
    progress = data.get("progress") if isinstance(data.get("progress"), dict) else {}
    n = 0
    for c in cards:
        if isinstance(c, dict) and card_due_state(progress.get(c.get("id")), today) in ("new", "due"):
            n += 1
    return n


def _mastery_stats(set_data: dict | None) -> dict:
    data = set_data if isinstance(set_data, dict) else {}
    cards = data.get("cards")
    total = len(cards) if isinstance(cards, list) else 0
    progress = data.get("progress")
    reviewed = 0
    known = 0
    if isinstance(progress, dict):
        for entry in progress.values():
            if not isinstance(entry, dict):
                continue
            reviews = entry.get("reviews")
            if isinstance(reviews, bool):
                reviews = 0
            if isinstance(reviews, int) and reviews >= 1:
                reviewed += 1
                if entry.get("known") is True:
                    known += 1
    unknown = reviewed - known
    mastery_pct = round(100.0 * known / reviewed) if reviewed else 0
    return {
        "total": total,
        "reviewed": reviewed,
        "known": known,
        "unknown": unknown,
        "mastery_pct": mastery_pct,
        "due": due_count(set_data),
    }


# ================= Feature 23 Ultimate Enhancements =================
# 23.1 Anki export | 23.2 Exam mode (timed sprint) | 23.3 Weak-card loop

import random as _random17
import re as _re17


def _clean17(text: str | None) -> str:
    return _re17.sub(r"<[^>]*>", "", text or "")


def anki_export(
    cards: list[dict] | tuple[dict, ...],
    deck_name: str = "STUDY-BUDDY",
) -> dict:
    """Export cards to Anki-importable TSV (tab-separated, UTF-8).

    cards: list of {question/front, answer/back, ...}. Returns {tsv, count,
    instructions}. Anki: File > Import > select .txt, fields separated by Tab.
    """
    if not isinstance(cards, (list, tuple)):
        raise ValueError("cards must be a list.")
    deck_name = _clean17(deck_name or "STUDY-BUDDY").strip()[:60] or "STUDY-BUDDY"
    lines = []
    for c in cards:
        if not isinstance(c, dict):
            continue
        front = _clean17(c.get("question") or c.get("front") or "").strip()
        back = _clean17(c.get("answer") or c.get("back") or "").strip()
        if not front or not back:
            continue
        # TSV-safe: tabs/newlines become spaces; quotes doubled is unnecessary for TSV.
        front = _re17.sub(r"[\t\r\n]+", " ", front)[:500]
        back = _re17.sub(r"[\t\r\n]+", " ", back)[:1000]
        lines.append(f"{front}\t{back}")
    if not lines:
        raise ValueError("No valid cards to export.")
    return {"tsv": "\n".join(lines), "count": len(lines),
            "deck_name": deck_name, "filename": f"{deck_name}.txt",
            "instructions": ("In Anki: File > Import > choose this .txt > "
                             "Fields separated by: Tab > Deck: " + deck_name)}


def exam_sprint(
    cards: list[dict] | tuple[dict, ...],
    seconds_per_card: int = 20,
    seed: Any = None,
) -> dict:
    """Timed recall sprint: N cards, countdown per card.

    Returns {rounds: [{card_id, question, seconds}], total_seconds, rules}.
    The client runs the timer; the server shuffles + orders.
    """
    if not isinstance(cards, (list, tuple)) or not cards:
        raise ValueError("cards must be a non-empty list.")
    try:
        seconds_per_card = int(seconds_per_card)
    except (TypeError, ValueError):
        seconds_per_card = 20
    seconds_per_card = max(5, min(120, seconds_per_card))
    rng = _random17.Random(seed)
    order = list(cards)
    rng.shuffle(order)
    rounds = []
    for i, c in enumerate(order):
        if not isinstance(c, dict):
            continue
        q = _clean17(c.get("question") or c.get("front") or "")[:300]
        if not q:
            continue
        rounds.append({"round": i + 1, "card_id": c.get("id"),
                       "question": q, "seconds": seconds_per_card})
    if not rounds:
        raise ValueError("No valid cards for a sprint.")
    return {"rounds": rounds, "total_cards": len(rounds),
            "total_seconds": len(rounds) * seconds_per_card,
            "rules": (f"{seconds_per_card}s per card. Say the answer aloud, then flip. "
                      "Mark got-it / missed — missed cards come back in the weak loop.")}


def weak_card_loop(
    card_stats: dict,
    cards: list[dict] | tuple[dict, ...],
) -> dict:
    """Missed cards resurface first.

    card_stats: {card_id: {wrong (int), right (int)}}. cards: list of card dicts.
    Returns {queue: [card dicts ordered weak-first], weak_count}.
    """
    if not isinstance(card_stats, dict):
        raise ValueError("card_stats must be a dict.")
    if not isinstance(cards, (list, tuple)):
        raise ValueError("cards must be a list.")
    scored = []
    for c in cards:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id"))
        st = card_stats.get(cid, {}) if isinstance(card_stats.get(cid), dict) else {}
        try:
            wrong = max(0, int(st.get("wrong") or 0))
            right = max(0, int(st.get("right") or 0))
        except (TypeError, ValueError):
            wrong, right = 0, 0
        total = wrong + right
        # Weakness = wrong rate, with Laplace smoothing; unseen cards in the middle.
        weakness = (wrong + 0.5) / (total + 1) if total else 0.5
        scored.append((weakness, -total, c))
    scored.sort(key=lambda x: (-x[0], x[1]))
    queue = [c for _, _, c in scored]
    weak_count = sum(1 for w, t, _ in scored if w > 0.5 and t < 0)
    return {"queue": queue, "weak_count": weak_count,
            "total": len(queue),
            "guidance": ("Weak cards first. Clear a card with 3 consecutive "
                         "correct recalls to graduate it.")}
