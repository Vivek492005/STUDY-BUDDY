"""Communication coach: filler words, pacing, clarity (Wave 2 feature #14).

Analyzes a pasted interview answer / transcript against the committed
filler library (data/filler_words.json, 104 entries) plus structural
metrics: sentence length / pacing, passive voice, hedge density.

Output: 0-100 clarity score with a transparent breakdown, highlighted
problem spots with per-issue suggestions, a rule-based cleaned rewrite,
and practice drills. Gemini (wired in unified_app.py) can polish the
rewrite and drills; the rule-based versions below always work.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

BANK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "filler_words.json")

_bank = None


def _load_bank() -> list[dict[str, Any]]:
    global _bank
    if _bank is None:
        with open(BANK_PATH, encoding="utf-8") as f:
            _bank = json.load(f)
    # longest phrases first so "so basically" matches before "so"
    _bank.sort(key=lambda e: len(e["word"]), reverse=True)
    return _bank


MIN_CHARS = 50
MAX_CHARS = 10000

PASSIVE_RE = re.compile(
    r"\b(was|were|am|is|are|been|be|being)\s+([a-z]+ed|built|written|done|"
    r"made|taken|given|shown|found|led|sent|paid|said|thought|brought)\b",
    re.IGNORECASE)


def _sentences(text: str | None) -> list[str]:
    parts = [s.strip() for s in re.split(r"(?<=[.!?])\s+", (text or "").strip())]
    return [s for s in parts if s]


def _words(text: str | None) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9'\-]*", text or "")


def find_filler_hits(text: str | None) -> list[dict[str, Any]]:
    """Return list of {word, category, severity, alternatives, note, count, positions}."""
    lowered = (text or "").lower()
    hits = []
    for entry in _load_bank():
        w = entry["word"].lower()
        pattern = r"(?<![a-z])" + re.escape(w) + r"(?![a-z])"
        positions = [m.start() for m in re.finditer(pattern, lowered)]
        if positions:
            hits.append({
                "word": entry["word"],
                "category": entry["category"],
                "severity": entry["severity"],
                "alternatives": entry.get("alternatives") or [],
                "note": entry.get("note") or "",
                "count": len(positions),
                "positions": positions[:20],
            })
    # most severe / most frequent first
    hits.sort(key=lambda h: (-h["severity"], -h["count"]))
    return hits


def _rewrite_clean(text: str, hits: list[dict[str, Any]]) -> str:
    """Rule-based cleanup: drop severity-3 fillers, apply first alternatives."""
    out = text
    for h in hits:
        w = h["word"]
        alt = (h["alternatives"] or [""])[0]
        pattern = re.compile(r"(?<![a-zA-Z])" + re.escape(w) + r"(?![a-zA-Z])",
                             re.IGNORECASE)
        if h["severity"] >= 3:
            out = pattern.sub("", out)
        elif alt:
            # replace only the first occurrence to stay conservative
            out = pattern.sub(alt, out, count=1)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"\s+([,.!?;:])", r"\1", out)
    return out.strip()


# ---------------------------------------------------------------------------
# Analyzer v2 dimensions: STAR structure, impact/evidence, confidence,
# delivery pacing and vocabulary. These sit alongside the original
# clarity score (whose formula is unchanged) so a student sees not
# just "how clean" an answer is, but whether it would land.
# ---------------------------------------------------------------------------

QUESTION_TYPES = {
    "general": {"label": "General answer", "target_seconds": (45, 120)},
    "self_intro": {"label": "Self-introduction", "target_seconds": (60, 90)},
    "behavioral": {"label": "Behavioral answer (STAR)", "target_seconds": (90, 150)},
    "hr": {"label": "HR question", "target_seconds": (45, 90)},
    "technical": {"label": "Technical explanation", "target_seconds": (60, 120)},
}

QUESTIONS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "coach_questions.json")

_questions = None


def coach_questions() -> list[dict[str, Any]]:
    """The practice-question bank (data/coach_questions.json)."""
    global _questions
    if _questions is None:
        try:
            with open(QUESTIONS_PATH, encoding="utf-8") as f:
                _questions = json.load(f).get("questions", [])
        except (FileNotFoundError, ValueError):
            _questions = []
    return _questions


def get_coach_question(question_id: str) -> dict[str, Any] | None:
    for q in coach_questions():
        if q.get("id") == question_id:
            return q
    return None


_ACTION_VERBS = {
    "built", "led", "designed", "implemented", "improved", "reduced",
    "increased", "launched", "automated", "debugged", "shipped", "created",
    "developed", "optimized", "optimised", "coordinated", "mentored",
    "analyzed", "analysed", "delivered", "achieved", "resolved", "migrated",
    "refactored", "tested", "documented", "presented", "negotiated",
    "planned", "trained", "organized", "organised", "fixed", "wrote",
    "researched", "proposed", "introduced", "streamlined", "monitored",
    "collaborated", "convinced", "prioritized", "prioritised", "tracked",
    "reviewed", "deployed", "integrated", "identified", "suggested",
}

_SITUATION_MARKERS = (
    "when i", "during", "at my previous", "in my previous", "we had",
    "there was", "there were", "our project", "the project was",
    "back in", "while i was", "in college", "at the time", "we faced",
    "the challenge was", "the problem was", "in my internship",
    "in our team", "the situation",
)
_TASK_MARKERS = (
    "my task", "i was responsible", "responsible for", "i needed to",
    "we needed to", "the goal was", "my goal", "asked me to", "assigned",
    "my role was", "i had to", "we had to", "expected to", "in charge",
)
_RESULT_WORDS = (
    "resulted", "improved", "reduced", "increased", "achieved",
    "successfully", "as a result", "led to", "in the end", "eventually",
    "helped us", "won", "selected", "shipped", "launched", "saved",
    "boosted", "cut down", "brought down",
)
_APOLOGY_MARKERS = (
    "sorry", "i guess", "not sure", "i don't know", "dont know",
    "maybe i", "just wanted", "i just", "only did", "i only",
)
_CERTAINTY_MARKERS = (
    "i will", "i can", "definitely", "clearly", "i decided", "i chose",
    "i am confident", "i ensured", "i made sure",
)
_CRUTCH_WORDS = ("actually", "basically", "literally", "stuff", "things",
                 "like", "right", "you know")
_JARGON_WORDS = ("synergy", "leverage", "paradigm", "bandwidth",
                 "deep dive", "circle back", "touch base", "low-hanging")
_METRIC_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|percent|x\b|times\b|users?\b|customers?\b|"
    r"students?\b|hours?\b|days?\b|weeks?\b|months?\b|rupees?\b|lakhs?\b|"
    r"crores?\b|₹|rs\.?\b|k\b)", re.IGNORECASE)
_STOPWORDS = {
    "that", "this", "with", "from", "have", "were", "was", "been", "then",
    "them", "they", "their", "there", "when", "what", "which", "about",
    "would", "could", "should", "into", "over", "also", "just", "like",
    "because", "really", "very", "much", "many", "some", "more", "most",
    "other", "after", "before", "during", "while", "where", "being",
}


def _marker_count(lowered: str, markers: tuple[str, ...]) -> int:
    return sum(lowered.count(m) for m in markers)


def _star_analysis(lowered: str, sentences: list[str]) -> dict[str, Any]:
    situation = _marker_count(lowered, _SITUATION_MARKERS) > 0
    task = _marker_count(lowered, _TASK_MARKERS) > 0
    i_actions = 0
    for s in sentences:
        toks = [w.lower() for w in _words(s)]
        for i, tok in enumerate(toks[:-1]):
            if tok in ("i",) and toks[i + 1] in _ACTION_VERBS:
                i_actions += 1
    action = i_actions >= 2
    result = _marker_count(lowered, _RESULT_WORDS) > 0 or bool(
        _METRIC_RE.search(lowered))
    parts = {"situation": situation, "task": task,
             "action": action, "result": result}
    missing = [name for name, ok in parts.items() if not ok]
    return {"parts": parts, "missing": missing,
            "action_verb_count": i_actions,
            "score": round(25 * (4 - len(missing)))}


def _impact_analysis(lowered: str, words_lower: list[str]) -> dict[str, Any]:
    metrics = [m.group(0) for m in _METRIC_RE.finditer(lowered)][:10]
    verbs = sum(1 for w in words_lower if w in _ACTION_VERBS)
    i_count = words_lower.count("i")
    we_count = words_lower.count("we")
    ownership = round(i_count / (i_count + we_count), 2) \
        if (i_count + we_count) else 0.0
    score = min(40, len(metrics) * 10) + min(30, verbs * 3) \
        + round(ownership * 30)
    return {"metrics_found": metrics, "action_verbs": verbs,
            "ownership_ratio": ownership,
            "score": max(0, min(100, score))}


def _confidence_analysis(lowered: str, hedge_events: int) -> dict[str, Any]:
    apologies = _marker_count(lowered, _APOLOGY_MARKERS)
    certainty = _marker_count(lowered, _CERTAINTY_MARKERS)
    score = 100 - min(60, hedge_events * 6 + apologies * 10) \
        + min(15, certainty * 3)
    return {"apology_markers": apologies, "certainty_markers": certainty,
            "score": max(0, min(100, round(score)))}


def _delivery_analysis(
    n_words: int,
    question_type: str,
    spoken_seconds: int | None = None,
) -> dict[str, Any]:
    est_seconds = round(n_words / 140 * 60)
    lo, hi = QUESTION_TYPES[question_type]["target_seconds"]
    wpm = None
    pace_verdict = None
    if spoken_seconds:
        wpm = round(n_words / spoken_seconds * 60)
        pace_verdict = ("too slow" if wpm < 110 else
                        "rushed" if wpm > 170 else "good pace")
        judge_seconds = spoken_seconds
    else:
        judge_seconds = est_seconds
    if judge_seconds < lo:
        length_verdict = "too short"
    elif judge_seconds > hi:
        length_verdict = "too long"
    else:
        length_verdict = "in range"
    pace_part = 50
    if pace_verdict == "too slow":
        pace_part = 25
    elif pace_verdict == "rushed":
        pace_part = 20
    length_part = {"in range": 50, "too short": 25, "too long": 30}[
        length_verdict]
    return {"estimated_seconds": est_seconds,
            "spoken_seconds": spoken_seconds,
            "wpm": wpm, "pace_verdict": pace_verdict,
            "target_seconds": [lo, hi], "length_verdict": length_verdict,
            "score": pace_part + length_part}


def _vocabulary_analysis(lowered: str, words_lower: list[str]) -> dict[str, Any]:
    n = len(words_lower)
    ttr = round(len(set(words_lower)) / n, 3) if n else 0.0
    counts = {}
    for w in words_lower:
        if len(w) >= 4 and w not in _STOPWORDS:
            counts[w] = counts.get(w, 0) + 1
    repeated = sorted(({"word": w, "count": c} for w, c in counts.items()
                         if c >= 3), key=lambda d: -d["count"])[:5]
    crutches = sum(lowered.count(w) for w in _CRUTCH_WORDS)
    jargon = sum(lowered.count(w) for w in _JARGON_WORDS)
    if ttr >= 0.55:
        base = 100
    elif ttr <= 0.30:
        base = 40
    else:
        base = round(40 + (ttr - 0.30) / 0.25 * 60)
    score = max(0, base - min(20, crutches * 2) - min(15, jargon * 5))
    return {"unique_ratio": ttr, "repeated_words": repeated,
            "crutch_count": crutches, "jargon_count": jargon,
            "score": score}


def _dimension_suggestions(
    dims: dict[str, dict[str, Any]],
    question_type: str,
) -> list[str]:
    out = []
    star = dims["star"]
    if star["missing"] and (question_type == "behavioral"
                            or star["score"] < 100):
        pretty = {"situation": "the Situation (set the scene)",
                  "task": "your Task (what YOU were responsible for)",
                  "action": "your Actions (use 'I built / I led…')",
                  "result": "the Result (end with the outcome, ideally a number)"}
        out.append("STAR check — your answer is missing "
                   + ", ".join(pretty[m] for m in star["missing"]) + ".")
    impact = dims["impact"]
    if not impact["metrics_found"]:
        out.append("Add at least one number — a %, a count, a time saved. "
                   "Interviewers remember quantified results.")
    if impact["ownership_ratio"] and impact["ownership_ratio"] < 0.4:
        out.append("You say 'we' a lot — switch to 'I' for your own "
                   "actions so your contribution is visible.")
    delivery = dims["delivery"]
    if delivery["pace_verdict"] == "rushed":
        out.append(f"You spoke at {delivery['wpm']} wpm — slow down; "
                   "130-150 wpm lands best.")
    elif delivery["pace_verdict"] == "too slow":
        out.append(f"You spoke at {delivery['wpm']} wpm — pick up the "
                   "pace a little; 130-150 wpm sounds energetic.")
    if delivery["length_verdict"] == "too short":
        out.append("This answer is short for its question type — add one "
                   "concrete example before you finish.")
    elif delivery["length_verdict"] == "too long":
        out.append("This answer runs long — cut background detail and get "
                   "to your actions sooner.")
    vocab = dims["vocabulary"]
    if vocab["repeated_words"]:
        top = vocab["repeated_words"][0]
        out.append(f"You repeat \u201c{top['word']}\u201d {top['count']}x — "
                   "vary the word or cut a few uses.")
    if dims["confidence"]["apology_markers"] >= 2:
        out.append("Drop the apologies and qualifiers ('sorry', 'I guess', "
                   "'just') — state your points plainly.")
    return out


def analyze_transcript(
    text: str | None,
    question_type: str | None = None,
    spoken_seconds: int | None = None,
) -> dict[str, Any]:
    """Full analysis. Returns a JSON-serializable report dict."""
    text = (text or "").strip()
    if question_type not in QUESTION_TYPES:
        question_type = "general"
    words = _words(text)
    sentences = _sentences(text)
    n_words = len(words)
    n_sent = len(sentences)

    filler_hits = find_filler_hits(text)
    filler_events = sum(h["count"] for h in filler_hits)
    filler_weighted = sum(h["count"] * h["severity"] for h in filler_hits)
    filler_per_100 = (filler_weighted / max(1, n_words)) * 100

    long_sentences = [s for s in sentences if len(_words(s)) > 30]
    avg_len = round(n_words / n_sent, 1) if n_sent else 0.0

    passive_hits = [{"match": m.group(0), "position": m.start()}
                    for m in PASSIVE_RE.finditer(text)][:20]

    hedge_hits = [h for h in filler_hits if h["category"] == "hedge"]
    hedge_events = sum(h["count"] for h in hedge_hits)

    # --- clarity score 0-100 ---
    # filler load 40 / pacing 25 / voice & hedges 20 / structure 15
    filler_score = max(0.0, 40.0 - min(40.0, filler_per_100 * 2.0))
    pacing_score = 25.0
    if n_sent:
        if avg_len > 32:
            pacing_score -= 12
        elif avg_len > 24:
            pacing_score -= 6
        if len(long_sentences) >= 3:
            pacing_score -= 7
    pacing_score = max(0.0, pacing_score)
    voice_score = 20.0 - min(20.0, len(passive_hits) * 2.5 + hedge_events * 1.5)
    voice_score = max(0.0, voice_score)
    structure_score = 15.0
    if n_sent < 2 and n_words > 40:
        structure_score -= 8  # wall of text
    if n_words < 60:
        structure_score -= 5  # too thin to judge structure
    structure_score = max(0.0, structure_score)
    clarity = round(filler_score + pacing_score + voice_score + structure_score, 1)

    cleaned = _rewrite_clean(text, filler_hits)

    # --- practice drills: 5 worst sentences, each with a target rewrite ---
    drills = []
    for s in sentences:
        s_hits = find_filler_hits(s)
        score = sum(h["count"] * h["severity"] for h in s_hits)
        if len(_words(s)) > 30:
            score += 4
        if score > 0:
            cleaned_sentence = _rewrite_clean(s, s_hits)
            drills.append({
                "sentence": s,
                "issues": [h["word"] for h in s_hits][:5],
                "after": cleaned_sentence if cleaned_sentence != s else "",
            })
    drills.sort(key=lambda d: -sum(1 for _ in d["issues"]))
    drills = drills[:5]

    suggestions = []
    for h in filler_hits[:8]:
        alt = (h["alternatives"] or [""])[0]
        if alt:
            suggestions.append(f"Replace \u201c{h['word']}\u201d with \u201c{alt}\u201d "
                               f"({h['count']}x). {h['note']}".strip())
        else:
            suggestions.append(f"Delete \u201c{h['word']}\u201d ({h['count']}x). "
                               f"{h['note']}".strip())
    if long_sentences:
        suggestions.append(f"Break up {len(long_sentences)} run-on sentence(s) over 30 words "
                           "- one idea per sentence.")
    if passive_hits:
        suggestions.append(f"Rewrite {len(passive_hits)} passive construction(s) in active voice "
                           "('was built by the team' -> 'the team built').")
    if hedge_events >= 3:
        suggestions.append("You hedge a lot - replace 'I think / maybe / probably' with "
                           "evidence or a clear stance.")

    # --- v2 dimensions ---
    lowered = text.lower()
    words_lower = [w.lower() for w in words]
    dimensions = {
        "star": _star_analysis(lowered, sentences),
        "impact": _impact_analysis(lowered, words_lower),
        "confidence": _confidence_analysis(lowered, hedge_events),
        "delivery": _delivery_analysis(n_words, question_type,
                                       spoken_seconds),
        "vocabulary": _vocabulary_analysis(lowered, words_lower),
    }
    suggestions = (suggestions
                   + _dimension_suggestions(dimensions, question_type))[:12]

    return {
        "clarity_score": clarity,
        "breakdown": {
            "filler_load": round(filler_score, 1),
            "pacing": round(pacing_score, 1),
            "voice_and_hedges": round(voice_score, 1),
            "structure": round(structure_score, 1),
        },
        "metrics": {
            "word_count": n_words,
            "sentence_count": n_sent,
            "avg_words_per_sentence": avg_len,
            "long_sentences": len(long_sentences),
            "filler_events": filler_events,
            "filler_per_100_words": round(filler_per_100, 1),
            "passive_constructions": len(passive_hits),
            "hedge_events": hedge_events,
        },
        "filler_hits": [{k: h[k] for k in ("word", "category", "severity",
                                           "alternatives", "note", "count",
                                           "positions")}
                        for h in filler_hits[:25]],
        "passive_hits": passive_hits[:10],
        "suggestions": suggestions,
        "cleaned_version": cleaned,
        "drills": drills,
        "question_type": question_type,
        "dimensions": dimensions,
    }


def validate_coach_inputs(
    payload: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Validate the analyze form. Returns (text, error)."""
    text = (payload.get("text") or "").strip()
    if len(text) < MIN_CHARS:
        return None, f"Please paste at least {MIN_CHARS} characters of your answer."
    if len(text) > MAX_CHARS:
        return None, f"Please keep your answer under {MAX_CHARS} characters."
    return text, None


