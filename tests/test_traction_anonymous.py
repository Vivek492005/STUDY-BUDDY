"""Anonymous-user tracking for the Live Traction Tracker.

Two populations, always separate:
- Anonymous resume analyses (/try demo, AnonymousAnalysis rows) feed the
  combined resume counts AND their own anonymous_resumes_* counters.
- Anonymous visitor sessions (random sb_anon cookie id, anon_session
  events, user_id NULL) feed anonymous_active_today/week only — they
  must NEVER leak into total_users / active_today / sessions_today.

Honesty rule: every metric is computed from real DB rows. Tests seed
rows with explicit timestamps and assert exact counts.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta

from studybuddy import traction as traction_mod
from tests.conftest import login, register
from tests.test_traction import FIXED_NOW, _make_user, _seed_traction_data
from unified_app import AnonymousAnalysis, ResumeAnalysis, TractionEvent, db

ANON_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


def _make_anon_resume(created_at, analysis_id=None):
    """An AnonymousAnalysis row, mirroring the /try flow (24h TTL)."""
    naive = created_at.replace(tzinfo=None) if created_at.tzinfo else created_at
    row = AnonymousAnalysis(
        id=analysis_id or uuid.uuid4().hex,
        filename="anon.pdf",
        result_json={"score": 70},
        created_at=naive,
        expires_at=naive + timedelta(seconds=24 * 3600),
    )
    db.session.add(row)
    return row


def _make_anon_session(session_id, created_at):
    naive = created_at.replace(tzinfo=None) if created_at.tzinfo else created_at
    db.session.add(
        TractionEvent(
            event_type="anon_session",
            user_id=None,
            session_id=session_id,
            created_at=naive,
        )
    )


# ---------- Part 1: anonymous resume analyses ----------


def test_anonymous_resumes_counted_in_combined_totals(app):
    """Anonymous analyses join the combined resume counts AND keep their
    own split counters."""
    with app.app_context():
        traction_mod.clear_traction_cache()
        bounds = traction_mod._window_bounds(FIXED_NOW)
        today = bounds["today_start"] + timedelta(hours=2)
        yesterday = bounds["yesterday_start"] + timedelta(hours=2)
        user = _make_user("r@example.com", "Resume User")
        db.session.add(ResumeAnalysis(user_id=user.id, filename="r1.pdf", created_at=today))
        db.session.add(ResumeAnalysis(user_id=user.id, filename="r2.pdf", created_at=yesterday))
        _make_anon_resume(today)
        _make_anon_resume(today + timedelta(hours=1))
        _make_anon_resume(yesterday)
        db.session.commit()
        data = traction_mod.compute_traction(now=FIXED_NOW)
    # Combined: 1 registered + 2 anon today; 1 + 1 yesterday; 2 + 3 total.
    assert data["resumes_today"] == 3
    assert data["resumes_total"] == 5
    assert data["resumes_today_delta"] == 1
    # Separate split, always visible.
    assert data["anonymous_resumes_today"] == 2
    assert data["anonymous_resumes_total"] == 3


def test_anonymous_resumes_alone_leave_registered_counters_zero(app):
    """Anonymous activity never invents users or registered sessions."""
    with app.app_context():
        traction_mod.clear_traction_cache()
        bounds = traction_mod._window_bounds(FIXED_NOW)
        today = bounds["today_start"] + timedelta(hours=2)
        _make_anon_resume(today)
        _make_anon_session("a" * 32, today)
        db.session.commit()
        data = traction_mod.compute_traction(now=FIXED_NOW)
    assert data["total_users"] == 0
    assert data["active_today"] == 0
    assert data["active_week"] == 0
    assert data["sessions_today"] == 0
    assert data["anonymous_resumes_today"] == 1
    assert data["anonymous_active_today"] == 1


# ---------- Part 2: anonymous visitor sessions ----------


def test_anon_session_recorded_once_per_day_per_session(app):
    """Second visit same day, same session: no double-count."""
    sid = "b" * 32
    with app.app_context():
        traction_mod.clear_traction_cache()
        assert traction_mod.ensure_daily_anon_session_event(sid, now=FIXED_NOW) is True
        assert traction_mod.ensure_daily_anon_session_event(sid, now=FIXED_NOW) is False
        events = TractionEvent.query.filter_by(event_type="anon_session").all()
        assert len(events) == 1
        assert events[0].user_id is None
        assert events[0].session_id == sid
        data = traction_mod.compute_traction(now=FIXED_NOW)
    assert data["anonymous_active_today"] == 1
    assert data["anonymous_active_week"] == 1


def test_distinct_sessions_count_separately(app):
    """Three sessions today = 3 visitors; one session active two days
    still counts once in the rolling week."""
    with app.app_context():
        traction_mod.clear_traction_cache()
        bounds = traction_mod._window_bounds(FIXED_NOW)
        today = bounds["today_start"] + timedelta(hours=2)
        yesterday = bounds["yesterday_start"] + timedelta(hours=2)
        _make_anon_session("c" * 32, today)
        _make_anon_session("c" * 32, yesterday)  # same session, two days
        _make_anon_session("d" * 32, today)
        _make_anon_session("e" * 32, today)
        db.session.commit()
        data = traction_mod.compute_traction(now=FIXED_NOW)
    assert data["anonymous_active_today"] == 3
    assert data["anonymous_active_week"] == 3  # c, d, e — c counted once


def test_week_window_excludes_old_sessions(app):
    """An 8-day-old session is out of the rolling 7-day window; a
    6-day-old one is in."""
    with app.app_context():
        traction_mod.clear_traction_cache()
        old = FIXED_NOW - timedelta(days=8)
        recent = FIXED_NOW - timedelta(days=6)
        _make_anon_session("f0" * 16, old)
        _make_anon_session("f1" * 16, recent)
        db.session.commit()
        data = traction_mod.compute_traction(now=FIXED_NOW)
    assert data["anonymous_active_week"] == 1
    assert data["anonymous_active_today"] == 0


def test_invalid_session_ids_never_record(app):
    """Junk cookie values are treated as absent — no garbage rows."""
    with app.app_context():
        traction_mod.clear_traction_cache()
        for bad in (None, "", "not-a-uuid", "A" * 32, "g" * 32, "a" * 31, "a" * 33):
            assert traction_mod.ensure_daily_anon_session_event(bad, now=FIXED_NOW) is False
        assert TractionEvent.query.count() == 0


# ---------- HTTP: cookie flow + endpoint ----------


def test_cookie_flow_records_one_session_per_day(app, client):
    """First visit mints sb_anon (HttpOnly, Lax); second visit records
    the session; third visit same day adds nothing."""
    traction_mod.clear_traction_cache()
    resp = client.get("/")
    assert resp.status_code == 200
    set_cookies = resp.headers.getlist("Set-Cookie")
    anon = [h for h in set_cookies if h.startswith("sb_anon=")]
    assert len(anon) == 1, set_cookies
    assert "HttpOnly" in anon[0]
    assert "SameSite=Lax" in anon[0]
    value = anon[0].split(";", 1)[0].split("=", 1)[1]
    assert ANON_HEX_RE.match(value), value
    with app.app_context():
        # First visit only mints the cookie — no event yet (bot filter).
        assert TractionEvent.query.filter_by(event_type="anon_session").count() == 0
    client.get("/")  # second visit, cookie jar returns sb_anon
    with app.app_context():
        assert TractionEvent.query.filter_by(event_type="anon_session").count() == 1
    client.get("/")  # third visit, same day
    with app.app_context():
        assert TractionEvent.query.filter_by(event_type="anon_session").count() == 1
    traction_mod.clear_traction_cache()
    resp = client.get("/api/traction")
    assert resp.status_code == 200
    assert resp.get_json()["anonymous_active_today"] == 1


def test_api_poll_without_cookie_records_nothing(app, client):
    """A cookie-less request (bot, curl, direct API hit) never mints a
    session — only returning cookie-holders count."""
    traction_mod.clear_traction_cache()
    resp = client.get("/api/traction")
    assert resp.status_code == 200
    assert resp.get_json()["anonymous_active_today"] == 0
    with app.app_context():
        assert TractionEvent.query.filter_by(event_type="anon_session").count() == 0


def test_invalid_cookie_is_replaced_not_trusted(app, client):
    """A junk sb_anon value is ignored (no event) and a fresh valid
    cookie is minted on the HTML response."""
    traction_mod.clear_traction_cache()
    client.set_cookie("sb_anon", "junk-value")
    resp = client.get("/")
    assert resp.status_code == 200
    anon = [h for h in resp.headers.getlist("Set-Cookie") if h.startswith("sb_anon=")]
    assert len(anon) == 1
    value = anon[0].split(";", 1)[0].split("=", 1)[1]
    assert ANON_HEX_RE.match(value), value
    with app.app_context():
        assert TractionEvent.query.filter_by(event_type="anon_session").count() == 0


def test_logged_in_user_gets_no_anon_cookie_or_event(app, client):
    """Once logged in, no sb_anon cookie is minted and no new
    anon_session events are recorded — registered users are counted by
    user id only. (The register/login requests themselves are anonymous
    browsing, so one anon event from before the login is legitimate and
    must not grow afterwards.)"""
    register(client)
    login(client)
    with app.app_context():
        anon_before = TractionEvent.query.filter_by(event_type="anon_session").count()
    resp = client.get("/")
    assert resp.status_code == 200
    assert not [h for h in resp.headers.getlist("Set-Cookie") if h.startswith("sb_anon=")]
    client.get("/dashboard")
    client.get("/")
    with app.app_context():
        assert (
            TractionEvent.query.filter_by(event_type="anon_session").count() == anon_before
        )
        # The login itself recorded a registered session event.
        assert TractionEvent.query.filter_by(event_type="session").count() >= 1


def test_endpoint_includes_anonymous_fields(app, client):
    """New fields ride the same public endpoint (shape + real values)."""
    with app.app_context():
        traction_mod.clear_traction_cache()
        # Seed relative to REAL now: the endpoint computes with the real
        # clock (see _seed_traction_data docstring — FIXED_NOW seeds
        # drift out of "today" on later run dates).
        now = datetime.now(UTC)
        bounds = traction_mod._window_bounds(now)
        today = bounds["today_start"] + timedelta(hours=2)
        _make_anon_resume(today)
        _make_anon_session("1" * 32, today)
        _make_anon_session("2" * 32, today)
        _seed_traction_data(now=now)
        db.session.commit()
    resp = client.get("/api/traction")
    assert resp.status_code == 200
    data = resp.get_json()
    for key in (
        "anonymous_resumes_today",
        "anonymous_resumes_total",
        "anonymous_active_today",
        "anonymous_active_week",
    ):
        assert isinstance(data[key], int), key
        assert data[key] >= 0, key
    assert data["anonymous_resumes_today"] == 1
    assert data["anonymous_resumes_total"] == 1
    assert data["anonymous_active_today"] == 2
    assert data["anonymous_active_week"] == 2
    # Combined resume counts include the anonymous analysis (seed has
    # 1 registered resume today, 1 yesterday).
    assert data["resumes_today"] == 2
    assert data["resumes_total"] == 3
    # Registered counters are exactly the seed's (anon adds nothing).
    assert data["total_users"] == 3
    assert data["active_today"] == 1
    assert data["sessions_today"] == 1


def test_privacy_page_mentions_anon_cookie(client):
    """The cookie is disclosed honestly: random ID, no personal data,
    no cross-site tracking, no fingerprinting."""
    resp = client.get("/privacy")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "sb_anon" in html
    assert "random ID" in html
    assert "no personal data" in html
    assert "cross-site tracking" in html
    assert "fingerprinting" in html
