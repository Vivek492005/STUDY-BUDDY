"""Skill quiz engine (Wave 3 feature #19).

Backed by the committed bank data/skill_quizzes.json: 320 questions
across 16 skills. Each question: id, skill, topic, difficulty
(easy/medium/hard), question, 4 options, answer index, explanation.

Answers/explanations never leave the server except through the graded
check endpoint. Scoring is fully deterministic.
"""

from __future__ import annotations

import json
import os
import random
from typing import Any

BANK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "skill_quizzes.json")

DIFFICULTIES = ("easy", "medium", "hard")
MAX_QUESTIONS = 25

_bank = None
_by_id = None


def _load() -> list[dict[str, Any]]:
    global _bank, _by_id
    if _bank is None:
        try:
            with open(BANK_PATH, encoding="utf-8") as f:
                _bank = json.load(f)
        except OSError:
            _bank = []
        _by_id = {q["id"]: q for q in _bank if "id" in q}
    return _bank


def _by_id_map() -> dict[str, dict[str, Any]]:
    _load()
    return _by_id


def get_question(qid: str) -> dict[str, Any] | None:
    return _by_id_map().get(qid)


def quiz_skills() -> list[tuple[str, int]]:
    """Sorted [(skill_id, question_count)]."""
    counts = {}
    for q in _load():
        counts[q.get("skill")] = counts.get(q.get("skill"), 0) + 1
    return sorted(counts.items())