def validate_coach_extras(
    payload: dict[str, Any] | None,
) -> tuple[str | None, dict[str, Any] | None, int | None, str | None]:
    """Validate the optional v2 fields of an analyze request.

    Returns (question_type, question, spoken_seconds, error). When a
    bank question is chosen, its own type drives the scoring targets.
    """
    payload = payload or {}
    question = None
    qid = (payload.get("question_id") or "").strip()
    if qid:
        question = get_coach_question(qid)
        if question is None:
            return None, None, None, "Unknown practice question."
    qtype = (payload.get("question_type") or "general").strip().lower()
    if question is not None:
        qtype = question.get("type") or qtype
    if qtype not in QUESTION_TYPES:
        return None, None, None, "Unknown question type."
    spoken = payload.get("spoken_seconds")
    if spoken is not None and spoken != "":
        try:
            spoken = int(spoken)
        except (TypeError, ValueError):
            return None, None, None, "Speaking time must be a number of seconds."
        if spoken < 5 or spoken > 1800:
            return None, None, None, "Speaking time must be between 5 seconds and 30 minutes."
    else:
        spoken = None
    return qtype, question, spoken, None


# ================= Feature 13 Ultimate Enhancements =================
# 13.1 Video presence checklist | 13.2 HR-round simulator | 13.3 Clarity score

