"""Deadline engine (Phase 0 foundations).

One sorted list of everything time-sensitive in a student's career life:
tracker deadlines they set themselves, plus deadlines of opportunities they
bookmarked. Pure reads, never raises: any failure yields an empty list.
"""

from __future__ import annotations

from datetime import date
from typing import Any

DEADLINE_KINDS = ("oa", "interview", "followup", "offer", "other")


def _parse_deadline(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def upcoming_deadlines(user_id: int, days: int = 30,
                       today: date | None = None) -> list[dict[str, Any]]:
    """Deadlines in [today, today+days], sorted by date ascending."""
    today = today or date.today()
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 30
    days = max(0, min(365, days))
    end = date.fromordinal(today.toordinal() + days)

    from unified_app import JobApplication, OpportunityBookmark

    out: list[dict[str, Any]] = []
    try:
        apps = JobApplication.query.filter_by(user_id=user_id).all()
        for a in apps:
            d = a.deadline
            if d is None or not (today <= d <= end):
                continue
            out.append({
                "title": f"{a.company} — {a.role}",
                "date": d.isoformat(),
                "kind": a.deadline_kind or "other",
                "source": "tracker",
                "ref_id": a.id,
                "days_left": (d - today).days,
            })
    except Exception:
        pass

    try:
        from studybuddy.opportunities import get_opportunity

        bookmarks = OpportunityBookmark.query.filter_by(
            user_id=user_id).all()
        for b in bookmarks:
            try:
                opp = get_opportunity(b.opp_id)
            except Exception:
                opp = None
            if not opp:
                continue
            d = _parse_deadline(opp.get("deadline"))
            if d is None or not (today <= d <= end):
                continue
            out.append({
                "title": opp.get("title") or b.opp_id,
                "date": d.isoformat(),
                "kind": "opportunity",
                "source": "opportunity",
                "ref_id": b.opp_id,
                "days_left": (d - today).days,
            })
    except Exception:
        pass

    out.sort(key=lambda e: (e["date"], e["title"]))
    return out