def skill_topics(skill: str) -> list[tuple[str, int]]:
    """Sorted [(topic, count)] for one skill."""
    counts = {}
    for q in _load():
        if q.get("skill") == skill:
            t = q.get("topic", "General")
            counts[t] = counts.get(t, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def public_question(q: dict[str, Any]) -> dict[str, Any]:
    """Client-safe question payload (no answer, no explanation)."""
    return {"id": q["id"], "skill": q.get("skill"), "topic": q.get("topic"),
            "difficulty": q.get("difficulty"), "question": q.get("question"),
            "options": list(q.get("options", []))}


def build_quiz(
    skill: str,
    n: int = 10,
    difficulties: list[str] | None = None,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """Pick n questions for a skill, optionally filtered by difficulty.

    Returns list of FULL question dicts (server-side). Use public_question
    before sending to the client.
    """
    pool = [q for q in _load() if q.get("skill") == skill]
    if difficulties:
        want = {d for d in difficulties if d in DIFFICULTIES}
        if want:
            pool = [q for q in pool if q.get("difficulty") in want]
    if not pool:
        return []
    rng = random.Random(seed) if seed is not None else random
    n = max(1, min(int(n or 10), MAX_QUESTIONS, len(pool)))
    # Stratify by difficulty when no filter: round-robin easy/medium/hard
    # so a mixed quiz covers the range.
    if not difficulties:
        by_diff = {d: [q for q in pool if q.get("difficulty") == d]
                   for d in DIFFICULTIES}
        picked, seen = [], set()
        order = ["easy", "medium", "hard"]
        i = 0
        while len(picked) < n and any(by_diff[d] for d in order):
            d = order[i % 3]
            if by_diff[d]:
                q = by_diff[d].pop(rng.randrange(len(by_diff[d])))
                if q["id"] not in seen:
                    seen.add(q["id"])
                    picked.append(q)
            i += 1
            if i > n * 6:  # safety
                break
        # top up from leftovers if stratification ran short
        rest = [q for q in pool if q["id"] not in seen]
        rng.shuffle(rest)
        picked += rest[: max(0, n - len(picked))]
        return picked[:n]
    return rng.sample(pool, n)


def check_answer(
    question: dict[str, Any],
    chosen_index: int,
) -> dict[str, Any]:
    """Grade one answer. Returns {correct, correct_index, explanation}."""
    try:
        chosen = int(chosen_index)
    except (TypeError, ValueError):
        chosen = -1
    correct = chosen == question.get("answer")
    return {"correct": correct,
            "correct_index": question.get("answer"),
            "explanation": question.get("explanation", "")}


def grade_quiz(
    questions: list[dict[str, Any]],
    answers: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Grade a full attempt.

    questions: list of full question dicts (in attempt order).
    answers: {question_id: chosen_index}.
    Returns (results, summary) where results is per-question detail and
    summary has score, percentage, per-difficulty and per-topic accuracy,
    plus weak-topic diagnosis.
    """
    results = []
    for q in questions:
        raw = answers.get(q["id"]) if isinstance(answers, dict) else None
        try:
            chosen = int(raw)
        except (TypeError, ValueError):
            chosen = None
        if chosen is not None and not 0 <= chosen <= 3:
            chosen = None
        chk = check_answer(q, chosen if chosen is not None else -1)
        results.append({
            "question_id": q["id"],
            "skill": q.get("skill"),
            "topic": q.get("topic", "General"),
            "difficulty": q.get("difficulty"),
            "question": q.get("question"),
            "options": list(q.get("options", [])),
            "chosen_index": chosen,
            "correct": chk["correct"],
            "correct_index": chk["correct_index"],
            "explanation": chk["explanation"],
        })
    total = len(results)
    right = sum(1 for r in results if r["correct"])
    by_diff, by_topic = {}, {}
    for r in results:
        d = by_diff.setdefault(r["difficulty"], [0, 0])
        d[r["correct"]] += 1
        t = by_topic.setdefault(r["topic"], [0, 0])
        t[r["correct"]] += 1
    diff_acc = {d: {"correct": v[1], "total": v[0] + v[1],
                    "pct": round(v[1] / (v[0] + v[1]) * 100) if (v[0] + v[1]) else 0}
                for d, v in by_diff.items()}
    topic_acc = {t: {"correct": v[1], "total": v[0] + v[1],
                     "pct": round(v[1] / (v[0] + v[1]) * 100) if (v[0] + v[1]) else 0}
                 for t, v in by_topic.items()}
    # Weak topics: >=2 questions attempted and accuracy < 60%.
    weak = sorted(
        [t for t, a in topic_acc.items() if a["total"] >= 2 and a["pct"] < 60],
        key=lambda t: topic_acc[t]["pct"])
    summary = {
        "score": right, "total": total,
        "pct": round(right / total * 100) if total else 0,
        "by_difficulty": diff_acc,
        "by_topic": topic_acc,
        "weak_topics": weak,
        "verdict": _verdict(right / total if total else 0),
    }
    return results, summary


def _verdict(ratio: float) -> str:
    if ratio >= 0.85:
        return "Excellent — interview-ready on this skill."
    if ratio >= 0.65:
        return "Solid — polish the weak topics below."
    if ratio >= 0.4:
        return "Developing — work through the roadmap stages for this skill."
    return "Foundations needed — start from the beginner stage of the roadmap."


def validate_start_payload(
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate {skill, num_questions, difficulties, timed, time_limit_sec}."""
    if not isinstance(payload, dict):
        return None, "Provide a JSON object."
    skill = (payload.get("skill") or "").strip()
    if skill not in {s for s, _ in quiz_skills()}:
        return None, "Choose a valid quiz skill."
    try:
        n = int(payload.get("num_questions", 10))
    except (TypeError, ValueError):
        return None, "num_questions must be a number."
    n = max(5, min(n, MAX_QUESTIONS))
    diffs = payload.get("difficulties") or []
    if not isinstance(diffs, list):
        return None, "difficulties must be a list."
    diffs = [d for d in diffs if d in DIFFICULTIES]
    timed = bool(payload.get("timed"))
    try:
        limit = int(payload.get("time_limit_sec", 0))
    except (TypeError, ValueError):
        limit = 0
    if timed:
        limit = max(60, min(limit or n * 60, 3600))
    else:
        limit = 0
    return {"skill": skill, "num_questions": n, "difficulties": diffs,
            "timed": timed, "time_limit_sec": limit}, None


def best_scores(attempt_rows: list[Any]) -> dict[str, dict[str, Any]]:
    """Per-skill best percentage across a user's attempts."""
    best = {}
    for r in attempt_rows:
        try:
            pct = (r.summary_json or {}).get("pct", 0)
        except AttributeError:
            pct = 0
        s = r.skill
        if pct > best.get(s, {}).get("pct", -1):
            best[s] = {"pct": pct, "score": (r.summary_json or {}).get("score", 0),
                       "total": (r.summary_json or {}).get("total", 0),
                       "attempt_id": r.id,
                       "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None}
    return best


def quiz_mistakes(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """Latest-state mistake book across a user's submitted quiz attempts.

    attempts: list of dicts {skill, results, submitted_at, attempt_id}
    where results is a grade_quiz() results list. A question counts as a
    mistake only when its LATEST graded state is wrong (a later correct
    attempt clears it — same semantics as the aptitude Mistake Book).
    Returns {"total": int, "skills": [{skill, mistakes: [...]}]} with
    skills ordered by mistake count.
    """
    latest = {}
    for att in sorted(attempts, key=lambda a: str(a.get("submitted_at") or "")):
        for r in att.get("results") or []:
            if not isinstance(r, dict) or not r.get("question_id"):
                continue
            latest[r["question_id"]] = (att, r)
    by_skill = {}
    for qid, (att, r) in latest.items():
        if r.get("correct"):
            continue
        entry = {
            "question_id": qid,
            "topic": r.get("topic", "General"),
            "difficulty": r.get("difficulty"),
            "question": r.get("question"),
            "options": list(r.get("options") or []),
            "chosen_index": r.get("chosen_index"),
            "correct_index": r.get("correct_index"),
            "explanation": r.get("explanation"),
            "skill": att.get("skill") or r.get("skill"),
            "attempt_id": att.get("attempt_id"),
        }
        by_skill.setdefault(entry["skill"], []).append(entry)
    skills = [{"skill": s, "mistakes": sorted(m, key=lambda e: e["topic"])}
              for s, m in by_skill.items()]
    skills.sort(key=lambda g: -len(g["mistakes"]))
    return {"total": sum(len(g["mistakes"]) for g in skills), "skills": skills}


# ================= Feature 16 Ultimate Enhancements =================
# 16.1 Certification-exam mode | 16.2 Weakness-targeted drills | 16.3 Leaderboard

import re as _re10


def _clean10(text: str | None) -> str:
    return _re10.sub(r"<[^>]*>", "", text or "")


# Real certification exam formats (question count, minutes, pass %).
_EXAM_FORMATS = {
    "aws-clf-c02": {"name": "AWS Certified Cloud Practitioner", "questions": 65,
                    "minutes": 90, "pass_pct": 70},
    "aws-saa-c03": {"name": "AWS Solutions Architect Associate", "questions": 65,
                    "minutes": 130, "pass_pct": 72},
    "gcp-ace": {"name": "Google Associate Cloud Engineer", "questions": 50,
                "minutes": 120, "pass_pct": 70},
    "az-900": {"name": "Microsoft Azure Fundamentals", "questions": 45,
               "minutes": 60, "pass_pct": 70},
    "cka": {"name": "Certified Kubernetes Administrator", "questions": 17,
            "minutes": 120, "pass_pct": 66},
    "generic": {"name": "Generic Certification Exam", "questions": 50,
                "minutes": 90, "pass_pct": 70},
}


def certification_exam_mode(
    skill: str,
    exam_id: str = "generic",
    seed: int | None = None,
) -> dict[str, Any]:
    """Build a quiz like the real certification exam: timed, weighted, pass/fail.

    Returns {exam, questions, minutes, pass_pct, instructions}.
    """
    skill = _clean10(skill or "").strip()
    if not skill:
        raise ValueError("Skill is required.")
    fmt = _EXAM_FORMATS.get(_clean10(exam_id or "").strip().lower() or "generic")
    if not fmt:
        fmt = _EXAM_FORMATS["generic"]
    n = fmt["questions"]
    # Weight toward medium/hard like real exams.
    full_questions = build_quiz(skill, n=min(n, 60),
                                difficulties=["Medium", "Hard", "Medium", "Easy"],
                                seed=seed)
    if not full_questions:
        raise ValueError(f"No questions available for skill '{skill}'.")
    questions = [public_question(q) for q in full_questions]
    return {"exam": fmt["name"], "exam_id": exam_id,
            "questions": questions, "minutes": fmt["minutes"],
            "pass_pct": fmt["pass_pct"], "total": len(questions),
            "instructions": (
                f"{fmt['name']} simulation: {len(questions)} questions, "
                f"{fmt['minutes']} minutes, pass at {fmt['pass_pct']}%. "
                "No pausing — train under pressure.")}


def grade_exam(
    questions: list[dict[str, Any]],
    answers: dict[str, Any],
    exam_id: str = "generic",
) -> dict[str, Any]:
    """Grade a certification-exam attempt with pass/fail verdict.

    questions may be public (client-safe) or full dicts — full versions are
    resolved by ID internally so answers never need to leave the server.
    """
    fmt = _EXAM_FORMATS.get(_clean10(exam_id or "").strip().lower() or "generic")
    if not fmt:
        fmt = _EXAM_FORMATS["generic"]
    full = []
    for q in (questions or []):
        if not isinstance(q, dict):
            continue
        if "answer" in q:
            full.append(q)
        else:
            fq = get_question(q.get("id"))
            if fq:
                full.append(fq)
    results, summary = grade_quiz(full, answers)
    pct = summary.get("pct", 0)
    passed = pct >= fmt["pass_pct"]
    gap = round(fmt["pass_pct"] - pct, 1)
    return {"results": results, "summary": summary,
            "pass_pct": fmt["pass_pct"], "scored_pct": pct,
            "passed": passed,
            "verdict": ("PASS — exam-ready." if passed else
                        f"FAIL — {gap:.0f} points short. Drill weak topics and retry."),
            "weak_topics": summary.get("weak_topics", [])}


def weakness_drill(
    skill: str,
    past_results: dict[str, Any] | list[dict[str, Any]],
    n: int = 10,
    seed: int | None = None,
) -> dict[str, Any]:
    """Auto-generate a drill set targeting the user's weakest topics.

    past_results: list of {question_id/topic, correct (bool)} or a grade_quiz
    summary dict with weak_topics. Returns {questions, target_topics}.
    """
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 10
    n = max(3, min(20, n))
    weak = []
    if isinstance(past_results, dict):
        weak = [str(t) for t in (past_results.get("weak_topics") or [])]
    elif isinstance(past_results, (list, tuple)):
        wrong_topics = {}
        for r in past_results:
            if not isinstance(r, dict):
                continue
            if not r.get("correct", True):
                t = str(r.get("topic") or r.get("skill") or "general")
                wrong_topics[t] = wrong_topics.get(t, 0) + 1
        weak = sorted(wrong_topics, key=wrong_topics.get, reverse=True)
    weak = [_clean10(t).strip() for t in weak if _clean10(t).strip()][:5]

    skill = _clean10(skill or "").strip()
    if not skill:
        raise ValueError("Skill is required.")
    topics = [t[0] if isinstance(t, (list, tuple)) else str(t)
              for t in skill_topics(skill)]
    target = [t for t in topics if any(w.lower() in t.lower() or t.lower() in w.lower()
                                       for w in weak)] if weak else []
    questions = []
    import random
    rng = random.Random(seed)
    # Prefer weak topics; fill the rest with general skill questions.
    if target:
        for t in target:
            # Bank rows carry a singular "topic" string — matching on a
            # "topics" list here silently emptied every drill (fixed
            # 2026-10-24: the drill always fell back to a general fill).
            qs = [q for q in _load()
                  if q.get("skill") == skill and q.get("topic") == t]
            rng.shuffle(qs)
            questions.extend(qs[: max(2, n // max(len(target), 1))])
    if len(questions) < n:
        rest = [q for q in _load() if q.get("skill") == skill
                and q not in questions]
        rng.shuffle(rest)
        questions.extend(rest[: n - len(questions)])
    questions = questions[:n]
    if not questions:
        raise ValueError(f"No questions available for skill '{skill}'.")
    return {"questions": [public_question(q) for q in questions],
            "target_topics": target or ["general"],
            "total": len(questions),
            "guidance": f"Drill set on: {', '.join(target) if target else 'mixed review'}."}


def leaderboard(
    entries: list[dict[str, Any]],
    current_user_id: int | str | None = None,
    top_n: int = 10,
) -> dict[str, Any]:
    """Peer leaderboard for quiz scores. Pure function.

    entries: list of {user_id, name, skill, score_pct, quizzes_taken}.
    Returns ranked list with rank, highlight for current user.
    """
    if not isinstance(entries, (list, tuple)):
        raise ValueError("entries must be a list.")
    try:
        top_n = int(top_n)
    except (TypeError, ValueError):
        top_n = 10
    top_n = max(1, min(50, top_n))
    clean = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        try:
            pct = float(e.get("score_pct") or 0)
        except (TypeError, ValueError):
            continue
        clean.append({
            "user_id": e.get("user_id"),
            "name": _clean10(e.get("name") or "Anonymous")[:40] or "Anonymous",
            "skill": _clean10(e.get("skill") or "")[:40],
            "score_pct": max(0, min(100, round(pct, 1))),
            "quizzes_taken": max(0, int(e.get("quizzes_taken") or 0)),
        })
    clean.sort(key=lambda x: (-x["score_pct"], -x["quizzes_taken"]))
    ranked = []
    for i, e in enumerate(clean[:top_n], 1):
        e["rank"] = i
        e["is_you"] = (current_user_id is not None
                       and str(e["user_id"]) == str(current_user_id))
        ranked.append(e)
    your_rank = next((e["rank"] for e in ranked if e["is_you"]), None)
    return {"leaders": ranked, "your_rank": your_rank,
            "guidance": "Top of the board? Raise difficulty. Bottom half? Drill weak topics."}
