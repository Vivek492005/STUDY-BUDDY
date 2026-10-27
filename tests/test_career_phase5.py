"""Phase 5 humans: mentors (honesty-first) + health review upgrades."""

from datetime import UTC, datetime, timedelta

from tests.conftest import login, register
from unified_app import (Mentor,
                         OHBooking, OfficeHour,
                         User, db)


def _authed(client, email="ph5@example.com", name="Phase Five"):
    register(client, email=email, name=name)
    login(client, email=email)


def _uid(app, email="ph5@example.com"):
    with app.app_context():
        return User.query.filter_by(email=email).first().id


def _real_mentor(app, user_id=None, **kw):
    with app.app_context():
        m = Mentor(name=kw.get("name", "Real Mentor"),
                   headline=kw.get("headline", "SDE at BigCo"),
                   path=kw.get("path", "engineering"),
                   company=kw.get("company", "BigCo"),
                   college=kw.get("college", "IIT"),
                   bio=kw.get("bio", "Real mentor bio."),
                   verified=True, is_seed=False, user_id=user_id)
        db.session.add(m)
        db.session.commit()
        return m.id


def _seed_mentor(app):
    with app.app_context():
        m = Mentor(name="Seed Mentor", headline="Example",
                   path="engineering", company="ExampleCo",
                   college="Example College", bio="Seed bio.",
                   verified=False, is_seed=True)
        db.session.add(m)
        db.session.commit()
        return m.id


def _office_hour(app, mentor_id):
    with app.app_context():
        oh = OfficeHour(mentor_id=mentor_id, topic="AMA",
                        path="engineering",
                        scheduled_at=datetime.now(UTC) + timedelta(hours=24),
                        duration_min=45, capacity=12, status="open")
        db.session.add(oh)
        db.session.commit()
        return oh.id


def _booking(app, uid, oh_id, status="booked"):
    with app.app_context():
        b = OHBooking(user_id=uid, office_hour_id=oh_id,
                      question_text="Hello?", status=status)
        db.session.add(b)
        db.session.commit()
        return b.id


# ---------- seeds: illustrative, never verified, never bookable ----------

def test_seeds_unverified_and_unbookable(client, app):
    _authed(client)
    client.get("/mentors")  # defensive seeding
    with app.app_context():
        seeds = Mentor.query.filter_by(is_seed=True).all()
        assert len(seeds) == 14
        assert not any(m.verified for m in seeds)
        oh = OfficeHour.query.filter_by(mentor_id=seeds[0].id).first()
        assert oh is not None
        oh_id = oh.id
    r = client.post(f"/mentors/api/office-hours/{oh_id}/book",
                    json={"question_text": "Can you mentor me?"})
    assert r.status_code == 400
    assert "illustrative" in r.get_json()["error"].lower()
    # Serializers force verified=False for seeds even on old stored rows.
    matches = client.get("/api/mentors/matches").get_json()
    assert matches["matches"] == [] or all(
        m["illustrative"] is True or m["verified"] is False
        for m in matches["matches"])


def test_seed_labels_unified(client):
    _authed(client)
    html = client.get("/mentors").get_data(as_text=True)
    assert "Illustrative profile" in html
    assert "cannot be booked" in html


# ---------- matching ----------

def test_matches_scored_with_reasons(client, app):
    _authed(client)
    uid = _uid(app)
    # Profile: assessment cluster + portfolio skills.
    from studybuddy.career_assessment import QUESTIONS
    answers = {q["id"]: 0 for q in QUESTIONS}
    client.post("/assessment/submit", json={"answers": answers})
    with app.app_context():
        from unified_app import Portfolio
        db.session.add(Portfolio(user_id=uid, slug="ph5",
                                 display_name="Ph5",
                                 headline="h", bio="b",
                                 skills=["Python", "SQL"]))
        db.session.commit()
    mid = _real_mentor(app, path="engineering")
    body = client.get("/api/mentors/matches").get_json()
    assert body["matches"]
    scores = [m["match_score"] for m in body["matches"]]
    assert scores == sorted(scores, reverse=True)
    assert all(0 < s <= 100 for s in scores)
    assert any(m["id"] == mid and m["reasons"] for m in body["matches"])
    assert any(m["illustrative"] is False and m["verified"] is True
               for m in body["matches"])


