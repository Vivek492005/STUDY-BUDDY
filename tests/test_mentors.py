"""Tests for Wave 6 #27: mentor & alumni office hours.

Covers: defensive seeding + idempotency, hub rendering + path filter,
mentor profile (incl. 404), booking rules (success / double-book / full /
past / cancelled-session), cancellation window, attendance + XP events,
action notes, 1-5 feedback + XP, mentor application validation +
duplicate-pending, reporting, admin-verify 403/404/success, auth
redirects, CSRF rejection, pure-logic helpers (booking_eligibility,
can_cancel, refresh_session_status, mentor_is_admin), and seed/template
data sanity.
"""
from datetime import UTC, datetime, timedelta

import pytest

import studybuddy.community as community_mod
from tests.conftest import login, register
from unified_app import (
    Mentor,
    MentorApplication,
    OfficeHour,
    OHBooking,
    OHFeedback,
    OHNote,
    OHReport,
    User,
    XPLedger,
    create_app,
    db,
)

UTC = UTC


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uid(app, email):
    with app.app_context():
        return User.query.filter_by(email=email).first().id


def _register_extra(client, name, email):
    """Register an additional user (logout first: register skips while a
    session is active)."""
    client.post("/logout", follow_redirects=False)
    return register(client, name=name, email=email)


def _login_as(client, email):
    client.post("/logout", follow_redirects=False)
    return login(client, email=email)


def _mentor(app, **kwargs):
    with app.app_context():
        m = Mentor(name=kwargs.get("name", "Test Mentor"),
                   headline=kwargs.get("headline", "Engineer"),
                   path=kwargs.get("path", "engineering"),
                   company=kwargs.get("company", "TestCo"),
                   college=kwargs.get("college", "Test College"),
                   bio=kwargs.get("bio", "A test mentor bio."),
                   verified=kwargs.get("verified", True),
                   is_seed=kwargs.get("is_seed", False))
        db.session.add(m)
        db.session.commit()
        return m.id


def _office_hour(app, mentor_id, hours_ahead=24, **kwargs):
    with app.app_context():
        oh = OfficeHour(
            mentor_id=mentor_id,
            topic=kwargs.get("topic", "Ask me anything"),
            path=kwargs.get("path", "engineering"),
            scheduled_at=datetime.now(UTC) + timedelta(hours=hours_ahead),
            duration_min=kwargs.get("duration_min", 45),
            capacity=kwargs.get("capacity", 12),
            status=kwargs.get("status", "open"))
        db.session.add(oh)
        db.session.commit()
        return oh.id


def _authed(client, name="Aarav Sharma", email="aarav@example.com"):
    register(client, name=name, email=email)
    login(client, email=email)


def _book(client, oh_id, question="My question?", template_id=None):
    body = {"question_text": question}
    if template_id:
        body["template_id"] = template_id
    return client.post("/mentors/api/office-hours/%d/book" % oh_id, json=body)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def test_hub_seeds_mentors_defensively(app, client):
    _authed(client)
    with app.app_context():
        assert Mentor.query.count() == 0
    r = client.get("/mentors")
    assert r.status_code == 200
    with app.app_context():
        mentors = Mentor.query.all()
        assert len(mentors) == 14
        # Phase 5: seeds are ILLUSTRATIVE — never verified, never bookable.
        assert not any(m.verified for m in mentors)
        assert all(m.is_seed for m in mentors)
        paths = {m.path for m in mentors}
        assert len(paths) >= 5  # spread across paths


def test_seed_idempotent(app, client):
    _authed(client)
    client.get("/mentors")
    client.get("/mentors")
    with app.app_context():
        assert Mentor.query.count() == 14
        assert community_mod.seed_mentors() == 0
        assert Mentor.query.count() == 14


def test_seed_mentors_paths_realistic(app):
    with app.app_context():
        n = community_mod.seed_mentors()
        assert n == 14
        mentors = Mentor.query.all()
        for m in mentors:
            assert m.path in community_mod.PATH_KEYS
            assert m.name and m.headline and m.bio


def test_seed_skipped_when_mentors_exist(app, client):
    _authed(client)
    _mentor(app, name="Existing Mentor")
    client.get("/mentors")
    with app.app_context():
        assert Mentor.query.count() == 1
        assert Mentor.query.first().name == "Existing Mentor"


