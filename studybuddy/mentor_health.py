"""Phase 5 (Career master plan): mentor matching + health-review helpers.

Pure functions only — no Flask, no DB imports at module level (callers
import models lazily, the same pattern studybuddy/community.py uses).

HONESTY RULES (hard):
- A seed mentor (is_seed=True) is an ILLUSTRATIVE profile, never a real
  person offering mentorship. Serializers must report
  ``illustrative=True`` and ``verified=False`` for seeds, even if an old
  database row still stores verified=True.
- Match scores, review averages, and peer benchmarks derive ONLY from
  real recorded rows. Missing data is reported as null/empty, never as
  a plausible-looking default.
"""

import json
import os
import re
from datetime import date, timedelta

# ---------------------------------------------------------------------------
# Mentor matching
# ---------------------------------------------------------------------------

# Career-assessment cluster id -> mentor path key. Only clear mappings are
# listed: pm/marketing have no honest mentor-path equivalent, so they earn
# no path points (skill points still apply).
CLUSTER_PATHS = {
    "swe": "engineering",
    "cloud": "engineering",
    "devops": "engineering",
    "embedded": "engineering",
    "game_dev": "engineering",
    "mobile": "engineering",
    "qa_testing": "engineering",
    "security": "engineering",
    "data_engineering": "engineering",
    "data_ai": "engineering",
    "analytics": "engineering",
    "design": "design",
}

PATH_LABELS = {
    "engineering": "Engineering & Tech",
    "medicine": "Medicine & Healthcare",
    "law": "Law",
    "commerce": "Commerce & Finance",
    "humanities": "Humanities & Social Sciences",
    "design": "Design & Creative",
    "other": "Other Paths",
}

# Fixed, documented weights. Total cap is 100.
MATCH_PATH_POINTS = 40
MATCH_SKILL_POINTS_EACH = 10
MATCH_SKILL_POINTS_CAP = 40
MATCH_VERIFIED_POINTS = 20

_TOKEN_RE = re.compile(r"[a-z0-9+#]+")


def _tokens(text):
    return set(_TOKEN_RE.findall((text or "").lower()))


def mentor_to_dict(mentor):
    """Serialize one mentor with the Phase 5 honesty labels.

    ``illustrative`` is True exactly when the row is a seed.
    ``verified`` is True only for a non-seed row stored as verified —
    a seed never reports verified, regardless of the stored flag.
    """
    is_seed = bool(getattr(mentor, "is_seed", False))
    return {
        "id": mentor.id,
        "name": mentor.name,
        "headline": mentor.headline,
        "path": mentor.path,
        "company": getattr(mentor, "company", None),
        "college": getattr(mentor, "college", None),
        "bio": getattr(mentor, "bio", None),
        "linkedin_url": getattr(mentor, "linkedin_url", None),
        "illustrative": is_seed,
        "verified": bool(getattr(mentor, "verified", False)) and not is_seed,
        "label": ("Illustrative profile" if is_seed
                  else ("Community mentor" if getattr(
                      mentor, "verified", False) else "Mentor")),
    }


def mentor_match_scores(cluster_id, portfolio_skills, mentors):
    """Score mentors against a user's cluster + portfolio skills.

    Scoring (fixed weights, documented here so the UI can explain them):
    - mentor.path == CLUSTER_PATHS[cluster_id]: +40, reason names the path.
    - each portfolio skill whose lowercased token appears in the mentor's
      headline/bio/company text: +10, capped at +40, reason lists skills.
    - real verified mentor (not a seed): +20, reason says so.
    Cap 100, sort desc, top 10. Every score > 0 carries >= 1 reason.
    Seeds are included but always flagged illustrative by the caller
    (mentor_to_dict); this function never invents a skill or a path.
    """
    want_path = CLUSTER_PATHS.get(cluster_id or "")
    skills = [str(s).strip().lower() for s in (portfolio_skills or [])
              if str(s).strip()]
    scored = []
    for m in mentors:
        score = 0
        reasons = []
        if want_path and (m.path or "") == want_path:
            score += MATCH_PATH_POINTS
            reasons.append("Shares your path: %s"
                           % PATH_LABELS.get(want_path, want_path))
        hay = _tokens(" ".join([m.headline or "", m.bio or "",
                                m.company or ""]))
        matched = [s for s in skills if s in hay or
                   any(tok in hay for tok in _TOKEN_RE.findall(s))]
        if matched:
            score += min(MATCH_SKILL_POINTS_CAP,
                         MATCH_SKILL_POINTS_EACH * len(matched))
            reasons.append("Skill overlap: %s" % ", ".join(sorted(matched)))
        if getattr(m, "verified", False) and not getattr(m, "is_seed", False):
            score += MATCH_VERIFIED_POINTS
            reasons.append("Verified community mentor")
        score = min(100, score)
        if score > 0 and not reasons:  # defensive: never an unexplained score
            reasons.append("General match")
        scored.append((score, m, reasons))
    scored.sort(key=lambda t: (-t[0], (t[1].name or "").lower()))
    out = []
    for score, m, reasons in scored[:10]:
        d = mentor_to_dict(m)
        d["match_score"] = score
        d["reasons"] = reasons
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Mentor Q&A library (data/mentor_qa.json)
# ---------------------------------------------------------------------------

