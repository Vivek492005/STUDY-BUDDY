"""Tests for the Live Traction Tracker (landing-page strip + /api/traction).

Honesty rule: every metric is computed from real DB rows. Tests seed rows
with explicit timestamps and assert exact counts — no invented numbers.

100%-working law: the endpoint must never 500 — cold cache, empty DB,
DB error, concurrent load, repeated polling. Fail-open paths are tested
explicitly (see test_fail_open_*).
"""

import threading
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import OperationalError

from studybuddy import traction as traction_mod
from tests.conftest import login, register
from unified_app import (
    DSAProgress,
    InterviewSession,
    QuizAttempt,
    ResumeAnalysis,
    TractionEvent,
    User,
    db,
)

FIXED_NOW = datetime(2026, 11, 5, 10, 30, tzinfo=UTC)  # 16:00 IST, mid-day

EXPECTED_KEYS = {
    "active_today",
    "active_today_delta",
    "active_week",
    "active_week_delta",
    "new_users_week",
    "daily",
    "total_users",
    "sessions_today",
    "sessions_today_delta",
    "quizzes_today",
    "quizzes_today_delta",
    "quizzes_total",
    "dsa_today",
    "dsa_today_delta",
    "dsa_total",
    "interviews_today",
    "interviews_today_delta",
    "interviews_total",
    "resumes_today",
    "resumes_today_delta",
    "resumes_total",
    "generated_at",
    "today_label",
    "degraded",
}

# Scalar int keys (everything in EXPECTED_KEYS except the daily series,
# generated_at, today_label, degraded).
INT_KEYS = EXPECTED_KEYS - {"daily", "generated_at", "today_label", "degraded"}

DAILY_KEYS = {"date", "label", "active", "quizzes", "dsa"}


def _assert_daily_zeros(data):
    """Daily series: 7 entries, honest zeros, IST day labels present."""
    daily = data["daily"]
    assert isinstance(daily, list) and len(daily) == 7
    for entry in daily:
        assert DAILY_KEYS <= set(entry.keys())
        assert entry["active"] == 0
        assert entry["quizzes"] == 0
        assert entry["dsa"] == 0
        assert isinstance(entry["date"], str) and entry["date"]
        assert isinstance(entry["label"], str) and entry["label"]


def _make_user(email, name="Test User"):
    user = User(name=name, email=email)
    user.set_password("Str0ng!Pass1")
    db.session.add(user)
    db.session.commit()
    return user


def _seed_traction_data():
    """Seed 3 users with today/yesterday activity. Returns (bounds, users).

    user1: session + quiz + DSA solved + interview done + resume — TODAY.
    user2: session + quiz + DSA solved + resume — YESTERDAY; interview
           started but NOT completed (total_score None → must not count).
    user3: created yesterday, no activity at all.
    """
    bounds = traction_mod._window_bounds(FIXED_NOW)
    today = bounds["today_start"] + timedelta(hours=2)
    yesterday = bounds["yesterday_start"] + timedelta(hours=2)

    u1 = _make_user("u1@example.com", "User One")
    u2 = _make_user("u2@example.com", "User Two")
    u3 = _make_user("u3@example.com", "User Three")
    db.session.execute(db.update(User).where(User.id == u3.id).values(created_at=yesterday))
    db.session.commit()

    db.session.add_all(
        [
            TractionEvent(event_type="session", user_id=u1.id, created_at=today),
            TractionEvent(event_type="session", user_id=u2.id, created_at=yesterday),
            QuizAttempt(
                user_id=u1.id, skill="python", num_questions=5, started_at=today, submitted_at=today
            ),
            QuizAttempt(
                user_id=u2.id,
                skill="python",
                num_questions=5,
                started_at=yesterday,
                submitted_at=yesterday,
            ),
            DSAProgress(
                user_id=u1.id,
                problem_id="two-sum",
                status="solved",
                created_at=today,
                updated_at=today,
            ),
            DSAProgress(
                user_id=u2.id,
                problem_id="two-sum",
                status="solved",
                created_at=yesterday,
                updated_at=yesterday,
            ),
            DSAProgress(
                user_id=u3.id,
                problem_id="two-sum",
                status="attempted",
                created_at=yesterday,
                updated_at=yesterday,
            ),
            InterviewSession(
                user_id=u1.id,
                company="Acme",
                role="SDE",
                category="technical",
                total_score=82.5,
                created_at=today,
            ),
            InterviewSession(
                user_id=u2.id,
                company="Acme",
                role="SDE",
                category="technical",
                total_score=None,
                created_at=yesterday,
            ),
            ResumeAnalysis(user_id=u1.id, filename="r1.pdf", created_at=today),
            ResumeAnalysis(user_id=u2.id, filename="r2.pdf", created_at=yesterday),
        ]
    )
    db.session.commit()
    return bounds, (u1, u2, u3)


