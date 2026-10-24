"""Skill-gap learning roadmaps engine (Wave 3 feature #18).

Backed by the committed bank data/learning_roadmaps.json:
  {"skills": [...24 skills...], "roles": [...12 target roles...]}
Each skill has 3 stages (beginner/intermediate/advanced) with topics,
free resources, practice tasks, and a milestone checkpoint.
Each role lists required skills with a 1-5 priority and minimum stage.

Deterministic throughout: gap ranking, roadmap assembly, and progress
validation never depend on an LLM. Gemini (wired in unified_app.py) may
polish study tips only.
"""

from __future__ import annotations

import json
import os
from typing import Any

BANK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "learning_roadmaps.json")

STAGE_ORDER = {"beginner": 0, "intermediate": 1, "advanced": 2}
STAGE_NAMES = ["beginner", "intermediate", "advanced"]
VALID_STATUSES = {"not_started", "in_progress", "completed"}

_bank = None


def _load() -> dict[str, Any]:
    global _bank
    if _bank is None:
        try:
            with open(BANK_PATH, encoding="utf-8") as f:
                _bank = json.load(f)
        except OSError:
            _bank = {"skills": [], "roles": []}
    return _bank


def _skills() -> list[dict[str, Any]]:
    return _load().get("skills", [])


def _roles() -> list[dict[str, Any]]:
    return _load().get("roles", [])


def list_skills(domain: str | None = None) -> list[dict[str, Any]]:
    """All skills, optionally filtered by domain. Returns summary dicts."""
    out = []
    for s in _skills():
        if domain and s.get("domain") != domain:
            continue
        stages = s.get("stages", [])
        out.append({
            "id": s["id"], "name": s["name"], "domain": s.get("domain"),
            "summary": s.get("summary", ""), "demand": s.get("demand", ""),
            "total_weeks": sum(st.get("weeks", 0) for st in stages),
            "num_resources": sum(len(st.get("resources", [])) for st in stages),
            "num_topics": sum(len(st.get("topics", [])) for st in stages),
        })
    return out


def list_domains() -> list[tuple[str, int]]:
    """Sorted [(domain, skill_count)]."""
    counts = {}
    for s in _skills():
        d = s.get("domain", "Other")
        counts[d] = counts.get(d, 0) + 1
    return sorted(counts.items())


def get_skill(skill_id: str) -> dict[str, Any] | None:
    """Full skill dict or None."""
    for s in _skills():
        if s.get("id") == skill_id:
            return s
    return None


def list_roles() -> list[dict[str, Any]]:
    """Role summaries for the gap-analyzer form."""
    return [{"id": r["id"], "name": r["name"], "short": r.get("short", ""),
             "description": r.get("description", ""),
             "num_skills": len(r.get("required_skills", []))}
            for r in _roles()]


def get_role(role_id: str) -> dict[str, Any] | None:
    for r in _roles():
        if r.get("id") == role_id:
            return r
    return None


def skill_ids() -> list[str]:
    return [s["id"] for s in _skills()]


def _stage_index(name: str | None) -> int:
    return STAGE_ORDER.get((name or "").lower(), 0)


