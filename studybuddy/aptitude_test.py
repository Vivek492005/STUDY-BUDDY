"""Timed aptitude & reasoning test engine (Wave 2 feature #16).

Backed by the committed bank data/aptitude_questions.json (338 questions
across quantitative / logical / verbal). Scoring is fully server-side;
the client timer is display-only — the server validates the time window
from its own start timestamp.
"""

from __future__ import annotations

import json
import os
import random
from typing import Any

BANK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "aptitude_questions.json")

CATEGORIES = ("quantitative", "logical", "verbal")
DIFFICULTIES = ("Easy", "Medium", "Hard")

# Grace period (seconds) after the nominal limit before a submission is
# treated as expired — absorbs network latency, not extra thinking time.
SUBMIT_GRACE_SEC = 60

_bank = None
_by_id = None


def _load() -> list[dict[str, Any]]:
    global _bank, _by_id
    if _bank is None:
        with open(BANK_PATH, encoding="utf-8") as f:
            _bank = json.load(f)
        _by_id = {q["id"]: q for q in _bank}
    return _bank


def all_questions() -> list[dict[str, Any]]:
    return _load()


def get_question(qid: str) -> dict[str, Any] | None:
    _load()
    return _by_id.get(qid)


def categories() -> list[tuple[str, int]]:
    """[(category, count)] for the setup page."""
    counts = {}
    for q in _load():
        counts[q["category"]] = counts.get(q["category"], 0) + 1
    return [(c, counts.get(c, 0)) for c in CATEGORIES]


# ---------------------------------------------------------------------------
# Tracks, topics, company mocks, concepts, mistakes (expansion)
# ---------------------------------------------------------------------------

TRACKS = {
    "logical": {
        "label": "Logical Reasoning & Problem Solving",
        "blurb": "Series, puzzles, blood relations, seating, data "
                 "interpretation — plus the quantitative problem solving "
                 "(percentages, time & work, speed, probability) that "
                 "fills company aptitude papers.",
        "categories": ("logical", "quantitative"),
    },
    "verbal": {
        "label": "Verbal Ability",
        "blurb": "Grammar (error spotting, correction, voice, narration, "
                 "para jumbles) and vocabulary (synonyms, antonyms, "
                 "idioms, one-word substitutions) with reading "
                 "comprehension.",
        "categories": ("verbal",),
    },
}

CONCEPTS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "aptitude_concepts.json")

_concepts = None


def track_of_category(category: str) -> str | None:
    for name, meta in TRACKS.items():
        if category in meta["categories"]:
            return name
    return None


def track_counts() -> list[tuple[str, str, int]]:
    """[(track, label, question_count)] for the two tracks."""
    counts = {name: 0 for name in TRACKS}
    for q in _load():
        t = track_of_category(q["category"])
        if t:
            counts[t] += 1
    return [(name, TRACKS[name]["label"], counts[name]) for name in TRACKS]


def topics(track: str | None = None) -> list[tuple[str, str, int]]:
    """[(subcategory, category, count)] — optionally for one track only."""
    counts = {}
    for q in _load():
        if track and track_of_category(q["category"]) != track:
            continue
        key = (q["subcategory"], q["category"])
        counts[key] = counts.get(key, 0) + 1
    return sorted((sub, cat, n) for (sub, cat), n in counts.items())


def topic_category(subcategory: str) -> str | None:
    for q in _load():
        if q["subcategory"] == subcategory:
            return q["category"]
    return None