# ---------- endpoint shape / public access ----------


def test_endpoint_is_public_and_shaped(client):
    """No login needed (landing page); response has every key, ints >= 0."""
    traction_mod.clear_traction_cache()
    resp = client.get("/api/traction")
    assert resp.status_code == 200
    data = resp.get_json()
    assert EXPECTED_KEYS <= set(data.keys())
    for key in INT_KEYS:
        assert isinstance(data[key], int), key
        assert data[key] >= 0, key
    _assert_daily_zeros(data)
    assert data["degraded"] is False
    assert data["today_label"] == "Today · 12:00 AM IST onwards"


def test_endpoint_zero_data_shows_zero(app):
    """Fresh DB, no users at all: compute returns honest zeros."""
    with app.app_context():
        traction_mod.clear_traction_cache()
        data = traction_mod.compute_traction(now=FIXED_NOW)
    for key in INT_KEYS:
        assert data[key] == 0, key
    _assert_daily_zeros(data)
    assert data["degraded"] is False


def test_landing_page_renders_tracker(client):
    """Landing page carries the live strip (honest 0s before JS fills in)."""
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="traction-grid"' in html
    assert "LIVE" in html
    assert 'id="tr-active-today"' in html
    assert 'id="tr-quizzes-total"' in html
    assert "Today · 12:00 AM IST onwards" in html
    # Growth showcase (2026-11-05): slogan + analytics row.
    assert "sb-traction-showcase" in html
    assert "Real students. Real growth." in html
    assert "Every day." in html
    assert 'id="traction-trend"' in html
    assert 'id="tr-new-users-week"' in html
    assert 'id="tr-active-week-delta"' in html


def test_dashboard_has_no_tracker(client):
    """Placement redirect: the tracker lives on the landing page only."""
    register(client)
    login(client)
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert 'id="traction-grid"' not in resp.get_data(as_text=True)


# ---------- metric SQL logic with seeded data ----------


def test_metric_counts_with_seeded_data(app):
    with app.app_context():
        traction_mod.clear_traction_cache()
        _seed_traction_data()
        data = traction_mod.compute_traction(now=FIXED_NOW)

    assert data["total_users"] == 3
    # Active today: only u1 (u2 acted yesterday, u3 attempted DSA yesterday).
    assert data["active_today"] == 1
    # Yesterday: u2 (session) + u3 (DSA attempt) → delta = 1 - 2 = -1.
    assert data["active_today_delta"] == -1
    # Active this week: u1 + u2 + u3 (u3's DSA attempt was yesterday).
    assert data["active_week"] == 3
    assert data["sessions_today"] == 1
    assert data["sessions_today_delta"] == 0
    assert data["quizzes_today"] == 1
    assert data["quizzes_total"] == 2
    assert data["quizzes_today_delta"] == 0
    assert data["dsa_today"] == 1
    assert data["dsa_total"] == 2  # u3 attempted-but-not-solved excluded
    assert data["dsa_today_delta"] == 0
    assert data["interviews_today"] == 1
    assert data["interviews_total"] == 1  # u2 incomplete excluded
    assert data["interviews_today_delta"] == 1
    assert data["resumes_today"] == 1
    assert data["resumes_total"] == 2
    assert data["resumes_today_delta"] == 0


