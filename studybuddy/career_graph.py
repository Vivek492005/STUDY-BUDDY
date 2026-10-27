"""Career graph snapshot (Phase 0 foundations).

READ-ONLY aggregation of a student's existing career data into one dict, so
every Career-section feature can share the same picture of the student
instead of keeping private copies of the truth.

Never raises: each section is guarded, and failures yield null/empty values
plus a warnings[] entry. Models are imported lazily (like momentum._models)
so this module never creates a circular import with unified_app.
"""

from __future__ import annotations

from typing import Any


def _models() -> tuple[Any, ...]:
    from unified_app import (
        CareerAssessment,
        Goal,
        HealthReview,
        JobApplication,
        OHBooking,
        OpportunityBookmark,
        Portfolio,
        StreakState,
        XPLedger,
    )

    return (CareerAssessment, Goal, HealthReview, JobApplication, OHBooking,
            OpportunityBookmark, Portfolio, StreakState, XPLedger)


def career_snapshot(user_id: int) -> tuple[dict[str, Any], list[str]]:
    """Return (snapshot, warnings) for one user. Never raises."""
    from studybuddy.career_assessment import CLUSTERS
    from studybuddy.momentum import level_for_xp

    (CareerAssessment, Goal, HealthReview, JobApplication, OHBooking,
     OpportunityBookmark, Portfolio, StreakState,
     XPLedger) = _models()

    warnings: list[str] = []
    snapshot: dict[str, Any] = {
        "cluster": None,
        "skills": [],
        "applications": {"total": 0, "by_stage": {}},
        "goals": [],
        "momentum": {"xp": None, "level": None, "streak": None},
        "health": None,
        "portfolio": {"exists": False, "slug": None},
        "mentors": {"bookings_count": 0},
        "opportunities_bookmarked": 0,
    }

    # Top cluster from the latest assessment.
    try:
        latest = (CareerAssessment.query.filter_by(user_id=user_id)
                  .order_by(CareerAssessment.created_at.desc()).first())
        if latest is not None and latest.top_clusters:
            top = latest.top_clusters
            if isinstance(top, list) and top:
                first = top[0]
                cid = first.get("id") or first.get("cluster") if isinstance(
                    first, dict) else first
            elif isinstance(top, dict) and top:
                cid = max(top, key=lambda k: (top[k] or {}).get(
                    "score", 0) if isinstance(top[k], dict) else top[k])
            else:
                cid = None
            if cid:
                info = CLUSTERS.get(cid, {}) if isinstance(cid, str) else {}
                snapshot["cluster"] = {
                    "id": cid,
                    "name": info.get("name") or str(cid),
                }
    except Exception:
        warnings.append("cluster unavailable")

    # Skills live on the portfolio (User has no skills field).
    try:
        portfolio = Portfolio.query.filter_by(user_id=user_id).first()
        if portfolio is not None:
            snapshot["skills"] = list(portfolio.skills or [])
            snapshot["portfolio"] = {"exists": True, "slug": portfolio.slug}
    except Exception:
        warnings.append("portfolio unavailable")

    # Applications by stage.
    try:
        apps = JobApplication.query.filter_by(user_id=user_id).all()
        by_stage: dict[str, int] = {}
        for a in apps:
            by_stage[a.stage] = by_stage.get(a.stage, 0) + 1
        snapshot["applications"] = {"total": len(apps), "by_stage": by_stage}
    except Exception:
        warnings.append("applications unavailable")

    # Goals (titles only — the snapshot is a map, not the data itself).
    try:
        goals = (Goal.query.filter_by(user_id=user_id)
                 .order_by(Goal.created_at.desc()).all())
        snapshot["goals"] = [g.title for g in goals]
    except Exception:
        warnings.append("goals unavailable")

    # Momentum: XP sum + level + current streak (pure reads, no
    # get_momentum_summary call — that finalizes league weeks as a side
    # effect, and this snapshot must stay read-only).
    try:
        rows = XPLedger.query.filter_by(user_id=user_id).all()
        xp = sum(int(r.xp or 0) for r in rows)
        st = StreakState.query.filter_by(user_id=user_id).first()
        snapshot["momentum"] = {
            "xp": xp,
            "level": level_for_xp(xp),
            "streak": int(st.current or 0) if st else 0,
        }
    except Exception:
        warnings.append("momentum unavailable")

    # Latest monthly health review.
    try:
        review = (HealthReview.query.filter_by(user_id=user_id)
                  .order_by(HealthReview.month_key.desc()).first())
        if review is not None:
            snapshot["health"] = {
                "month_key": review.month_key,
                "dimensions": review.dimensions_json or {},
            }
    except Exception:
        warnings.append("health unavailable")

    # Mentor bookings (cancelled seats don't count).
    try:
        snapshot["mentors"] = {
            "bookings_count": OHBooking.query.filter_by(
                user_id=user_id).filter(
                    OHBooking.status != "cancelled").count(),
        }
    except Exception:
        warnings.append("mentors unavailable")

    # Bookmarked opportunities.
    try:
        snapshot["opportunities_bookmarked"] = (
            OpportunityBookmark.query.filter_by(user_id=user_id).count())
    except Exception:
        warnings.append("opportunities unavailable")

    return snapshot, warnings
