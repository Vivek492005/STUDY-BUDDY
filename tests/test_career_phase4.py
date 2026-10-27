"""Phase 4 engine: momentum upgrades + retrospective upgrades."""

import json
from datetime import UTC, date, datetime, timedelta

from studybuddy.momentum import QUEST_TEMPLATES
from studybuddy.retrospective_data import RETRO_REASONS
from tests.conftest import login, register
from unified_app import (Experiment, Retrospective,
                         StreakState, User, XPLedger, db)


def _authed(client, email="ph4@example.com", name="Phase Four"):
    register(client, email=email, name=name)
    login(client, email=email)


def _user_id(app, email="ph4@example.com"):
    with app.app_context():
        return User.query.filter_by(email=email).first().id


def _add_app(client, company="Acme", role="SDE", stage="wishlist"):
    resp = client.post("/tracker/add",
                       data=json.dumps({"company": company, "role": role,
                                        "stage": stage}),
                       content_type="application/json",
                       headers={"X-CSRFToken": "test"})
    assert resp.status_code in (200, 201), resp.get_data(as_text=True)[:300]
    return resp.get_json()["application"]["id"]


def _mk_retro(app, uid, app_id, reason="skill-gap-dsa", stage="interview",
              completed=True, shared=False):
    with app.app_context():
        retro = Retrospective(user_id=uid, application_id=app_id,
                              reason=reason, stage_reached=stage,
                              completed=completed,
                              completed_at=datetime.now(UTC) if completed else None,
                              anonymous_share=shared)
        db.session.add(retro)
        db.session.commit()
        return retro.id


# ---------- quests + career XP ----------

def test_quest_templates_24_originals_first():
    assert len(QUEST_TEMPLATES) == 24
    ids = [q["id"] for q in QUEST_TEMPLATES]
    assert ids[:6] == ["quiz-3", "dsa-5", "mock-1", "flash-4", "apt-2",
                       "reflect-1"]
    assert len(ids) == len(set(ids))


def test_career_xp_hook_idempotent(app):
    from studybuddy.momentum import career_xp_hook
    with app.app_context():
        user = User(name="Hook", email="hook4@example.com",
                    password_hash="x")
        db.session.add(user)
        db.session.commit()
        uid = user.id
    with app.app_context():
        career_xp_hook(uid, "tracker_add", ref_id=123)
        career_xp_hook(uid, "tracker_add", ref_id=123)
        career_xp_hook(uid, "tracker_add", ref_id=124)
        rows = XPLedger.query.filter_by(
            user_id=uid, event_type="tracker_added").all()
        assert len(rows) == 2  # ref 123 paid once, ref 124 paid once


def test_tracker_add_pays_career_xp(client, app):
    _authed(client)
    uid = _user_id(app)
    _add_app(client)
    with app.app_context():
        rows = XPLedger.query.filter_by(
            user_id=uid, event_type="tracker_added").all()
        assert len(rows) == 1
    recap = client.get("/api/momentum/recap").get_json()["recap"]
    assert recap["applications_added"] >= 1
    assert recap["xp_earned"] >= 10


# ---------- badges ----------

def test_badges_count_and_first_tracker_earned(client, app):
    _authed(client)
    _add_app(client)
    body = client.get("/api/momentum/badges").get_json()
    assert len(body["badges"]) == 20
    by_id = {b["id"]: b for b in body["badges"]}
    assert by_id["first-tracker-add"]["earned"] is True
    assert by_id["first-tracker-add"]["earned_at"]
    assert by_id["ten-applications"]["earned"] is False
    # Login-gated route: anon gets a login redirect, not a JSON 401.
    assert app.test_client().get("/api/momentum/badges").status_code == 302


# ---------- team leagues ----------