def test_endpoint_reflects_seeded_data(app, client):
    """Endpoint returns the same seeded counts over HTTP."""
    with app.app_context():
        traction_mod.clear_traction_cache()
        _seed_traction_data()
    resp = client.get("/api/traction")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_users"] == 3
    assert data["active_today"] == 1
    assert data["quizzes_total"] == 2
    assert data["interviews_total"] == 1


# ---------- analytics: 7-day trend + growth callouts ----------


def test_daily_series_with_seeded_data(app):
    """7 IST days, oldest first; today/yesterday real, older days zero."""
    with app.app_context():
        traction_mod.clear_traction_cache()
        _seed_traction_data()
        data = traction_mod.compute_traction(now=FIXED_NOW)
    daily = data["daily"]
    assert isinstance(daily, list) and len(daily) == 7
    assert [e["date"] for e in daily] == [
        "2026-10-30",
        "2026-10-31",
        "2026-11-01",
        "2026-11-02",
        "2026-11-03",
        "2026-11-04",
        "2026-11-05",
    ]
    # FIXED_NOW = 2026-11-05 10:30 UTC = 16:00 IST (Thursday).
    assert daily[-1] == {
        "date": "2026-11-05",
        "label": "Thu 5 Nov",
        "active": 1,
        "quizzes": 1,
        "dsa": 1,
    }
    # Yesterday: u2 (session + quiz + DSA) and u3 (DSA attempt) active.
    assert daily[-2] == {
        "date": "2026-11-04",
        "label": "Wed 4 Nov",
        "active": 2,
        "quizzes": 1,
        "dsa": 1,
    }
    for entry in daily[:-2]:
        assert entry["active"] == 0
        assert entry["quizzes"] == 0
        assert entry["dsa"] == 0


def test_growth_fields_with_seeded_data(app):
    """new_users_week counts signups in the rolling 7 days; the
    week-over-week delta compares active_week with the prior 7 days."""
    with app.app_context():
        traction_mod.clear_traction_cache()
        bounds, (u1, u2, u3) = _seed_traction_data()
        today = bounds["today_start"] + timedelta(hours=2)
        yesterday = bounds["yesterday_start"] + timedelta(hours=2)
        # Pin signups to FIXED_NOW-relative days (_make_user otherwise
        # stamps real-now, which drifts from FIXED_NOW).
        db.session.execute(db.update(User).where(User.id == u1.id).values(created_at=today))
        db.session.execute(db.update(User).where(User.id == u2.id).values(created_at=yesterday))
        # A user from 10 days ago: in total_users, NOT in the week.
        u4 = _make_user("u4@example.com", "User Four")
        db.session.execute(
            db.update(User)
            .where(User.id == u4.id)
            .values(created_at=FIXED_NOW - timedelta(days=10))
        )
        db.session.commit()
        data = traction_mod.compute_traction(now=FIXED_NOW)
    assert data["total_users"] == 4
    assert data["new_users_week"] == 3
    # Prior 7 days had no activity at all → delta == active_week.
    assert data["active_week"] == 3
    assert data["active_week_delta"] == 3


def test_endpoint_includes_analytics_fields(app, client):
    """New analytics fields ride the same public endpoint (shape + the
    relational invariants that hold whatever day the suite runs)."""
    with app.app_context():
        traction_mod.clear_traction_cache()
        _seed_traction_data()
        # Pin signups to real-now-relative days so the week count holds
        # on any run date (the endpoint computes with real now).
        now = datetime.now(UTC).replace(tzinfo=None)
        for email, when in [
            ("u1@example.com", now),
            ("u2@example.com", now - timedelta(days=1)),
            ("u3@example.com", now - timedelta(days=1)),
        ]:
            user = User.query.filter_by(email=email).first()
            db.session.execute(db.update(User).where(User.id == user.id).values(created_at=when))
        db.session.commit()
    resp = client.get("/api/traction")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["new_users_week"] == 3
    # No seeded activity predates the fixture window, so the prior
    # week's actives are 0 and the delta equals active_week.
    assert data["active_week_delta"] == data["active_week"]
    daily = data["daily"]
    assert isinstance(daily, list) and len(daily) == 7
    for entry in daily:
        assert DAILY_KEYS <= set(entry.keys())
        assert entry["active"] >= 0
        assert entry["quizzes"] >= 0
        assert entry["dsa"] >= 0


