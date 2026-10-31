"""Company-specific AI mock interviews with rubric scoring (Wave 2 feature #13).

Question bank: data/interview_questions.json (tagged by
company/role/category/difficulty, each with ideal-answer key points).

Rubric (per answer, 0-100, deterministic and server-side):
  - correctness / approach ........ 40 (key-point coverage, plus a bounded
    similarity bonus — lexical TF-IDF cosine by default (word-overlap
    statistics, NOT embeddings; see _key_point_similarity), or Gemini
    embedding cosine when the RUBRIC_EMBEDDINGS flag is on and an embed
    callable is passed; see _key_point_similarity_with_signal)
  - communication ................. 25
  - depth ......................... 20
  - edge cases / completeness ..... 15

Gemini (wired in unified_app.py) polishes strengths/improvements when
available; the rule-based feedback below is the guaranteed fallback.
The numeric score never depends on the LLM.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
from collections import Counter, defaultdict
from typing import Any

from studybuddy.rubric_embeddings import cosine_vectors
from studybuddy.rubric_tfidf import improved_key_point_similarity

BANK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "interview_questions.json")

_bank = None


def _load_bank() -> list[dict[str, Any]]:
    global _bank
    if _bank is None:
        with open(BANK_PATH, encoding="utf-8") as f:
            _bank = json.load(f)
    return _bank


def _filler_entries() -> list[dict[str, Any]]:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "filler_words.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return []


_FILLERS = None


def _fillers() -> list[dict[str, Any]]:
    global _FILLERS
    if _FILLERS is None:
        _FILLERS = _filler_entries()
    return _FILLERS


def interview_options() -> dict[str, list[str]]:
    """Distinct companies, roles, categories for the setup form."""
    bank = _load_bank()
    companies = sorted({c for q in bank for c in q["companies"]})
    roles = sorted({r for q in bank for r in q["roles"]})
    categories = sorted({q["category"] for q in bank})
    return {"companies": companies, "roles": roles, "categories": categories}


# Bank category -> display label used on the /practice hub.
CATEGORY_DISPLAY = {
    "behavioral": "Behavioral",
    "technical": "Technical",
    "dsa": "DSA",
    "system_design": "System Design",
    "hr": "HR",
    "puzzles": "Puzzles",
}
_DISPLAY_TO_BANK = {v.lower(): k for k, v in CATEGORY_DISPLAY.items()}


def category_from_display(value: str | None) -> str | None:
    """Accept a hub filter value (display label or bank value) and return
    the bank category, or None when it matches nothing."""
    if not value:
        return None
    key = value.strip().lower()
    if key in CATEGORY_DISPLAY:
        return key
    return _DISPLAY_TO_BANK.get(key)


def interview_hub_data() -> dict[str, Any]:
    """Aggregates for the /practice hub, computed from the JSON bank
    (single source of truth — the legacy CSV is no longer served here).

    Returns companies sorted by question count (desc, then name),
    per-company stats with top categories, and hub totals.
    """
    bank = _load_bank()
    counts = Counter()
    cats = defaultdict(Counter)
    for q in bank:
        for comp in q.get("companies", []):
            counts[comp] += 1
            cats[comp][q.get("category", "")] += 1
    companies = sorted(counts, key=lambda c: (-counts[c], c.lower()))
    stats = {}
    for comp in companies:
        stats[comp] = {
            "total": counts[comp],
            "top_categories": [CATEGORY_DISPLAY.get(cat, cat)
                               for cat, _ in cats[comp].most_common(2)],
        }
    return {
        "companies": companies,
        "stats": stats,
        "total_questions": len(bank),
        "total_companies": len(companies),
        "total_categories": len({q.get("category") for q in bank}),
    }


def hub_company_questions(
    company: str,
    category: str | None = None,
    difficulty: str | None = None,
) -> list[dict[str, Any]]:
    """Bank rows for one company, shaped for the /practice question cards
    (display category, time estimate, rubric key points included)."""
    bank = _load_bank()
    cat_bank = category_from_display(category) if category else None
    out = []
    for q in bank:
        if not any(company.lower() == c.lower()
                   for c in q.get("companies", [])):
            continue
        if cat_bank and q.get("category") != cat_bank:
            continue
        if difficulty and q.get("difficulty", "").lower() != difficulty.lower():
            continue
        out.append({
            "question": q.get("question", ""),
            "category": CATEGORY_DISPLAY.get(q.get("category"),
                                            q.get("category", "")),
            "difficulty": q.get("difficulty", ""),
            "company": company,
            "time_minutes": q.get("time_minutes"),
            "key_points": q.get("key_points", []),
        })
    return out


def _content_words(text: str | None) -> list[str]:
    words = re.findall(r"[a-z][a-z0-9+\-#]*", (text or "").lower())
    stop = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for",
            "with", "is", "are", "was", "were", "be", "it", "its", "that",
            "this", "as", "by", "at", "from", "you", "your", "we", "our",
            "they", "their", "i", "my", "me", "so", "if",
            "not", "no", "do", "does", "did", "have", "has", "had", "will",
            "would", "can", "could", "should", "what", "when", "where",
            "which", "who", "how", "why"}
    # instructional verbs that appear in key-point guidance, not in answers
    instructional = {"describe", "list", "explain", "discuss", "tell", "give",
                     "show", "mention", "include", "provide", "outline",
                     "quantify", "name", "state", "share", "walk", "cover"}
    out = []
    for w in words:
        if w in stop or w in instructional or len(w) <= 2:
            continue
        out.append(_stem(w))
    return out


def _stem(w: str) -> str:
    """Very light stemmer so 'recovered' matches 'recovery', etc."""
    if len(w) > 5:
        for suf in ("ational", "tion", "ing", "edly", "ed", "ies", "es", "s", "ly"):
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                if suf == "ies":
                    return w[:-3] + "y"
                return w[:-len(suf)]
    return w


def _tfidf_cosine(a_tokens: list[str], b_tokens: list[str]) -> float:
    """Lexical TF-IDF cosine similarity between two token lists.

    NOT embeddings and NOT semantic: this is pure word-overlap statistics.
    Term frequency within each document is weighted by inverse document
    frequency over the two documents (shared terms get weight 1.0, terms
    unique to one side get log(3/2) + 1), then cosine similarity is taken
    over the two vectors. Returns a float in [0, 1]; 0.0 when either side
    has no tokens. Deterministic, stdlib only.
    """
    if not a_tokens or not b_tokens:
        return 0.0
    ca, cb = Counter(a_tokens), Counter(b_tokens)
    idf = {}
    for term in set(ca) | set(cb):
        df = (1 if term in ca else 0) + (1 if term in cb else 0)
        idf[term] = math.log(3.0 / (1.0 + df)) + 1.0
    va = {t: (c / len(a_tokens)) * idf[t] for t, c in ca.items()}
    vb = {t: (c / len(b_tokens)) * idf[t] for t, c in cb.items()}
    dot = sum(va[t] * vb.get(t, 0.0) for t in va)
    na = math.sqrt(sum(v * v for v in va.values()))
    nb = math.sqrt(sum(v * v for v in vb.values()))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return round(max(0.0, min(1.0, dot / (na * nb))), 4)


def _key_point_similarity(
    key_points: list[str] | None,
    answer: str | None,
) -> tuple[float, list[float]]:
    """Mean lexical TF-IDF cosine similarity between the answer and each
    key point, plus the per-point scores.

    LEXICAL signal only: it rewards answers that use the key points'
    vocabulary with similar emphasis, and it penalizes off-topic
    verbosity (answer terms absent from a key point dilute the cosine).
    It is NOT semantic similarity — a paraphrase sharing no words scores
    0 here, which is exactly why it only ever blends into (never
    replaces) the coverage metric. Mirrors _key_point_coverage: an empty
    answer scores 0.0, and no usable key points score 1.0 (nothing to
    miss, so the blend leaves correctness unchanged).
    """
    ans_tokens = _content_words(answer)
    if not ans_tokens:
        return 0.0, []
    sims = []
    for kp in key_points or []:
        kp_tokens = _content_words(kp)
        if not kp_tokens:
            continue
        sims.append(_tfidf_cosine(ans_tokens, kp_tokens))
    if not sims:
        return 1.0, []
    return sum(sims) / len(sims), sims


def _key_point_similarity_with_signal(
    key_points: list[str] | None,
    answer: str | None,
    embed=None,
) -> tuple[float, list[float], str]:
    """(mean, per-point, signal) — hybrid: API embeddings or improved TF-IDF.

    ``embed`` is an optional callable ``(text) -> vector`` (from
    studybuddy.rubric_embeddings.get_rubric_embed). Precedence per call:

    1. embed provided AND at least one embedding cosine actually computed
       -> signal "embeddings" (method "gemini").
    2. embed provided but unavailable/failed (None, empty, raises) ->
       improved TF-IDF fallback (studybuddy.rubric_tfidf) -> signal
       "tfidf" (method "tfidf").
    3. embed is None (flag off, the default) -> the LEGACY TF-IDF path
       below is NOT used here; callers keep calling _key_point_similarity
       directly for byte-identical output. (This function is only reached
       with embed=None from legacy call sites that predate the flag.)

    The rule is all-or-nothing per call: mixing signals inside one score
    would make the label a lie, and a partially-embedded score would be
    neither reproducible nor honest about what produced it.
    """
    if embed is not None:
        try:
            ans_vec = embed(answer or "")
            if ans_vec:
                sims: list[float] = []
                failed = False
                for kp in key_points or []:
                    if not (kp or "").strip():
                        continue  # mirrors the TF-IDF path's unusable-point skip
                    kp_vec = embed(kp)
                    if not kp_vec:
                        failed = True
                        break
                    sims.append(round(cosine_vectors(ans_vec, kp_vec), 4))
                if not failed and sims:
                    return sum(sims) / len(sims), sims, "embeddings"
        except Exception:
            pass
        # API unavailable or failed -> improved TF-IDF fallback (hybrid).
        mean, sims = improved_key_point_similarity(key_points, answer)
        return mean, sims, "tfidf"
    mean, sims = _key_point_similarity(key_points, answer)
    return mean, sims, "tfidf"


def _key_point_coverage(
    key_points: list[str] | None,
    answer: str | None,
) -> tuple[float, list[str]]:
    """Mean partial credit per key point (50% overlap = full credit)."""
    ans_words = set(_content_words(answer))
    if not ans_words:
        return 0.0, []
    ratios = []
    hits = []
    for kp in key_points or []:
        kp_words = set(_content_words(kp))
        if not kp_words:
            continue
        overlap = len(kp_words & ans_words) / len(kp_words)
        ratios.append(min(1.0, overlap / 0.5))
        if overlap >= 0.35:
            hits.append(kp)
    mean_ratio = sum(ratios) / len(ratios) if ratios else 1.0
    return mean_ratio, hits


def _filler_penalty(answer: str | None) -> tuple[float, list[str]]:
    """Severity-weighted filler hits per 100 words, capped."""
    words = _content_words(answer)
    n = max(1, len(words))
    lowered = " " + (answer or "").lower() + " "
    penalty = 0
    hits = []
    for entry in _fillers():
        w = entry["word"].lower()
        # match whole words/phrases
        count = len(re.findall(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", lowered))
        if count:
            penalty += count * entry["severity"]
            hits.append(entry["word"])
    per_100 = penalty / n * 100
    return min(1.0, per_100 / 12.0), sorted(set(hits))


DEPTH_SIGNALS = ("trade-off", "tradeoff", "however", "alternatively", "because",
                 "measured", "metric", "improved", "reduced", "increased",
                 "specifically", "for example", "e.g.", "resulted", "led to")
EDGE_SIGNALS = ("edge case", "complexity", "o(n", "o(log", "scale", "failure",
                 "fallback", "alternative", "trade-off", "tradeoff", "test",
                 "validate", "assumption")


def _signal_ratio(
    answer: str | None,
    signals: tuple[str, ...],
) -> tuple[float, list[str]]:
    lowered = (answer or "").lower()
    found = [s for s in signals if s in lowered]
    return min(1.0, len(found) / 3.0), found


def score_answer(
    question: dict[str, Any],
    answer: str | None,
    embed=None,
) -> dict[str, Any]:
    """Score one answer against the rubric. Returns a detailed dict.

    ``embed`` is an optional callable ``(text) -> vector`` (Gemini
    embeddings behind the RUBRIC_EMBEDDINGS flag; see
    studybuddy.rubric_embeddings.get_rubric_embed). When it is None
    (the default, flag off) the output is byte-identical to before this
    feature existed. When it is passed, the output gains exactly one
    key, "similarity_signal" ("embeddings" or "tfidf"), stating which
    signal actually produced the similarity numbers.
    """
    answer = (answer or "").strip()
    words = _content_words(answer)
    if not words:
        out: dict[str, Any] = {
            "question_id": question.get("id"),
            "scores": {"correctness": 0.0, "communication": 0.0,
                       "depth": 0.0, "edge_cases": 0.0},
            "total": 0.0,
            "key_points_hit": [],
            "key_points_total": len(question.get("key_points") or []),
            "key_point_similarity": 0.0,
            "key_point_similarities": [],
            "filler_hits": [],
            "word_count": 0,
            "depth_signals": [],
            "edge_signals": [],
        }
        if embed is not None:
            out["similarity_signal"] = "tfidf"
            out["similarity_method"] = "tfidf"
        return out
    key_points = question.get("key_points") or []

    # --- correctness / approach (40) ---
    # Coverage-led: 40 * key-point word coverage, plus a bounded bonus
    # from the similarity signal — lexical TF-IDF cosine by default (NOT
    # embeddings — see _key_point_similarity), or Gemini embedding cosine
    # when an embed callable is passed (RUBRIC_EMBEDDINGS flag; see
    # _key_point_similarity_with_signal). Bonus-only is deliberate:
    # per-point cosine between a long answer and a short key point is
    # inherently small (length dilution — a strong test answer measured
    # 0.04), so a subtractive blend would systematically penalize
    # thorough answers. The bonus (max +2.0) rewards topical alignment
    # without ever lowering a coverage-based score. Deterministic and
    # rule-based when embed is None.
    coverage, hits = _key_point_coverage(key_points, answer)
    similarity, similarities, signal = _key_point_similarity_with_signal(
        key_points, answer, embed=embed)
    correctness = 40.0 * coverage + min(2.0, 8.0 * similarity)
    if len(words) < 20:
        correctness *= 0.4  # too thin to judge
    correctness = round(min(40.0, correctness), 1)

    # --- communication (25) ---
    filler_ratio, filler_hits = _filler_penalty(answer)
    sentences = [s for s in re.split(r"[.!?]+", answer) if s.strip()]
    comm = 25.0 * (1.0 - 0.7 * filler_ratio)
    if sentences:
        avg_len = len(words) / len(sentences)
        if avg_len > 45:
            comm -= 5  # run-on sentences
        if len(sentences) == 1 and len(words) > 30:
            comm -= 4  # wall of text
    if len(words) < 20:
        comm *= 0.5
    comm = round(max(0.0, min(25.0, comm)), 1)

    # --- depth (20) ---
    depth_ratio, depth_found = _signal_ratio(answer, DEPTH_SIGNALS)
    has_number = bool(re.search(r"\d", answer))
    depth = 20.0 * depth_ratio + (3 if has_number else 0)
    if len(words) < 20:
        depth *= 0.5
    depth = round(min(20.0, depth), 1)

    # --- edge cases / completeness (15) ---
    edge_ratio, edge_found = _signal_ratio(answer, EDGE_SIGNALS)
    edge = round(15.0 * edge_ratio, 1)

    total = round(min(100.0, correctness + comm + depth + edge), 1)
    out = {
        "question_id": question.get("id"),
        "scores": {
            "correctness": correctness,
            "communication": comm,
            "depth": depth,
            "edge_cases": edge,
        },
        "total": total,
        "key_points_hit": hits,
        "key_points_total": len(key_points),
        "key_point_similarity": round(similarity, 4),
        "key_point_similarities": similarities,
        "filler_hits": filler_hits,
        "word_count": len(words),
        "depth_signals": depth_found,
        "edge_signals": edge_found,
    }
    if embed is not None:
        out["similarity_signal"] = signal
        out["similarity_method"] = "gemini" if signal == "embeddings" else "tfidf"
    return out


def generate_rule_based_feedback(
    scored: dict[str, Any],
    question: dict[str, Any],
) -> dict[str, list[str]]:
    """Strengths + improvements from the rubric, no LLM needed."""
    s = scored["scores"]
    strengths, improvements = [], []
    if s["correctness"] >= 28:
        strengths.append("Strong coverage of the ideal answer's key points.")
    elif s["correctness"] >= 16:
        improvements.append(
            "Hit more of the key points: " +
            "; ".join((question.get("key_points") or [])[:3]))
    else:
        improvements.append(
            "Restructure around the key points: " +
            "; ".join((question.get("key_points") or [])[:3]))
    if s["communication"] >= 18:
        strengths.append("Clear, filler-light delivery.")
    else:
        improvements.append(
            "Tighten delivery: cut fillers (%s) and break run-on sentences."
            % (", ".join(scored["filler_hits"][:4]) or "none detected"))
    if s["depth"] >= 14:
        strengths.append("Good depth with specifics and reasoning.")
    else:
        improvements.append(
            "Add depth: quantify outcomes, name trade-offs, explain 'why'.")
    if s["edge_cases"] >= 10:
        strengths.append("Nice attention to edge cases and completeness.")
    else:
        improvements.append(
            "Discuss edge cases, complexity, and alternatives explicitly.")
    if scored["word_count"] < 20:
        improvements.append("Answer is too thin - aim for a structured 60-120 word response.")
    return {"strengths": strengths, "improvements": improvements}


def pick_questions(
    company: str,
    role: str,
    category: str,
    n: int = 5,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """Select n questions matching the filters (randomized, seedable)."""
    bank = _load_bank()
    pool = [q for q in bank
            if company in q["companies"] and role in q["roles"]
            and q["category"] == category]
    if len(pool) < n:
        # fall back: relax company, keep role+category
        pool = [q for q in bank if role in q["roles"] and q["category"] == category]
    if len(pool) < n:
        # fall back: category only
        pool = [q for q in bank if q["category"] == category]
    rng = random.Random(seed)
    return rng.sample(pool, min(n, len(pool)))


def validate_interview_setup(
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate the setup form. Returns (cleaned, error)."""
    company = (payload.get("company") or "").strip()
    role = (payload.get("role") or "").strip()
    category = (payload.get("category") or "").strip()
    try:
        n = int(payload.get("num_questions") or 5)
    except (TypeError, ValueError):
        return None, "Number of questions must be an integer."
    if not (1 <= n <= 10):
        return None, "Number of questions must be between 1 and 10."
    opts = interview_options()
    if company not in opts["companies"]:
        return None, "Unknown company selected."
    if role not in opts["roles"]:
        return None, "Unknown role selected."
    if category not in opts["categories"]:
        return None, "Unknown category selected."
    return {"company": company, "role": role, "category": category, "n": n}, None