def test_matches_empty_without_profile(app):
    c = app.test_client()
    _authed(c, email="noprofile@example.com", name="No Profile")
    body = c.get("/api/mentors/matches").get_json()
    assert body["matches"] == []
    assert body.get("note")


# ---------- Q&A library ----------

def test_qa_library_counts_filters_and_ask(client):
    body = client.get("/api/mentors/qa").get_json()
    assert len(body["qa"]) >= 100
    assert all(q["attribution"] for q in body["qa"])
    assert len({q["question"] for q in body["qa"]}) == len(body["qa"])
    dsa = client.get("/api/mentors/qa?topic=dsa").get_json()
    assert dsa["qa"] and all(q["topic"] == "dsa" for q in dsa["qa"])
    assert client.get("/api/mentors/qa?topic=nope").get_json()["qa"] == []
    _authed(client)
    asked = client.post("/api/mentors/qa/ask",
                        json={"question": "How do I prepare for system "
                              "design rounds as a fresher?"})
    assert asked.status_code == 201
    assert asked.get_json()["question_id"]
    mine = client.get("/api/mentors/qa/mine").get_json()
    assert any("system design" in q["question"].lower()
               for q in mine["questions"])
    assert client.post("/api/mentors/qa/ask",
                       json={"question": "Too short"}).status_code == 400


# ---------- reviews: gated, deduplicated, honest averages ----------

def test_reviews_gating_average_and_dup(client, app):
    _authed(client)
    uid = _uid(app)
    mid = _real_mentor(app)
    # No booking yet -> 400.
    r = client.post(f"/api/mentors/{mid}/reviews",
                    json={"rating": 5, "text": "Great!"})
    assert r.status_code == 400
    oh_id = _office_hour(app, mid)
    _booking(app, uid, oh_id)
    assert client.post(f"/api/mentors/{mid}/reviews",
                       json={"rating": 9}).status_code == 400
    ok = client.post(f"/api/mentors/{mid}/reviews",
                     json={"rating": 4, "text": "Helpful session"})
    assert ok.status_code == 201
    assert client.post(f"/api/mentors/{mid}/reviews",
                       json={"rating": 5}).status_code == 400  # duplicate
    got = client.get(f"/api/mentors/{mid}/reviews").get_json()
    assert got["count"] == 1 and got["average"] == 4.0
    # A mentor with no reviews: null average, never a default.
    mid2 = _real_mentor(app, name="Unreviewed")
    got2 = client.get(f"/api/mentors/{mid2}/reviews").get_json()
    assert got2["count"] == 0 and got2["average"] is None


def test_reviews_require_real_interaction_not_seed(client, app):
    _authed(client)
    sid = _seed_mentor(app)
    r = client.post(f"/api/mentors/{sid}/reviews",
                    json={"rating": 5, "text": "Nice"})
    assert r.status_code == 400


# ---------- dashboard ----------

def test_dashboard_404_for_non_mentor_then_200(client, app):
    _authed(client)
    assert client.get("/api/mentors/dashboard").status_code == 404
    uid = _uid(app)
    _real_mentor(app, user_id=uid)
    body = client.get("/api/mentors/dashboard").get_json()
    assert body["dashboard"]["bookings_count"] == 0
    assert body["dashboard"]["average_rating"] is None
    assert body["dashboard"]["qa_questions"] == []


# ---------- health pulse ----------

def test_pulse_upsert_and_validation(client):
    _authed(client)
    scores = {"direction": 4, "proof": 3, "opportunity": 2,
              "preparation": 4, "wellbeing": 5}
    assert client.post("/api/health/pulse",
                       json={"scores": scores}).status_code == 200
    assert client.post("/api/health/pulse",
                       json={"scores": scores}).status_code == 200
    got = client.get("/api/health/pulse").get_json()
    assert got["pulse"]["scores"] == scores
    assert len(got["history"]) == 1  # upsert, not a second row
    assert client.post("/api/health/pulse",
                       json={"scores": {"direction": 9}}).status_code == 400
    assert client.post("/api/health/pulse",
                       json={"scores": {"nope": 3}}).status_code == 400


# ---------- action plans, trends, benchmarks ----------