import re as _re7


def _clean7(text: str | None) -> str:
    return _re7.sub(r"<[^>]*>", "", text or "")


# Video presence rubric: what the camera sees.
_VIDEO_CHECKS = [
    ("eye_contact", "Look at the camera, not your own video tile", 20),
    ("framing", "Head + shoulders visible, centered, well-lit face", 15),
    ("background", "Clean, uncluttered background (or subtle blur)", 15),
    ("posture", "Sit upright, lean slightly forward — shows engagement", 15),
    ("gestures", "Hands visible occasionally; avoid fidgeting", 10),
    ("audio", "Clear mic, no echo; test before the call", 15),
    ("attire", "Dress one level above the company's daily wear", 10),
]


def video_presence_checklist(
    self_scores: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Self-scored video presence checklist for AI video screens.

    self_scores: optional dict {check_id: True/False}. Returns {checks,
    score, priority_fixes}.
    """
    self_scores = self_scores or {}
    checks = []
    total = 0
    fixes = []
    for cid, label, pts in _VIDEO_CHECKS:
        done = bool(self_scores.get(cid)) if isinstance(self_scores, dict) else False
        earned = pts if done else 0
        total += earned
        checks.append({"id": cid, "label": label, "points": earned, "max": pts,
                       "done": done})
        if not done:
            fixes.append((pts, label))
    fixes.sort(reverse=True)
    return {"checks": checks, "score": total,
            "priority_fixes": [f[1] for f in fixes[:3]],
            "verdict": ("Camera-ready." if total >= 85 else
                        "Almost there — fix the top priority." if total >= 60 else
                        "Do a dry run before any real interview.")}


# HR round: classic questions with model answer frameworks.
_HR_QUESTIONS = [
    {"id": "tell_me", "question": "Tell me about yourself.",
     "framework": "Present → Past → Future (60-90 seconds).",
     "model": ("I am a {role} with {n} years building {what}. Recently I {win}. "
               "I am excited about this role because {why_company}."),
     "tips": ["End with why THIS company", "No life story — stay professional",
              "Quantify one win"]},
    {"id": "why_us", "question": "Why do you want to work here?",
     "framework": "Specific → Personal → Forward.",
     "model": ("Three reasons: your {specific_thing} (I used it in {project}), "
               "the team's focus on {value}, and the growth path for {role}s here."),
     "tips": ["Name something specific — never 'great culture'",
              "Connect it to YOUR work", "Show you researched"]},
    {"id": "strength_weakness", "question": "What is your greatest weakness?",
     "framework": "Real weakness → What you're doing → Progress.",
     "model": ("I used to {weakness}. I have been {action} — for example, {example}. "
               "It is still a work in progress, but {progress}."),
     "tips": ["Pick a real, non-fatal weakness", "Show the fix in action",
              "Never say 'I am a perfectionist'"]},
    {"id": "why_leave", "question": "Why are you leaving your current role?",
     "framework": "Grateful → Growth → Forward.",
     "model": ("I have learned a lot at {current} — especially {skill}. "
               "I am looking for {growth}, which this role offers through {specific}."),
     "tips": ["Never badmouth", "Make it about growth, not escape",
              "Tie to this specific role"]},
    {"id": "salary_expect", "question": "What are your salary expectations?",
     "framework": "Deflect → Range → Flexible.",
     "model": ("I would rather focus on fit first. Based on market data, roles like this "
               "are typically ₹{lo}-{hi} LPA — I am flexible for the right opportunity."),
     "tips": ["Give a range, not a number", "Anchor on market data",
              "Signal flexibility"]},
]


def hr_round_simulator(question_id: str | None = None) -> dict[str, Any]:
    """HR-round practice: questions + frameworks + model answers.

    question_id: specific one, or None for the full set.
    """
    if question_id:
        qid = _clean7(question_id).strip().lower()
        for q in _HR_QUESTIONS:
            if q["id"] == qid:
                return {"question": dict(q), "count": 1}
        raise ValueError(f"Unknown HR question '{question_id}'.")
    return {"questions": [dict(q) for q in _HR_QUESTIONS],
            "count": len(_HR_QUESTIONS),
            "guidance": "Practice each aloud in 90 seconds. Record yourself once."}


def clarity_score(transcript: str | None) -> dict[str, Any]:
    """Accent-clarity score for Indian English speakers (0-100).

    Checks: pace (via analyze_transcript if available), sentence length,
    filler density, jargon density, repetition. Never raises on hostile input.
    """
    text = _clean7(transcript or "")
    words = _words(text)
    n = len(words)
    if n == 0:
        return {"score": 0, "grade": "F", "signals": [], "fixes": ["No speech detected."]}
    signals = []
    total = 100.0

    # Sentence length: long sentences hurt clarity.
    sents = _sentences(text)
    avg_len = n / max(len(sents), 1)
    if avg_len > 25:
        total -= 15
        signals.append({"signal": "long_sentences", "effect": -15,
                        "detail": f"Avg {avg_len:.0f} words/sentence — break them up."})
    else:
        signals.append({"signal": "good_sentence_length", "effect": 0,
                        "detail": f"Avg {avg_len:.0f} words/sentence."})

    # Filler density.
    try:
        fillers = find_filler_hits(text)
        filler_rate = len(fillers) / n * 100
    except Exception:
        filler_rate = 0
    if filler_rate > 5:
        total -= 15
        signals.append({"signal": "high_filler_rate", "effect": -15,
                        "detail": f"{filler_rate:.1f}% filler words — pause instead."})
    elif filler_rate > 2:
        total -= 5
        signals.append({"signal": "some_fillers", "effect": -5,
                        "detail": f"{filler_rate:.1f}% filler words."})

    # Repetition: same word 3+ times in a row or overused crutch words.
    crutch = _re7.findall(r"\b(actually|basically|literally|stuff|things)\b", text, _re7.I)
    if len(crutch) > 3:
        total -= 10
        signals.append({"signal": "crutch_words", "effect": -10,
                        "detail": f"{len(crutch)} crutch words (actually/basically/...)."})

    # Jargon density: too much unexplained jargon.
    jargon = _re7.findall(r"\b(synergy|leverage|paradigm|bandwidth|deep dive|circle back)\b",
                          text, _re7.I)
    if jargon:
        total -= 5 * min(len(jargon), 3)
        signals.append({"signal": "jargon", "effect": -5 * min(len(jargon), 3),
                        "detail": f"{len(jargon)} jargon phrases — use plain words."})

    total = max(0, min(100, round(total, 1)))
    grade = ("A — crystal clear" if total >= 85 else
             "B — clear" if total >= 70 else
             "C — needs work" if total >= 50 else
             "D — hard to follow")
    fixes = [s["detail"] for s in signals if s["effect"] < 0][:4]
    return {"score": total, "grade": grade, "signals": signals,
            "fixes": fixes or ["Clear speech — keep it up."],
            "word_count": n}