# ---------------------------------------------------------------------------
# Hub rendering + path filter
# ---------------------------------------------------------------------------

def test_hub_requires_login(client):
    r = client.get("/mentors")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_hub_renders(app, client):
    _authed(client)
    r = client.get("/mentors")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Mentors" in html
    assert "office hours" in html.lower()


def test_hub_path_filter(app, client):
    _authed(client)
    _mentor(app, name="Eng Mentor", path="engineering")
    _mentor(app, name="Med Mentor", path="medicine")
    r = client.get("/mentors?path=engineering")
    html = r.get_data(as_text=True)
    assert "Eng Mentor" in html
    assert "Med Mentor" not in html


def test_hub_invalid_path_filter_ignored(app, client):
    _authed(client)
    _mentor(app, name="Eng Mentor", path="engineering")
    r = client.get("/mentors?path=nonsense")
    assert r.status_code == 200
    assert "Eng Mentor" in r.get_data(as_text=True)


def test_hub_shows_upcoming_sessions(app, client):
    _authed(client)
    mid = _mentor(app)
    _office_hour(app, mid, topic="Future of AI careers")
    r = client.get("/mentors")
    assert "Future of AI careers" in r.get_data(as_text=True)


def test_hub_shows_my_bookings(app, client):
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid, topic="My booked topic")
    _book(client, oh_id)
    r = client.get("/mentors")
    assert "My booked topic" in r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Mentor profile
# ---------------------------------------------------------------------------

def test_profile_renders(app, client):
    _authed(client)
    mid = _mentor(app, name="Profile Mentor")
    r = client.get("/mentors/%d" % mid)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Profile Mentor" in html
    assert "csrf-token" in html


def test_profile_404(app, client):
    _authed(client)
    r = client.get("/mentors/999999")
    assert r.status_code == 404


def test_profile_requires_login(client):
    r = client.get("/mentors/1")
    assert r.status_code == 302


def test_profile_includes_question_templates(app, client):
    _authed(client)
    mid = _mentor(app)
    _office_hour(app, mid)
    r = client.get("/mentors/%d" % mid)
    html = r.get_data(as_text=True)
    # at least a few template prompts are offered on the page
    assert "first 90 days" in html
    assert "myth" in html.lower()


def test_profile_lists_upcoming_sessions(app, client):
    _authed(client)
    mid = _mentor(app)
    _office_hour(app, mid, topic="Profile session topic")
    r = client.get("/mentors/%d" % mid)
    assert "Profile session topic" in r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------

def test_booking_success(app, client):
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid)
    r = _book(client, oh_id)
    assert r.status_code == 201
    data = r.get_json()
    assert data["success"] is True
    assert data["booking_id"]
    with app.app_context():
        b = db.session.get(OHBooking, data["booking_id"])
        assert b.question_text == "My question?"
        assert b.status == "booked"


def test_booking_with_template(app, client):
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid)
    r = _book(client, oh_id, question="Custom follow-up",
              template_id="first_90_days")
    assert r.status_code == 201


def test_booking_invalid_template_rejected(app, client):
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid)
    r = _book(client, oh_id, template_id="nope_not_real")
    assert r.status_code == 400


def test_booking_question_too_long(app, client):
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid)
    r = _book(client, oh_id, question="x" * 2001)
    assert r.status_code == 400


def test_booking_double_book_rejected(app, client):
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid)
    assert _book(client, oh_id).status_code == 201
    r = _book(client, oh_id, question="second attempt")
    assert r.status_code == 400
    assert "already" in r.get_json()["error"].lower()


def test_booking_full_rejected(app, client):
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid, capacity=1)
    assert _book(client, oh_id).status_code == 201
    _register_extra(client, "Second User", "second@example.com")
    _login_as(client, "second@example.com")
    r = _book(client, oh_id)
    assert r.status_code == 400
    assert "full" in r.get_json()["error"].lower()


def test_booking_marks_session_full(app, client):
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid, capacity=1)
    _book(client, oh_id)
    with app.app_context():
        assert db.session.get(OfficeHour, oh_id).status == "full"


def test_booking_past_session_rejected(app, client):
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid, hours_ahead=-2)
    r = _book(client, oh_id)
    assert r.status_code == 400