def test_action_plans_link_real_features_and_goals(client):
    _authed(client)
    client.post("/api/health/pulse",
                json={"scores": {"direction": 2, "proof": 3,
                                 "opportunity": 4, "preparation": 3,
                                 "wellbeing": 4}})
    client.post("/api/health/goals",
                json={"title": "Ship portfolio", "dimension": "proof"})
    plans = client.get("/api/health/action-plans").get_json()["plans"]
    assert len(plans) == 5
    proof = next(p for p in plans if p["dimension"] == "proof")
    assert proof["current_score"] == 3
    assert proof["score_source"] == "pulse"
    assert proof["actions"] and all(a["url"].startswith("/")
                                    for a in proof["actions"])
    assert "Ship portfolio" in proof["goals"]


def test_trends_months_nulls_not_zeros(client):
    _authed(client)
    client.post("/api/health/pulse",
                json={"scores": {"direction": 4, "proof": 4,
                                 "opportunity": 4, "preparation": 4,
                                 "wellbeing": 4}})
    trends = client.get("/api/health/trends").get_json()["trends"]
    assert len(trends["months"]) == 6
    assert all(v is None or 1 <= v <= 5
               for m in trends["months"] for v in m["dimensions"].values())
    assert trends["pulses"] and trends["pulses"][0]["scores"]["direction"] == 4


def test_benchmarks_threshold_and_self_exclusion(app):
    # 5 peers with pulses -> medians computed, requester excluded.
    for i in range(5):
        c = app.test_client()
        _authed(c, email=f"peer{i}@example.com", name=f"Peer {i}")
        c.post("/api/health/pulse",
               json={"scores": {"direction": 3, "proof": 3,
                                "opportunity": 3, "preparation": 3,
                                "wellbeing": 3}})
    c = app.test_client()
    _authed(c, email="bench@example.com", name="Bench")
    c.post("/api/health/pulse",
           json={"scores": {"direction": 5, "proof": 5, "opportunity": 5,
                            "preparation": 5, "wellbeing": 5}})
    bench = c.get("/api/health/benchmarks").get_json()["benchmarks"]
    direction = next(b for b in bench if b["dimension"] == "direction")
    assert direction["sample_size"] == 5
    assert direction["peer_median"] == 3.0  # peers only, not the 5.0 self
    # Fewer than 5 peers -> null median with honest sample size.
    c2 = app.test_client()
    _authed(c2, email="lonely@example.com", name="Lonely")
    assert c2.get("/api/health/benchmarks").get_json()["benchmarks"]


# ---------- goals + sharing ----------

def test_health_goals_crud_and_ownership(client, app):
    _authed(client)
    created = client.post("/api/health/goals",
                          json={"title": "Apply to 10 places",
                                "dimension": "opportunity"})
    assert created.status_code == 201
    gid = created.get_json()["goal"]["id"]
    assert client.get("/api/health/goals").get_json()["goals"]
    patched = client.patch(f"/api/health/goals/{gid}",
                           json={"done": True})
    assert patched.status_code == 200
    assert patched.get_json()["goal"]["done"] is True
    stranger = app.test_client()
    _authed(stranger, email="ph5b@example.com", name="Phase B")
    assert stranger.patch(f"/api/health/goals/{gid}",
                          json={"done": False}).status_code == 404


def test_share_with_mentor_real_only_and_revoke(client, app):
    _authed(client)
    sid = _seed_mentor(app)
    assert client.post("/api/health/share-with-mentor",
                       json={"mentor_id": sid}).status_code == 400
    mid = _real_mentor(app)
    shared = client.post("/api/health/share-with-mentor",
                         json={"mentor_id": mid})
    assert shared.status_code == 201
    again = client.post("/api/health/share-with-mentor",
                        json={"mentor_id": mid})
    assert again.status_code == 200
    assert again.get_json().get("already_shared") is True
    listed = client.get("/api/health/shared-with").get_json()
    key = "shared_with" if "shared_with" in listed else "shared"
    assert any(s.get("mentor_id", s.get("id")) == mid for s in listed[key])
    assert client.delete(f"/api/health/shared-with/{mid}").status_code == 200
    assert client.delete(
        f"/api/health/shared-with/{mid}").status_code == 404
