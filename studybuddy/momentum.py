"""Humane Momentum System (Wave 5 #27): XP, quests, streaks with forgiveness,
weekly leagues, and weekly reflections.

Design philosophy: reward *meaningful career actions* — not raw screen time.
Actions earn XP only when they improve readiness (a completed quiz, a solved
DSA problem, a finished mock interview, ...). Streaks forgive: two shields
per calendar month auto-repair a missed day, and a broken streak resets
gracefully to 1, never 0. Opt-out is always one click away and makes the
whole system a no-op.

Module layout (mirrors the codebase convention: feature modules stay free of
top-level Flask imports so there are no circular imports with unified_app):

- Pure rules & date math: XP_RULES, QUEST_TEMPLATES, level curve, streak
  transition (apply_streak), ISO-week helpers. Never raises, fully testable.
- DB-backed operations: record_event, claim_quest, join_league, leave_league,
  save_reflection, set_opt_out, finalize_league_week, get_momentum_summary.
  These import unified_app's models lazily (inside the function) and never
  raise — every public entry point is wrapped in try/except with a rollback.

Idempotency: every daily event is recorded at most once per
(user, event_type, day) via the XPLedger.dedupe_key unique constraint
("<user_id>:<event_type>:<YYYY-MM-DD>"). Duplicates are skipped silently —
never an exception. Weekly counting (quests, leagues) is derived from those
same dedupe keys, so it is timezone-safe on both SQLite and Postgres.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: XP awarded per event type. Progress toward evidence, not screen time.
XP_RULES = {
    "quiz_completed": 20,
    "dsa_solved": 25,
    "mock_completed": 30,
    "resume_analyzed": 15,
    "roadmap_step": 10,
    "flashcards_reviewed": 15,
    "aptitude_completed": 20,
    "coach_analyzed": 15,
    "star_saved": 10,
    "paper_quiz_completed": 20,
    "peer_session_completed": 40,
    "retrospective_logged": 15,
    "mentor_session_attended": 30,
    "mentor_feedback_given": 10,
    "health_review_completed": 20,
    "quest_completed": 50,  # awarded only via claim_quest(), never directly
}
# NOTE (Phase 4): career XP values live in CAREER_XP_ACTIONS below, NOT
# in XP_RULES. XP_RULES is pinned by existing tests (16 entries) and
# drives record_event()'s daily dedupe; career actions flow ONLY through
# career_xp_hook() with per-ref idempotency, so they must not enter the
# daily-dedupe set — two tracker adds on the same day both pay.

#: Event types that record_event() accepts directly. quest_completed is
#: bonus-only: it must flow through claim_quest() so the bonus stays tied to
#: an actually-completed quest.
DIRECT_EVENTS = frozenset(e for e in XP_RULES if e != "quest_completed")

#: One-time welcome bonus for the user's very first XP event ("day-one win").
DAY_ONE_BONUS_XP = 25
DAY_ONE_EVENT = "day_one_win"

#: Streak shields per calendar month: missed days auto-repaired, no anxiety.
SHIELDS_PER_MONTH = 2

#: Weekly quest templates. Progress = count of the matching ledger event in
#: the current ISO week. Targets stay <= 7 because every event is
#: idempotent per (user, event, day) — consistency over grinding.
QUEST_TEMPLATES = [
    {"id": "quiz-3",
     "title": "Quiz triple",
     "description": "Complete 3 skill quizzes this week",
     "event_type": "quiz_completed", "target": 3},
    {"id": "dsa-5",
     "title": "Steady solver",
     "description": "Solve 5 DSA problems this week",
     "event_type": "dsa_solved", "target": 5},
    {"id": "mock-1",
     "title": "Mock milestone",
     "description": "Finish a mock interview this week",
     "event_type": "mock_completed", "target": 1},
    {"id": "flash-4",
     "title": "Flashcard flow",
     "description": "Review flashcards on 4 days this week",
     "event_type": "flashcards_reviewed", "target": 4},
    {"id": "apt-2",
     "title": "Aptitude ace",
     "description": "Complete 2 timed aptitude tests this week",
     "event_type": "aptitude_completed", "target": 2},
    {"id": "reflect-1",
     "title": "Look back",
     "description": "Log your weekly reflection",
     "event_type": "retrospective_logged", "target": 1},
    # ---- Phase 4 career quests (18) ----
    # Quests with a "source" count verifiable source rows in the current
    # ISO week (see _count_source_in_week) instead of ledger dedupe keys,
    # because career actions are per-ref, not per-day: two tracker adds on
    # the same day must both count. The original 6 templates are untouched.
    {"id": "apply-2",
     "title": "Apply twice",
     "description": "Add 2 applications to the tracker this week",
     "event_type": "tracker_added", "target": 2, "source": "tracker_added"},
    {"id": "apply-5",
     "title": "Pipeline builder",
     "description": "Add 5 applications to the tracker this week",
     "event_type": "tracker_added", "target": 5, "source": "tracker_added"},
    {"id": "move-2",
     "title": "Keep it moving",
     "description": "Move 2 applications to a new stage this week",
     "event_type": "tracker_moved", "target": 2, "source": "tracker_moved"},
    {"id": "deadlines-2",
     "title": "Deadline keeper",
     "description": "Set deadlines on 2 applications this week",
     "event_type": "deadline_set", "target": 2, "source": "deadline_set"},
    {"id": "refer-1",
     "title": "Ask once",
     "description": "Ask 1 person for a referral this week",
     "event_type": "referral_asked", "target": 1, "source": "referral_asked"},
    {"id": "refer-3",
     "title": "Network week",
     "description": "Ask 3 people for referrals this week",
     "event_type": "referral_asked", "target": 3, "source": "referral_asked"},
    {"id": "retro-1",
     "title": "Learn from a no",
     "description": "Complete 1 rejection retrospective this week",
     "event_type": "retrospective_completed", "target": 1,
     "source": "retrospective_completed"},
    {"id": "portfolio-1",
     "title": "Portfolio polish",
     "description": "Update your portfolio this week",
     "event_type": "portfolio_updated", "target": 1,
     "source": "portfolio_updated"},
    {"id": "bookmark-3",
     "title": "Scout three",
     "description": "Bookmark 3 opportunities this week",
     "event_type": "opportunity_bookmarked", "target": 3,
     "source": "opportunity_bookmarked"},
    {"id": "bookmark-5",
     "title": "Opportunity radar",
     "description": "Bookmark 5 opportunities this week",
     "event_type": "opportunity_bookmarked", "target": 5,
     "source": "opportunity_bookmarked"},
    {"id": "assess-1",
     "title": "Know yourself",
     "description": "Complete a career-discovery assessment this week",
     "event_type": "assessment_completed", "target": 1,
     "source": "assessment_completed"},
    {"id": "salary-1",
     "title": "Know your worth",
     "description": "Evaluate 1 offer against salary bands this week",
     "event_type": "salary_evaluated", "target": 1,
     "source": "salary_evaluated"},
    {"id": "salary-2",
     "title": "Compare two",
     "description": "Evaluate 2 offers this week",
     "event_type": "salary_evaluated", "target": 2,
     "source": "salary_evaluated"},
    {"id": "mentor-1",
     "title": "Book a mentor",
     "description": "Book 1 mentor office-hour session this week",
     "event_type": "mentor_booked", "target": 1, "source": "mentor_booked"},
    # Ledger-event career quests (daily-deduped like the original 6).
    {"id": "resume-1",
     "title": "Resume refresh",
     "description": "Analyze a resume this week",
     "event_type": "resume_analyzed", "target": 1},
    {"id": "star-1",
     "title": "Story bank",
     "description": "Save 1 STAR interview story this week",
     "event_type": "star_saved", "target": 1},
    {"id": "peer-1",
     "title": "Practice with a peer",
     "description": "Complete 1 peer mock session this week",
     "event_type": "peer_session_completed", "target": 1},
    {"id": "coach-1",
     "title": "Coach check-in",
     "description": "Run 1 communication-coach analysis this week",
     "event_type": "coach_analyzed", "target": 1},
]

QUEST_IDS = frozenset(q["id"] for q in QUEST_TEMPLATES)

#: Every event type that can ever appear in the ledger (for weekly key sets).
ALL_EVENT_TYPES = frozenset(XP_RULES) | {DAY_ONE_EVENT}


# ---------------------------------------------------------------------------
# Pure helpers: dates, levels, streaks (never raise, no DB)
# ---------------------------------------------------------------------------

def _coerce_now(now: datetime | None) -> datetime:
    """Normalize the optional ``now`` override to an aware UTC datetime."""
    try:
        if now is None:
            return datetime.now(UTC)
        if isinstance(now, datetime):
            return now if now.tzinfo else now.replace(tzinfo=UTC)
        return datetime.now(UTC)
    except Exception:
        return datetime.now(UTC)


def week_start_date(now: datetime | None = None) -> date:
    """Monday (date) of the ISO week containing ``now``. Never raises."""
    try:
        d = _coerce_now(now).date()
        return d - timedelta(days=d.weekday())
    except Exception:
        d = datetime.now(UTC).date()
        return d - timedelta(days=d.weekday())


def iso_week_key(now: datetime | None = None) -> str:
    """Compact ISO-week key like "2026-W42". Never raises."""
    try:
        iso = _coerce_now(now).isocalendar()
        return "%d-W%02d" % (iso[0], iso[1])
    except Exception:
        iso = datetime.now(UTC).isocalendar()
        return "%d-W%02d" % (iso[0], iso[1])


def month_key(now: datetime | None = None) -> str:
    """Calendar-month key like "2026-10" (shield accounting). Never raises."""
    try:
        d = _coerce_now(now).date()
    except Exception:
        d = datetime.now(UTC).date()
    return "%04d-%02d" % (d.year, d.month)


def dedupe_key_for(user_id: int, event_type: str, day: date) -> str:
    """Daily idempotency key: "<user_id>:<event_type>:<YYYY-MM-DD>"."""
    try:
        return "%d:%s:%s" % (int(user_id), str(event_type), day.isoformat())
    except Exception:
        return ""


def level_for_xp(total_xp: float | int | None) -> int:
    """Level from lifetime XP: level = floor(sqrt(total/100)) + 1.

    Level 1: 0-99 XP, level 2: 100-399, level 3: 400-899, ... gentle curve,
    no prestige grind. Never raises.
    """
    try:
        total = max(0, int(total_xp or 0))
    except (TypeError, ValueError):
        total = 0
    import math
    return int(math.floor(math.sqrt(total / 100.0))) + 1


def xp_for_level(level: int) -> int:
    """Minimum lifetime XP needed to reach ``level``. Never raises."""
    try:
        level = max(1, int(level))
    except (TypeError, ValueError):
        level = 1
    return 100 * (level - 1) ** 2


def level_progress(
    total_xp: float | int | None,
) -> tuple[int, int, int, float]:
    """(level, xp_into_level, xp_for_next_level, pct) tuple. Never raises."""
    level = level_for_xp(total_xp)
    base = xp_for_level(level)
    nxt = xp_for_level(level + 1)
    try:
        total = max(0, int(total_xp or 0))
    except (TypeError, ValueError):
        total = 0
    into = max(0, total - base)
    span = max(1, nxt - base)
    pct = round(100.0 * into / span, 1)
    return level, into, nxt - base, pct


def apply_streak(
    current: int | None,
    longest: int | None,
    last_active: date | None,
    shields_used_month: int | None,
    shields_month: str | None,
    today: date,
) -> dict[str, Any]:
    """Pure streak transition for one active day.

    ``today`` is a date. Returns a dict with the new
    (current, longest, last_active, shields_used_month, shields_month) plus
    flags ``shield_used`` and ``reset``. Rules:

    - first-ever activity -> streak 1
    - already active today -> unchanged
    - active yesterday -> streak + 1
    - missed day(s) but a shield remains this month -> shield consumed,
      streak continues (forgiveness)
    - otherwise -> graceful reset to 1 (kindness: never 0)

    Never raises.
    """
    out = {"current": 0, "longest": 0, "last_active": last_active,
           "shields_used_month": 0, "shields_month": shields_month,
           "shield_used": False, "reset": False}
    try:
        current = int(current or 0)
        longest = int(longest or 0)
        used = int(shields_used_month or 0)
    except (TypeError, ValueError):
        current, longest, used = 0, 0, 0
    out["current"], out["longest"] = current, longest

    try:
        mk = today.strftime("%Y-%m")
    except Exception:
        mk = month_key()
    if shields_month != mk:
        used = 0
        shields_month = mk
    out["shields_used_month"] = used
    out["shields_month"] = shields_month

    try:
        if last_active is None:
            out["current"] = 1
        elif last_active == today:
            return out  # already counted today; nothing changes
        elif last_active == today - timedelta(days=1):
            out["current"] = current + 1
        elif used < SHIELDS_PER_MONTH:
            out["shields_used_month"] = used + 1
            out["shield_used"] = True
            out["current"] = current + 1
        else:
            out["reset"] = True
            out["current"] = 1  # graceful: restart at 1, never 0
        if out["current"] > longest:
            out["longest"] = out["current"]
        out["last_active"] = today
    except Exception:
        log.exception("apply_streak failed; keeping prior state")
    return out


def _week_dedupe_keys(
    user_id: int,
    event_types: Iterable[str] | None,
    week_start: date,
) -> list[str]:
    """All daily dedupe keys for (user, events, Mon-Sun week)."""
    keys = []
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return keys
    for ev in event_types or ():
        for i in range(7):
            day = week_start + timedelta(days=i)
            try:
                keys.append("%d:%s:%s" % (uid, str(ev), day.isoformat()))
            except Exception:
                continue
    return keys


# ---------------------------------------------------------------------------
# Internal DB helpers (lazy unified_app imports; callers wrap in try/except)
# ---------------------------------------------------------------------------

def _models() -> tuple[Any, ...]:
    from unified_app import (
        LeagueMember,
        LeagueResult,
        MomentumSettings,
        QuestProgress,
        StreakState,
        User,
        WeeklyReflection,
        XPLedger,
        db,
    )
    return (db, XPLedger, StreakState, LeagueMember, LeagueResult,
            QuestProgress, WeeklyReflection, MomentumSettings, User)


def _is_opted_out(MomentumSettings: Any, user_id: int) -> bool:
    try:
        row = MomentumSettings.query.filter_by(user_id=user_id).first()
        return bool(row and row.momentum_opt_out)
    except Exception:
        return False


def _count_events_in_week(
    XPLedger: Any,
    user_id: int,
    event_type: str,
    wk_start: date,
) -> int:
    try:
        keys = _week_dedupe_keys(user_id, [event_type], wk_start)
        if not keys:
            return 0
        return (XPLedger.query
                .filter(XPLedger.user_id == user_id,
                        XPLedger.dedupe_key.in_(keys))
                .count())
    except Exception:
        return 0


def _in_week(dt: Any, wk_start: date) -> bool:
    """True when a (naive=UTC-assumed or aware) datetime falls in the week
    starting ``wk_start`` (Mon-Sun). Never raises."""
    try:
        if dt is None:
            return False
        d = dt.date() if isinstance(dt, datetime) else dt
        return wk_start <= d < wk_start + timedelta(days=7)
    except Exception:
        return False


def _as_utc(dt: Any) -> Any:
    try:
        if isinstance(dt, datetime) and dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt
    except Exception:
        return dt


def _count_source_in_week(user_id: int, source: str, wk_start: date) -> int:
    """Count verifiable source rows for a career quest in one ISO week.

    Source rows are the ground truth (not ledger keys) because career
    actions are per-ref: two tracker adds on the same day are two rows.
    Timestamps are compared in Python so SQLite/Postgres storage
    differences cannot skew the count. Never raises.
    """
    try:
        from unified_app import (CareerAssessment, JobApplication, OHBooking,
                                 OpportunityBookmark, Portfolio, Referral,
                                 Retrospective, SalaryEvaluation, db)
    except Exception:
        return 0
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return 0
    try:
        if source == "tracker_added":
            rows = (JobApplication.query.filter_by(user_id=uid).all())
            return sum(1 for r in rows if _in_week(r.created_at, wk_start))
        if source == "tracker_moved":
            rows = (JobApplication.query.filter_by(user_id=uid).all())
            return sum(1 for r in rows
                       if r.stage_updated_at and r.created_at
                       and _as_utc(r.stage_updated_at) > _as_utc(r.created_at)
                       and _in_week(r.stage_updated_at, wk_start))
        if source == "deadline_set":
            rows = (JobApplication.query.filter_by(user_id=uid).all())
            return sum(1 for r in rows
                       if r.deadline_set_at
                       and _in_week(r.deadline_set_at, wk_start))
        if source == "referral_asked":
            rows = (Referral.query.filter_by(user_id=uid).all())
            return sum(1 for r in rows if _in_week(r.created_at, wk_start))
        if source == "retrospective_completed":
            rows = (Retrospective.query.filter_by(user_id=uid,
                                                  completed=True).all())
            return sum(1 for r in rows
                       if _in_week(r.completed_at or r.created_at, wk_start))
        if source == "portfolio_updated":
            row = Portfolio.query.filter_by(user_id=uid).first()
            return 1 if row and _in_week(row.updated_at, wk_start) else 0
        if source == "opportunity_bookmarked":
            rows = (OpportunityBookmark.query.filter_by(user_id=uid).all())
            return sum(1 for r in rows if _in_week(r.created_at, wk_start))
        if source == "assessment_completed":
            rows = (CareerAssessment.query.filter_by(user_id=uid).all())
            return sum(1 for r in rows if _in_week(r.created_at, wk_start))
        if source == "salary_evaluated":
            rows = (SalaryEvaluation.query.filter_by(user_id=uid).all())
            return sum(1 for r in rows if _in_week(r.created_at, wk_start))
        if source == "mentor_booked":
            rows = (OHBooking.query.filter_by(user_id=uid).all())
            return sum(1 for r in rows
                       if r.status != "cancelled"
                       and _in_week(r.created_at, wk_start))
    except Exception:
        log.exception("source count failed for %s", source)
        try:
            db.session.rollback()
        except Exception:
            pass
    return 0


def _quest_count(XPLedger: Any, user_id: int, template: dict,
                 wk_start: date) -> int:
    """Quest progress for one template: source rows when the template has
    a ``source``, otherwise the legacy ledger-dedupe-key count."""
    source = template.get("source")
    if source:
        return _count_source_in_week(user_id, source, wk_start)
    return _count_events_in_week(XPLedger, user_id,
                                 template["event_type"], wk_start)


def _weekly_stats(
    db: Any,
    XPLedger: Any,
    QuestProgress: Any,
    user_id: int,
    wk_start: date,
) -> dict[str, Any]:
    """Consistency-first weekly stats: active days + quests, XP as garnish.

    score = 10 * active_days + 25 * quests_completed + 0.1 * xp — leagues
    compare showing up, not raw XP prestige.
    """
    stats = {"xp": 0, "streak_days": 0, "quests_completed": 0, "score": 0.0}
    try:
        keys = _week_dedupe_keys(user_id, ALL_EVENT_TYPES, wk_start)
        rows = (XPLedger.query
                .filter(XPLedger.user_id == user_id,
                        XPLedger.dedupe_key.in_(keys)).all()) if keys else []
        key_days = {}
        for ev in ALL_EVENT_TYPES:
            for i in range(7):
                day = wk_start + timedelta(days=i)
                key_days["%d:%s:%s" % (int(user_id), ev, day.isoformat())] = day
        days = set()
        xp = 0
        for r in rows:
            try:
                xp += int(r.xp or 0)
            except (TypeError, ValueError):
                pass
            d = key_days.get(r.dedupe_key)
            if d is not None:
                days.add(d)
        quests = (QuestProgress.query
                  .filter_by(user_id=user_id, week_start=wk_start,
                             completed=True).count())
        stats = {"xp": xp, "streak_days": len(days),
                 "quests_completed": int(quests or 0),
                 "score": len(days) * 10 + int(quests or 0) * 25 + xp * 0.1}
    except Exception:
        log.exception("weekly stats failed for user %s", user_id)
    return stats


def _total_xp(db: Any, XPLedger: Any, user_id: int) -> int:
    try:
        total = (db.session.query(db.func.sum(XPLedger.xp))
                 .filter(XPLedger.user_id == user_id).scalar())
        return int(total or 0)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Public DB operations — none of these ever raises.
# ---------------------------------------------------------------------------

def record_event(
    user_id: int,
    event_type: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record one XP-earning career action.

    Creates an XPLedger row (idempotent per user/event/day), updates the
    streak (with shield forgiveness), refreshes this week's quest progress,
    and grants the one-time day-one-win bonus on the user's first event.

    Returns a result dict; never raises. Unknown event types and opted-out
    users are graceful no-ops.
    """
    try:
        return _record_event(user_id, event_type, now)
    except Exception:
        log.exception("record_event failed")
        try:
            from unified_app import db
            db.session.rollback()
        except Exception:
            pass
        return {"ok": False}