def test_booking_cancelled_session_rejected(app, client):
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid, status="cancelled")
    r = _book(client, oh_id)
    assert r.status_code == 400


def test_booking_missing_session_404(client):
    _authed(client)
    r = _book(client, 999999)
    assert r.status_code == 404


def test_booking_unique_constraint(app, client):
    """DB-level guard: the unique constraint also blocks double-booking."""
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid)
    uid = _uid(app, "aarav@example.com")
    with app.app_context():
        db.session.add(OHBooking(user_id=uid, office_hour_id=oh_id,
                                 status="booked"))
        db.session.commit()
        db.session.add(OHBooking(user_id=uid, office_hour_id=oh_id,
                                 status="booked"))
        with pytest.raises(Exception):  # noqa: B017 - intent is 'any commit failure'
            db.session.commit()
        db.session.rollback()


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

def test_cancel_success_reopens_session(app, client):
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid, capacity=1)
    booking_id = _book(client, oh_id).get_json()["booking_id"]
    with app.app_context():
        assert db.session.get(OfficeHour, oh_id).status == "full"
    r = client.post("/mentors/api/bookings/%d/cancel" % booking_id)
    assert r.status_code == 200
    assert r.get_json()["session_status"] == "open"
    with app.app_context():
        assert db.session.get(OHBooking, booking_id).status == "cancelled"


def test_cancel_too_close_rejected(app, client):
    _authed(client)
    mid = _mentor(app)
    # session starts in 30 minutes -> inside the 60-min cutoff
    with app.app_context():
        oh = OfficeHour(mentor_id=mid, topic="Soon", path="engineering",
                        scheduled_at=datetime.now(UTC) + timedelta(minutes=30),
                        capacity=12, status="open")
        db.session.add(oh)
        db.session.commit()
        oh_id = oh.id
    booking_id = _book(client, oh_id).get_json()["booking_id"]
    r = client.post("/mentors/api/bookings/%d/cancel" % booking_id)
    assert r.status_code == 400
    assert "hour" in r.get_json()["error"].lower()


def test_cancel_twice_rejected(app, client):
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid)
    booking_id = _book(client, oh_id).get_json()["booking_id"]
    assert client.post("/mentors/api/bookings/%d/cancel" % booking_id).status_code == 200
    r = client.post("/mentors/api/bookings/%d/cancel" % booking_id)
    assert r.status_code == 400


def test_cancel_other_users_booking_404(app, client):
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid)
    booking_id = _book(client, oh_id).get_json()["booking_id"]
    _register_extra(client, "Other User", "other@example.com")
    _login_as(client, "other@example.com")
    r = client.post("/mentors/api/bookings/%d/cancel" % booking_id)
    assert r.status_code == 404


def test_cancel_missing_booking_404(client):
    _authed(client)
    r = client.post("/mentors/api/bookings/999999/cancel")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Attendance + XP
# ---------------------------------------------------------------------------

def test_attend_records_xp(app, client):
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid)
    booking_id = _book(client, oh_id).get_json()["booking_id"]
    uid = _uid(app, "aarav@example.com")
    r = client.post("/mentors/api/bookings/%d/attend" % booking_id)
    assert r.status_code == 200
    with app.app_context():
        assert db.session.get(OHBooking, booking_id).status == "attended"
        row = XPLedger.query.filter_by(
            user_id=uid, event_type="mentor_session_attended").first()
        assert row is not None
        assert row.xp == 30


def test_attend_twice_rejected(app, client):
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid)
    booking_id = _book(client, oh_id).get_json()["booking_id"]
    client.post("/mentors/api/bookings/%d/attend" % booking_id)
    r = client.post("/mentors/api/bookings/%d/attend" % booking_id)
    assert r.status_code == 400


def test_attend_cancelled_rejected(app, client):
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid)
    booking_id = _book(client, oh_id).get_json()["booking_id"]
    client.post("/mentors/api/bookings/%d/cancel" % booking_id)
    r = client.post("/mentors/api/bookings/%d/attend" % booking_id)
    assert r.status_code == 400


def test_attend_missing_booking_404(client):
    _authed(client)
    assert client.post("/mentors/api/bookings/999999/attend").status_code == 404


# ---------------------------------------------------------------------------
# Action notes
# ---------------------------------------------------------------------------