_QA_CACHE = None
QA_ATTRIBUTION = "Community knowledge base"


def load_mentor_qa():
    """Load the curated Q&A library from data/mentor_qa.json.

    Entries are general career knowledge, attributed to the community
    knowledge base — never to a named mentor who did not write them.
    Returns a list of {question, answer, topic} dicts (cached).
    """
    global _QA_CACHE
    if _QA_CACHE is not None:
        return _QA_CACHE
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "data", "mentor_qa.json")
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        rows = []
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        q = (row.get("question") or "").strip()
        a = (row.get("answer") or "").strip()
        t = (row.get("topic") or "").strip() or "general"
        if q and a:
            clean.append({"question": q, "answer": a, "topic": t})
    _QA_CACHE = clean
    return clean


# ---------------------------------------------------------------------------
# Health-review helpers: week/month keys + 1-5 score scale
# ---------------------------------------------------------------------------

def iso_week_key(d):
    """ISO week key 'YYYY-Www' for a date."""
    iso = d.isocalendar()
    return "%04d-W%02d" % (iso[0], iso[1])


def last_n_week_keys(n, today=None):
    """The last n ISO week keys ending with the current week, newest last."""
    today = today or date.today()
    keys = []
    d = today
    for _ in range(n):
        keys.append(iso_week_key(d))
        d = d - timedelta(days=7)
    return list(reversed(keys))


def last_n_month_keys(n, today=None):
    """The last n month keys 'YYYY-MM' ending with the current month."""
    today = today or date.today()
    keys = []
    y, m = today.year, today.month
    for _ in range(n):
        keys.append("%04d-%02d" % (y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(keys))


def review_score_to_1to5(score_0_100):
    """Convert a monthly-review dimension score (0-100) to the 1-5 pulse
    scale so reviews and pulses share one scale in trends/action plans.
    None stays None (locked/unknown is never reported as a number)."""
    if score_0_100 is None:
        return None
    try:
        v = float(score_0_100)
    except (TypeError, ValueError):
        return None
    return max(1, min(5, int(round(v / 20.0))))


# Per-dimension action links. Every URL is a real GET route in this app:
# /assessment (career discovery), /portfolio (builder), /opportunities
# (radar), /interview (mock-interview bank), /momentum (streaks/reflect).
ACTION_PLAN_ACTIONS = {
    "direction": [
        {"title": "Retake the career discovery assessment",
         "feature": "Career discovery", "url": "/assessment"},
        {"title": "Explore roadmaps for your top cluster",
         "feature": "Learning roadmaps", "url": "/learn/roadmaps"},
    ],
    "proof": [
        {"title": "Strengthen your portfolio with a new section",
         "feature": "Portfolio builder", "url": "/portfolio"},
        {"title": "Re-score your resume and fix the checklist",
         "feature": "Resume scorer", "url": "/resume"},
    ],
    "opportunity": [
        {"title": "Check your opportunity matches",
         "feature": "Opportunity radar", "url": "/opportunities"},
        {"title": "Move a wishlist item to applied",
         "feature": "Application tracker", "url": "/tracker"},
    ],
    "preparation": [
        {"title": "Run a mock interview for your target role",
         "feature": "Mock interviews", "url": "/interview"},
        {"title": "Drill your weakest quiz skill",
         "feature": "Skill quizzes", "url": "/learn/quizzes"},
    ],
    "wellbeing": [
        {"title": "Reflect on your week in Momentum",
         "feature": "Momentum", "url": "/momentum"},
        {"title": "Review this month's health narrative",
         "feature": "Health review", "url": "/review"},
    ],
}

DIMENSION_LABELS = {
    "direction": "Direction",
    "proof": "Proof",
    "opportunity": "Opportunity",
    "preparation": "Preparation",
    "wellbeing": "Wellbeing",
}


def median(values):
    """Median of a non-empty numeric list (float); None when empty."""
    vals = sorted(values)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    if n % 2 == 1:
        return float(vals[mid])
    return (vals[mid - 1] + vals[mid]) / 2.0
