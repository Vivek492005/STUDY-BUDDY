"""Live Traction Tracker — real usage metrics for the main dashboard.

Every number comes from real database data via indexed COUNT queries.
Zero data shows 0. Nothing is hardcoded, seeded, or faked.

100%-WORKING LAW (Vivek, 2026-11-05): this feature must work for EVERY
user at EVERY moment. That drives the design:

- Fail-open: :func:`get_traction` NEVER raises. On any error it returns
  the last good cached payload, or honest zeros with a valid shape. The
  ``/api/traction`` endpoint therefore never 500s — not on an empty DB,
  not on a DB hiccup, not on a cold cache, not under concurrent load.
- The frontend (templates/history.html) never blanks and never shows an
  error banner: on API failure it keeps the last good numbers and shows
  "updating…"; on first-load failure it shows honest zeros.
- Privacy: aggregate counts only. The only new table (traction_event)
  stores event type + timestamp + user id — no IP, no user agent, no PII.

Module layout mirrors studybuddy/momentum.py: no top-level Flask imports;
unified_app models are imported lazily inside functions, so there are no
circular imports with the monolith.

Time windows: "today" is the IST calendar day (the audience is Indian;
the dashboard labels it "Today · 12:00 AM IST onwards"). "This week"
is the rolling last 7 days. All timestamps are stored in UTC; window
bounds are converted to naive UTC datetimes for SQL comparisons, which
matches how the stored values are read back on both SQLite and Postgres
(TIMESTAMP WITHOUT TIME ZONE).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta, timezone

log = logging.getLogger(__name__)

# IST has no DST, so a fixed offset is exactly correct forever — no tzdata
# dependency (Render cold starts, minimal containers).
IST = timezone(timedelta(hours=5, minutes=30))

CACHE_TTL_SECONDS = 60

SESSION_EVENT = "session"

# ---------------------------------------------------------------------------
# Cache (in-memory, per-process; each gunicorn worker keeps its own copy).
# ---------------------------------------------------------------------------
_cache_lock = threading.Lock()
_cache: dict = {"at": 0.0, "payload": None}


def clear_traction_cache() -> None:
    """Drop the cached payload (tests / admin). Never raises."""
    try:
        with _cache_lock:
            _cache["at"] = 0.0
            _cache["payload"] = None
    except Exception:
        pass


def _window_bounds(now: datetime | None = None) -> dict:
    """IST day boundaries as naive UTC datetimes for SQL comparisons."""
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now_ist = now.astimezone(IST)
    today_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_ist = today_ist - timedelta(days=1)
    return {
        "now": now,
        "today_start": today_ist.astimezone(UTC).replace(tzinfo=None),
        "yesterday_start": yesterday_ist.astimezone(UTC).replace(tzinfo=None),
        "week_start": (now - timedelta(days=7)).replace(tzinfo=None),
    }


def _zeros_payload() -> dict:
    """Honest all-zero payload, same shape as a real one."""
    return {
        "active_today": 0,
        "active_today_delta": 0,
        "active_week": 0,
        "total_users": 0,
        "sessions_today": 0,
        "sessions_today_delta": 0,
        "quizzes_today": 0,
        "quizzes_today_delta": 0,
        "quizzes_total": 0,
        "dsa_today": 0,
        "dsa_today_delta": 0,
        "dsa_total": 0,
        "interviews_today": 0,
        "interviews_today_delta": 0,
        "interviews_total": 0,
        "resumes_today": 0,
        "resumes_today_delta": 0,
        "resumes_total": 0,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "today_label": "Today · 12:00 AM IST onwards",
        "degraded": True,
    }


# ---------------------------------------------------------------------------
# Event recording (never raises — a tracking failure must never break
# login, the dashboard, or any other user flow).
# ---------------------------------------------------------------------------
def record_session_event(user_id: int, now: datetime | None = None) -> bool:
    """Record one session-start event (a successful login).

    Every login is a session start, so logins are never deduped. Returns
    True on success, False on any failure (already logged).
    """
    try:
        from unified_app import TractionEvent, db

        event = TractionEvent(
            event_type=SESSION_EVENT,
            user_id=user_id,
            created_at=(now or datetime.now(UTC)),
        )
        db.session.add(event)
        db.session.commit()
        return True
    except Exception:
        log.exception("record_session_event failed (non-fatal)")
        try:
            from unified_app import db

            db.session.rollback()
        except Exception:
            pass
        return False


def ensure_daily_session_event(user_id: int, now: datetime | None = None) -> bool:
    """Record a session event for a remember-me dashboard visit.

    Only if the user has no session event yet today (IST day) — this is
    what makes "sessions today" count remember-me users who never
    re-logged in, without double-counting users who logged in today.
    Returns True if a row was inserted, False otherwise. Never raises.
    """
    try:
        from unified_app import TractionEvent, db

        bounds = _window_bounds(now)
        exists = (
            db.session.query(TractionEvent.id)
            .filter(
                TractionEvent.user_id == user_id,
                TractionEvent.event_type == SESSION_EVENT,
                TractionEvent.created_at >= bounds["today_start"],
            )
            .first()
        )
        if exists is not None:
            return False
        return record_session_event(user_id, now=now)
    except Exception:
        log.exception("ensure_daily_session_event failed (non-fatal)")
        try:
            from unified_app import db

            db.session.rollback()
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# Metric computation (may raise — get_traction() guards it).
# ---------------------------------------------------------------------------
def _count(query) -> int:
    """COUNT(*) for a filtered query, 0-safe."""
    return int(query.count() or 0)


def _active_users(
    db,
    TractionEvent,
    QuizAttempt,
    DSAProgress,
    InterviewSession,
    ResumeAnalysis,
    AptitudeAttempt,
    start,
    end=None,
) -> int:
    """Distinct users active in [start, end): session events OR any content
    action (submitted quiz/aptitude, DSA progress touch, completed
    interview, resume analysis). One UNION over indexed columns."""

    def _windowed(q, column):
        q = q.filter(column >= start)
        if end is not None:
            q = q.filter(column < end)
        return q

    parts = [
        _windowed(
            db.session.query(TractionEvent.user_id).filter(TractionEvent.user_id.isnot(None)),
            TractionEvent.created_at,
        ),
        _windowed(
            db.session.query(QuizAttempt.user_id).filter(QuizAttempt.submitted_at.isnot(None)),
            QuizAttempt.submitted_at,
        ),
        _windowed(
            db.session.query(AptitudeAttempt.user_id).filter(
                AptitudeAttempt.submitted_at.isnot(None)
            ),
            AptitudeAttempt.submitted_at,
        ),
        _windowed(db.session.query(DSAProgress.user_id), DSAProgress.updated_at),
        _windowed(
            db.session.query(InterviewSession.user_id).filter(
                InterviewSession.total_score.isnot(None)
            ),
            InterviewSession.created_at,
        ),
        _windowed(db.session.query(ResumeAnalysis.user_id), ResumeAnalysis.created_at),
    ]
    union = parts[0]
    for part in parts[1:]:
        union = union.union(part)
    return int(db.session.query(union.subquery()).count() or 0)


def compute_traction(now: datetime | None = None) -> dict:
    """Compute all metrics from real DB data. May raise on DB errors —
    callers must use get_traction(), which fails open."""
    from unified_app import (
        AptitudeAttempt,
        DSAProgress,
        InterviewSession,
        QuizAttempt,
        ResumeAnalysis,
        TractionEvent,
        User,
        db,
    )

    bounds = _window_bounds(now)
    today_start = bounds["today_start"]
    yesterday_start = bounds["yesterday_start"]
    week_start = bounds["week_start"]

    def _in_window(query, column, start, end=None):
        query = query.filter(column >= start)
        if end is not None:
            query = query.filter(column < end)
        return query

    active_today = _active_users(
        db,
        TractionEvent,
        QuizAttempt,
        DSAProgress,
        InterviewSession,
        ResumeAnalysis,
        AptitudeAttempt,
        today_start,
    )
    active_yesterday = _active_users(
        db,
        TractionEvent,
        QuizAttempt,
        DSAProgress,
        InterviewSession,
        ResumeAnalysis,
        AptitudeAttempt,
        yesterday_start,
        today_start,
    )
    active_week = _active_users(
        db,
        TractionEvent,
        QuizAttempt,
        DSAProgress,
        InterviewSession,
        ResumeAnalysis,
        AptitudeAttempt,
        week_start,
    )

    total_users = _count(db.session.query(User.id))

    sessions_today = _count(
        _in_window(
            db.session.query(TractionEvent.id).filter(TractionEvent.event_type == SESSION_EVENT),
            TractionEvent.created_at,
            today_start,
        )
    )
    sessions_yesterday = _count(
        _in_window(
            db.session.query(TractionEvent.id).filter(TractionEvent.event_type == SESSION_EVENT),
            TractionEvent.created_at,
            yesterday_start,
            today_start,
        )
    )

    submitted_quizzes = db.session.query(QuizAttempt.id).filter(
        QuizAttempt.submitted_at.isnot(None)
    )
    quizzes_today = _count(_in_window(submitted_quizzes, QuizAttempt.submitted_at, today_start))
    quizzes_yesterday = _count(
        _in_window(submitted_quizzes, QuizAttempt.submitted_at, yesterday_start, today_start)
    )
    quizzes_total = _count(submitted_quizzes)

    solved_dsa = db.session.query(DSAProgress.id).filter(DSAProgress.status == "solved")
    dsa_today = _count(_in_window(solved_dsa, DSAProgress.updated_at, today_start))
    dsa_yesterday = _count(
        _in_window(solved_dsa, DSAProgress.updated_at, yesterday_start, today_start)
    )
    dsa_total = _count(solved_dsa)

    done_interviews = db.session.query(InterviewSession.id).filter(
        InterviewSession.total_score.isnot(None)
    )
    interviews_today = _count(_in_window(done_interviews, InterviewSession.created_at, today_start))
    interviews_yesterday = _count(
        _in_window(done_interviews, InterviewSession.created_at, yesterday_start, today_start)
    )
    interviews_total = _count(done_interviews)

    resumes = db.session.query(ResumeAnalysis.id)
    resumes_today = _count(_in_window(resumes, ResumeAnalysis.created_at, today_start))
    resumes_yesterday = _count(
        _in_window(resumes, ResumeAnalysis.created_at, yesterday_start, today_start)
    )
    resumes_total = _count(resumes)

    return {
        "active_today": active_today,
        "active_today_delta": active_today - active_yesterday,
        "active_week": active_week,
        "total_users": total_users,
        "sessions_today": sessions_today,
        "sessions_today_delta": sessions_today - sessions_yesterday,
        "quizzes_today": quizzes_today,
        "quizzes_today_delta": quizzes_today - quizzes_yesterday,
        "quizzes_total": quizzes_total,
        "dsa_today": dsa_today,
        "dsa_today_delta": dsa_today - dsa_yesterday,
        "dsa_total": dsa_total,
        "interviews_today": interviews_today,
        "interviews_today_delta": interviews_today - interviews_yesterday,
        "interviews_total": interviews_total,
        "resumes_today": resumes_today,
        "resumes_today_delta": resumes_today - resumes_yesterday,
        "resumes_total": resumes_total,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "today_label": "Today · 12:00 AM IST onwards",
        "degraded": False,
    }


def get_traction(now: datetime | None = None) -> dict:
    """Cached traction payload — NEVER raises (100%-working law).

    Returns the ~60s cached payload when fresh; otherwise recomputes.
    On any error: the last good cached payload if one exists, else honest
    zeros — always a valid response shape, never an exception.
    """
    try:
        with _cache_lock:
            cached_at = _cache["at"]
            cached = _cache["payload"]
        if cached is not None and (time.monotonic() - cached_at) < CACHE_TTL_SECONDS:
            return cached
    except Exception:
        log.exception("traction cache read failed (non-fatal)")
    try:
        fresh = compute_traction(now=now)
    except Exception:
        log.exception("compute_traction failed — failing open")
        try:
            with _cache_lock:
                cached = _cache["payload"]
            if cached is not None:
                stale = dict(cached)
                stale["degraded"] = True
                return stale
        except Exception:
            pass
        return _zeros_payload()
    try:
        with _cache_lock:
            _cache["at"] = time.monotonic()
            _cache["payload"] = fresh
    except Exception:
        log.exception("traction cache write failed (non-fatal)")
    return fresh
