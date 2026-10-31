"""Improved TF-IDF similarity for interview rubric scoring (issue #55).

Hybrid approach (user decision 2026-10-31): Gemini API embeddings when the
RUBRIC_EMBEDDINGS flag is on AND a key is configured; otherwise THIS module
— a genuinely improved lexical scorer — as the fallback/default.

How it is better than the legacy scorer in mock_interview._tfidf_cosine
(which is preserved byte-identical for the flag-off path):

- N-gram ranges (unigrams + bigrams): bigrams capture phrase order, so
  scrambled word salads ("Repeated computation avoids cached results")
  score LOWER than with pure unigrams — less fooled by word order.
- Sublinear TF scaling (1 + log(tf)): a word repeated 10x does not get
  10x the weight — dampens repetition gaming.
- Better preprocessing: expanded interview-filler stopwords, an improved
  stemmer that fixes e-ending verbs ("improved"/"improve" -> "improv",
  "cached"/"cache" -> "cach") and doubled consonants ("running" -> "run").
- Smoothed IDF over the two-document corpus.

Honest limits (documented, not hidden): this is STILL lexical. A true
paraphrase sharing no content words scores 0.0 here, exactly like the
legacy scorer. Only the Gemini API path can see paraphrase. The
calibration report (scripts/rubric_calibration_report.py) shows all three
columns side by side so the gap is visible, not marketed away.

Stdlib only, deterministic, never raises on bad input.
"""

from __future__ import annotations

import math
import re
from collections import Counter

_VOWELS = frozenset("aeiou")

# Base English stopwords plus interview-answer filler: words candidates
# sprinkle that carry no topical signal ("basically", "actually", ...).
_STOPWORDS = frozenset({
    # core English
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "be", "been", "being", "it", "its",
    "that", "this", "these", "those", "as", "by", "at", "from", "into",
    "you", "your", "yours", "we", "our", "ours", "they", "their", "theirs",
    "i", "my", "me", "mine", "he", "him", "his", "she", "her", "hers",
    "so", "if", "not", "no", "nor", "do", "does", "did", "done", "have",
    "has", "had", "having", "will", "would", "can", "could", "shall",
    "should", "may", "might", "must", "what", "when", "where", "which",
    "who", "whom", "whose", "how", "why", "there", "here", "than", "then",
    "too", "very", "just", "also", "only", "own", "same", "such", "up",
    "down", "out", "off", "over", "under", "again", "once", "all", "any",
    "both", "each", "few", "more", "most", "other", "some",
    # interview filler / hedges
    "basically", "actually", "really", "quite", "rather", "pretty",
    "stuff", "things", "thing", "something", "anything", "everything",
    "like", "well", "yeah", "yes", "okay", "ok", "uh", "um", "hmm",
    "kind", "sort", "lot", "lots", "bit", "much", "many", "get", "got",
    "getting", "make", "made", "making", "take", "took", "taken", "taking",
    "use", "used", "using", "usually", "often", "always", "never",
    "every", "everyone", "someone", "anyone", "nobody", "everybody",
    # instructional verbs (appear in key-point guidance, not answers)
    "describe", "list", "explain", "discuss", "tell", "give", "show",
    "mention", "include", "including", "provide", "outline", "quantify",
    "name", "state", "share", "walk", "cover", "covering",
})

_WORD_RE = re.compile(r"[a-z][a-z0-9+\-#]*")


def stem_improved(w: str) -> str:
    """Compact stemmer with e-ending and doubled-consonant fixes.

    The legacy _stem() turned "improved" into "improv" but left "improve"
    as "improve" (no match), and "cached" into "cach" vs "cache" (no
    match). This version maps each pair to one stem. Both sides of every
    comparison use it, so consistent over-stemming is safe: deterministic
    and symmetric, never favouring one side.
    """
    if len(w) <= 3:
        return w
    # ies -> y : 'frequencies' -> 'frequency'
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    # plural s : 'indexes' -> 'index' (ss/us endings kept: 'process', 'status')
    elif (w.endswith("s") and not w.endswith(("ss", "us"))
            and len(w) > 4):
        w = w[:-1]
    # ing : 'running' -> 'run' (doubled consonant), 'counting' -> 'count'
    # Doubled l/s/z are kept ('roll', not 'rol') — Porter rule.
    if w.endswith("ing") and len(w) > 5:
        stem = w[:-3]
        if (len(stem) > 2 and stem[-1] == stem[-2]
                and stem[-1] not in _VOWELS | {"l", "s", "z"}):
            stem = stem[:-1]
        w = stem
    # ed : 'improved' -> 'improv', 'stopped' -> 'stop' (not 'eed': 'agreed')
    elif w.endswith("ed") and len(w) > 4 and not w.endswith("eed"):
        stem = w[:-2]
        if (len(stem) > 2 and stem[-1] == stem[-2]
                and stem[-1] not in _VOWELS | {"l", "s", "z"}):
            stem = stem[:-1]
        w = stem
    # trailing e : 'improve' -> 'improv' (matches 'improved'), 'cache' ->
    # 'cach' (matches 'cached'). Skipped for ee/oe/ye endings ('free',
    # 'agree') and short words.
    if (w.endswith("e") and len(w) > 4
            and not w.endswith(("ee", "oe", "ye"))):
        w = w[:-1]
    return w