def test_note_saved(app, client):
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid)
    booking_id = _book(client, oh_id).get_json()["booking_id"]
    r = client.post("/mentors/api/bookings/%d/note" % booking_id,
                    json={"note_text": "Apply to 3 internships this week."})
    assert r.status_code == 201
    with app.app_context():
        note = db.session.get(OHNote, r.get_json()["note_id"])
        assert note.note_text == "Apply to 3 internships this week."


def test_note_empty_rejected(app, client):
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid)
    booking_id = _book(client, oh_id).get_json()["booking_id"]
    r = client.post("/mentors/api/bookings/%d/note" % booking_id,
                    json={"note_text": "   "})
    assert r.status_code == 400


def test_note_too_long_rejected(app, client):
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid)
    booking_id = _book(client, oh_id).get_json()["booking_id"]
    r = client.post("/mentors/api/bookings/%d/note" % booking_id,
                    json={"note_text": "x" * 3001})
    assert r.status_code == 400


def test_note_on_cancelled_rejected(app, client):
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid)
    booking_id = _book(client, oh_id).get_json()["booking_id"]
    client.post("/mentors/api/bookings/%d/cancel" % booking_id)
    r = client.post("/mentors/api/bookings/%d/note" % booking_id,
                    json={"note_text": "too late"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

def _attended_booking(client, app):
    mid = _mentor(app)
    oh_id = _office_hour(app, mid)
    booking_id = _book(client, oh_id).get_json()["booking_id"]
    client.post("/mentors/api/bookings/%d/attend" % booking_id)
    return booking_id


def test_feedback_success_records_xp(app, client):
    _authed(client)
    booking_id = _attended_booking(client, app)
    uid = _uid(app, "aarav@example.com")
    r = client.post("/mentors/api/bookings/%d/feedback" % booking_id,
                    json={"rating": 5, "what_helped": "The 90-day breakdown."})
    assert r.status_code == 201
    with app.app_context():
        fb = db.session.get(OHFeedback, r.get_json()["feedback_id"])
        assert fb.rating == 5
        row = XPLedger.query.filter_by(
            user_id=uid, event_type="mentor_feedback_given").first()
        assert row is not None
        assert row.xp == 10


def test_feedback_before_attended_rejected(app, client):
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid)
    booking_id = _book(client, oh_id).get_json()["booking_id"]
    r = client.post("/mentors/api/bookings/%d/feedback" % booking_id,
                    json={"rating": 4})
    assert r.status_code == 400


@pytest.mark.parametrize("rating", [0, 6, -1, 99])
def test_feedback_rating_out_of_range(app, client, rating):
    _authed(client)
    booking_id = _attended_booking(client, app)
    r = client.post("/mentors/api/bookings/%d/feedback" % booking_id,
                    json={"rating": rating})
    assert r.status_code == 400


def test_feedback_rating_non_integer(app, client):
    _authed(client)
    booking_id = _attended_booking(client, app)
    r = client.post("/mentors/api/bookings/%d/feedback" % booking_id,
                    json={"rating": "great"})
    assert r.status_code == 400


def test_feedback_duplicate_rejected(app, client):
    _authed(client)
    booking_id = _attended_booking(client, app)
    url = "/mentors/api/bookings/%d/feedback" % booking_id
    assert client.post(url, json={"rating": 4}).status_code == 201
    assert client.post(url, json={"rating": 5}).status_code == 400


def test_feedback_what_helped_too_long(app, client):
    _authed(client)
    booking_id = _attended_booking(client, app)
    r = client.post("/mentors/api/bookings/%d/feedback" % booking_id,
                    json={"rating": 3, "what_helped": "x" * 2001})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# My bookings endpoint
# ---------------------------------------------------------------------------

def test_my_bookings_lists_and_filters(app, client):
    _authed(client)
    mid1 = _mentor(app, name="Mentor One")
    mid2 = _mentor(app, name="Mentor Two")
    oh1 = _office_hour(app, mid1, topic="Topic one")
    oh2 = _office_hour(app, mid2, topic="Topic two")
    _book(client, oh1)
    _book(client, oh2)
    r = client.get("/mentors/api/my-bookings")
    assert r.status_code == 200
    assert len(r.get_json()["bookings"]) == 2
    r2 = client.get("/mentors/api/my-bookings?mentor_id=%d" % mid1)
    items = r2.get_json()["bookings"]
    assert len(items) == 1
    assert items[0]["topic"] == "Topic one"


def test_my_bookings_requires_login(client):
    r = client.get("/mentors/api/my-bookings")
    assert r.status_code == 302


# ---------------------------------------------------------------------------
# Mentor application
# ---------------------------------------------------------------------------

def test_apply_form_renders(client):
    _authed(client)
    r = client.get("/mentors/apply")
    assert r.status_code == 200
    assert "mentor" in r.get_data(as_text=True).lower()


def test_apply_success(app, client):
    _authed(client)
    r = client.post("/mentors/api/apply", json={
        "name": "Aarav Sharma",
        "headline": "SDE at Startup",
        "path": "engineering",
        "company": "Startup",
        "college": "GEHU",
        "bio": "I have mentored juniors for two years and love answering "
               "career questions about backend engineering.",
        "linkedin_url": "https://www.linkedin.com/in/aarav",
    })
    assert r.status_code == 201
    with app.app_context():
        row = db.session.get(MentorApplication, r.get_json()["application_id"])
        assert row.status == "pending"
        assert row.path == "engineering"


def test_apply_validation(app, client):
    _authed(client)
    base = {"name": "Aarav Sharma", "headline": "SDE",
            "path": "engineering", "bio": "x" * 60}
    bad_bios = dict(base, bio="too short")
    assert client.post("/mentors/api/apply", json=bad_bios).status_code == 400
    bad_path = dict(base, path="astronaut")
    assert client.post("/mentors/api/apply", json=bad_path).status_code == 400
    bad_url = dict(base, linkedin_url="not-a-url")
    assert client.post("/mentors/api/apply", json=bad_url).status_code == 400
    no_headline = dict(base, headline="")
    assert client.post("/mentors/api/apply", json=no_headline).status_code == 400


def test_apply_duplicate_pending_rejected(app, client):
    _authed(client)
    payload = {"name": "Aarav Sharma", "headline": "SDE",
               "path": "engineering",
               "bio": "I have mentored juniors for two years and love this."}
    assert client.post("/mentors/api/apply", json=payload).status_code == 201
    r = client.post("/mentors/api/apply", json=payload)
    assert r.status_code == 400
    assert "pending" in r.get_json()["error"].lower()


def test_apply_form_shows_pending_status(app, client):
    _authed(client)
    payload = {"name": "Aarav Sharma", "headline": "SDE",
               "path": "engineering",
               "bio": "I have mentored juniors for two years and love this."}
    client.post("/mentors/api/apply", json=payload)
    r = client.get("/mentors/apply")
    assert "Under review" in r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def test_report_mentor(app, client):
    _authed(client)
    mid = _mentor(app)
    r = client.post("/mentors/api/report", json={
        "mentor_id": mid, "reason": "spam", "details": "Sent promo links."})
    assert r.status_code == 201
    with app.app_context():
        rep = db.session.get(OHReport, r.get_json()["report_id"])
        assert rep.reason == "spam"
        assert rep.status == "open"
        assert rep.mentor_id == mid


def test_report_session_only(app, client):
    _authed(client)
    mid = _mentor(app)
    oh_id = _office_hour(app, mid)
    r = client.post("/mentors/api/report", json={
        "office_hour_id": oh_id, "reason": "no_show"})
    assert r.status_code == 201


def test_report_unknown_reason_rejected(client):
    _authed(client)
    r = client.post("/mentors/api/report", json={
        "mentor_id": 1, "reason": "rude"})
    assert r.status_code == 400


def test_report_needs_target(client):
    _authed(client)
    r = client.post("/mentors/api/report", json={"reason": "spam"})
    assert r.status_code == 400


def test_report_bad_mentor_404(client):
    _authed(client)
    r = client.post("/mentors/api/report", json={
        "mentor_id": 999999, "reason": "spam"})
    assert r.status_code == 404


def test_report_details_too_long(app, client):
    _authed(client)
    mid = _mentor(app)
    r = client.post("/mentors/api/report", json={
        "mentor_id": mid, "reason": "other", "details": "x" * 2001})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Admin verify (admin-lite via STUDYBUDDY_ADMINS)
# ---------------------------------------------------------------------------

def test_admin_verify_forbidden_for_non_admin(app, client, monkeypatch):
    monkeypatch.setenv("STUDYBUDDY_ADMINS", "boss@example.com")
    _authed(client)
    mid = _mentor(app, verified=False)
    r = client.post("/mentors/api/admin/verify/%d" % mid)
    assert r.status_code == 403
    with app.app_context():
        assert db.session.get(Mentor, mid).verified is False


def test_admin_verify_forbidden_when_env_empty(app, client, monkeypatch):
    monkeypatch.delenv("STUDYBUDDY_ADMINS", raising=False)
    _authed(client)
    mid = _mentor(app, verified=False)
    assert client.post("/mentors/api/admin/verify/%d" % mid).status_code == 403


def test_admin_verify_success(app, client, monkeypatch):
    monkeypatch.setenv("STUDYBUDDY_ADMINS", "aarav@example.com")
    _authed(client)
    mid = _mentor(app, verified=False)
    r = client.post("/mentors/api/admin/verify/%d" % mid)
    assert r.status_code == 200
    with app.app_context():
        assert db.session.get(Mentor, mid).verified is True


def test_admin_verify_404(app, client, monkeypatch):
    monkeypatch.setenv("STUDYBUDDY_ADMINS", "aarav@example.com")
    _authed(client)
    assert client.post("/mentors/api/admin/verify/999999").status_code == 404


def test_admin_verify_requires_login(client):
    assert client.post("/mentors/api/admin/verify/1").status_code == 302


# ---------------------------------------------------------------------------
# Unauthenticated access -> 302 on every protected route
# ---------------------------------------------------------------------------

def test_unauth_redirects(app, client):
    mid = _mentor(app)
    oh_id = _office_hour(app, mid)
    urls = [
        ("GET", "/mentors"),
        ("GET", "/mentors/%d" % mid),
        ("GET", "/mentors/apply"),
        ("GET", "/mentors/api/my-bookings"),
        ("POST", "/mentors/api/office-hours/%d/book" % oh_id),
        ("POST", "/mentors/api/bookings/1/cancel"),
        ("POST", "/mentors/api/bookings/1/attend"),
        ("POST", "/mentors/api/bookings/1/note"),
        ("POST", "/mentors/api/bookings/1/feedback"),
        ("POST", "/mentors/api/apply"),
        ("POST", "/mentors/api/report"),
        ("POST", "/mentors/api/admin/verify/%d" % mid),
        ("POST", "/mentors/api/admin/office-hours"),
    ]
    for method, url in urls:
        r = client.open(url, method=method)
        assert r.status_code == 302, url
        assert "/login" in r.headers["Location"], url


# ---------------------------------------------------------------------------
# CSRF: JSON POSTs without the X-CSRFToken header must be rejected (400)
# ---------------------------------------------------------------------------

@pytest.fixture()
def csrf_app():
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SECRET_KEY": "mentor-csrf-test",
        "WTF_CSRF_ENABLED": True,
    })
    with app.app_context():
        db.create_all()
    yield app


