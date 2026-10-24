"""DSA practice engine: filtering, progressive hints, weak-topic diagnosis
(Wave 2 feature #15). Backed by the committed bank data/dsa_problems.json
(120 problems across 43 topics)."""

from __future__ import annotations

import json
import os
from typing import Any

BANK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "dsa_problems.json")

A2Z_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "striver_a2z.json")

_bank = None
_by_id = None


def _load() -> list[dict[str, Any]]:
    global _bank, _by_id
    if _bank is None:
        with open(BANK_PATH, encoding="utf-8") as f:
            _bank = json.load(f)
        _by_id = {p["id"]: p for p in _bank}
        # A2Z sheet rows join the id lookup so the shared progress
        # endpoints can track them; classic ids win any clash.
        for entry in _load_a2z():
            _by_id.setdefault(entry["id"], _a2z_public(entry))
    return _bank


def all_problems() -> list[dict[str, Any]]:
    return _load()


def get_problem(problem_id: str | int) -> dict[str, Any] | None:
    return _load_by_id().get(problem_id)


def _load_by_id() -> dict[Any, dict[str, Any]]:
    _load()
    return _by_id


def topics() -> list[tuple[str, int]]:
    """Sorted [(topic, count)] across the bank."""
    counts = {}
    for p in _load():
        for t in p.get("topics", []):
            counts[t] = counts.get(t, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def difficulties() -> list[tuple[str, int]]:
    counts = {}
    for p in _load():
        d = p.get("difficulty", "Medium")
        counts[d] = counts.get(d, 0) + 1
    order = ["Easy", "Medium", "Hard"]
    return [(d, counts.get(d, 0)) for d in order]


def filter_problems(
    topic: str | None = None,
    difficulty: str | None = None,
) -> list[dict[str, Any]]:
    """Return summary dicts for problems matching the filters."""
    out = []
    for p in _load():
        if topic and topic not in p.get("topics", []):
            continue
        if difficulty and p.get("difficulty") != difficulty:
            continue
        out.append({"id": p["id"], "title": p["title"],
                    "difficulty": p.get("difficulty"),
                    "topics": p.get("topics", [])})
    return out


def public_problem(
    problem_id: str | int,
    hints_revealed: int = 0,
) -> dict[str, Any] | None:
    """Problem payload for the client. Hints beyond `hints_revealed` are
    withheld; the approach outline is withheld until explicitly requested."""
    p = get_problem(problem_id)
    if not p:
        return None
    hints = p.get("hints", []) or []
    return {
        "id": p["id"],
        "title": p["title"],
        "statement": p.get("statement"),
        "difficulty": p.get("difficulty"),
        "topics": p.get("topics", []),
        "complexity": p.get("complexity"),
        "pitfalls": p.get("pitfalls", []),
        "leetcode_url": p.get("leetcode_url"),
        "total_hints": len(hints),
        "hints_revealed": min(hints_revealed, len(hints)),
        "hints": hints[:hints_revealed],
        "approach": None,  # revealed only via explicit request
    }


def reveal_hint(
    problem_id: str | int,
    hints_revealed: int,
) -> tuple[str | None, int]:
    """Next progressive hint, or None when exhausted."""
    p = get_problem(problem_id)
    if not p:
        return None, 0
    hints = p.get("hints", []) or []
    if hints_revealed < len(hints):
        return hints[hints_revealed], hints_revealed + 1
    return None, hints_revealed


def reveal_approach(problem_id: str | int) -> str | None:
    """Full approach outline — only when the user explicitly asks."""
    p = get_problem(problem_id)
    return p.get("approach") if p else None


VALID_STATUSES = {"attempted", "solved", "stuck"}


def validate_status_payload(
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    status = (payload.get("status") or "").strip().lower()
    if status not in VALID_STATUSES:
        return None, "Status must be one of: attempted, solved, stuck."
    code = payload.get("code") or ""
    if len(code) > 20000:
        return None, "Code is too long (max 20000 characters)."
    return {"status": status, "code": code}, None


def _aggregate_topic_stats(
    progress_rows: list[dict[str, Any]],
    problems: dict[Any, dict[str, Any]],
) -> tuple[dict[str, dict[str, int]], int, int, int, set[Any]]:
    """Fold progress rows into per-topic counters.

    Returns (by_topic, solved, stuck, attempted, solved_ids).
    """
    by_topic = {}
    solved = stuck = attempted = 0
    solved_ids = set()
    for row in progress_rows:
        p = problems.get(row["problem_id"])
        if not p:
            continue
        st = row.get("status")
        if st == "solved":
            solved += 1
            solved_ids.add(row["problem_id"])
        elif st == "stuck":
            stuck += 1
        else:
            attempted += 1
        for t in p.get("topics", []):
            agg = by_topic.setdefault(t, {"attempted": 0, "solved": 0, "stuck": 0})
            agg["attempted"] += 1
            if st == "solved":
                agg["solved"] += 1
            elif st == "stuck":
                agg["stuck"] += 1
    return by_topic, solved, stuck, attempted, solved_ids


def _topic_rows(
    by_topic: dict[str, dict[str, int]],
) -> list[dict[str, Any]]:
    """Per-topic solve/stuck rates, weakest first."""
    rows = []
    for t, agg in by_topic.items():
        n = agg["attempted"]
        rows.append({
            "topic": t, "attempted": n, "solved": agg["solved"],
            "stuck": agg["stuck"],
            "solve_rate": round(agg["solved"] / n * 100, 1) if n else 0.0,
            "stuck_rate": round(agg["stuck"] / n * 100, 1) if n else 0.0,
        })
    rows.sort(key=lambda r: (r["solve_rate"], -r["stuck_rate"]))
    return rows


def _weak_topic_recommendations(
    topic_rows: list[dict[str, Any]],
    solved_ids: set[Any],
) -> list[dict[str, Any]]:
    """Unsolved easy-first problems from the weakest topics (max 6)."""
    recommendations = []
    order = {"Easy": 0, "Medium": 1, "Hard": 2}
    for tr in topic_rows[:4]:
        if tr["solve_rate"] >= 80 and tr["stuck_rate"] == 0:
            continue
        cands = [p for p in _load()
                 if tr["topic"] in p.get("topics", []) and p["id"] not in solved_ids]
        cands.sort(key=lambda p: order.get(p.get("difficulty"), 1))
        for p in cands[:2]:
            recommendations.append({
                "problem_id": p["id"], "title": p["title"],
                "difficulty": p.get("difficulty"),
                "reason": f"Weak topic: {tr['topic']} "
                          f"(solve rate {tr['solve_rate']}%, stuck {tr['stuck']}x).",
            })
        if len(recommendations) >= 6:
            break
    return recommendations


def _strong_topic_recommendations(
    topic_rows: list[dict[str, Any]],
    solved_ids: set[Any],
) -> list[dict[str, Any]]:
    """Hard problems in practiced topics, when nothing is weak."""
    recommendations = []
    strong = [tr for tr in topic_rows if tr["solve_rate"] >= 80][:2]
    for tr in strong:
        cands = [p for p in _load()
                 if tr["topic"] in p.get("topics", [])
                 and p.get("difficulty") == "Hard"
                 and p["id"] not in solved_ids][:2]
        for p in cands:
            recommendations.append({
                "problem_id": p["id"], "title": p["title"],
                "difficulty": "Hard",
                "reason": f"You're strong at {tr['topic']} — level up with a Hard problem.",
            })
    return recommendations


def weak_topic_diagnosis(
    progress_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Per-topic solve/stuck rates + recommended next topics/problems.

    progress_rows: iterable of dicts {problem_id, status, hints_used}.
    Returns {"topics": [...], "recommendations": [...], "overall": {...}}.
    """
    problems = _load_by_id()
    by_topic, solved, stuck, attempted, solved_ids = _aggregate_topic_stats(
        progress_rows, problems)
    topic_rows = _topic_rows(by_topic)
    # 1) revisit weakest topics with unsolved problems in them
    recommendations = _weak_topic_recommendations(topic_rows, solved_ids)
    # 2) if nothing weak, push to harder problems in practiced topics
    if not recommendations and topic_rows:
        recommendations = _strong_topic_recommendations(topic_rows, solved_ids)
    total = solved + stuck + attempted
    return {
        "topics": topic_rows,
        "recommendations": recommendations[:8],
        "overall": {
            "attempted": total, "solved": solved, "stuck": stuck,
            "solve_rate": round(solved / total * 100, 1) if total else 0.0,
        },
    }


# ================= Feature 12 Ultimate Enhancements =================
# 12.1 Pattern mastery map | 12.2 Mock OA simulator | 12.3 Spaced repetition

import re as _re6
from datetime import UTC, datetime, timedelta


def _clean6(text: str | None) -> str:
    return _re6.sub(r"<[^>]*>", "", text or "")


# The 14 canonical DSA patterns mapped from the bank's topic vocabulary.
_PATTERNS = [
    ("Sliding Window", ["sliding-window", "subarray"]),
    ("Two Pointers", ["two-pointers", "pointers", "cyclic-sort"]),
    ("Binary Search", ["searching", "binary", "quickselect"]),
    ("DFS / BFS", ["dfs", "bfs", "graphs", "trees", "topological-sort", "cycles"]),
    ("Dynamic Programming", ["dynamic programming", "dp", "knapsack", "interval-dp"]),
    ("Greedy", ["greedy", "intervals"]),
    ("Heap", ["heaps", "priority queue"]),
    ("Stack / Queue", ["stacks", "queues", "monotonic-stack", "deque"]),
    ("Hashing", ["hash maps", "hashing", "hash set", "bucket-sort", "prefix-sums"]),
    ("Linked List", ["linked lists", "reversal"]),
    ("Recursion / Backtracking", ["recursion", "backtracking"]),
    ("Sorting", ["sorting"]),
    ("Bit Manipulation", ["bit manipulation", "binary-exponentiation"]),
    ("Graphs Advanced", ["dijkstra", "union-find", "matrix", "bst"]),
]


def pattern_mastery(
    solved_ids: list[str | int],
    attempted_ids: list[str | int] | None = None,
) -> dict[str, Any]:
    """Mastery % per DSA pattern from solved problem IDs.

    solved_ids: list of problem ids (str or int) the user solved.
    Returns {patterns: [{pattern, solved, total, mastery_pct}], overall}.
    """
    if not isinstance(solved_ids, (list, tuple)):
        raise ValueError("solved_ids must be a list.")
    solved = set()
    for pid in solved_ids:
        if isinstance(pid, str) and pid.strip():
            solved.add(pid.strip())
        elif isinstance(pid, int):
            solved.add(str(pid))
    by_id = {str(p["id"]): p for p in _load()}
    out = []
    total_solved = total_bank = 0
    for pattern, keywords in _PATTERNS:
        pids = [pid for pid, p in by_id.items()
                if any(kw in " ".join(p.get("topics", [])).lower() for kw in keywords)]
        n_total = len(pids)
        n_solved = sum(1 for pid in pids if pid in solved)
        total_solved += n_solved
        total_bank += n_total
        mastery = round(n_solved / n_total * 100, 1) if n_total else 0.0
        out.append({"pattern": pattern, "solved": n_solved, "total": n_total,
                    "mastery_pct": mastery,
                    "status": ("mastered" if mastery >= 80 else
                               "progressing" if mastery >= 40 else "weak")})
    out.sort(key=lambda x: x["mastery_pct"])
    overall = round(total_solved / total_bank * 100, 1) if total_bank else 0.0
    weakest = out[0]["pattern"] if out else None
    return {"patterns": out, "overall_mastery_pct": overall,
            "weakest_pattern": weakest,
            "guidance": (f"Attack '{weakest}' next — it is your bottleneck."
                         if weakest else "No pattern data.")}


def mock_oa(
    num_questions: int = 2,
    minutes: int = 60,
    seed: int | None = None,
) -> dict[str, Any]:
    """Simulate a real Online Assessment: N questions, time limit, mixed difficulty.

    Returns {questions: [public dicts], minutes, total, instructions}.
    Picks 1 medium + rest mixed for realism.
    """
    try:
        num_questions = int(num_questions)
    except (TypeError, ValueError):
        num_questions = 2
    num_questions = max(1, min(4, num_questions))
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = 60
    minutes = max(10, min(180, minutes))

    import random
    rng = random.Random(seed)
    bank = _load()
    mediums = [p for p in bank if p.get("difficulty") == "Medium"]
    rest = [p for p in bank if p.get("difficulty") != "Medium"]
    picks = []
    if mediums:
        picks.append(rng.choice(mediums))
    pool = [p for p in rest if p not in picks]
    rng.shuffle(pool)
    picks += pool[:num_questions - len(picks)]
    rng.shuffle(picks)
    questions = [{"id": p["id"], "title": p["title"],
                  "difficulty": p.get("difficulty"),
                  "topics": p.get("topics", [])} for p in picks]
    return {"questions": questions, "minutes": minutes,
            "total": len(questions),
            "instructions": (
                f"You have {minutes} minutes for {len(questions)} questions. "
                "No hints, no solutions — like the real thing. "
                "Score yourself after: 1 point per solved, 0.5 per partial.")}


def spaced_repetition_queue(
    mistakes: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Wrong problems resurface after 3/7/14 days.

    mistakes: list of {problem_id, wrong_at (iso str), streak (consecutive
    wrong count)}. Returns {due: [...], upcoming: [...]} sorted by urgency.
    """
    if not isinstance(mistakes, (list, tuple)):
        raise ValueError("mistakes must be a list.")
    now = now or datetime.now(UTC)
    intervals = {1: 3, 2: 7}
    due, upcoming = [], []
    for m in mistakes:
        if not isinstance(m, dict) or m.get("problem_id") is None:
            continue
        pid = m["problem_id"]
        if isinstance(pid, str) and not pid.strip():
            continue
        if not isinstance(pid, (str, int)):
            continue
        try:
            streak = max(1, int(m.get("streak") or 1))
        except (TypeError, ValueError):
            streak = 1
        days = intervals.get(streak, 14)
        try:
            wrong_at = datetime.fromisoformat(str(m.get("wrong_at") or "").replace("Z", "+00:00"))
            if wrong_at.tzinfo is None:
                wrong_at = wrong_at.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            wrong_at = now
        due_at = wrong_at + timedelta(days=days)
        entry = {"problem_id": pid, "streak": streak,
                 "interval_days": days, "due_at": due_at.isoformat(),
                 "overdue_days": max(0, (now - due_at).days)}
        (due if now >= due_at else upcoming).append(entry)
    due.sort(key=lambda x: -x["overdue_days"])
    upcoming.sort(key=lambda x: x["due_at"])
    return {"due": due, "upcoming": upcoming,
            "due_count": len(due),
            "guidance": (f"{len(due)} problem(s) due for review — spaced repetition "
                         "is how patterns stick." if due else
                         "Nothing due. Keep solving — mistakes will queue here.")}


# ---------------------------------------------------------------------------
# Striver A2Z sheet (data/striver_a2z.json)
#
# The sheet is deliberately simple (per product direction): every row
# is a name, a module (the sheet's own category), an optional
# difficulty and an external practice link. There are no in-house
# statements or hints for these rows — students solve on the original
# platform and mark progress here. Progress reuses the DSAProgress
# table through the shared status endpoint.
# ---------------------------------------------------------------------------

_a2z = None


def _load_a2z() -> list[dict[str, Any]]:
    global _a2z
    if _a2z is None:
        try:
            with open(A2Z_PATH, encoding="utf-8") as f:
                _a2z = json.load(f)
        except FileNotFoundError:
            _a2z = []
    return _a2z


def _a2z_public(entry: dict[str, Any]) -> dict[str, Any]:
    """The minimal problem-shaped view of a sheet row, so the shared
    diagnosis machinery can read its category."""
    return {
        "id": entry["id"],
        "title": entry["title"],
        "difficulty": entry.get("difficulty"),
        "topics": [entry["step_name"].lower()],
        "leetcode_url": entry.get("url"),
        "a2z": True,
    }


def a2z_problems() -> list[dict[str, Any]]:
    return _load_a2z()


def sheet_structure(
    statuses: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Sheet rows grouped by module, in sheet order.

    ``statuses`` maps problem_id -> status for the current user; each
    item gains a ``status`` key (None when untracked) and each module
    gains ``solved``/``total`` counts.
    """
    statuses = statuses or {}
    steps = {}
    order = []
    for entry in _load_a2z():
        key = entry["step"]
        if key not in steps:
            steps[key] = {"step": entry["step"],
                          "step_name": entry["step_name"],
                          "total": 0, "solved": 0, "rows": []}
            order.append(key)
        item = dict(entry)
        item["status"] = statuses.get(entry["id"])
        steps[key]["rows"].append(item)
        steps[key]["total"] += 1
        if item["status"] == "solved":
            steps[key]["solved"] += 1
    for step in steps.values():
        # Subtopic groups in first-appearance (sheet) order — the
        # sheet's sequence is the learning path, so never re-sort it.
        groups = {}
        for item in step["rows"]:
            groups.setdefault(item["subtopic"], []).append(item)
        step["groups"] = [{"subtopic": name, "rows": rows}
                          for name, rows in groups.items()]
    return [steps[key] for key in order]


def a2z_tracker(
    progress_rows: list[tuple[str, str]],
) -> dict[str, Any]:
    """Aggregate a user's progress over the A2Z sheet.

    ``progress_rows`` is an iterable of (problem_id, status) pairs, as
    stored by the shared DSA progress endpoint. Returns overall,
    per-module and per-difficulty totals. Theory rows (no difficulty)
    count towards module and overall totals but not the difficulty
    breakdown.
    """
    status_by_id = {pid: status for pid, status in progress_rows}
    by_id = {entry["id"]: entry for entry in _load_a2z()}
    total = len(by_id)
    solved = attempted = stuck = 0
    steps = {}
    step_order = []
    difficulties = {"Easy": [0, 0], "Medium": [0, 0], "Hard": [0, 0]}
    for pid, entry in by_id.items():
        key = entry["step"]
        if key not in steps:
            steps[key] = {"step": entry["step"],
                          "step_name": entry["step_name"],
                          "total": 0, "solved": 0, "attempted": 0}
            step_order.append(key)
        steps[key]["total"] += 1
        status = status_by_id.get(pid)
        if status:
            steps[key]["attempted"] += 1
        if status == "solved":
            solved += 1
            steps[key]["solved"] += 1
        elif status == "attempted":
            attempted += 1
        elif status == "stuck":
            stuck += 1
        diff = entry.get("difficulty")
        if diff in difficulties:
            difficulties[diff][1] += 1
            if status == "solved":
                difficulties[diff][0] += 1
    tracked = sum(1 for pid in status_by_id if pid in by_id)
    for step in steps.values():
        step["pct"] = round(100 * step["solved"] / step["total"]) if step["total"] else 0
    return {
        "total": total,
        "solved": solved,
        "attempted": attempted,
        "stuck": stuck,
        "tracked": tracked,
        "pct": round(100 * solved / total) if total else 0,
        "by_step": [steps[key] for key in step_order],
        "by_difficulty": [
            {"difficulty": name, "solved": pair[0], "total": pair[1],
             "pct": round(100 * pair[0] / pair[1]) if pair[1] else 0}
            for name, pair in difficulties.items()
        ],
    }


import re as _re_slug_mod

_SLUG_RE = _re_slug_mod.compile(r"leetcode\.com/problems/([a-z0-9-]+)")
_STATUS_RANK = {"attempted": 1, "stuck": 2, "solved": 3}
_counterparts_cache = None


def _lc_slug(url: str | None) -> str | None:
    if not url:
        return None
    m = _SLUG_RE.search(url)
    return m.group(1) if m else None


def _counterpart_map() -> dict[Any, set[Any]]:
    """problem_id -> ids of the SAME problem in the other set.

    The classic bank and the A2Z sheet are independent lists, but many
    entries are literally the same problem (e.g. classic Two Sum is
    also an A2Z sheet row). They are matched by LeetCode slug, which
    is unique per problem on the platform.
    """
    global _counterparts_cache
    if _counterparts_cache is None:
        classic_by_slug, a2z_by_slug = {}, {}
        for p in all_problems():
            slug = _lc_slug(p.get("leetcode_url"))
            if slug:
                classic_by_slug.setdefault(slug, []).append(p["id"])
        for entry in _load_a2z():
            slug = _lc_slug(entry.get("url"))
            if slug:
                a2z_by_slug.setdefault(slug, []).append(entry["id"])
        mapping = {}
        for slug, classic_ids in classic_by_slug.items():
            for a2z_id in a2z_by_slug.get(slug, []):
                for classic_id in classic_ids:
                    mapping.setdefault(classic_id, set()).add(a2z_id)
                    mapping.setdefault(a2z_id, set()).add(classic_id)
        _counterparts_cache = mapping
    return _counterparts_cache


def counterpart_ids(problem_id: str | int) -> list[Any]:
    """Ids of the same problem in the other set (may be empty)."""
    return sorted(_counterpart_map().get(problem_id, ()))


def expand_statuses(
    progress_rows: list[tuple[Any, str]],
) -> dict[Any, str]:
    """Best status per problem id, shared across both sets.

    A status recorded against either copy of a problem counts for
    both: marking Two Sum solved in the classic list also lights it
    up in the A2Z tracker, and vice versa. The strongest status wins
    (solved > stuck > attempted) so nothing ever downgrades.
    """
    pairs = list(progress_rows)
    best = {}

    def offer(pid: Any, status: str) -> None:
        if status not in _STATUS_RANK:
            return
        current = best.get(pid)
        if current is None or _STATUS_RANK[status] > _STATUS_RANK[current]:
            best[pid] = status

    for pid, status in pairs:
        offer(pid, status)
    counterparts = _counterpart_map()
    for pid, status in pairs:
        for other in counterparts.get(pid, ()):
            offer(other, status)
    return best


def progress_summary(
    progress_rows: list[tuple[Any, str]],
) -> dict[str, Any]:
    """Full DSA progress summary for tracker surfaces.

    Combines the Striver A2Z aggregate with the classic problem-set
    aggregate so the DSA home page and the dedicated tracker page show
    the same numbers from the same progress rows. Statuses are first
    expanded across both sets (see expand_statuses), so a solve of a
    problem that appears in both lists counts in both.
    """
    rows = list(progress_rows)
    expanded = expand_statuses(rows)
    a2z_pairs = [(pid, status) for pid, status in expanded.items()
                 if pid.startswith("a2z-")]
    classic_ids = {p["id"] for p in all_problems()}
    classic_solved = sum(1 for pid in classic_ids
                         if expanded.get(pid) == "solved")
    classic_total = len(classic_ids)
    return {
        "a2z": a2z_tracker(a2z_pairs),
        "classic": {
            "total": classic_total,
            "solved": classic_solved,
            "pct": round(100 * classic_solved / classic_total)
                   if classic_total else 0,
        },
    }
