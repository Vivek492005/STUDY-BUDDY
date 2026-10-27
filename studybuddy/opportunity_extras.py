"""Phase 3 radar helpers: match scoring, deadline alerts, ICS export.

All helpers are pure over the opportunities dataset + the career snapshot
and never raise: a snapshot failure yields empty matches with a note, never
a 500. Match scoring weights (documented, fixed):

- Cluster overlap: +40 when the opportunity's tags (or title words) overlap
  the user's discovery cluster keywords. One reason names the cluster.
- Skill overlap: +10 per user skill found in the opportunity title, tags or
  eligibility text, capped at +40. One reason per matched skill.
- Both-factor bonus: +20 when cluster AND at least one skill match.
- Total capped at 100. Score 0 entries are excluded from matches.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

# Cluster id -> keywords matched against opportunity tags/title (lowercase).
CLUSTER_KEYWORDS: dict[str, set[str]] = {
    "swe": {"software", "coding", "programming", "developer", "engineering",
            "hackathon", "code"},
    "data_ai": {"data", "ai", "machine", "learning", "ml", "analytics",
                "python"},
    "design": {"design", "ui", "ux", "creative"},
    "security": {"security", "cyber", "hacking", "ethical"},
    "cloud": {"cloud", "aws", "azure", "devops"},
    "pm": {"product", "management", "business", "mba"},
    "marketing": {"marketing", "brand", "digital-marketing"},
    "analytics": {"analytics", "data", "statistics"},
    "devops": {"devops", "cloud", "ci", "docker", "kubernetes",
               "infrastructure"},
    "mobile": {"mobile", "android", "ios", "app"},
    "qa_testing": {"testing", "qa", "quality"},
    "embedded": {"embedded", "iot", "robotics", "hardware", "electronics"},
    "game_dev": {"game", "gaming", "unity", "unreal"},
    "data_engineering": {"data", "pipeline", "etl", "sql", "warehouse",
                         "spark"},
}


def raw_opportunities() -> list[dict[str, Any]]:
    """Raw dataset entries (with tags/status/description), never raising."""
    try:
        from studybuddy.opportunities import load_dataset
        data = load_dataset()
        opps = data.get("opportunities") or []
        return [o for o in opps if isinstance(o, dict)]
    except Exception:
        return []


def raw_opportunity(opp_id: str) -> dict[str, Any] | None:
    for o in raw_opportunities():
        if o.get("id") == opp_id:
            return o
    return None


def is_open(o: dict[str, Any], today: date | None = None) -> bool:
    """Open = status field is not 'closed' AND deadline is None or >= today.

    The status field is stamped by scripts/refresh_opportunities.py; the
    deadline check keeps the answer correct even before the script runs.
    """
    if o.get("status") == "closed":
        return False
    today = today or date.today()
    dl = o.get("deadline")
    if dl is None:
        return True
    try:
        year, month, day = (int(p) for p in str(dl).split("-"))
        return date(year, month, day) >= today
    except (ValueError, TypeError):
        return False


def days_left(o: dict[str, Any], today: date | None = None) -> int | None:
    dl = o.get("deadline")
    if dl is None:
        return None
    today = today or date.today()
    try:
        year, month, day = (int(p) for p in str(dl).split("-"))
        return (date(year, month, day) - today).days
    except (ValueError, TypeError):
        return None


def eligibility_list(o: dict[str, Any]) -> list[str]:
    """Checklist form of the stored eligibility string.

    Entries authored with ';' separators split into one item per criterion;
    prose entries stay a single item. Nothing is invented: the list never
    contains a criterion that is not in the stored string.
    """
    text = (o.get("eligibility") or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in text.split(";") if p.strip()]
    return parts or [text]


def _opp_blob(o: dict[str, Any]) -> tuple[str, set[str]]:
    tags = {str(t).lower() for t in (o.get("tags") or [])
            if isinstance(t, str)}
    title_words = set(re.findall(r"[a-z][a-z0-9+]+",
                                 str(o.get("title") or "").lower()))
    blob = " ".join([str(o.get("title") or ""), " ".join(sorted(tags)),
                     str(o.get("eligibility") or "")]).lower()
    return blob, tags | title_words


def score_opportunity(o: dict[str, Any], cluster_id: str | None,
                      cluster_name: str | None,
                      skills: list[str]) -> tuple[int, list[str]]:
    """Return (score 0-100, reasons) for one opportunity."""
    blob, tokens = _opp_blob(o)
    reasons: list[str] = []
    score = 0
    cluster_hit = False
    if cluster_id and cluster_id in CLUSTER_KEYWORDS:
        keywords = CLUSTER_KEYWORDS[cluster_id]
        if keywords & tokens:
            cluster_hit = True
            score += 40
            reasons.append(
                f"Matches your {cluster_name or cluster_id} cluster")
    matched_skills: list[str] = []
    for skill in skills:
        s = (skill or "").strip().lower()
        if not s:
            continue
        # Whole-word match against title/tags/eligibility blob.
        if re.search(r"(?<![a-z0-9+])" + re.escape(s) + r"(?![a-z0-9+])",
                     blob):
            matched_skills.append(skill.strip())
    if matched_skills:
        score += min(40, 10 * len(matched_skills))
        for s in matched_skills:
            reasons.append(f"{s} appears in your skills")
    if cluster_hit and matched_skills:
        score += 20
    return min(100, score), reasons


def match_opportunities(user_id: int, top_n: int = 20):
    """Return (matches, note). Never raises."""
    try:
        from studybuddy.career_graph import career_snapshot
        snapshot, _warnings = career_snapshot(user_id)
    except Exception:
        return [], "Career snapshot unavailable — matches need your profile."
    cluster = snapshot.get("cluster") or {}
    cluster_id = cluster.get("id")
    cluster_name = cluster.get("name")
    skills = [s for s in (snapshot.get("skills") or [])
              if isinstance(s, str)]
    if not cluster_id and not skills:
        return [], ("Take the career discovery assessment or add skills to "
                    "your portfolio for personalised matches.")
    today = date.today()
    scored = []
    for o in raw_opportunities():
        if not is_open(o, today):
            continue
        score, reasons = score_opportunity(o, cluster_id, cluster_name,
                                           skills)
        if score <= 0 or not reasons:
            continue
        scored.append({
            "opp_id": o["id"],
            "title": o.get("title"),
            "url": o.get("link"),
            "deadline": o.get("deadline"),
            "score": score,
            "reasons": reasons,
        })
    scored.sort(key=lambda m: (-m["score"], m["title"] or ""))
    return scored[:top_n], None


def bookmark_alerts(user_id: int, bookmarked_ids: set[str]):
    """Bucketed deadline alerts over the user's bookmarked OPEN opps.

    Buckets are cumulative thresholds: due_1d (days_left <= 1), due_3d
    (<= 3), due_7d (<= 7); days_left >= 0 only.
    """
    today = date.today()
    buckets = {"due_1d": [], "due_3d": [], "due_7d": []}
    for o in raw_opportunities():
        if o.get("id") not in bookmarked_ids:
            continue
        if not is_open(o, today):
            continue
        dl = days_left(o, today)
        if dl is None or dl < 0:
            continue
        item = {"opp_id": o["id"], "title": o.get("title"),
                "deadline": o.get("deadline"), "days_left": dl,
                "url": o.get("link")}
        if dl <= 1:
            buckets["due_1d"].append(item)
        if dl <= 3:
            buckets["due_3d"].append(item)
        if dl <= 7:
            buckets["due_7d"].append(item)
    for key in buckets:
        buckets[key].sort(key=lambda i: i["days_left"])
    return buckets


def _ics_escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def _ics_fold(line: str) -> str:
    """Fold a single ICS line at 75 octets (continuation starts with space)."""
    out = []
    while len(line.encode("utf-8")) > 75:
        cut = 75
        while len(line[:cut].encode("utf-8")) > 75:
            cut -= 1
        out.append(line[:cut])
        line = " " + line[cut:]
    out.append(line)
    return "\r\n".join(out)


def build_ics(o: dict[str, Any]) -> str | None:
    """One VEVENT for the opportunity's deadline, or None without one."""
    dl = o.get("deadline")
    if not dl:
        return None
    try:
        year, month, day = (int(p) for p in str(dl).split("-"))
        dt = date(year, month, day).strftime("%Y%m%d")
    except (ValueError, TypeError):
        return None
    summary = _ics_escape(str(o.get("title") or "Opportunity deadline"))
    desc = _ics_escape(f"{o.get('organization') or ''}\n{o.get('link') or ''}"
                       .strip())
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//STUDY-BUDDY//Opportunity Radar//EN",
        "BEGIN:VEVENT",
        f"UID:{o['id']}@study-buddy",
        f"DTSTART;VALUE=DATE:{dt}",
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{desc}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(_ics_fold(line) for line in lines) + "\r\n"