def test_team_league_create_join_board(client, app):
    _authed(client)
    created = client.post("/api/momentum/leagues", json={"name": "Sprint"})
    assert created.status_code == 201
    league = created.get_json()["league"]
    assert league["invite_code"]
    stranger = app.test_client()
    _authed(stranger, email="ph4b@example.com", name="Phase B")
    joined = stranger.post("/api/momentum/leagues/join",
                           json={"invite_code": league["invite_code"]})
    assert joined.status_code == 200
    assert joined.get_json()["already_member"] is False
    again = stranger.post("/api/momentum/leagues/join",
                          json={"invite_code": league["invite_code"]})
    assert again.get_json()["already_member"] is True
    assert stranger.post("/api/momentum/leagues/join",
                         json={"invite_code": "deadbeef"}).status_code == 404
    board = stranger.get("/api/momentum/leagues").get_json()["leagues"]
    assert len(board) == 1
    assert board[0]["members_count"] == 2
    assert board[0]["my_rank"] and board[0]["my_xp"] is not None
    assert "members" not in board[0]  # never other members' identities


# ---------- streak repair ----------

def test_streak_repair_success_then_cooldown(client, app):
    _authed(client)
    uid = _user_id(app)
    with app.app_context():
        db.session.add(StreakState(user_id=uid, current=5, longest=5,
                                   last_active_date=date.today() - timedelta(days=2)))
        db.session.commit()
    ok = client.post("/api/momentum/streak/repair")
    assert ok.status_code == 200, ok.get_data(as_text=True)[:300]
    assert ok.get_json()["success"] is True
    with app.app_context():
        st = StreakState.query.filter_by(user_id=uid).first()
        assert st.last_active_date == date.today() - timedelta(days=1)
    # Second repair within 30 days is refused.
    assert client.post("/api/momentum/streak/repair").status_code == 400


def test_streak_repair_gap_too_long(client, app):
    _authed(client)
    uid = _user_id(app)
    with app.app_context():
        db.session.add(StreakState(user_id=uid, current=5, longest=5,
                                   last_active_date=date.today() - timedelta(days=5)))
        db.session.commit()
    assert client.post("/api/momentum/streak/repair").status_code == 400


# ---------- retrospective taxonomy + trends ----------

def test_reasons_15(client):
    # The reasons route is login-gated (302 anon), unlike the 401-JSON APIs.
    _authed(client)
    body = client.get("/api/retrospectives/reasons").get_json()
    assert len(body["reasons"]) == 15
    assert len(RETRO_REASONS) == 15
    by_id = {r["id"]: r for r in body["reasons"]}
    assert by_id["skill-gap-dsa"]["label"]
    assert by_id["skill-gap-dsa"]["stage_hint"]


def test_trends_counts_completed_only(client, app):
    _authed(client)
    uid = _user_id(app)
    app_id = _add_app(client, stage="rejected")
    _mk_retro(app, uid, app_id)
    app_id2 = _add_app(client, company="Beta", stage="rejected")
    _mk_retro(app, uid, app_id2, completed=False)
    trends = client.get("/api/retrospectives/trends").get_json()["trends"]
    assert trends["total"] == 1
    assert any(r["count"] == 1 for r in trends["by_reason"])
    assert any(s["count"] == 1 for s in trends["by_stage"])


def test_wizard_facts_reason_persists(client, app):
    _authed(client)
    app_id = _add_app(client, stage="rejected")
    resp = client.post("/retrospectives/api/save",
                       data=json.dumps({"application_id": app_id,
                                        "step": "facts",
                                        "facts": "Got rejected after OA.",
                                        "reason": "skill-gap-dsa"}),
                       content_type="application/json",
                       headers={"X-CSRFToken": "test"})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    with app.app_context():
        retro = Retrospective.query.filter_by(
            application_id=app_id).first()
        assert retro.reason == "skill-gap-dsa"
    # Free text wins over a radio pick.
    client.post("/retrospectives/api/save",
                data=json.dumps({"application_id": app_id,
                                 "step": "facts",
                                 "facts": "Got rejected after OA.",
                                 "reason": "skill-gap-dsa",
                                 "reason_other": "Panel was rushed"}),
                content_type="application/json",
                headers={"X-CSRFToken": "test"})
    with app.app_context():
        retro = Retrospective.query.filter_by(
            application_id=app_id).first()
        assert retro.reason == "Panel was rushed"


# ---------- experiments ----------