# ================= Feature 11 Ultimate Enhancements =================
# 11.1 Voice interview prep | 11.2 Company intel cards | 11.3 Redo loop

import re as _re5


def _clean5(text: str | None) -> str:
    return _re5.sub(r"<[^>]*>", "", text or "")


def voice_interview_prep(
    question: dict[str, Any],
    answer_text: str | None,
    seconds_taken: float,
) -> dict[str, Any]:
    """Spoken-answer simulator: score content + delivery pacing.

    question: dict with question text. answer_text: what they said (transcribed).
    seconds_taken: how long they spoke. Returns {wpm, pacing verdict,
    content score (via score_answer), delivery tips}.
    Ideal speaking pace: 130-160 wpm.
    """
    if not isinstance(question, dict):
        raise ValueError("question must be a dict.")
    answer = _clean5(answer_text or "")
    words = len(answer.split())
    try:
        secs = float(seconds_taken)
    except (TypeError, ValueError):
        raise ValueError("seconds_taken must be a number.")
    if secs <= 0:
        raise ValueError("seconds_taken must be positive.")
    wpm = round(words / secs * 60, 1)

    if wpm < 100:
        pacing = "Too slow — you sound unsure. Aim for 130-160 wpm."
    elif wpm <= 170:
        pacing = "Good pace — clear and confident."
    else:
        pacing = "Too fast — slow down, interviewers can't follow."
    tips = []
    if words < 40:
        tips.append("Answer is thin — add an example or a number.")
    if _re5.search(r"\b(um+|uh+|like,)\b", answer, _re5.I):
        tips.append("Filler words detected — pause silently instead.")
    if secs > 180:
        tips.append("Over 3 minutes — tighten to 90 seconds for behavioral questions.")

    try:
        content = score_answer(question, answer)
        content_score = content.get("score", 0)
    except Exception:
        content_score = 0
    return {"wpm": wpm, "word_count": words, "seconds": round(secs, 1),
            "pacing": pacing, "content_score": content_score,
            "delivery_tips": tips,
            "verdict": ("Ready for voice screens." if wpm >= 100 and wpm <= 170
                        and content_score >= 60 and not tips
                        else "Keep practicing — see tips above.")}


