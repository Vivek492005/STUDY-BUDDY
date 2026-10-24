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
            count = _count_events_in_week(XPLedger, user_id, event_type,
                                          wk_start)
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
        # from the ledger, the source of truth.
        count = _count_events_in_week(XPLedger, user_id,
                                      template["event_type"], wk_start)
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
        count = int(qp.count) if qp else _count_events_in_week(
            XPLedger, user_id, q["event_type"], wk_start)
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
