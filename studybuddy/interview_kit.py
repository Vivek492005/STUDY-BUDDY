"""Interview kit builder (Career master plan, Phase 1: tracker spine).

Builds a per-application prep kit from data that already exists in the repo:
- questions from data/interview_questions.json (1,455 questions, tagged by
  company and role) — matched, never invented;
- a roadmap link from data/learning_roadmaps.json (role -> top required
  skill -> /learn/roadmaps/<skill_id>);
- a static preparation checklist.

Matching rules (honest, best-effort):
- Company match: normalized company name equality against a question's
  ``companies`` list. Up to 10 questions, bank order.
- If (and only if) no company matches, role fallback: the application's role
  text is tokenized and matched against question ``roles`` slugs via an
  alias map. Up to 10 questions, bank order.
- If neither matches, questions is [] and ``note`` explains why.

Never raises: any load/match failure returns an empty kit with a note.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

_BANK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "interview_questions.json")
_ROADMAPS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "data", "learning_roadmaps.json")

_bank: list[dict[str, Any]] | None = None
_roadmaps: dict[str, Any] | None = None

# Question-bank role slugs -> keywords found in free-text role titles.
_ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    "sde": ("software", "sde", "developer", "development", "engineer"),
    "backend": ("backend", "back end", "server"),
    "frontend": ("frontend", "front end", "ui", "web"),
    "devops": ("devops", "dev ops", "sre", "platform", "infrastructure"),
    "data_analyst": ("data analyst", "analyst", "business analyst"),
    "data_scientist": ("data scientist", "data science", "machine learning",
                       "ml engineer", "ai engineer"),
    "pm": ("product manager", "product management", "pm"),
}

_CHECKLIST: list[str] = [
    "Re-read the job description and map 3 of your bullets to it.",
    "Prepare a 2-minute walkthrough of your strongest project.",
    "Prepare 2 questions to ask the interviewer about the team.",
    "Review the company's recent product news before the interview.",
    "Do one timed practice round on your weakest topic.",
    "Prepare your salary range using the salary explorer before any offer talk.",
]

_TOKEN_STOPWORDS = frozenset({"and", "of", "for", "the", "a", "an", "in", "to"})


def _load_bank() -> list[dict[str, Any]]:
    global _bank
    if _bank is None:
        try:
            with open(_BANK_PATH, encoding="utf-8") as f:
                data = json.load(f)
            _bank = data if isinstance(data, list) else []
        except Exception:
            _bank = []
    return _bank


def _load_roadmaps() -> dict[str, Any]:
    global _roadmaps
    if _roadmaps is None:
        try:
            with open(_ROADMAPS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            _roadmaps = data if isinstance(data, dict) else {}
        except Exception:
            _roadmaps = {}
    return _roadmaps


def _norm(text: str | None) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str | None) -> set[str]:
    toks = set()
    for tok in _norm(text).split(" "):
        if not tok or tok in _TOKEN_STOPWORDS:
            continue
        if tok == "dev":
            tok = "developer"
        toks.add(tok)
    return toks


def _company_questions(bank: list[dict[str, Any]], company: str) -> list[dict[str, Any]]:
    want = _norm(company)
    if not want:
        return []
    out = []
    for q in bank:
        companies = q.get("companies") or []
        if any(_norm(c) == want for c in companies):
            out.append(q)
        if len(out) >= 10:
            break
    return out


def _role_slugs(role: str) -> set[str]:
    text = _norm(role)
    slugs = set()
    for slug, keywords in _ROLE_ALIASES.items():
        if any(kw in text for kw in keywords):
            slugs.add(slug)
    # Direct slug match (e.g. role text literally contains "data_analyst").
    for slug in _ROLE_ALIASES:
        if slug in text.replace(" ", "_") or slug.replace("_", " ") in text:
            slugs.add(slug)
    return slugs


def _role_questions(bank: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    slugs = _role_slugs(role)
    if not slugs:
        return []
    out = []
    for q in bank:
        q_slugs = set(q.get("roles") or [])
        if q_slugs & slugs:
            out.append(q)
        if len(out) >= 10:
            break
    return out


def _roadmap_for_role(role: str) -> dict[str, str] | None:
    """Best-effort role -> roadmap link.

    Scores each roadmap role by token overlap with the application's role
    text, then links to that role's highest-priority required skill
    (/learn/roadmaps/<skill_id> is the skill-detail route). Returns None
    when nothing overlaps.
    """
    try:
        data = _load_roadmaps()
        roles = data.get("roles") or []
        skills = {s.get("id"): s for s in (data.get("skills") or [])
                  if isinstance(s, dict) and s.get("id")}
        want = _tokens(role)
        if not want or not roles:
            return None
        best = None
        best_score = 0
        for r in roles:
            if not isinstance(r, dict):
                continue
            name_toks = _tokens(r.get("name")) | _tokens((r.get("id") or "").replace("-", " "))
            score = len(want & name_toks)
            if score > best_score:
                best_score = score
                best = r
        if best is None or best_score == 0:
            return None
        required = sorted(best.get("required_skills") or [],
                          key=lambda s: -(s.get("priority") or 0))
        if not required:
            return None
        skill_id = required[0].get("skill_id")
        skill = skills.get(skill_id) if skill_id else None
        if not skill:
            return None
        return {"title": skill.get("name") or skill_id,
                "url": f"/learn/roadmaps/{skill_id}",
                "skill_id": skill_id,
                "role_id": best.get("id"),
                "role_name": best.get("name")}
    except Exception:
        return None


def _public_question(q: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": q.get("id"),
        "question": q.get("question"),
        "category": q.get("category"),
        "difficulty": q.get("difficulty"),
        "companies": list(q.get("companies") or []),
        "roles": list(q.get("roles") or []),
        "key_points": list(q.get("key_points") or []),
        "time_minutes": q.get("time_minutes"),
    }


def build_kit(company: str | None, role: str | None) -> dict[str, Any]:
    """Build an interview kit for one application. Never raises."""
    company = (company or "").strip()
    role = (role or "").strip()
    try:
        bank = _load_bank()
        matched = _company_questions(bank, company)
        source = "company" if matched else None
        if not matched:
            matched = _role_questions(bank, role)
            source = "role" if matched else None
        questions = [_public_question(q) for q in matched]
        roadmap = _roadmap_for_role(role)
        kit: dict[str, Any] = {
            "company": company,
            "role": role,
            "questions": questions,
            "question_source": source,
            "roadmap": roadmap,
            "checklist": list(_CHECKLIST),
            "note": None,
        }
        if not questions:
            kit["note"] = ("No matching questions found in the interview bank "
                           "for this company/role — the checklist and roadmap "
                           "above still apply.")
        return kit
    except Exception:
        return {
            "company": company,
            "role": role,
            "questions": [],
            "question_source": None,
            "roadmap": None,
            "checklist": list(_CHECKLIST),
            "note": "Interview kit could not be built right now.",
        }