def company_intel(company: str | None) -> dict[str, Any]:
    """Interview intel card per company from the question bank.

    Returns {company, total_questions, categories: {cat: count},
    top_roles, sample_questions, focus_tip}.
    """
    company = _clean5(company or "").strip()
    if not company:
        raise ValueError("Company name is required.")
    bank = _load_bank()
    matched = [q for q in bank
               if any(company.lower() in c.lower() for c in q.get("companies", []))]
    if not matched:
        # Fallback: all-questions overview so the card is never empty.
        matched = bank
        note = f"No company-specific data for '{company}' — showing overall trends."
    else:
        note = f"Based on {len(matched)} banked questions for {company}."
    cats = {}
    roles = {}
    for q in matched:
        cats[q.get("category", "General")] = cats.get(q.get("category", "General"), 0) + 1
        for r in q.get("roles", []):
            roles[r] = roles.get(r, 0) + 1
    top_roles = sorted(roles, key=roles.get, reverse=True)[:5]
    samples = [{"question": q.get("question", "")[:120],
                "category": q.get("category", "")} for q in matched[:5]]
    top_cat = max(cats, key=cats.get) if cats else "General"
    return {"company": company, "total_questions": len(matched),
            "categories": cats, "top_roles": top_roles,
            "sample_questions": samples, "note": note,
            "focus_tip": f"{top_cat} questions dominate — master those first."}


def redo_tracker(
    attempts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """Track re-answers of the same question; measure improvement.

    attempts: list of {question_id, score, timestamp?} in chronological order.
    Returns per-question {attempts, first, latest, improvement, trend}.
    """
    if not isinstance(attempts, (list, tuple)):
        raise ValueError("attempts must be a list.")
    by_q = {}
    for a in attempts:
        if not isinstance(a, dict) or not a.get("question_id"):
            continue
        try:
            score = float(a.get("score") or 0)
        except (TypeError, ValueError):
            continue
        by_q.setdefault(str(a["question_id"]), []).append(max(0, min(100, score)))
    out = []
    for qid, scores in by_q.items():
        first, latest = scores[0], scores[-1]
        imp = round(latest - first, 1)
        trend = ("improving" if imp >= 5 else "declining" if imp <= -5 else "flat")
        out.append({"question_id": qid, "attempts": len(scores),
                    "first": first, "latest": latest,
                    "improvement": imp, "trend": trend})
    out.sort(key=lambda x: x["improvement"])
    return {"questions": out, "total_redos": sum(o["attempts"] - 1 for o in out),
            "guidance": "Redo any question until improvement plateaus — that is mastery."}