def test_csrf_rejected_on_mentor_posts(csrf_app):
    # CSRFProtect runs before auth: every POST without the X-CSRFToken
    # header must 400, no login needed. Seed via the DB directly.
    client = csrf_app.test_client()
    with csrf_app.app_context():
        m = Mentor(name="CSRF Mentor", headline="Eng", path="engineering",
                   verified=True)
        db.session.add(m)
        db.session.flush()
        oh = OfficeHour(mentor_id=m.id, topic="CSRF session",
                        path="engineering",
                        scheduled_at=datetime.now(UTC) + timedelta(hours=5),
                        capacity=12, status="open")
        db.session.add(oh)
        db.session.flush()
        u = User(name="Csrf User", email="csrf@example.com")
        u.set_password("secret")
        db.session.add(u)
        db.session.commit()
        oh_id, mid = oh.id, m.id
    for url, body in (
            ("/mentors/api/office-hours/%d/book" % oh_id, {"question_text": "q"}),
            ("/mentors/api/bookings/1/cancel", {}),
            ("/mentors/api/bookings/1/attend", {}),
            ("/mentors/api/bookings/1/note", {"note_text": "n"}),
            ("/mentors/api/bookings/1/feedback", {"rating": 5}),
            ("/mentors/api/apply", {"name": "x"}),
            ("/mentors/api/report", {"reason": "spam"}),
            ("/mentors/api/admin/verify/%d" % mid, {}),
            ("/mentors/api/admin/office-hours", {"mentor_id": mid})):
        rr = client.post(url, json=body)
        assert rr.status_code == 400, url