def _record_event(
    user_id: int,
    event_type: str,
    now: datetime | None,
) -> dict[str, Any]:
    (db, XPLedger, StreakState, _LM, _LR, QuestProgress,
     _WR, MomentumSettings, _User) = _models()
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "bad user"}
    if user_id <= 0:
        return {"ok": False, "reason": "bad user"}
    if event_type not in DIRECT_EVENTS:
        return {"ok": False, "reason": "unknown event"}

    now = _coerce_now(now)
    today = now.date()
    if _is_opted_out(MomentumSettings, user_id):
        return {"ok": True, "skipped": True, "reason": "opted out"}

    key = dedupe_key_for(user_id, event_type, today)
    if not key:
        return {"ok": False, "reason": "bad key"}
    try:
        if XPLedger.query.filter_by(dedupe_key=key).first():
            return {"ok": True, "duplicate": True}
        is_first = XPLedger.query.filter_by(user_id=user_id).count() == 0
    except Exception:
        log.exception("record_event pre-check failed")
        db.session.rollback()
        return {"ok": False}

    xp = int(XP_RULES[event_type])
    try:
        db.session.add(XPLedger(user_id=user_id, event_type=event_type,
                                xp=xp, dedupe_key=key))
        day_one = False
        if is_first:
            # Day-one win: first-ever XP event earns a one-time welcome bonus.
            day_one = True
            db.session.add(XPLedger(
                user_id=user_id, event_type=DAY_ONE_EVENT,
                xp=DAY_ONE_BONUS_XP,
                dedupe_key="%d:%s:once" % (user_id, DAY_ONE_EVENT)))

        # Streak update (shield forgiveness lives in apply_streak).
        st = StreakState.query.filter_by(user_id=user_id).first()
        if st is None:
            st = StreakState(user_id=user_id, current=0, longest=0,
                             last_active_date=None, shields_used_month=0,
                             shields_month=None)
            db.session.add(st)
            db.session.flush()
        t = apply_streak(st.current, st.longest, st.last_active_date,
                         st.shields_used_month, st.shields_month, today)
        st.current = t["current"]
        st.longest = t["longest"]
        st.last_active_date = t["last_active"]
        st.shields_used_month = t["shields_used_month"]
        st.shields_month = t["shields_month"]

        # Weekly quest progress, derived from ledger rows.
        wk_start = week_start_date(now)
        for q in QUEST_TEMPLATES:
            if q["event_type"] != event_type:
                continue
            # Count AFTER this row was added (it is in the session; flush
            # first so the count query sees it).
            db.session.flush()
            count = _quest_count(XPLedger, user_id, q, wk_start)
            qp = (QuestProgress.query
                  .filter_by(user_id=user_id, week_start=wk_start,
                             quest_id=q["id"]).first())
            if qp is None:
                qp = QuestProgress(user_id=user_id, week_start=wk_start,
                                   quest_id=q["id"], count=0,
                                   completed=False, claimed=False)
                db.session.add(qp)
            qp.count = count
            if count >= int(q["target"]):
                qp.completed = True

        db.session.commit()
    except Exception:
        log.exception("record_event commit failed")
        db.session.rollback()
        return {"ok": False}
    return {"ok": True, "xp": xp, "day_one": day_one,
            "bonus": DAY_ONE_BONUS_XP if day_one else 0,
            "shield_used": t["shield_used"], "streak_reset": t["reset"],
            "streak": t["current"]}