# ---------- session event recording ----------


def test_login_records_session_event(app, client):
    register(client)
    login(client)
    with app.app_context():
        user = User.query.filter_by(email="aarav@example.com").first()
        event = TractionEvent.query.filter_by(user_id=user.id, event_type="session").first()
    assert event is not None


def test_dashboard_visit_records_one_session_per_day(app, client):
    register(client)
    login(client)
    client.get("/dashboard")
    client.get("/dashboard")
    with app.app_context():
        user = User.query.filter_by(email="aarav@example.com").first()
        events = TractionEvent.query.filter_by(user_id=user.id, event_type="session").all()
    # One from the login + at most one from the dashboard visits.
    assert len(events) == 1


# ---------- cache behaviour ----------


def test_cache_serves_stale_then_refreshes(app):
    with app.app_context():
        traction_mod.clear_traction_cache()
        first = traction_mod.get_traction(now=FIXED_NOW)
        assert first["total_users"] == 0
        _make_user("new@example.com", "New User")
        second = traction_mod.get_traction(now=FIXED_NOW)
        assert second["total_users"] == 0  # cached, not recomputed
        assert second is first
        traction_mod.clear_traction_cache()
        third = traction_mod.get_traction(now=FIXED_NOW)
        assert third["total_users"] == 1


def test_cache_ttl_expiry_recomputes(app, monkeypatch):
    with app.app_context():
        traction_mod.clear_traction_cache()
        monkeypatch.setattr(traction_mod, "CACHE_TTL_SECONDS", 0)
        first = traction_mod.get_traction(now=FIXED_NOW)
        assert first["total_users"] == 0
        _make_user("new@example.com", "New User")
        second = traction_mod.get_traction(now=FIXED_NOW)
        assert second["total_users"] == 1


# ---------- fail-open (100%-working law) ----------


def _boom(*args, **kwargs):
    raise OperationalError("SELECT 1", None, Exception("db down"))


def test_fail_open_cold_cache_returns_zeros(app, monkeypatch):
    """DB error + cold cache → 200-shaped honest zeros, degraded=True."""
    with app.app_context():
        traction_mod.clear_traction_cache()
        monkeypatch.setattr(traction_mod, "compute_traction", _boom)
        data = traction_mod.get_traction(now=FIXED_NOW)
    assert data["degraded"] is True
    for key in INT_KEYS:
        assert data[key] == 0, key
    _assert_daily_zeros(data)


def test_fail_open_warm_cache_returns_last_good(app, monkeypatch):
    """DB error + warm cache → last good numbers, never an error.

    Fresh cache hit: the cached payload is returned as-is (degraded
    False — it was computed successfully <60s ago; the broken compute
    is never even called). Expired cache: recompute fails, so the
    last-good payload is served with degraded=True.
    """
    with app.app_context():
        traction_mod.clear_traction_cache()
        _seed_traction_data()
        good = traction_mod.get_traction(now=FIXED_NOW)
        assert good["total_users"] == 3
        monkeypatch.setattr(traction_mod, "compute_traction", _boom)
        fresh_hit = traction_mod.get_traction(now=FIXED_NOW)
        assert fresh_hit["total_users"] == 3
        assert fresh_hit["active_today"] == 1
        assert fresh_hit["degraded"] is False
        # Expire the cache so the next call must recompute (and fail).
        monkeypatch.setattr(traction_mod, "CACHE_TTL_SECONDS", 0)
        stale = traction_mod.get_traction(now=FIXED_NOW)
    assert stale["degraded"] is True
    assert stale["total_users"] == 3
    assert stale["active_today"] == 1


def test_endpoint_fail_open_on_db_error(app, client, monkeypatch):
    """Endpoint never 500s when the DB fails: 200 + valid shape."""
    with app.app_context():
        traction_mod.clear_traction_cache()
    monkeypatch.setattr(traction_mod, "compute_traction", _boom)
    resp = client.get("/api/traction")
    assert resp.status_code == 200
    data = resp.get_json()
    assert EXPECTED_KEYS <= set(data.keys())
    assert data["degraded"] is True
    assert data["total_users"] == 0


