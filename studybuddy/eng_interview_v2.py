"""ENG-12: Interview answer scoring v2 — transparent rubric, semantic coverage.

Scores a free-text interview answer against a list of key points the ideal
answer should cover:

- **Key-point coverage (40%)** — semantic: cosine similarity of each key
  point against the answer with a tunable threshold. Uses a caller-provided
  embedding function when available, otherwise a stemmed token-recall
  lexical fallback (honest about paraphrase limits; no dependencies).
- **STAR structure (25%)** — detects Situation / Task / Action / Result cues.
- **Specificity (20%)** — concrete numbers, named tools, concrete outcomes.
- **Clarity & length (15%)** — sane length band, sentence structure,
  filler-word penalty.

Anti-gaming:
- Keyword lists without sentences get their coverage capped
  (KEYWORD_LIST_COVERAGE_CAP) instead of full credit.
- Verbatim repetition of the question is penalized, not rewarded.

Optional LLM-as-judge: ``llm_judge_answer`` runs a FIXED rubric prompt
through a caller-provided LLM callable, expects strict JSON, validates it,
and labels the engine. Any failure falls back to the rule-based engine and
says so. A score is NEVER returned without the rubric and example
improvements — ``score_answer`` enforces this structurally.
"""

import json
import math
import re

from studybuddy.rubric_tfidf import improved_tfidf_cosine

# ---------------------------------------------------------------------------
# Transparent rubric (weights sum to 100)
# ---------------------------------------------------------------------------

RUBRIC = [
    {"dimension": "key_point_coverage", "weight": 40,
     "description": "How many of the ideal answer's key points the answer covers, semantically."},
    {"dimension": "star_structure", "weight": 25,
     "description": "Situation / Task / Action / Result structure is present."},
    {"dimension": "specificity", "weight": 20,
     "description": "Concrete numbers, named tools/technologies, specific outcomes — not vague claims."},
    {"dimension": "clarity_and_length", "weight": 15,
     "description": "Clear sentences in a sensible length band; penalizes filler and rambling."},
]

COVERAGE_THRESHOLD = 0.65          # semantic similarity to count a key point covered
KEYWORD_LIST_COVERAGE_CAP = 0.4    # coverage ceiling when answer is a bare keyword list
VERBATIM_REPETITION_THRESHOLD = 0.5  # longest-common-word-run ratio that triggers penalty
MIN_WORDS, MAX_WORDS = 40, 350

_FILLER_RX = re.compile(
    r"\b(um+|uh+|like|you know|basically|actually|literally|stuff|things?)\b",
    re.IGNORECASE)
_NUMBER_RX = re.compile(r"\b\d+(?:\.\d+)?%?\b")
_SPECIFIC_RX = re.compile(
    r"\b(\d+\s*(%|percent|x|times|users|customers|ms|seconds?|days?|weeks?|months?)|"
    r"(increased|decreased|reduced|improved|cut|grew|shipped|launched|migrated))\b",
    re.IGNORECASE)

_STAR_CUES = {
    "situation": [r"\bsituation\b", r"\bcontext\b", r"\bwhen i joined\b",
                  r"\bthe (problem|challenge|issue) was\b", r"\bwe (were|had)\b",
                  r"\bfaced with\b", r"\bat (the time|that point)\b"],
    "task": [r"\bmy (task|role|responsibility) (was|is)\b", r"\bi was asked to\b",
             r"\bthe goal was\b", r"\bneeded to\b", r"\bhad to\b"],
    "action": [r"\bi (built|designed|implemented|decided|chose|wrote|led|drove|created|fixed|refactored|deployed)\b",
               r"\bwe (built|decided|chose|migrated|shipped)\b", r"\bsteps? i took\b",
               r"\b(built|designed|implemented|deployed|rewrote|migrated|shipped|created|launched|refactored|profiled)\b"],
    "result": [r"\bas a result\b", r"\bthe outcome (was|is)\b", r"\bresulted in\b",
               r"\bled to\b", r"\bimproved by\b", r"\breduced .* by\b"],
}