# Company mock blueprints: scaled-down versions of the real papers'
# section mixes (foundation rounds), with a matching time budget.
COMPANY_TESTS = {
    "TCS NQT": {
        "blueprint": {"quantitative": 10, "logical": 10, "verbal": 10},
        "time_limit": 1800,
        "note": "Foundation pattern — numerical, reasoning and verbal "
                "in equal measure.",
    },
    "Infosys": {
        "blueprint": {"logical": 10, "quantitative": 8, "verbal": 7},
        "time_limit": 1500,
        "note": "Reasoning-heavy: puzzles, series and data sufficiency "
                "style problem solving.",
    },
    "Accenture": {
        "blueprint": {"verbal": 10, "logical": 10, "quantitative": 5},
        "time_limit": 1500,
        "note": "English comprehension and abstract reasoning lead; "
                "quant is lighter.",
    },
    "Wipro": {
        "blueprint": {"quantitative": 8, "logical": 8, "verbal": 9},
        "time_limit": 1500,
        "note": "Balanced paper with a strong verbal section.",
    },
    "Cognizant": {
        "blueprint": {"logical": 10, "quantitative": 8, "verbal": 7},
        "time_limit": 1500,
        "note": "Aptitude + reasoning with quick verbal checks.",
    },
    "Capgemini": {
        "blueprint": {"quantitative": 8, "logical": 9, "verbal": 8},
        "time_limit": 1500,
        "note": "Game-based reasoning style: patterns, deductions, "
                "data interpretation.",
    },
}


def concepts() -> list[dict[str, Any]]:
    global _concepts
    if _concepts is None:
        try:
            with open(CONCEPTS_PATH, encoding="utf-8") as f:
                _concepts = json.load(f).get("concepts", [])
        except (FileNotFoundError, ValueError):
            _concepts = []
    return _concepts


def get_concept(topic: str) -> dict[str, Any] | None:
    for c in concepts():
        if c.get("topic") == topic:
            return c
    return None


def check_answer(
    question_id: str,
    chosen: int,
) -> dict[str, Any] | None:
    """Practice-mode instant check. Returns a result dict or None."""
    q = get_question(question_id)
    if q is None:
        return None
    try:
        chosen = int(chosen)
    except (TypeError, ValueError):
        chosen = None
    return {
        "id": q["id"],
        "correct": chosen == q["answer"],
        "correct_index": q["answer"],
        "explanation": q["explanation"],
        "trick": q.get("trick") or "",
    }