def claim_quest(
    user_id: int,
    quest_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Award the quest-completion bonus for a finished weekly quest.

    Idempotent: claiming twice (or racing two requests) awards the 50 XP
    bonus exactly once per (user, quest, week). Never raises.
    """
    try:
        return _claim_quest(user_id, quest_id, now)
    except Exception:
        log.exception("claim_quest failed")
        try:
            from unified_app import db
            db.session.rollback()
        except Exception:
            pass
        return {"ok": False}


def _claim_quest(
    user_id: int,
    quest_id: str,
    now: datetime | None,
) -> dict[str, Any]:
    (db, XPLedger, _SS, _LM, _LR, QuestProgress,
     _WR, MomentumSettings, _User) = _models()
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "bad user"}
    if user_id <= 0:
        return {"ok": False, "reason": "bad user"}
    quest_id = str(quest_id or "")
    if quest_id not in QUEST_IDS:
        return {"ok": False, "reason": "unknown quest"}
    if _is_opted_out(MomentumSettings, user_id):
        return {"ok": True, "skipped": True, "reason": "opted out"}

    now = _coerce_now(now)
    wk_start = week_start_date(now)
    template = next(q for q in QUEST_TEMPLATES if q["id"] == quest_id)
    try:
        qp = (QuestProgress.query
              .filter_by(user_id=user_id, week_start=wk_start,
                         quest_id=quest_id).first())
        # Recompute in case the row is missing/stale: progress is derived
        # from the ledger / source rows, the source of truth.
        count = _quest_count(XPLedger, user_id, template, wk_start)
        if qp is None:
            qp = QuestProgress(user_id=user_id, week_start=wk_start,
                               quest_id=quest_id, count=count,
                               completed=count >= int(template["target"]),
                               claimed=False)
            db.session.add(qp)
            db.session.flush()
        else:
            qp.count = count
            if count >= int(template["target"]):
                qp.completed = True
        if not qp.completed:
            db.session.commit()
            return {"ok": False, "reason": "not completed",
                    "count": qp.count, "target": int(template["target"])}
        if qp.claimed:
            db.session.commit()
            return {"ok": True, "duplicate": True}

        bonus = int(XP_RULES["quest_completed"])
        key = "%d:%s:%s:%s" % (user_id, "quest_completed", quest_id,
                               wk_start.isoformat())
        if XPLedger.query.filter_by(dedupe_key=key).first():
            qp.claimed = True  # bonus row exists (e.g. raced request)
            db.session.commit()
            return {"ok": True, "duplicate": True}
        db.session.add(XPLedger(user_id=user_id, event_type="quest_completed",
                                xp=bonus, dedupe_key=key))
        qp.claimed = True
        db.session.commit()
    except Exception:
        log.exception("claim_quest commit failed")
        db.session.rollback()
        return {"ok": False}
    return {"ok": True, "xp": bonus, "quest_id": quest_id}


def join_league(
    user_id: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Opt into this ISO week's league cohort. Idempotent. Never raises."""
    try:
        return _join_league(user_id, now)
    except Exception:
        log.exception("join_league failed")
        try:
            from unified_app import db
            db.session.rollback()
        except Exception:
            pass
        return {"ok": False}


def _join_league(user_id: int, now: datetime | None) -> dict[str, Any]:
    (db, _XL, _SS, LeagueMember, _LR, _QP,
     _WR, MomentumSettings, _User) = _models()
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "bad user"}
    if user_id <= 0:
        return {"ok": False, "reason": "bad user"}
    if _is_opted_out(MomentumSettings, user_id):
        return {"ok": False, "reason": "opted out"}
    now = _coerce_now(now)
    league_key = iso_week_key(now)
    try:
        row = LeagueMember.query.filter_by(user_id=user_id).first()
        if row is not None and not row.opted_out:
            return {"ok": True, "duplicate": True,
                    "league_key": row.league_key}
        if row is None:
            row = LeagueMember(user_id=user_id, league_key=league_key,
                               joined_at=now, opted_out=False)
            db.session.add(row)
        else:
            row.league_key = league_key
            row.joined_at = now
            row.opted_out = False
        db.session.commit()
    except Exception:
        log.exception("join_league commit failed")
        db.session.rollback()
        return {"ok": False}
    return {"ok": True, "league_key": league_key}