def test_csrf_header_path_wired(csrf_app):
    """With a valid X-CSRFToken header, a JSON POST passes the CSRF layer
    and reaches auth (302 to /login for an anonymous user) instead of the
    400 CSRF rejection — proving the header path is wired for the mentor
    APIs."""
    import re
    client = csrf_app.test_client()
    page = client.get("/login")
    token_m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"',
                        page.get_data(as_text=True))
    assert token_m, "login page must carry a CSRF token"
    rr = client.post("/mentors/api/apply",
                     json={"name": "x"},
                     headers={"X-CSRFToken": token_m.group(1)})
    assert rr.status_code == 302
    assert "/login" in rr.headers["Location"]


# ---------------------------------------------------------------------------
# Pure-logic helpers
# ---------------------------------------------------------------------------

def test_paths_and_labels():
    assert community_mod.PATH_KEYS == ["engineering", "medicine", "law",
                                       "commerce", "humanities", "design",
                                       "other"]
    assert community_mod.path_label("engineering") == "Engineering & Tech"
    assert community_mod.path_label("bogus") == "bogus"


def test_question_templates_shape():
    templates = community_mod.QUESTION_TEMPLATES
    assert len(templates) == 12
    for t in templates:
        assert t["id"] and t["category"] and t["text"]
    assert len(community_mod.QUESTION_TEMPLATE_IDS) == 12
    assert "first_90_days" in community_mod.QUESTION_TEMPLATE_IDS