def test_experiment_outcome_patch(client, app):
    _authed(client)
    uid = _user_id(app)
    with app.app_context():
        exp = Experiment(user_id=uid, hypothesis="H", change="C",
                         target="T")
        db.session.add(exp)
        db.session.commit()
        exp_id = exp.id
    ok = client.patch(f"/api/retrospectives/experiments/{exp_id}",
                      json={"outcome": "helped",
                            "outcome_note": "Worked well"})
    assert ok.status_code == 200
    assert ok.get_json()["experiment"]["outcome"] == "helped"
    assert client.patch(f"/api/retrospectives/experiments/{exp_id}",
                        json={"outcome": "magic"}).status_code == 400
    stranger = app.test_client()
    _authed(stranger, email="ph4b@example.com", name="Phase B")
    assert stranger.patch(f"/api/retrospectives/experiments/{exp_id}",
                          json={"outcome": "hurt"}).status_code == 404
    lst = client.get("/api/retrospectives/experiments").get_json()
    assert lst["experiments"][0]["outcome"] == "helped"


# ---------- interview feedback ----------

def test_interview_feedback_flow(client, app):
    _authed(client)
    uid = _user_id(app)
    app_id = _add_app(client, stage="rejected")
    retro_id = _mk_retro(app, uid, app_id)
    resp = client.post(f"/api/retrospectives/{retro_id}/feedback",
                       json={"round": "Tech 1",
                             "questions_asked": ["Explain CAP theorem"],
                             "self_rating": 3, "notes": "Need depth"})
    assert resp.status_code == 200
    assert len(resp.get_json()["feedback"]) == 1
    assert client.post(f"/api/retrospectives/{retro_id}/feedback",
                       json={"round": "Tech 2",
                             "questions_asked": [],
                             "self_rating": 9}).status_code == 400
    assert client.post(f"/api/retrospectives/{retro_id}/feedback",
                       json={"round": "",
                             "questions_asked": [],
                             "self_rating": 3}).status_code == 400
    got = client.get(f"/api/retrospectives/{retro_id}").get_json()
    assert got["retrospective"]["interview_feedback"][0]["round"] == "Tech 1"
    stranger = app.test_client()
    _authed(stranger, email="ph4b@example.com", name="Phase B")
    assert stranger.get(f"/api/retrospectives/{retro_id}").status_code == 404


# ---------- anonymous learnings ----------

def test_learnings_opt_in_and_suppression(client, app):
    # Three sharers with the same reason+stage -> cell appears (count 3).
    for i in range(3):
        c = app.test_client()
        _authed(c, email=f"share{i}@example.com", name=f"Sharer {i}")
        uid_i = _user_id(app, email=f"share{i}@example.com")
        app_id = _add_app(c, company=f"Co{i}", stage="rejected")
        retro_id = _mk_retro(app, uid_i, app_id, shared=True)
        assert c.post(f"/api/retrospectives/{retro_id}/share-anonymous",
                      json={"share": True}).status_code == 200
    # One sharer with a unique reason -> suppressed (needs 3).
    c = app.test_client()
    _authed(c, email="solo@example.com", name="Solo")
    uid_s = _user_id(app, email="solo@example.com")
    app_id = _add_app(c, company="SoloCo", stage="rejected")
    retro_id = _mk_retro(app, uid_s, app_id, reason="location",
                         stage="offer", shared=True)
    learnings = app.test_client().get(
        "/api/retrospectives/learnings").get_json()["learnings"]
    assert any(l["reason"] == "skill-gap-dsa" and l["count"] == 3
               for l in learnings)
    assert not any(l["reason"] == "location" for l in learnings)
    # Unshared rows never appear, even with 3 identical ones.
    for i in range(3):
        c = app.test_client()
        _authed(c, email=f"noshare{i}@example.com", name=f"NS {i}")
        uid_i = _user_id(app, email=f"noshare{i}@example.com")
        app_id = _add_app(c, company=f"NS{i}", stage="rejected")
        _mk_retro(app, uid_i, app_id, reason="company-freeze",
                  stage="applied", shared=False)
    learnings2 = app.test_client().get(
        "/api/retrospectives/learnings").get_json()["learnings"]
    assert not any(l["reason"] == "company-freeze" for l in learnings2)