def test_concurrent_requests_all_valid(app):
    """20 parallel callers: every one gets a valid payload, no exception.

    The cache is warmed in the main thread first: tests use an in-memory
    SQLite DB, which is per-connection, so worker threads cannot see the
    main thread's tables — concurrent cache reads are the meaningful
    concurrency test here (the lock + double-checked get). Cold-cache
    concurrent compute against a shared DB is exercised in production
    (Postgres); any failure there still fails open to last-good/zeros.
    """
    with app.app_context():
        traction_mod.clear_traction_cache()
        _seed_traction_data()
        warm = traction_mod.get_traction(now=FIXED_NOW)
        assert warm["total_users"] == 3
    results = []
    errors = []

    def _call():
        try:
            with app.app_context():
                results.append(traction_mod.get_traction(now=FIXED_NOW))
        except Exception as exc:  # pragma: no cover - fail-open must prevent
            errors.append(exc)

    threads = [threading.Thread(target=_call) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(results) == 20
    for data in results:
        assert EXPECTED_KEYS <= set(data.keys())
        assert data["total_users"] == 3


def test_repeated_polling_never_errors(client):
    """70 rapid polls (a user polling every 60s for over an hour): all 200."""
    traction_mod.clear_traction_cache()
    for _ in range(70):
        resp = client.get("/api/traction")
        assert resp.status_code == 200
        assert resp.get_json()["degraded"] is False


def test_endpoint_timing_under_200ms(app, client):
    """Realistic volume: 200 quiz + 200 DSA + 100 interviews + 100 resumes."""
    with app.app_context():
        traction_mod.clear_traction_cache()
        user = _make_user("bulk@example.com", "Bulk User")
        now = datetime.now(UTC).replace(tzinfo=None)
        db.session.add_all(
            [
                QuizAttempt(
                    user_id=user.id,
                    skill="python",
                    num_questions=5,
                    started_at=now,
                    submitted_at=now,
                )
                for _ in range(200)
            ]
        )
        db.session.add_all(
            [
                DSAProgress(
                    user_id=user.id,
                    problem_id=f"p-{i}",
                    status="solved",
                    created_at=now,
                    updated_at=now,
                )
                for i in range(200)
            ]
        )
        db.session.add_all(
            [
                InterviewSession(
                    user_id=user.id,
                    company="Acme",
                    role="SDE",
                    category="technical",
                    total_score=80.0,
                    created_at=now,
                )
                for _ in range(100)
            ]
        )
        db.session.add_all(
            [
                ResumeAnalysis(user_id=user.id, filename=f"r{i}.pdf", created_at=now)
                for i in range(100)
            ]
        )
        db.session.commit()
    start = time.monotonic()
    resp = client.get("/api/traction")
    elapsed = time.monotonic() - start
    assert resp.status_code == 200
    assert resp.get_json()["quizzes_total"] == 200
    assert elapsed < 0.2, f"endpoint took {elapsed:.3f}s"


def test_record_session_event_never_raises(app):
    """record/ensure helpers swallow DB errors (login must never break)."""
    with app.app_context():
        traction_mod.clear_traction_cache()
        assert traction_mod.record_session_event(999999) is True
        # Non-existent user id: FK violation on Postgres; SQLite ignores
        # FKs by default — either way the helper must not raise.
        assert traction_mod.ensure_daily_session_event(999999) in (True, False)


def test_rate_key_is_per_user_when_logged_in(app):
    """Rate-limit key scopes to the user (not the shared IP) when authed."""
    from flask_login import login_user

    from unified_app import _traction_rate_key

    with app.app_context():
        user = _make_user("key@example.com", "Key User")
        user_id = user.id
    # Anonymous context → IP fallback (non-empty string).
    with app.test_request_context("/"):
        key = _traction_rate_key()
        assert isinstance(key, str) and key
        assert not key.startswith("traction-user:")
    # Authenticated context → per-user key.
    with app.test_request_context("/"):
        login_user(user)
        key = _traction_rate_key()
        assert key == f"traction-user:{user_id}"