def leave_league(user_id: int) -> dict[str, Any]:
    """Opt out of the league board (membership history is kept, the board
    only shows active members). Never raises."""
    try:
        (db, _XL, _SS, LeagueMember, _LR, _QP,
         _WR, _MS, _User) = _models()
        user_id = int(user_id)
        row = LeagueMember.query.filter_by(user_id=user_id).first()
        if row is None or row.opted_out:
            return {"ok": True, "duplicate": True}
        row.opted_out = True
        db.session.commit()
        return {"ok": True}
    except Exception:
        log.exception("leave_league failed")
        try:
            from unified_app import db
            db.session.rollback()
        except Exception:
            pass
        return {"ok": False}


def save_reflection(
    user_id: int,
    wins: str | None,
    focus_next: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Save this week's 2-minute reflection (upsert). Also logs the
    retrospective_logged XP event so the "Look back" quest is completable.
    Never raises."""
    try:
        return _save_reflection(user_id, wins, focus_next, now)
    except Exception:
        log.exception("save_reflection failed")
        try:
            from unified_app import db
            db.session.rollback()
        except Exception:
            pass
        return {"ok": False}


def _save_reflection(
    user_id: int,
    wins: str | None,
    focus_next: str | None,
    now: datetime | None,
) -> dict[str, Any]:
    (db, _XL, _SS, _LM, _LR, _QP, WeeklyReflection,
     MomentumSettings, _User) = _models()
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "bad user"}
    if user_id <= 0:
        return {"ok": False, "reason": "bad user"}
    wins = (wins or "").strip()[:2000]
    focus_next = (focus_next or "").strip()[:2000]
    if not wins and not focus_next:
        return {"ok": False, "reason": "empty reflection"}
    if _is_opted_out(MomentumSettings, user_id):
        return {"ok": False, "reason": "opted out"}
    now = _coerce_now(now)
    wk_start = week_start_date(now)
    try:
        row = (WeeklyReflection.query
               .filter_by(user_id=user_id, week_start=wk_start).first())
        if row is None:
            row = WeeklyReflection(user_id=user_id, week_start=wk_start,
                                   wins=wins, focus_next=focus_next)
            db.session.add(row)
        else:
            row.wins = wins
            row.focus_next = focus_next
        db.session.commit()
    except Exception:
        log.exception("save_reflection commit failed")
        db.session.rollback()
        return {"ok": False}
    # A logged reflection is a meaningful career action: it earns XP (and
    # feeds the "Look back" quest). Defensive: never breaks the save above.
    try:
        record_event(user_id, "retrospective_logged", now=now)
    except Exception:
        pass
    return {"ok": True, "week_start": wk_start.isoformat()}


def set_opt_out(user_id: int, opt_out: bool) -> dict[str, Any]:
    """Set the momentum opt-out flag. When opted out, record_event() is a
    no-op and the hub shows a rejoin CTA. Never raises."""
    try:
        (db, _XL, _SS, _LM, _LR, _QP,
         _WR, MomentumSettings, _User) = _models()
        user_id = int(user_id)
        row = MomentumSettings.query.filter_by(user_id=user_id).first()
        if row is None:
            row = MomentumSettings(user_id=user_id,
                                   momentum_opt_out=bool(opt_out))
            db.session.add(row)
        else:
            row.momentum_opt_out = bool(opt_out)
        db.session.commit()
        return {"ok": True, "opted_out": bool(opt_out)}
    except Exception:
        log.exception("set_opt_out failed")
        try:
            from unified_app import db
            db.session.rollback()
        except Exception:
            pass
        return {"ok": False}


def finalize_league_week(now: datetime | None = None) -> dict[str, Any]:
    """Write LeagueResult rows for the ISO week that just ended, for every
    league cohort with active members. Idempotent (skips already-finalized
    league/week pairs). Called opportunistically from the hub/summary so no
    cron job is needed. Never raises."""
    try:
        return _finalize_league_week(now)
    except Exception:
        log.exception("finalize_league_week failed")
        try:
            from unified_app import db
            db.session.rollback()
        except Exception:
            pass
        return {"ok": False}


def _finalize_league_week(now: datetime | None) -> dict[str, Any]:
    (db, XPLedger, _SS, LeagueMember, LeagueResult, QuestProgress,
     _WR, _MS, User) = _models()
    now = _coerce_now(now)
    prev_start = week_start_date(now) - timedelta(days=7)
    try:
        league_keys = [r[0] for r in
                       db.session.query(LeagueMember.league_key)
                       .filter_by(opted_out=False).distinct().all()]
    except Exception:
        log.exception("finalize: league lookup failed")
        db.session.rollback()
        return {"ok": False}
    done = 0
    try:
        for league_key in league_keys:
            if not league_key:
                continue
            exists = (LeagueResult.query
                      .filter_by(league_key=league_key,
                                 week_start=prev_start).first())
            if exists:
                continue
            members = (LeagueMember.query
                       .filter_by(league_key=league_key, opted_out=False).all())
            scored = []
            for m in members:
                stats = _weekly_stats(db, XPLedger, QuestProgress,
                                      m.user_id, prev_start)
                scored.append((m.user_id, stats))
            # Consistency-first ranking: active days, then quests, then XP.
            scored.sort(key=lambda t: (-t[1]["score"], -t[1]["streak_days"],
                                       -t[1]["quests_completed"], -t[1]["xp"]))
            for rank, (uid, stats) in enumerate(scored):
                db.session.add(LeagueResult(
                    league_key=league_key, user_id=uid,
                    week_start=prev_start, xp=stats["xp"],
                    streak_days=stats["streak_days"],
                    quests_completed=stats["quests_completed"],
                    won=(rank == 0 and stats["score"] > 0)))
            done += 1
        db.session.commit()
    except Exception:
        log.exception("finalize: commit failed")
        db.session.rollback()
        return {"ok": False}
    return {"ok": True, "leagues": done,
            "week_start": prev_start.isoformat()}


# ---------------------------------------------------------------------------
# Summary for the hub page, the JSON API, and the dashboard widget.
# ---------------------------------------------------------------------------

def empty_summary() -> dict[str, Any]:
    """Safe fallback summary (used when anything goes wrong)."""
    return {
        "opted_out": False,
        "total_xp": 0, "level": 1, "level_pct": 0.0,
        "xp_into_level": 0, "xp_for_next_level": 100,
        "streak": {"current": 0, "longest": 0, "shields_left": SHIELDS_PER_MONTH,
                   "last_active": None},
        "day_one": {"earned": False, "bonus": DAY_ONE_BONUS_XP},
        "quests": [], "league": {"member": False, "league_key": None,
                                 "board": [], "last_week": None},
        "reflection": None,
    }


def get_momentum_summary(
    user_id: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the full hub/summary dict. Never raises — falls back to
    empty_summary() on any failure."""
    try:
        return _build_summary(user_id, now)
    except Exception:
        log.exception("get_momentum_summary failed")
        try:
            from unified_app import db
            db.session.rollback()
        except Exception:
            pass
        return empty_summary()


def _build_summary(user_id: int, now: datetime | None) -> dict[str, Any]:
    (db, XPLedger, StreakState, LeagueMember, LeagueResult, QuestProgress,
     WeeklyReflection, MomentumSettings, User) = _models()
    user_id = int(user_id)
    now = _coerce_now(now)
    wk_start = week_start_date(now)

    # Opportunistic weekly finalization: no cron needed.
    try:
        finalize_league_week(now=now)
    except Exception:
        pass

    opted_out = _is_opted_out(MomentumSettings, user_id)
    total_xp = _total_xp(db, XPLedger, user_id)
    level, into, span, pct = level_progress(total_xp)

    st = StreakState.query.filter_by(user_id=user_id).first()
    shields_left = SHIELDS_PER_MONTH
    if st is not None and st.shields_month == month_key(now):
        shields_left = max(0, SHIELDS_PER_MONTH - int(st.shields_used_month or 0))
    streak = {
        "current": int(st.current or 0) if st else 0,
        "longest": int(st.longest or 0) if st else 0,
        "shields_left": shields_left,
        "shields_total": SHIELDS_PER_MONTH,
        "last_active": (st.last_active_date.isoformat()
                        if st and st.last_active_date else None),
    }

    day_one_earned = (XPLedger.query
                      .filter_by(user_id=user_id,
                                 event_type=DAY_ONE_EVENT).count() > 0)

    quests = []
    for q in QUEST_TEMPLATES:
        qp = (QuestProgress.query
              .filter_by(user_id=user_id, week_start=wk_start,
                         quest_id=q["id"]).first())
        count = int(qp.count) if qp else _quest_count(
            XPLedger, user_id, q, wk_start)
        completed = bool(qp.completed) if qp else count >= int(q["target"])
        claimed = bool(qp.claimed) if qp else False
        target = int(q["target"])
        quests.append({
            "id": q["id"], "title": q["title"],
            "description": q["description"], "target": target,
            "count": count, "completed": completed, "claimed": claimed,
            "bonus": int(XP_RULES["quest_completed"]),
            "pct": round(100.0 * min(count, target) / target, 1),
        })

    league = {"member": False, "league_key": None, "board": [],
              "last_week": None}
    member = LeagueMember.query.filter_by(user_id=user_id).first()
    if member is not None and not member.opted_out:
        league["member"] = True
        league["league_key"] = member.league_key
        mates = (LeagueMember.query
                 .filter_by(league_key=member.league_key, opted_out=False).all())
        name_by_id = {}
        try:
            for u in User.query.filter(
                    User.id.in_([m.user_id for m in mates])).all():
                name_by_id[u.id] = u.name or "Member"
        except Exception:
            pass
        board = []
        for m in mates:
            stats = _weekly_stats(db, XPLedger, QuestProgress,
                                  m.user_id, wk_start)
            board.append({
                "user_id": m.user_id,
                "name": name_by_id.get(m.user_id, "Member"),
                "is_you": m.user_id == user_id,
                "xp": stats["xp"], "streak_days": stats["streak_days"],
                "quests_completed": stats["quests_completed"],
                "score": round(stats["score"], 1),
            })
        board.sort(key=lambda e: (-e["score"], -e["streak_days"],
                                  -e["quests_completed"], -e["xp"]))
        for i, e in enumerate(board):
            e["rank"] = i + 1
        league["board"] = board

        prev_start = wk_start - timedelta(days=7)
        prev_rows = (LeagueResult.query
                     .filter_by(league_key=member.league_key,
                                week_start=prev_start)
                     .order_by(LeagueResult.won.desc(),
                               LeagueResult.streak_days.desc(),
                               LeagueResult.xp.desc()).all())
        if prev_rows:
            winner = next((r for r in prev_rows if r.won), None)
            name_by_id = {}
            try:
                ids = [r.user_id for r in prev_rows[:12]]
                if winner and winner.user_id not in ids:
                    ids.append(winner.user_id)
                for u in User.query.filter(User.id.in_(ids)).all():
                    name_by_id[u.id] = u.name or "Member"
            except Exception:
                pass
            league["last_week"] = {
                "week_start": prev_start.isoformat(),
                "winner": name_by_id.get(winner.user_id, "—") if winner else "—",
                "entries": [
                    {"name": name_by_id.get(r.user_id, "Member"),
                     "xp": r.xp, "streak_days": r.streak_days,
                     "quests_completed": r.quests_completed, "won": r.won}
                    for r in prev_rows[:12]
                ],
            }

    refl = (WeeklyReflection.query
            .filter_by(user_id=user_id, week_start=wk_start).first())
    reflection = None
    if refl is not None:
        reflection = {"wins": refl.wins or "",
                      "focus_next": refl.focus_next or "",
                      "week_start": wk_start.isoformat()}

    return {
        "opted_out": opted_out,
        "total_xp": total_xp, "level": level, "level_pct": pct,
        "xp_into_level": into, "xp_for_next_level": span,
        "streak": streak,
        "day_one": {"earned": day_one_earned, "bonus": DAY_ONE_BONUS_XP},
        "quests": quests, "league": league, "reflection": reflection,
        "week_start": wk_start.isoformat(),
    }


# ================= Feature 27 Ultimate Enhancements =================
# 27.1 Streak freeze | 27.2 Squad challenges | 27.3 Reward unlocks

import re as _re20
from datetime import date as _date20


def _clean20(text: str | None) -> str:
    return _re20.sub(r"<[^>]*>", "", text or "")


def streak_freeze_status(
    last_active: date | None,
    freezes_used_week: int | None,
    week_start: date | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Weekly rest day: 1 streak freeze per week, auto-applied on missed days.

    Returns {freezes_available, freeze_would_apply, message}.
    A freeze preserves the streak through ONE missed day per week.
    """
    today = today or _date20.today()
    try:
        used = int(freezes_used_week or 0)
    except (TypeError, ValueError):
        used = 0
    available = max(0, 1 - used)
    # Would a freeze apply right now? (missed yesterday, freeze available)
    would_apply = False
    try:
        if last_active:
            delta = (today - last_active).days
            would_apply = delta == 2 and available > 0  # missed exactly yesterday
    except (TypeError, AttributeError):
        pass
    return {"freezes_available": available,
            "freeze_would_apply": would_apply,
            "message": ("Rest day available — skip a day guilt-free, streak stays."
                        if available and not would_apply else
                        "Freeze would save your streak today — take the rest day."
                        if would_apply else
                        "Freeze used this week — stay active to keep the streak.")}


def squad_leaderboard(
    members: list[dict[str, Any]],
    week_key: str | None = None,
) -> dict[str, Any]:
    """Weekly XP competition between friends.

    members: list of {user_id, name, weekly_xp}. Returns ranked {squad,
    leader, your_rank?}.
    """
    if not isinstance(members, (list, tuple)):
        raise ValueError("members must be a list.")
    clean = []
    for m in members:
        if not isinstance(m, dict):
            continue
        try:
            xp = max(0, int(m.get("weekly_xp") or 0))
        except (TypeError, ValueError):
            xp = 0
        clean.append({"user_id": m.get("user_id"),
                      "name": _clean20(m.get("name") or "Anonymous")[:40] or "Anonymous",
                      "weekly_xp": xp})
    if not clean:
        raise ValueError("Squad needs at least one member.")
    clean.sort(key=lambda x: -x["weekly_xp"])
    for i, m in enumerate(clean, 1):
        m["rank"] = i
    medals = {1: "gold", 2: "silver", 3: "bronze"}
    for m in clean:
        m["medal"] = medals.get(m["rank"])
    leader = clean[0]
    return {"squad": clean, "leader": leader["name"],
            "leader_xp": leader["weekly_xp"],
            "week": week_key or iso_week_key(),
            "hype": (f"{leader['name']} leads with {leader['weekly_xp']} XP — "
                     f"{'can anyone catch up?' if len(clean) > 1 else 'solo grind!'}" )}


# Reward tiers: streak/XP milestones unlock perks.
_REWARD_TIERS = [
    (7, 0, "7-day streak", "Unlock: premium mock interview pack"),
    (14, 0, "14-day streak", "Unlock: AI resume deep-review"),
    (30, 0, "30-day streak", "Unlock: priority mentor matching"),
    (0, 1000, "1000 XP", "Unlock: exclusive DSA hard pack"),
    (0, 5000, "5000 XP", "Unlock: 1:1 career strategy session"),
    (60, 10000, "60-day streak + 10k XP", "Unlock: STUDY-BUDDY champion badge"),
]


def reward_tiers(
    current_streak: int | None,
    total_xp: int | None,
) -> dict[str, Any]:
    """What has the user unlocked? What's next?

    Returns {unlocked: [...], next_up: {...} or None, progress}.
    """
    try:
        streak = max(0, int(current_streak or 0))
    except (TypeError, ValueError):
        streak = 0
    try:
        xp = max(0, int(total_xp or 0))
    except (TypeError, ValueError):
        xp = 0
    unlocked, locked = [], []
    for req_streak, req_xp, name, perk in _REWARD_TIERS:
        got = streak >= req_streak and xp >= req_xp
        (unlocked if got else locked).append(
            {"name": name, "perk": perk, "unlocked": got})
    next_up = locked[0] if locked else None
    return {"unlocked": unlocked, "next_up": next_up,
            "unlocked_count": len(unlocked), "total_tiers": len(_REWARD_TIERS),
            "progress": f"{len(unlocked)}/{len(_REWARD_TIERS)} rewards unlocked",
            "motivation": (f"Next: {next_up['name']} → {next_up['perk']}"
                           if next_up else "All rewards unlocked — legend status.")}


# ================= Phase 4: career XP, badges, recap, repair, teams =====
# Career XP flows through the SAME XPLedger and level curve as learning
# XP, but with per-ref idempotency: dedupe_key "<user>:<event>:ref:<ref>"
# pays exactly once per (user, action, ref). Daily-deduped record_event()
# cannot do this — two tracker adds on the same day would collide.

#: action -> (ledger event_type, xp, quest source). Every career hook
#: call passes a ref (application id, referral id, opp id, section name,
#: ...) so the same action on the same object never pays twice.
CAREER_XP_ACTIONS = {
    "tracker_add": ("tracker_added", 10, "tracker_added"),
    "tracker_move": ("tracker_moved", 5, "tracker_moved"),
    "deadline_set": ("deadline_set", 5, "deadline_set"),
    "referral_ask": ("referral_asked", 15, "referral_asked"),
    "retrospective_complete": ("retrospective_completed", 20,
                               "retrospective_completed"),
    "portfolio_section": ("portfolio_updated", 15, "portfolio_updated"),
    "opportunity_bookmark": ("opportunity_bookmarked", 5,
                             "opportunity_bookmarked"),
    "assessment_complete": ("assessment_completed", 15,
                            "assessment_completed"),
    "salary_evaluate": ("salary_evaluated", 10, "salary_evaluated"),
    "mentor_book": ("mentor_booked", 20, "mentor_booked"),
}


def _career_ref_key(ref_id: Any) -> str:
    try:
        return str(ref_id or "").strip()[:60]
    except Exception:
        return ""


def career_xp_hook(user_id: int, action: str, ref_id: Any = None,
                   now: datetime | None = None) -> dict[str, Any]:
    """Award career XP for one verified action, once per ref.

    Idempotent: the same (user, action, ref) pays exactly once, however
    often the hook fires. Updates the streak and refreshes the matching
    career quests. Never raises — callers invoke it after their own
    commit, and an XP failure must never break the primary action.
    """
    try:
        return _career_xp_hook(user_id, action, ref_id, now)
    except Exception:
        log.exception("career_xp_hook failed")
        try:
            from unified_app import db
            db.session.rollback()
        except Exception:
            pass
        return {"ok": False}


def _career_xp_hook(user_id: int, action: str, ref_id: Any,
                    now: datetime | None) -> dict[str, Any]:
    (db, XPLedger, StreakState, _LM, _LR, QuestProgress,
     _WR, MomentumSettings, _User) = _models()
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "bad user"}
    if user_id <= 0:
        return {"ok": False, "reason": "bad user"}
    spec = CAREER_XP_ACTIONS.get(str(action or ""))
    if spec is None:
        return {"ok": False, "reason": "unknown action"}
    event_type, xp, source = spec
    ref = _career_ref_key(ref_id)
    if not ref:
        return {"ok": False, "reason": "ref required"}
    if _is_opted_out(MomentumSettings, user_id):
        return {"ok": True, "skipped": True, "reason": "opted out"}
    now = _coerce_now(now)
    today = now.date()
    key = "%d:%s:ref:%s" % (user_id, event_type, ref)
    try:
        if XPLedger.query.filter_by(dedupe_key=key).first():
            return {"ok": True, "duplicate": True}
        is_first = XPLedger.query.filter_by(user_id=user_id).count() == 0
    except Exception:
        log.exception("career_xp_hook pre-check failed")
        db.session.rollback()
        return {"ok": False}
    try:
        db.session.add(XPLedger(user_id=user_id, event_type=event_type,
                                xp=int(xp), dedupe_key=key))
        day_one = False
        if is_first:
            day_one = True
            db.session.add(XPLedger(
                user_id=user_id, event_type=DAY_ONE_EVENT,
                xp=DAY_ONE_BONUS_XP,
                dedupe_key="%d:%s:once" % (user_id, DAY_ONE_EVENT)))
        st = StreakState.query.filter_by(user_id=user_id).first()
        if st is None:
            st = StreakState(user_id=user_id, current=0, longest=0,
                             last_active_date=None, shields_used_month=0,
                             shields_month=None)
            db.session.add(st)
            db.session.flush()
        t = apply_streak(st.current, st.longest, st.last_active_date,
                         st.shields_used_month, st.shields_month, today)
        st.current = t["current"]
        st.longest = t["longest"]
        st.last_active_date = t["last_active"]
        st.shields_used_month = t["shields_used_month"]
        st.shields_month = t["shields_month"]
        # Refresh only the quests fed by this action's source.
        wk_start = week_start_date(now)
        db.session.flush()
        for q in QUEST_TEMPLATES:
            if q.get("source") != source:
                continue
            count = _count_source_in_week(user_id, source, wk_start)
            qp = (QuestProgress.query
                  .filter_by(user_id=user_id, week_start=wk_start,
                             quest_id=q["id"]).first())
            if qp is None:
                qp = QuestProgress(user_id=user_id, week_start=wk_start,
                                   quest_id=q["id"], count=0,
                                   completed=False, claimed=False)
                db.session.add(qp)
            qp.count = count
            if count >= int(q["target"]):
                qp.completed = True
        db.session.commit()
    except Exception:
        log.exception("career_xp_hook commit failed")
        db.session.rollback()
        return {"ok": False}
    return {"ok": True, "xp": int(xp), "day_one": day_one,
            "bonus": DAY_ONE_BONUS_XP if day_one else 0,
            "shield_used": t["shield_used"], "streak_reset": t["reset"],
            "streak": t["current"]}


#: Badge definitions: (id, title, description, rule key). Every rule is
#: checkable from existing rows at award time — no award table, no
#: backfilled history. earned_at is set only where the exact moment is
#: provable (the Nth row's timestamp); state-based badges report None.
BADGES = [
    ("first-tracker-add", "First application",
     "Added your first application to the tracker", "first_tracker_add"),
    ("ten-applications", "Pipeline of ten",
     "Tracked 10 applications", "ten_applications"),
    ("first-referral", "First ask",
     "Asked someone for a referral", "first_referral"),
    ("three-referrals", "Network builder",
     "Asked 3 people for referrals", "three_referrals"),
    ("first-retrospective", "Learned from a no",
     "Completed a rejection retrospective", "first_retrospective"),
    ("portfolio-complete", "Portfolio complete",
     "Filled all 9 portfolio sections", "portfolio_complete"),
    ("streak-7", "Week of momentum",
     "Reached a 7-day streak", "streak_7"),
    ("streak-30", "Month of momentum",
     "Reached a 30-day streak", "streak_30"),
    ("level-5", "Level 5",
     "Reached momentum level 5", "level_5"),
    ("level-10", "Level 10",
     "Reached momentum level 10", "level_10"),
    ("first-interview", "Interview stage",
     "An application reached the interview stage", "first_interview"),
    ("first-offer", "Offer stage",
     "An application reached the offer stage", "first_offer"),
    ("five-bookmarks", "Opportunity scout",
     "Bookmarked 5 opportunities", "five_bookmarks"),
    ("assessment-complete", "Self-aware",
     "Completed a career-discovery assessment", "assessment_complete"),
    ("salary-evaluated", "Know your worth",
     "Evaluated an offer against salary bands", "salary_evaluated"),
    ("five-quests", "Quest finisher",
     "Completed 5 weekly quests", "five_quests"),
    ("league-join", "Team player",
     "Joined a league", "league_join"),
    ("ten-deadlines", "Deadline keeper",
     "Set deadlines on 10 applications", "ten_deadlines"),
    ("portfolio-published", "Published",
     "Published your portfolio page", "portfolio_published"),
    ("comeback", "Comeback",
     "Repaired a broken streak", "comeback"),
]

#: Portfolio sections counted for the portfolio-complete badge.
PORTFOLIO_SECTIONS = ("bio", "skills", "projects", "links", "certifications",
                       "experience", "education", "achievements", "contact")


def _section_filled(portfolio: Any, section: str) -> bool:
    try:
        value = getattr(portfolio, section, None)
    except Exception:
        return False
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return len(value) > 0
    return bool(value)


def compute_badges(user_id: int) -> list[dict[str, Any]]:
    """Evaluate all 20 badges live from the user's rows. Never raises;
    on any failure returns unearned badges (safe fallback)."""
    try:
        return _compute_badges(user_id)
    except Exception:
        log.exception("compute_badges failed")
        try:
            from unified_app import db
            db.session.rollback()
        except Exception:
            pass
        return [{"id": b[0], "title": b[1], "description": b[2],
                 "earned": False, "earned_at": None} for b in BADGES]


def _nth_timestamp(rows: list, n: int, field: str) -> Any:
    """Timestamp of the Nth row (1-based) ordered by the field ascending,
    or None when fewer than N rows exist."""
    try:
        ordered = sorted((r for r in rows if getattr(r, field, None)),
                         key=lambda r: _as_utc(getattr(r, field)))
        if len(ordered) >= n:
            return _as_utc(getattr(ordered[n - 1], field)).isoformat()
    except Exception:
        pass
    return None


def _compute_badges(user_id: int) -> list[dict[str, Any]]:
    (db, XPLedger, StreakState, LeagueMember, _LR, QuestProgress,
     _WR, MomentumSettings, _User) = _models()
    from unified_app import (CareerAssessment, JobApplication,
                             OpportunityBookmark, Portfolio, Referral,
                             Retrospective, SalaryEvaluation,
                             TeamLeagueMember)
    user_id = int(user_id)
    apps = JobApplication.query.filter_by(user_id=user_id).all()
    refs = Referral.query.filter_by(user_id=user_id).all()
    retros = Retrospective.query.filter_by(user_id=user_id,
                                           completed=True).all()
    bookmarks = OpportunityBookmark.query.filter_by(user_id=user_id).all()
    assessments = CareerAssessment.query.filter_by(user_id=user_id).all()
    evals = SalaryEvaluation.query.filter_by(user_id=user_id).all()
    portfolio = Portfolio.query.filter_by(user_id=user_id).first()
    st = StreakState.query.filter_by(user_id=user_id).first()
    total_xp = _total_xp(db, XPLedger, user_id)
    level = level_for_xp(total_xp)
    quests_done = (QuestProgress.query.filter_by(user_id=user_id,
                                                completed=True).count())
    in_league = (TeamLeagueMember.query.filter_by(user_id=user_id).first()
                 is not None)
    if not in_league:
        lm = LeagueMember.query.filter_by(user_id=user_id).first()
        in_league = bool(lm and not lm.opted_out)
    settings = MomentumSettings.query.filter_by(user_id=user_id).first()
    deadline_rows = [a for a in apps if a.deadline_set_at]
    stages = {a.stage for a in apps}
    earned: dict[str, Any] = {
        "first_tracker_add": (len(apps) >= 1,
                              _nth_timestamp(apps, 1, "created_at")),
        "ten_applications": (len(apps) >= 10,
                             _nth_timestamp(apps, 10, "created_at")),
        "first_referral": (len(refs) >= 1,
                           _nth_timestamp(refs, 1, "created_at")),
        "three_referrals": (len(refs) >= 3,
                            _nth_timestamp(refs, 3, "created_at")),
        "first_retrospective": (len(retros) >= 1,
                                _nth_timestamp(retros, 1, "completed_at")),
        "portfolio_complete": (
            bool(portfolio) and all(_section_filled(portfolio, s)
                                    for s in PORTFOLIO_SECTIONS), None),
        "streak_7": (bool(st and (st.longest or 0) >= 7), None),
        "streak_30": (bool(st and (st.longest or 0) >= 30), None),
        "level_5": (level >= 5, None),
        "level_10": (level >= 10, None),
        "first_interview": ("interview" in stages, None),
        "first_offer": ("offer" in stages, None),
        "five_bookmarks": (len(bookmarks) >= 5,
                           _nth_timestamp(bookmarks, 5, "created_at")),
        "assessment_complete": (len(assessments) >= 1,
                                _nth_timestamp(assessments, 1, "created_at")),
        "salary_evaluated": (len(evals) >= 1,
                             _nth_timestamp(evals, 1, "created_at")),
        "five_quests": (quests_done >= 5, None),
        "league_join": (in_league, None),
        "ten_deadlines": (len(deadline_rows) >= 10,
                          _nth_timestamp(deadline_rows, 10,
                                         "deadline_set_at")),
        "portfolio_published": (bool(portfolio and portfolio.is_published),
                                None),
        "comeback": (bool(settings and settings.last_streak_repair_at),
                      settings.last_streak_repair_at.isoformat()
                      if settings and settings.last_streak_repair_at
                      else None),
    }
    out = []
    for bid, title, desc, rule in BADGES:
        got, when = earned.get(rule, (False, None))
        out.append({"id": bid, "title": title, "description": desc,
                    "earned": bool(got), "earned_at": when if got else None})
    return out


def weekly_recap(user_id: int, now: datetime | None = None) -> dict[str, Any]:
    """Last-7-days recap from ledger + source rows. Pure read, never
    writes. Never raises (falls back to an empty recap)."""
    try:
        return _weekly_recap(user_id, now)
    except Exception:
        log.exception("weekly_recap failed")
        try:
            from unified_app import db
            db.session.rollback()
        except Exception:
            pass
        return {"week_start": week_start_date(now).isoformat(),
                "xp_earned": 0, "quests_completed": [], "streak_days": 0,
                "applications_added": 0, "applications_moved": 0,
                "referrals_asked": 0, "top_action": None}


def _weekly_recap(user_id: int, now: datetime | None) -> dict[str, Any]:
    (db, XPLedger, StreakState, _LM, _LR, QuestProgress,
     _WR, _MS, _User) = _models()
    from unified_app import JobApplication, Referral
    user_id = int(user_id)
    now = _coerce_now(now)
    wk_start = week_start_date(now)
    ledger = XPLedger.query.filter_by(user_id=user_id).all()
    week_rows = [r for r in ledger if _in_week(r.created_at, wk_start)]
    xp_earned = sum(int(r.xp or 0) for r in week_rows)
    counts: dict[str, int] = {}
    for r in week_rows:
        counts[r.event_type] = counts.get(r.event_type, 0) + 1
    top_action = None
    if counts:
        top_action = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    title_by_id = {q["id"]: q["title"] for q in QUEST_TEMPLATES}
    done = (QuestProgress.query.filter_by(user_id=user_id,
                                         week_start=wk_start,
                                         completed=True).all())
    quests = [title_by_id.get(q.quest_id, q.quest_id) for q in done]
    st = StreakState.query.filter_by(user_id=user_id).first()
    apps = JobApplication.query.filter_by(user_id=user_id).all()
    added = sum(1 for a in apps if _in_week(a.created_at, wk_start))
    moved = sum(1 for a in apps
                if a.stage_updated_at and a.created_at
                and _as_utc(a.stage_updated_at) > _as_utc(a.created_at)
                and _in_week(a.stage_updated_at, wk_start))
    refs = Referral.query.filter_by(user_id=user_id).all()
    asked = sum(1 for r in refs if _in_week(r.created_at, wk_start))
    return {"week_start": wk_start.isoformat(), "xp_earned": xp_earned,
            "quests_completed": quests,
            "streak_days": int(st.current or 0) if st else 0,
            "applications_added": added, "applications_moved": moved,
            "referrals_asked": asked, "top_action": top_action}


#: Manual streak repair: at most one repair per rolling 30 days.
REPAIR_COOLDOWN_DAYS = 30


def repair_streak(user_id: int, now: datetime | None = None) -> dict[str, Any]:
    """Manually repair a streak broken by exactly one missed day.

    Rule: the streak's last active day must be exactly 2 days ago
    (yesterday was missed, today not yet active). Repair fills the missed
    day — last_active_date becomes yesterday — so today's activity
    continues the streak instead of resetting it. It does NOT itself
    count as activity: the user must still act today.

    Rejections: no streak row / no activity yet -> "no streak";
    last active yesterday or today -> "no break to repair" (streak alive);
    last active 3+ days ago -> "gap too long"; a repair inside the last
    30 days -> "repair already used". Never raises.
    """
    try:
        return _repair_streak(user_id, now)
    except Exception:
        log.exception("repair_streak failed")
        try:
            from unified_app import db
            db.session.rollback()
        except Exception:
            pass
        return {"ok": False, "reason": "repair failed"}


def _repair_streak(user_id: int, now: datetime | None) -> dict[str, Any]:
    (db, _XL, StreakState, _LM, _LR, _QP,
     _WR, MomentumSettings, _User) = _models()
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "bad user"}
    if user_id <= 0:
        return {"ok": False, "reason": "bad user"}
    now = _coerce_now(now)
    today = now.date()
    st = StreakState.query.filter_by(user_id=user_id).first()
    if st is None or not st.last_active_date or not (st.current or 0):
        return {"ok": False, "reason": "no streak"}
    last = st.last_active_date
    if last >= today - timedelta(days=1):
        return {"ok": False, "reason": "no break to repair"}
    if last <= today - timedelta(days=3):
        return {"ok": False, "reason": "gap too long"}
    # last == today - 2: exactly one missed day (yesterday).
    settings = MomentumSettings.query.filter_by(user_id=user_id).first()
    if settings is None:
        settings = MomentumSettings(user_id=user_id, momentum_opt_out=False)
        db.session.add(settings)
        db.session.flush()
    if settings.last_streak_repair_at:
        last_repair = _as_utc(settings.last_streak_repair_at)
        if (now - last_repair).days < REPAIR_COOLDOWN_DAYS:
            return {"ok": False, "reason": "repair already used"}
    st.last_active_date = today - timedelta(days=1)
    settings.last_streak_repair_at = now
    try:
        db.session.commit()
    except Exception:
        log.exception("repair_streak commit failed")
        db.session.rollback()
        return {"ok": False, "reason": "repair failed"}
    return {"ok": True, "streak": {
        "current": int(st.current or 0), "longest": int(st.longest or 0),
        "last_active": st.last_active_date.isoformat()}}


# ---- Team leagues (Phase 4) ----
# Distinct from the weekly ISO-week cohorts (LeagueMember): a team league
# is a named, invite-code group. Privacy rule: the board endpoint returns
# ONLY the requesting user's rank + total members + own XP — never other
# members' identities or scores. (The pre-existing weekly cohort board
# already exposes member names; that behavior is left untouched, not
# expanded to team leagues.)

def create_team_league(user_id: int, name: str) -> dict[str, Any]:
    """Create a named team league; the creator auto-joins. Never raises."""
    try:
        from unified_app import TeamLeague, TeamLeagueMember, db
        import secrets
        user_id = int(user_id)
        clean = (name or "").strip()[:120]
        if not clean:
            return {"ok": False, "reason": "name required"}
        code = secrets.token_hex(8)
        league = TeamLeague(name=clean, invite_code=code,
                            created_by=user_id)
        db.session.add(league)
        db.session.flush()
        db.session.add(TeamLeagueMember(league_id=league.id,
                                        user_id=user_id))
        db.session.commit()
        return {"ok": True, "league": {"id": league.id, "name": clean,
                                       "invite_code": code}}
    except Exception:
        log.exception("create_team_league failed")
        try:
            from unified_app import db
            db.session.rollback()
        except Exception:
            pass
        return {"ok": False}


def join_team_league_by_code(user_id: int, code: str) -> dict[str, Any]:
    """Join a team league via its invite code. Never raises."""
    try:
        from unified_app import TeamLeague, TeamLeagueMember, db
        user_id = int(user_id)
        clean = (code or "").strip()
        if not clean:
            return {"ok": False, "reason": "bad code"}
        league = TeamLeague.query.filter_by(invite_code=clean).first()
        if league is None:
            return {"ok": False, "reason": "bad code"}
        existing = (TeamLeagueMember.query
                    .filter_by(league_id=league.id, user_id=user_id).first())
        if existing is not None:
            return {"ok": True, "duplicate": True, "league_id": league.id}
        db.session.add(TeamLeagueMember(league_id=league.id,
                                        user_id=user_id))
        db.session.commit()
        return {"ok": True, "league_id": league.id}
    except Exception:
        log.exception("join_team_league_by_code failed")
        try:
            from unified_app import db
            db.session.rollback()
        except Exception:
            pass
        return {"ok": False}


def team_league_boards(user_id: int) -> list[dict[str, Any]]:
    """The user's team leagues: own rank + member count + own XP only.
    Rank = 1 + members with strictly greater lifetime XP. Never raises."""
    try:
        from unified_app import TeamLeague, TeamLeagueMember, XPLedger, db
        user_id = int(user_id)
        memberships = (TeamLeagueMember.query
                       .filter_by(user_id=user_id).all())
        league_ids = [m.league_id for m in memberships]
        if not league_ids:
            return []
        leagues = (TeamLeague.query.filter(
            TeamLeague.id.in_(league_ids)).order_by(TeamLeague.name).all())
        totals = dict(db.session.query(XPLedger.user_id,
                                       db.func.sum(XPLedger.xp))
                      .filter(XPLedger.user_id.in_(
                          [m.user_id for m in
                           TeamLeagueMember.query.filter(
                               TeamLeagueMember.league_id.in_(league_ids)).all()]))
                      .group_by(XPLedger.user_id).all())
        out = []
        for league in leagues:
            members = (TeamLeagueMember.query
                       .filter_by(league_id=league.id).all())
            my_xp = int(totals.get(user_id, 0) or 0)
            rank = 1 + sum(1 for m in members
                           if int(totals.get(m.user_id, 0) or 0) > my_xp)
            out.append({"id": league.id, "name": league.name,
                        "members_count": len(members),
                        "my_rank": rank, "my_xp": my_xp})
        return out
    except Exception:
        log.exception("team_league_boards failed")
        try:
            from unified_app import db
            db.session.rollback()
        except Exception:
            pass
        return []