def _fake_session(**kwargs):
    class S:
        pass
    s = S()
    s.status = kwargs.get("status", "open")
    s.capacity = kwargs.get("capacity", 12)
    s.scheduled_at = kwargs.get("scheduled_at",
                                datetime.now(UTC) + timedelta(hours=5))
    return s


def test_booking_eligibility_rules():
    ok, _ = community_mod.booking_eligibility(_fake_session(), 0, None)
    assert ok
    ok, err = community_mod.booking_eligibility(_fake_session(), 5, object())
    assert not ok and "already" in err.lower()
    ok, err = community_mod.booking_eligibility(_fake_session(capacity=2), 2, None)
    assert not ok and "full" in err.lower()
    past = _fake_session(scheduled_at=datetime.now(UTC) - timedelta(hours=1))
    ok, err = community_mod.booking_eligibility(past, 0, None)
    assert not ok and "started" in err.lower()
    ok, err = community_mod.booking_eligibility(_fake_session(status="cancelled"), 0, None)
    assert not ok
    ok, _ = community_mod.booking_eligibility(None, 0, None)
    assert not ok


def test_can_cancel_rules():
    ok, _ = community_mod.can_cancel(_fake_session())
    assert ok
    soon = _fake_session(scheduled_at=datetime.now(UTC) + timedelta(minutes=30))
    ok, err = community_mod.can_cancel(soon)
    assert not ok and "hour" in err.lower()
    ok, _ = community_mod.can_cancel(_fake_session(status="completed"))
    assert not ok


def test_refresh_session_status():
    s = _fake_session(capacity=2)
    assert community_mod.refresh_session_status(s, 2) == "full"
    assert s.status == "full"
    assert community_mod.refresh_session_status(s, 1) == "open"
    s.status = "completed"
    assert community_mod.refresh_session_status(s, 0) == "completed"


def test_mentor_is_admin(monkeypatch):
    monkeypatch.setenv("STUDYBUDDY_ADMINS", "boss@example.com, Admin@Example.com ")
    assert community_mod.mentor_is_admin("boss@example.com")
    assert community_mod.mentor_is_admin("ADMIN@example.com")
    assert not community_mod.mentor_is_admin("stranger@example.com")
    assert not community_mod.mentor_is_admin("")
    monkeypatch.delenv("STUDYBUDDY_ADMINS", raising=False)
    assert not community_mod.mentor_is_admin("boss@example.com")