def tokenize(text: str | None) -> list[str]:
    """Content tokens: lowercase words, stopwords/filler removed, stemmed.

    Tokens of length <= 2 are dropped (same as the legacy path).
    """
    out: list[str] = []
    for w in _WORD_RE.findall((text or "").lower()):
        if w in _STOPWORDS or len(w) <= 2:
            continue
        out.append(stem_improved(w))
    return out


def _cosine_from_counters(ca: Counter, cb: Counter) -> float:
    """Cosine of sublinear-TF * smoothed-IDF vectors from token counters."""
    idf: dict[str, float] = {}
    for term in set(ca) | set(cb):
        df = (1 if term in ca else 0) + (1 if term in cb else 0)
        idf[term] = math.log(3.0 / (1.0 + df)) + 1.0

    def vec(counter: Counter) -> dict[str, float]:
        # Sublinear TF: 1 + log(tf) — a term repeated 10x gets ~3.3x
        # weight, not 10x. Dampens repetition gaming.
        return {t: (1.0 + math.log(c)) * idf[t]
                for t, c in counter.items()}

    va, vb = vec(ca), vec(cb)
    dot = sum(va[t] * vb.get(t, 0.0) for t in va)
    na = math.sqrt(sum(v * v for v in va.values()))
    nb = math.sqrt(sum(v * v for v in vb.values()))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


def improved_tfidf_cosine(a_text: str | None, b_text: str | None) -> float:
    """Improved lexical TF-IDF cosine similarity in [0, 1].

    ``a_text`` is the reference (e.g. a key point), ``b_text`` the
    candidate (e.g. the answer). Components:

    - Unigram cosine with sublinear TF (1 + log tf) and smoothed IDF —
      the base topical-overlap signal.
    - Order factor from bigram/trigram RECALL of a's n-grams in b:
      0.4 + 0.4 * bigram_recall + 0.2 * trigram_recall. A scrambled
      candidate keeps its unigrams but loses n-gram overlap, so the
      factor drops (near 0.4 when fully scrambled vs near 1.0 when
      phrasing is preserved).

    0.0 when either side has no content tokens. Deterministic.
    """
    a_toks = tokenize(a_text)
    b_toks = tokenize(b_text)
    if not a_toks or not b_toks:
        return 0.0
    uni = _cosine_from_counters(Counter(a_toks), Counter(b_toks))

    def _ngrams(toks: list[str], n: int) -> list[str]:
        return ["_".join(toks[i:i + n]) for i in range(len(toks) - n + 1)]

    a_bi = _ngrams(a_toks, 2)
    b_bi = set(_ngrams(b_toks, 2))
    a_tri = _ngrams(a_toks, 3)
    b_tri = set(_ngrams(b_toks, 3))
    bi_recall = (sum(1 for g in a_bi if g in b_bi) / len(a_bi)
                 if a_bi else 1.0)
    tri_recall = (sum(1 for g in a_tri if g in b_tri) / len(a_tri)
                  if a_tri else 1.0)
    order_factor = 0.4 + 0.4 * bi_recall + 0.2 * tri_recall
    return round(uni * order_factor, 4)


def improved_key_point_similarity(
    key_points: list[str] | None,
    answer: str | None,
) -> tuple[float, list[float]]:
    """Mean improved TF-IDF cosine between the answer and each key point.

    Mirrors mock_interview._key_point_similarity's contract (mean,
    per-point list; 0.0 on empty answer; 1.0 when no usable key points)
    so it is a drop-in replacement for the flag-on fallback path. The
    flag-off path keeps using the legacy scorer (byte-identical).
    """
    if not (answer or "").strip():
        return 0.0, []
    if not tokenize(answer):
        return 0.0, []
    sims: list[float] = []
    for kp in key_points or []:
        if not (kp or "").strip():
            continue
        if not tokenize(kp):
            continue
        # (kp, answer): n-gram recall measures how much of the key
        # point's phrasing the answer preserves.
        sims.append(improved_tfidf_cosine(kp, answer))
    if not sims:
        return 1.0, []
    return sum(sims) / len(sims), sims