def mistakes_from_attempts(
    results_history: list[list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Latest state per question across a user's attempts.

    results_history: list of per-attempt results lists, NEWEST first
    (as produced by score_attempt). A question counts as a mistake
    when its most recent appearance was answered wrong or skipped.
    Returns (wrong_results, by_subcategory_counts).
    """
    latest = {}
    for results in results_history:
        for r in results or []:
            if r["id"] not in latest:
                latest[r["id"]] = r
    wrong = [r for r in latest.values() if not r.get("correct")]
    by_sub = {}
    for r in wrong:
        by_sub[r["subcategory"]] = by_sub.get(r["subcategory"], 0) + 1
    return wrong, by_sub


def mastery_from_attempts(
    results_history: list[list[dict[str, Any]]],
) -> dict[str, dict[str, float]]:
    """Per-subcategory accuracy across attempts (all appearances)."""
    agg = {}
    for results in results_history:
        for r in results or []:
            b = agg.setdefault(r["subcategory"],
                               {"total": 0, "correct": 0})
            b["total"] += 1
            if r.get("correct"):
                b["correct"] += 1
    for b in agg.values():
        b["pct"] = round(b["correct"] / b["total"] * 100, 1) if b["total"] else 0.0
    return agg


def subcategories(category: str) -> list[tuple[str, int]]:
    counts = {}
    for q in _load():
        if q["category"] == category:
            s = q["subcategory"]
            counts[s] = counts.get(s, 0) + 1
    return sorted(counts.items())


def public_question(q: dict[str, Any]) -> dict[str, Any]:
    """Client-safe view: statement + options, no answer/explanation."""
    return {
        "id": q["id"],
        "category": q["category"],
        "subcategory": q["subcategory"],
        "difficulty": q["difficulty"],
        "statement": q["statement"],
        "options": q["options"],
        "companies": q.get("companies") or [],
    }


def pick_questions(
    category: str | None = None,
    n: int = 10,
    seed: int | None = None,
    subcategory: str | None = None,
    track: str | None = None,
    difficulty: str | None = None,
) -> list[dict[str, Any]]:
    """Pick n questions (shuffled). category=None/'mixed' draws evenly."""
    pool = _load()
    if track in TRACKS:
        cats = TRACKS[track]["categories"]
        pool = [q for q in pool if q["category"] in cats]
    elif category and category != "mixed":
        pool = [q for q in pool if q["category"] == category]
    if subcategory:
        pool = [q for q in pool if q["subcategory"] == subcategory]
    if difficulty in DIFFICULTIES:
        pool = [q for q in pool if q["difficulty"] == difficulty]
    rnd = random.Random(seed)
    picked = rnd.sample(pool, min(n, len(pool)))
    rnd.shuffle(picked)
    return [public_question(q) for q in picked]


def selection_counts() -> dict[str, Any]:
    """Exact pool sizes for the custom-test builder UI.

    Returns {"tracks": {track: {"total": n, "Easy": n, ...}},
             "topics": {subcategory: {"total": n, "Easy": n, ...,
                                      "track": track, "category": cat}}}.
    """
    tracks = {}
    topics_map = {}
    for q in _load():
        track = track_of_category(q["category"])
        t = tracks.setdefault(track, {"total": 0})
        t["total"] += 1
        t[q["difficulty"]] = t.get(q["difficulty"], 0) + 1
        s = topics_map.setdefault(
            q["subcategory"],
            {"total": 0, "track": track, "category": q["category"]})
        s["total"] += 1
        s[q["difficulty"]] = s.get(q["difficulty"], 0) + 1
    return {"tracks": tracks, "topics": topics_map}


def pick_for_setup(
    cleaned: dict[str, Any],
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """Materialize the public question list for a validated setup."""
    mode = cleaned.get("mode", "category")
    rnd = random.Random(seed)
    if mode == "retest":
        by_id = {q["id"]: q for q in _load()}
        picked = [by_id[qid] for qid in cleaned["question_ids"]
                  if qid in by_id]
        rnd.shuffle(picked)
        return [public_question(q) for q in picked]
    if mode == "company":
        preset = COMPANY_TESTS[cleaned["company"]]
        picked = []
        for cat, count in preset["blueprint"].items():
            picked.extend(pick_questions(cat, n=count,
                                         seed=rnd.randint(0, 2 ** 31 - 1)))
        rnd.shuffle(picked)
        return picked
    if mode == "topic":
        return pick_questions(cleaned["category"], n=cleaned["n"],
                              seed=seed, subcategory=cleaned["subcategory"],
                              difficulty=cleaned.get("difficulty"))
    if mode == "track":
        return pick_questions(n=cleaned["n"], seed=seed,
                              track=cleaned["track"],
                              difficulty=cleaned.get("difficulty"))
    return pick_questions(cleaned["category"], n=cleaned["n"], seed=seed,
                          difficulty=cleaned.get("difficulty"))


def _setup_time_limit(
    payload: dict[str, Any],
    default: int,
) -> tuple[int | None, str | None]:
    try:
        limit = int(payload.get("time_limit") or default)
    except (TypeError, ValueError):
        return None, "Time limit must be a number."
    if not 60 <= limit <= 3600:
        return None, "Time limit must be between 1 and 60 minutes."
    return limit, None


def _setup_n(payload: dict[str, Any]) -> tuple[int | None, str | None]:
    try:
        n = int(payload.get("num_questions") or 0)
    except (TypeError, ValueError):
        return None, "Question count must be a number."
    if not 5 <= n <= 50:
        return None, "Question count must be between 5 and 50."
    return n, None


def _setup_difficulty(
    payload: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Optional difficulty filter: (value_or_None, error)."""
    raw = str(payload.get("difficulty") or "").strip()
    if not raw or raw.lower() == "mixed":
        return None, None
    for d in DIFFICULTIES:
        if raw.lower() == d.lower():
            return d, None
    return None, "Unknown difficulty."


def validate_test_setup(
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Return (cleaned, error).

    Modes: legacy category test; track test; single-topic test;
    company mock (blueprint); mistake retest (explicit question_ids).
    Category/track/topic modes accept an optional difficulty filter.
    """
    if not isinstance(payload, dict):
        return None, "Invalid request."
    difficulty, derr = _setup_difficulty(payload)
    if derr:
        return None, derr

    qids = payload.get("question_ids")
    if qids:
        if not isinstance(qids, list) or not 1 <= len(qids) <= 50:
            return None, "Pick between 1 and 50 questions to retest."
        _load()
        unknown = [q for q in qids if q not in _by_id]
        if unknown:
            return None, "Some questions no longer exist."
        limit, err = _setup_time_limit(payload, 60 * len(qids))
        if err:
            return None, err
        return {"mode": "retest", "category": "mistakes",
                "question_ids": list(qids), "n": len(qids),
                "time_limit": limit}, None

    company = str(payload.get("company") or "").strip()
    if company:
        if company not in COMPANY_TESTS:
            return None, "Unknown company test."
        preset = COMPANY_TESTS[company]
        return {"mode": "company", "category": f"company:{company}",
                "company": company,
                "n": sum(preset["blueprint"].values()),
                "time_limit": preset["time_limit"]}, None

    topic = str(payload.get("topic") or "").strip().lower()
    if topic:
        cat = topic_category(topic)
        if cat is None:
            return None, "Unknown topic."
        n, err = _setup_n(payload)
        if err:
            return None, err
        limit, err = _setup_time_limit(payload, 60 * n)
        if err:
            return None, err
        return {"mode": "topic", "category": cat, "subcategory": topic,
                "difficulty": difficulty,
                "n": n, "time_limit": limit}, None

    track = str(payload.get("track") or "").strip().lower()
    if track:
        if track not in TRACKS:
            return None, "Unknown track."
        n, err = _setup_n(payload)
        if err:
            return None, err
        limit, err = _setup_time_limit(payload, 60 * n)
        if err:
            return None, err
        return {"mode": "track", "category": f"track:{track}",
                "track": track, "difficulty": difficulty,
                "n": n, "time_limit": limit}, None

    category = str(payload.get("category") or "mixed").strip().lower()
    if category not in CATEGORIES and category != "mixed":
        return None, "Unknown category."
    n, err = _setup_n(payload)
    if err:
        return None, err
    limit, err = _setup_time_limit(payload, 600)
    if err:
        return None, err
    return {"mode": "category", "category": category,
            "difficulty": difficulty, "n": n,
            "time_limit": limit}, None


def score_attempt(
    question_ids: list[str],
    answers: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Server-side scoring. answers: {qid: option_index}.

    Returns (results, summary). results: per-question dicts with
    correctness + correct answer + explanation (safe to show after submit).
    """
    results = []
    correct = 0
    by_cat = {}
    by_diff = {}
    by_sub = {}
    for qid in question_ids:
        q = get_question(qid)
        if q is None:
            continue
        chosen = answers.get(qid)
        try:
            chosen = int(chosen)
        except (TypeError, ValueError):
            chosen = None
        ok = chosen == q["answer"]
        if ok:
            correct += 1
        results.append({
            "id": qid,
            "category": q["category"],
            "subcategory": q["subcategory"],
            "difficulty": q["difficulty"],
            "statement": q["statement"],
            "options": q["options"],
            "chosen": chosen,
            "correct_index": q["answer"],
            "correct": ok,
            "explanation": q["explanation"],
            "trick": q.get("trick") or "",
            "companies": q.get("companies") or [],
        })
        for key, bucket in (("category", by_cat), ("difficulty", by_diff),
                            ("subcategory", by_sub)):
            k = q[key]
            b = bucket.setdefault(k, {"total": 0, "correct": 0})
            b["total"] += 1
            if ok:
                b["correct"] += 1

    def _pct(bucket: dict[str, dict[str, int]]) -> dict[str, dict[str, Any]]:
        return {k: {"total": v["total"], "correct": v["correct"],
                    "pct": round(v["correct"] / v["total"] * 100, 1)}
                for k, v in bucket.items()}

    total = len(results)
    summary = {
        "total": total,
        "correct": correct,
        "score_pct": round(correct / total * 100, 1) if total else 0.0,
        "by_category": _pct(by_cat),
        "by_difficulty": _pct(by_diff),
        "by_subcategory": _pct(by_sub),
    }
    weak = [k for k, v in summary["by_category"].items() if v["pct"] < 60]
    summary["weak_areas"] = weak
    summary["weak_topics"] = [k for k, v in summary["by_subcategory"].items()
                              if v["pct"] < 60]
    return results, summary