def test_xp_rules_registered():
    from studybuddy.momentum import XP_RULES
    assert XP_RULES["mentor_session_attended"] == 30
    assert XP_RULES["mentor_feedback_given"] == 10
    assert XP_RULES["health_review_completed"] == 20


# ---------------------------------------------------------------------------
# Session seeding + admin session creation
# ---------------------------------------------------------------------------

def test_seed_office_hours(app):
    with app.app_context():
        assert community_mod.seed_mentors() == 14
        n = community_mod.seed_office_hours()
        assert n == 4
        assert OfficeHour.query.count() == 4
        for oh in OfficeHour.query.all():
            assert oh.status == "open"
            # SQLite returns naive datetimes; the codebase treats those as UTC.
            sched = oh.scheduled_at
            if sched.tzinfo is None:
                sched = sched.replace(tzinfo=UTC)
            assert sched > datetime.now(UTC)
            assert oh.capacity == 12
        # idempotent
        assert community_mod.seed_office_hours() == 0
        assert OfficeHour.query.count() == 4


def test_seed_office_hours_skipped_when_sessions_exist(app, client):
    _authed(client)
    mid = _mentor(app)
    _office_hour(app, mid)
    with app.app_context():
        assert community_mod.seed_office_hours() == 0
        assert OfficeHour.query.count() == 1


def test_hub_seeds_sessions_on_first_load(app, client):
    _authed(client)
    client.get("/mentors")
    with app.app_context():
        assert OfficeHour.query.count() == 4
    html = client.get("/mentors").get_data(as_text=True)
    assert "Ask me anything" in html


def test_admin_create_session_success(app, client, monkeypatch):
    monkeypatch.setenv("STUDYBUDDY_ADMINS", "aarav@example.com")
    _authed(client)
    mid = _mentor(app, path="law")
    future = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
    r = client.post("/mentors/api/admin/office-hours", json={
        "mentor_id": mid, "topic": "Judiciary prep AMA",
        "scheduled_at": future, "duration_min": 60, "capacity": 20})
    assert r.status_code == 201
    data = r.get_json()
    assert data["success"] is True
    assert data["office_hour"]["topic"] == "Judiciary prep AMA"
    assert data["office_hour"]["path"] == "law"
    with app.app_context():
        oh = db.session.get(OfficeHour, data["office_hour"]["id"])
        assert oh.capacity == 20
        assert oh.duration_min == 60


def test_admin_create_session_forbidden(app, client, monkeypatch):
    monkeypatch.setenv("STUDYBUDDY_ADMINS", "boss@example.com")
    _authed(client)
    mid = _mentor(app)
    future = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
    r = client.post("/mentors/api/admin/office-hours", json={
        "mentor_id": mid, "topic": "Nope", "scheduled_at": future})
    assert r.status_code == 403


def test_admin_create_session_validation(app, client, monkeypatch):
    monkeypatch.setenv("STUDYBUDDY_ADMINS", "aarav@example.com")
    _authed(client)
    mid = _mentor(app)
    future = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
    past = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    base = {"mentor_id": mid, "topic": "T", "scheduled_at": future}
    assert client.post("/mentors/api/admin/office-hours",
                       json=dict(base, mentor_id=999999)).status_code == 404
    assert client.post("/mentors/api/admin/office-hours",
                       json=dict(base, scheduled_at=past)).status_code == 400
    assert client.post("/mentors/api/admin/office-hours",
                       json=dict(base, scheduled_at="soon")).status_code == 400
    assert client.post("/mentors/api/admin/office-hours",
                       json=dict(base, topic="")).status_code == 400
    assert client.post("/mentors/api/admin/office-hours",
                       json=dict(base, capacity=0)).status_code == 400
    assert client.post("/mentors/api/admin/office-hours",
                       json=dict(base, duration_min=5)).status_code == 400


# ---------------------------------------------------------------------------
# Question templates present on profile booking forms
# ---------------------------------------------------------------------------

def test_all_template_categories_reachable(app, client):
    _authed(client)
    mid = _mentor(app)
    _office_hour(app, mid)
    html = client.get("/mentors/%d" % mid).get_data(as_text=True)
    for t in community_mod.QUESTION_TEMPLATES:
        assert t["id"] in html
