"""Role cards for the career-discovery result page (Phase 2).

Two honest sources, neither pretending to be live openings:
- data/role_cards.json: curated role-exploration cards (title, cluster,
  skills, description). No company, no salary, no "hiring now" claims.
- data/jobs.csv titles: the existing resume-matching corpus contributes
  role TITLES (and their skill lists) only — company, location, and salary
  columns are deliberately dropped, so a card never looks like a listing.

jobs.csv is NOT extended with invented rows: its rows carry real company
names and feed resume matching, so adding company rows would fabricate
openings. Role cards are a separate, honestly labelled dataset.
"""

from __future__ import annotations

import csv
import json
import os
from functools import lru_cache

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data")

# Keywords matched against (title + required_skills), lowercased.
CLUSTER_KEYWORDS = {
    "swe": ["software engineer", "software developer", "full-stack",
            "full stack", "backend", "frontend", "systems engineer",
            "web developer", "application developer"],
    "data_ai": ["data scientist", "machine learning", "ml engineer",
                "ai engineer", "nlp", "computer vision", "deep learning",
                "research scientist"],
    "design": ["ui ", "ux ", "ui/", "ux/", "product designer",
               "interaction designer", "visual designer", "designer"],
    "security": ["security", "penetration", "soc analyst", "infosec",
                 "cyber"],
    "cloud": ["cloud", "solutions architect", "aws", "azure", "gcp"],
    "pm": ["product manager", "product owner", "program manager"],
    "marketing": ["marketing", "seo", "content", "social media", "growth"],
    "analytics": ["analyst", "business intelligence", "bi ", "bi developer",
                  "reporting"],
    "devops": ["devops", "site reliability", "sre", "platform engineer",
               "release engineer", "infrastructure engineer"],
    "mobile": ["mobile", "android", "ios", "flutter", "react native"],
    "qa_testing": ["qa", "quality assurance", "test engineer", "sdet",
                   "automation engineer", "tester"],
    "embedded": ["embedded", "firmware", "iot ", "robotics",
                 "microcontroller"],
    "game_dev": ["game", "gameplay", "level designer", "technical artist",
                 "graphics programmer"],
    "data_engineering": ["data engineer", "etl", "big data",
                         "data platform", "data architect", "data warehouse"],
}


@lru_cache(maxsize=1)
def _load_curated() -> list[dict]:
    path = os.path.join(_DATA_DIR, "role_cards.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    cards = data.get("cards") if isinstance(data, dict) else None
    if not isinstance(cards, list):
        return []
    out = []
    for c in cards:
        if not isinstance(c, dict) or not c.get("title") or not c.get("cluster"):
            continue
        out.append({
            "title": str(c["title"])[:120],
            "cluster": str(c["cluster"]),
            "skills": [str(s)[:60] for s in (c.get("skills") or [])[:8]
                       if isinstance(s, str)],
            "description": str(c.get("description") or "")[:400],
            "source": "curated",
        })
    return out


@lru_cache(maxsize=1)
def _load_job_titles() -> list[dict]:
    """Distinct job titles from jobs.csv (titles + skills only)."""
    path = os.path.join(_DATA_DIR, "jobs.csv")
    seen = set()
    out = []
    try:
        with open(path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                title = (row.get("title") or "").strip()
                if not title or "," in title:
                    continue  # ragged row: shifted columns, unreliable
                key = title.lower()
                if key in seen:
                    continue
                seen.add(key)
                skills = [s.strip() for s in
                          (row.get("required_skills") or "").split(",")
                          if s.strip()][:8]
                out.append({"title": title, "skills": skills,
                            "description": (row.get("description") or "")
                            .strip()[:400]})
    except (OSError, ValueError):
        return []
    return out


def _matches_cluster(text: str, cluster_id: str) -> bool:
    hay = text.lower()
    return any(kw in hay for kw in CLUSTER_KEYWORDS.get(cluster_id, []))


def role_cards_for_cluster(cluster_id: str, limit: int = 12) -> list[dict]:
    """Up to `limit` role cards for one cluster: curated first, then
    jobs.csv titles that match the cluster (titles only, no company or
    salary). Never raises."""
    try:
        cards = [dict(c) for c in _load_curated()
                 if c["cluster"] == cluster_id]
        have = {c["title"].lower() for c in cards}
        if len(cards) < limit:
            for job in _load_job_titles():
                if len(cards) >= limit:
                    break
                if job["title"].lower() in have:
                    continue
                hay = job["title"] + " " + " ".join(job["skills"])
                if _matches_cluster(hay, cluster_id):
                    cards.append({
                        "title": job["title"],
                        "cluster": cluster_id,
                        "skills": job["skills"],
                        "description": job["description"],
                        "source": "job_titles",
                    })
                    have.add(job["title"].lower())
        return cards[:limit]
    except Exception:
        return []


def role_cards_for_clusters(cluster_ids, per_cluster: int = 12) -> dict:
    """{cluster_id: [cards]} for each id. Never raises."""
    try:
        return {cid: role_cards_for_cluster(cid, limit=per_cluster)
                for cid in cluster_ids}
    except Exception:
        return {}


def enrich_cluster(cluster: dict, per_cluster: int = 12) -> dict:
    """Add handoff fields to one assessment result cluster dict:
    roadmap_url / quiz_url (None when no real page exists — never a dead
    link) and role_cards. Returns the same dict (mutated). Never raises."""
    try:
        from studybuddy.career_assessment import CLUSTER_HANDOFFS
        hands = CLUSTER_HANDOFFS.get(cluster.get("id") or {}, {})
        skill = hands.get("roadmap_skill")
        cluster["roadmap_url"] = (f"/learn/roadmaps/{skill}"
                                  if skill else None)
        cluster["roadmap_skill"] = skill
        quiz = hands.get("quiz_skill")
        cluster["quiz_url"] = "/learn/quizzes" if quiz else None
        cluster["quiz_skill"] = quiz
        cluster["role_cards"] = role_cards_for_cluster(
            cluster.get("id") or "", limit=per_cluster)
    except Exception:
        cluster.setdefault("roadmap_url", None)
        cluster.setdefault("quiz_url", None)
        cluster.setdefault("role_cards", [])
    return cluster


def enrich_clusters(clusters, per_cluster: int = 12) -> list[dict]:
    """Enrich every cluster in a result list. Never raises."""
    try:
        return [enrich_cluster(dict(c), per_cluster=per_cluster)
                for c in (clusters or []) if isinstance(c, dict)]
    except Exception:
        return list(clusters or [])