def _tokens(text):
    return re.findall(r"[a-z0-9][a-z0-9+#.\-]{2,}", (text or "").lower())


def _stem(tok):
    """Light suffix stemming for the lexical fallback only (not a real stemmer)."""
    for suf in ("ing", "ed", "es"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 4:
            tok = tok[: -len(suf)]
            break
    if tok.endswith("e") and len(tok) > 4:
        tok = tok[:-1]
    if tok.endswith("s") and len(tok) > 4 and not tok.endswith("ss"):
        tok = tok[:-1]
    return tok


def lexical_recall(key_point, answer):
    """Stemmed token-recall: fraction of key-point tokens appearing in the answer.

    Honest lexical fallback for when no embeddings are configured. It cannot
    see paraphrase ("reduced" vs "dropped"); the embedding path handles that.
    """
    kp = {_stem(t) for t in _tokens(key_point)}
    if not kp:
        return 0.0
    ans = {_stem(t) for t in _tokens(answer)}
    return len(kp & ans) / len(kp)


def _sentences(text):
    return [s.strip() for s in re.split(r"[.!?]+", (text or "")) if s.strip()]


def tfidf_cosine(a, b):
    """Cosine similarity of TF-IDF vectors over the two-doc corpus [a, b]."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    vocab = set(ta) | set(tb)
    idf = {}
    for term in vocab:
        df = (1 if term in set(ta) else 0) + (1 if term in set(tb) else 0)
        idf[term] = math.log(2 / df) + 1.0  # df in {1,2}
    def vec(toks):
        tf = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        n = len(toks)
        return {t: (c / n) * idf[t] for t, c in tf.items()}
    va, vb = vec(ta), vec(tb)
    dot = sum(va[t] * vb.get(t, 0.0) for t in va)
    na = math.sqrt(sum(v * v for v in va.values()))
    nb = math.sqrt(sum(v * v for v in vb.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _cosine_vec(va, vb):
    dot = sum(x * y for x, y in zip(va, vb, strict=False))
    na = math.sqrt(sum(x * x for x in va))
    nb = math.sqrt(sum(y * y for y in vb))
    return dot / (na * nb) if na and nb else 0.0


def semantic_similarity(a, b, embed=None):
    """Similarity of ``a`` against ``b`` — hybrid (issue #55).

    ``embed`` is an optional callable ``(text) -> vector`` (see
    studybuddy.rubric_embeddings.get_rubric_embed). Precedence:

    1. embed provided and the call succeeds -> true embedding cosine.
    2. embed provided but the call fails/returns None (flag on, API
       unavailable) -> improved TF-IDF fallback (studybuddy.rubric_tfidf),
       sentence-maxed like the legacy path. Labelled "tfidf".
    3. embed is None (flag off, default) -> the legacy stemmed
       token-recall path, byte-identical to before. Labelled nothing
       (no similarity_signal key).
    """
    if embed is not None:
        try:
            from studybuddy.rubric_embeddings import (
                calibrated_embedding_similarity)
            va, vb = embed(a), embed(b)
            if va and vb:
                return calibrated_embedding_similarity(a, b, va, vb)
            raise ValueError("embed returned None")
        except Exception:
            pass  # fall through to the improved TF-IDF fallback
        candidates = _sentences(b) + [b or ""]
        return max(improved_tfidf_cosine(a, c) for c in candidates)
    candidates = _sentences(b) + [b or ""]
    return max(lexical_recall(a, c) for c in candidates)


# ---------------------------------------------------------------------------
# Dimension scorers
# ---------------------------------------------------------------------------

def key_point_coverage(answer, key_points, *, threshold=COVERAGE_THRESHOLD,
                       embed=None):
    """Fraction of key points covered. Anti-gaming cap applied for keyword lists."""
    points = [p for p in (key_points or []) if p and str(p).strip()]
    if not points:
        return 1.0, []
    covered = []
    for p in points:
        sim = semantic_similarity(str(p), answer or "", embed=embed)
        covered.append({"key_point": str(p), "similarity": round(sim, 3),
                        "covered": sim >= threshold})
    fraction = sum(1 for c in covered if c["covered"]) / len(covered)
    if is_keyword_list(answer or ""):
        fraction = min(fraction, KEYWORD_LIST_COVERAGE_CAP)
    return round(fraction, 3), covered


def star_structure(answer):
    """Detect STAR components; returns per-letter booleans + cues found."""
    lower = (answer or "").lower()
    found = {}
    for letter, patterns in _STAR_CUES.items():
        cues = [p for p in patterns if re.search(p, lower)]
        found[letter] = {"present": bool(cues), "cues": cues}
    n = sum(1 for v in found.values() if v["present"])
    return {"components": found, "count": n, "score": round(n / 4, 3)}


def specificity(answer):
    """Concrete detail density: numbers, outcome verbs, named specifics."""
    text = answer or ""
    words = len(text.split())
    if words == 0:
        return {"score": 0.0, "numbers": 0, "specific_hits": 0}
    numbers = len(_NUMBER_RX.findall(text))
    specific = len(_SPECIFIC_RX.findall(text))
    # ~1 concrete token per 25 words is strong; saturate at 1.0.
    score = min(1.0, (numbers + specific) / max(1.0, words / 25))
    return {"score": round(score, 3), "numbers": numbers, "specific_hits": specific}


def clarity_and_length(answer):
    """Length band + sentence structure + filler penalty."""
    text = (answer or "").strip()
    words = len(text.split())
    sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()]
    notes = []
    if words < MIN_WORDS:
        length_score, notes = 0.3, [f"too short ({words} words; aim for {MIN_WORDS}+)"]
    elif words > MAX_WORDS:
        length_score, notes = 0.5, [f"too long ({words} words; aim for under {MAX_WORDS})"]
    else:
        length_score, notes = 1.0, []
    filler = len(_FILLER_RX.findall(text))
    filler_penalty = min(0.4, filler * 0.05)
    if filler:
        notes.append(f"{filler} filler word(s) weaken clarity")
    avg_len = words / max(1, len(sentences))
    structure_bonus = 0.0
    if 3 <= len(sentences) <= 12 and 8 <= avg_len <= 30:
        structure_bonus = 0.0  # already fine
    elif len(sentences) < 2 and words >= MIN_WORDS:
        notes.append("single block of text — break into sentences")
    score = max(0.0, min(1.0, length_score - filler_penalty + structure_bonus))
    return {"score": round(score, 3), "words": words, "sentences": len(sentences),
            "filler_hits": filler, "notes": notes}


def is_keyword_list(answer):
    """Heuristic: comma-dense text with few verbs and few sentences."""
    text = (answer or "").strip()
    if not text or len(text.split()) < 10:
        return False
    commas = text.count(",")
    sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()]
    verbs = len(re.findall(
        r"\b(am|is|are|was|were|built|made|did|led|used|created|worked|managed|developed)\b",
        text.lower()))
    words = len(text.split())
    return commas >= 4 and len(sentences) <= 2 and verbs / max(1, words) < 0.04


def verbatim_repetition_penalty(question, answer):
    """Penalty 0..1 for copying the question verbatim into the answer.

    Uses the longest common contiguous word run as a fraction of the
    question's word count.
    """
    qw = _tokens(question or "")
    aw = _tokens(answer or "")
    if not qw or not aw:
        return 0.0
    # Longest common contiguous subsequence via dynamic programming on words.
    prev = [0] * (len(aw) + 1)
    best = 0
    for i in range(1, len(qw) + 1):
        cur = [0] * (len(aw) + 1)
        for j in range(1, len(aw) + 1):
            if qw[i - 1] == aw[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    ratio = best / len(qw)
    if ratio >= VERBATIM_REPETITION_THRESHOLD:
        return round(min(1.0, ratio), 3)
    return 0.0


# ---------------------------------------------------------------------------
# Improvements + main entry
# ---------------------------------------------------------------------------

def example_improvements(question, answer, coverage_detail, star, spec, clarity):
    """Concrete, example-driven improvements — never empty."""
    tips = []
    uncovered = [c["key_point"] for c in coverage_detail if not c["covered"]]
    if uncovered:
        tips.append({
            "area": "Coverage",
            "tip": f"Address this missing point explicitly: '{uncovered[0][:80]}'. "
                   "Example: open with 'One thing I focused on was …' and give a concrete instance.",
        })
    missing_star = [k for k, v in star["components"].items() if not v["present"]]
    if missing_star:
        tips.append({
            "area": "Structure",
            "tip": f"Add the missing STAR part(s): {', '.join(missing_star)}. "
                   "Example: 'The result was a 30% drop in load time, measured over two weeks.'",
        })
    if spec["score"] < 0.5:
        tips.append({
            "area": "Specificity",
            "tip": "Replace one vague claim with a number. Example: instead of "
                   "'improved performance', say 'cut p95 latency from 800ms to 320ms'.",
        })
    if clarity["notes"]:
        tips.append({"area": "Clarity", "tip": " ".join(clarity["notes"]) + "."})
    if not tips:
        tips.append({
            "area": "Polish",
            "tip": "Strong answer. To stand out, add one quantified outcome and "
                   "tie it explicitly to the role you're interviewing for.",
        })
    return tips


def score_answer(question, answer, key_points, *, threshold=COVERAGE_THRESHOLD,
                 embed=None, llm=None, llm_model=""):
    """Score an interview answer. Always returns rubric + example improvements.

    ``llm`` is an optional callable ``(prompt) -> str``; when provided, the
    fixed-rubric LLM judge runs and the result is labeled engine="llm".
    Otherwise (or on any LLM failure) the rule-based engine scores and the
    result is labeled engine="rule-based".
    """
    engine = "rule-based"
    llm_result = None
    if llm is not None:
        llm_result = llm_judge_answer(question, answer, key_points, llm,
                                      model=llm_model)
        if llm_result.get("valid"):
            engine = "llm"

    # Embedding-use tracking for the honest signal label: semantic_similarity
    # falls back to the lexical path per key point when an embed call
    # fails, so "embeddings" here means at least one key point was scored
    # with embeddings (any failed pair fell back to TF-IDF-style recall).
    # The wrapper lets embed exceptions propagate to that fallback.
    _embed_used: list[bool] = []
    _tracked_embed = None
    if embed is not None:
        def _tracked_embed(text):
            vec = embed(text)
            if vec:
                _embed_used.append(True)
            return vec

    coverage, coverage_detail = key_point_coverage(
        answer, key_points, threshold=threshold, embed=_tracked_embed)
    star = star_structure(answer)
    spec = specificity(answer)
    clarity = clarity_and_length(answer)
    repetition = verbatim_repetition_penalty(question, answer)

    base = (0.40 * coverage + 0.25 * star["score"]
            + 0.20 * spec["score"] + 0.15 * clarity["score"])
    final = max(0.0, base - 0.20 * repetition)  # up to 20 pts off for copying

    breakdown = {
        "key_point_coverage": {"score": round(coverage * 40, 1), "max": 40,
                               "detail": coverage_detail},
        "star_structure": {"score": round(star["score"] * 25, 1), "max": 25,
                           "detail": star},
        "specificity": {"score": round(spec["score"] * 20, 1), "max": 20,
                        "detail": spec},
        "clarity_and_length": {"score": round(clarity["score"] * 15, 1), "max": 15,
                               "detail": clarity},
        "anti_gaming": {"verbatim_repetition_penalty": repetition,
                        "keyword_list_detected": is_keyword_list(answer or ""),
                        "points_deducted": round(0.20 * repetition * 100, 1)},
    }
    if engine == "llm":
        breakdown["llm_judge"] = llm_result["scores"]

    out: dict = {
        "score": round(final * 100, 1),
        "engine": engine,
        "llm_model": llm_model if engine == "llm" else None,
        "rubric": RUBRIC,  # never show a score without the rubric
        "breakdown": breakdown,
        "improvements": example_improvements(
            question, answer, coverage_detail, star, spec, clarity),
        "note": ("Rule-based semantic scoring" if engine == "rule-based"
                 else f"LLM-as-judge scoring (model: {llm_model}); validated against the fixed rubric"),
    }
    if embed is not None:
        # Added only when an embed callable was passed (flag on); flag-off
        # output is byte-identical to before this feature existed.
        out["similarity_signal"] = "embeddings" if _embed_used else "tfidf"
        out["similarity_method"] = "gemini" if _embed_used else "tfidf"
        if _embed_used:
            out["note"] = ("Rule-based scoring; key-point similarity via "
                           "Gemini embeddings (lexical fallback for any "
                           "failed pair)")
    return out


# ---------------------------------------------------------------------------
# Optional LLM-as-judge (fixed prompt, strict JSON, validated)
# ---------------------------------------------------------------------------

LLM_JUDGE_PROMPT = """You are an interview-answer judge. Score the candidate's ANSWER
against the KEY POINTS using ONLY this rubric. Reply with STRICT JSON and
nothing else — no markdown, no prose.

Rubric (weights sum to 100):
- key_point_coverage (0-40): semantic coverage of the key points, not keyword matching.
- star_structure (0-25): Situation / Task / Action / Result present.
- specificity (0-20): concrete numbers, named tools, specific outcomes.
- clarity_and_length (0-15): clear sentences, sensible length.

QUESTION: {question}

KEY POINTS (ideal answer covers these):
{key_points}

ANSWER:
{answer}

Respond with exactly this JSON shape:
{{"key_point_coverage": <0-40>, "star_structure": <0-25>,
  "specificity": <0-20>, "clarity_and_length": <0-15>,
  "improvements": ["<concrete tip>", "..."]}}
"""


def parse_llm_judge_output(raw):
    """Validate LLM judge output. Returns {"valid": bool, ...}."""
    try:
        data = json.loads((raw or "").strip())
    except (json.JSONDecodeError, AttributeError, TypeError):
        return {"valid": False, "error": "not valid JSON"}
    expected = {"key_point_coverage": (0, 40), "star_structure": (0, 25),
                "specificity": (0, 20), "clarity_and_length": (0, 15)}
    scores = {}
    for key, (lo, hi) in expected.items():
        val = data.get(key)
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            return {"valid": False, "error": f"missing/invalid {key}"}
        if not (lo <= val <= hi):
            return {"valid": False, "error": f"{key} out of range"}
        scores[key] = val
    improvements = data.get("improvements")
    if not isinstance(improvements, list) or not all(isinstance(t, str) for t in improvements):
        return {"valid": False, "error": "improvements must be a string list"}
    return {"valid": True, "scores": scores, "improvements": improvements}


def llm_judge_answer(question, answer, key_points, llm_call, model=""):
    """Run the fixed-rubric LLM judge. Falls back to invalid on any failure."""
    points = "\n".join(f"- {p}" for p in (key_points or [])) or "(none provided)"
    prompt = LLM_JUDGE_PROMPT.format(question=question or "",
                                     key_points=points, answer=answer or "")
    try:
        raw = llm_call(prompt)
    except Exception as exc:  # noqa: BLE001 — any LLM failure -> fallback
        return {"valid": False, "error": f"llm call failed: {exc}"}
    result = parse_llm_judge_output(raw)
    result["model"] = model
    return result