def analyze_gap(
    current_skills: list[dict[str, Any]] | None,
    target_role_id: str,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """Rank missing/under-levelled skills for a target role.

    current_skills: [{"skill_id": "...", "stage": "beginner|intermediate|advanced"}]
    Returns (gap_items, role) where gap_items are sorted by priority desc:
      {skill_id, skill_name, domain, priority, min_stage, current_stage
       (None if not started), stages_to_cover: [stage dicts], weeks_needed,
       reason}
    Skills already at/above the required stage are excluded.
    """
    role = get_role(target_role_id)
    if not role:
        return None, None
    have = {}
    for c in current_skills or []:
        sid = (c.get("skill_id") or "").strip()
        if sid:
            have[sid] = max(have.get(sid, -1), _stage_index(c.get("stage")))
    gaps = []
    for req in role.get("required_skills", []):
        sid = req.get("skill_id")
        skill = get_skill(sid)
        if not skill:
            continue
        need = _stage_index(req.get("min_stage"))
        got = have.get(sid, -1)
        if got >= need:
            continue
        stages = skill.get("stages", [])
        cover = [st for st in stages if _stage_index(st.get("stage")) > got
                 and _stage_index(st.get("stage")) <= need]
        gaps.append({
            "skill_id": sid,
            "skill_name": skill.get("name", sid),
            "domain": skill.get("domain", ""),
            "priority": int(req.get("priority", 1)),
            "min_stage": STAGE_NAMES[need],
            "current_stage": STAGE_NAMES[got] if got >= 0 else None,
            "stages_to_cover": cover,
            "weeks_needed": sum(st.get("weeks", 0) for st in cover),
            "reason": req.get("reason", ""),
        })
    gaps.sort(key=lambda g: (-g["priority"], g["skill_id"]))
    return gaps, role


def build_personal_roadmap(
    gap_items: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Combine gap items into one ordered study plan.

    Returns {"items": [...], "total_weeks": n} where items keep gap order
    (priority) and each carries its stages_to_cover.
    """
    items = list(gap_items or [])
    return {
        "items": items,
        "total_weeks": sum(g.get("weeks_needed", 0) for g in items),
        "num_skills": len(items),
    }


def validate_progress_payload(
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate {skill_id, stage, status} for the progress endpoint."""
    if not isinstance(payload, dict):
        return None, "Provide a JSON object."
    sid = (payload.get("skill_id") or "").strip()
    stage = (payload.get("stage") or "").strip().lower()
    status = (payload.get("status") or "").strip().lower()
    if not get_skill(sid):
        return None, "Unknown skill."
    if stage not in STAGE_ORDER:
        return None, "Stage must be beginner, intermediate, or advanced."
    if status not in VALID_STATUSES:
        return None, "Status must be not_started, in_progress, or completed."
    return {"skill_id": sid, "stage": stage, "status": status}, None


def validate_gap_payload(
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate {target_role, current_skills} for the gap analyzer."""
    if not isinstance(payload, dict):
        return None, "Provide a JSON object."
    role_id = (payload.get("target_role") or "").strip()
    if not get_role(role_id):
        return None, "Choose a valid target role."
    current = payload.get("current_skills") or []
    if not isinstance(current, list):
        return None, "current_skills must be a list."
    known = set(skill_ids())
    cleaned = []
    for c in current[:40]:
        if not isinstance(c, dict):
            continue
        sid = (c.get("skill_id") or "").strip()
        if sid not in known:
            continue
        stage = (c.get("stage") or "beginner").strip().lower()
        if stage not in STAGE_ORDER:
            stage = "beginner"
        cleaned.append({"skill_id": sid, "stage": stage})
    # de-dupe, keep highest stage
    best = {}
    for c in cleaned:
        if _stage_index(c["stage"]) > _stage_index(best.get(c["skill_id"], {}).get("stage", "beginner")):
            best[c["skill_id"]] = c
    return {"target_role": role_id,
            "current_skills": list(best.values())}, None


def progress_summary(progress_rows: list[Any]) -> list[dict[str, Any]]:
    """Aggregate RoadmapProgress rows into per-skill dashboard data."""
    by_skill = {}
    for r in progress_rows:
        by_skill.setdefault(r.skill_id, {})[r.stage] = r.status
    out = []
    for sid, stages in sorted(by_skill.items()):
        skill = get_skill(sid)
        total = 3
        done = sum(1 for st in STAGE_NAMES if stages.get(st) == "completed")
        out.append({
            "skill_id": sid,
            "skill_name": skill.get("name", sid) if skill else sid,
            "domain": skill.get("domain", "") if skill else "",
            "stages": {st: stages.get(st, "not_started") for st in STAGE_NAMES},
            "completed": done, "total": total,
            "pct": round(done / total * 100),
        })
    return out


# ================= Feature 15 Ultimate Enhancements =================
# 15.1 Agentic-AI track | 15.2 Project milestones | 15.3 Weekly auto-planner

import re as _re9
from datetime import UTC, datetime, timedelta


def _clean9(text: str | None) -> str:
    return _re9.sub(r"<[^>]*>", "", text or "")


# The 234%-growth skill cluster as a first-class roadmap.
_AGENTIC_TRACK = {
    "id": "agentic-ai",
    "name": "Agentic AI Engineer",
    "tagline": "The fastest-growing skill cluster of 2026: agents, RAG, eval.",
    "stages": [
        {"stage": "Foundations", "weeks": 3,
         "skills": ["Python APIs", "Prompt engineering basics", "LLM fundamentals"],
         "project": "Build a Q&A bot over your own notes using an LLM API."},
        {"stage": "RAG", "weeks": 4,
         "skills": ["Embeddings", "Vector databases", "Chunking strategies", "Retrieval tuning"],
         "project": "RAG chatbot over 50 PDFs with cited answers."},
        {"stage": "Agents", "weeks": 4,
         "skills": ["Tool calling", "Agent loops", "Multi-agent patterns", "LangChain/LlamaIndex"],
         "project": "Research agent that browses, summarizes, and cites sources."},
        {"stage": "Eval & Ship", "weeks": 3,
         "skills": ["LLM evaluation", "Guardrails", "Cost optimization", "Monitoring"],
         "project": "Production-ready agent with evals, guardrails, and a demo video."},
    ],
}


def agentic_ai_track() -> dict[str, Any]:
    """Return the Agentic AI roadmap (the 234%-growth cluster)."""
    track = {"id": _AGENTIC_TRACK["id"], "name": _AGENTIC_TRACK["name"],
             "tagline": _AGENTIC_TRACK["tagline"],
             "total_weeks": sum(s["weeks"] for s in _AGENTIC_TRACK["stages"]),
             "stages": [dict(s) for s in _AGENTIC_TRACK["stages"]]}
    return track


def project_milestones(
    roadmap_stages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Attach a portfolio-worthy project milestone to each roadmap stage.

    roadmap_stages: list of {stage/name, weeks?, project?}. Stages without a
    project get a generated one. Returns enriched stages.
    """
    if not isinstance(roadmap_stages, (list, tuple)):
        raise ValueError("roadmap_stages must be a list.")
    _FALLBACK_PROJECTS = [
        "Build a small working demo of this stage's core skill and push it to GitHub.",
        "Write a blog post / LinkedIn post explaining what you built and one lesson.",
        "Extend the demo with one real-world feature and record a 2-min demo video.",
        "Polish, document, and add the project to your portfolio with metrics.",
    ]
    out = []
    for i, st in enumerate(roadmap_stages):
        if not isinstance(st, dict):
            continue
        name = _clean9(st.get("stage") or st.get("name") or f"Stage {i+1}")[:80]
        project = _clean9(st.get("project") or "").strip()
        if not project:
            project = _FALLBACK_PROJECTS[i % len(_FALLBACK_PROJECTS)]
        out.append({"stage": name,
                    "weeks": st.get("weeks"),
                    "project": project,
                    "deliverable": "GitHub repo + README + demo",
                    "proof": "Recruiters see proof of work, not just claims."})
    if not out:
        raise ValueError("No valid stages to attach milestones to.")
    return {"stages": out, "total_projects": len(out),
            "guidance": "Finish each stage's project before moving on — half-done tutorials don't count."}


def weekly_planner(
    roadmap_stages: list[dict[str, Any]],
    hours_per_week: float = 6,
    start_date: str | None = None,
) -> dict[str, Any]:
    """Turn roadmap stages into a week-by-week calendar plan.

    Returns {weeks: [{week, dates, focus, project, hours}], total_weeks}.
    """
    if not isinstance(roadmap_stages, (list, tuple)) or not roadmap_stages:
        raise ValueError("roadmap_stages must be a non-empty list.")
    try:
        hours_per_week = float(hours_per_week)
    except (TypeError, ValueError):
        hours_per_week = 6
    hours_per_week = max(1, min(40, hours_per_week))
    try:
        start = datetime.fromisoformat(str(start_date or "").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        start = datetime.now(UTC)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)

    weeks = []
    week_no = 1
    for st in roadmap_stages:
        if not isinstance(st, dict):
            continue
        name = _clean9(st.get("stage") or st.get("name") or "Stage")[:80]
        try:
            n_weeks = max(1, int(st.get("weeks") or 2))
        except (TypeError, ValueError):
            n_weeks = 2
        project = _clean9(st.get("project") or "Practice + build")[:120]
        for _ in range(n_weeks):
            w_start = start + timedelta(weeks=week_no - 1)
            weeks.append({
                "week": week_no,
                "dates": f"{w_start.strftime('%b %d')} – {(w_start + timedelta(days=6)).strftime('%b %d')}",
                "focus": name,
                "project": project if week_no and _ == n_weeks - 1 else "Learn + practice",
                "hours": hours_per_week,
                "reminder": f"Week {week_no}: {hours_per_week:.0f}h on {name}",
            })
            week_no += 1
    total_hours = round(len(weeks) * hours_per_week, 1)
    return {"weeks": weeks, "total_weeks": len(weeks),
            "total_hours": total_hours,
            "guidance": f"{total_hours:.0f} hours total. Miss a week? Shift — never skip."}
