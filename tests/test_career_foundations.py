"""Phase 0 foundations: career snapshot, freshness, deadline engine,
and the freshness/deadlines template wiring."""

import json
from datetime import date, timedelta

from unified_app import (JobApplication, OpportunityBookmark, Portfolio,
                         User, db)


def _authed(client, email="career0@example.com", name="Career Zero"):
    client.post("/register", data={
        "name": name, "email": email, "password": "Str0ng!Pass1",
        "age_confirm": "1"}, follow_redirects=False)
    client.post("/login", data={"email": email, "password": "Str0ng!Pass1"},
                follow_redirects=False)


def _user_id(app, email="career0@example.com"):
    with app.app_context():
        return User.query.filter_by(email=email).first().id


def _add_app(client, company="Acme", role="SDE"):
    resp = client.post("/tracker/add",
                       data=json.dumps({"company": company, "role": role}),
                       content_type="application/json",
                       headers={"X-CSRFToken": "test"})
    assert resp.status_code in (200, 201), resp.get_data(as_text=True)[:300]
    return resp.get_json()["application"]["id"]


# ---------- freshness ----------

def test_freshness_statuses():
    from studybuddy.freshness import all_freshness, freshness_status
    sal = freshness_status("salary")
    assert sal["last_updated"] == "2026-10-26" and sal["stale"] is False
    opp = freshness_status("opportunities")
    assert opp["last_updated"] == "2026-10-25" and opp["stale"] is False
    stale = freshness_status("opportunities", today=date(2027, 3, 1))
    assert stale["stale"] is True and "out of date" in stale["message"]
    unknown = freshness_status("nope")
    assert unknown["last_updated"] is None
    all_f = all_freshness()
    assert set(all_f.keys()) == {"salary", "opportunities", "jobs"}


def test_freshness_api_public(app):
    resp = app.test_client().get("/api/career/freshness")
    assert resp.status_code == 200
    assert set(resp.get_json()["datasets"].keys()) == {
        "salary", "opportunities", "jobs"}


# ---------- snapshot ----------

def test_snapshot_shape(client, app):
    _authed(client)
    uid = _user_id(app)
    with app.app_context():
        db.session.add(JobApplication(user_id=uid, company="Acme",
                                      role="SDE", stage="applied"))
        db.session.add(Portfolio(user_id=uid, slug="career-zero",
                                 display_name="Career Zero",
                                 skills=["Python", "SQL"]))
        db.session.commit()
    resp = client.get("/api/career/snapshot")
    assert resp.status_code == 200
    body = resp.get_json()
    snap = body["snapshot"]
    assert set(snap.keys()) == {"cluster", "skills", "applications", "goals",
                                "momentum", "health", "portfolio", "mentors",
                                "opportunities_bookmarked"}
    assert snap["applications"]["total"] == 1
    assert snap["applications"]["by_stage"]["applied"] == 1
    assert snap["skills"] == ["Python", "SQL"]
    assert snap["portfolio"] == {"exists": True, "slug": "career-zero"}
    assert snap["cluster"] is None and body["warnings"] == []
    assert app.test_client().get("/api/career/snapshot").status_code == 401


# ---------- deadlines ----------

def test_deadline_crud_and_list(client, app):
    _authed(client)
    app_id = _add_app(client)
    future = (date.today() + timedelta(days=5)).isoformat()
    resp = client.post(f"/tracker/{app_id}/deadline",
                       json={"deadline": future, "deadline_kind": "interview"},
                       headers={"X-CSRFToken": "test"})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    ad = resp.get_json()["application"]
    assert ad["deadline"] == future and ad["deadline_kind"] == "interview"
    lst = client.get("/api/career/deadlines")
    assert lst.status_code == 200
    items = lst.get_json()["deadlines"]
    assert len(items) == 1 and items[0]["days_left"] == 5
    assert items[0]["source"] == "tracker"
    assert client.post(f"/tracker/{app_id}/deadline",
                       json={"deadline": "not-a-date"},
                       headers={"X-CSRFToken": "test"}).status_code == 400
    assert client.post(f"/tracker/{app_id}/deadline",
                       json={"deadline": future, "deadline_kind": "bogus"},
                       headers={"X-CSRFToken": "test"}).status_code == 400
    cleared = client.post(f"/tracker/{app_id}/deadline",
                          json={"deadline": None},
                          headers={"X-CSRFToken": "test"})
    assert cleared.get_json()["application"]["deadline"] is None
    assert client.get("/api/career/deadlines").get_json()["deadlines"] == []
    stranger = app.test_client()
    _authed(stranger, email="other0@example.com", name="Other Zero")
    assert stranger.post(f"/tracker/{app_id}/deadline",
                         json={"deadline": future},
                         headers={"X-CSRFToken": "test"}).status_code == 404
    assert app.test_client().get("/api/career/deadlines").status_code == 401


def test_deadlines_include_bookmarked_opportunity(client, app):
    # Date-independent: pick any opportunity whose deadline is still in the
    # future (a hardcoded id+date rots once the deadline passes).
    from studybuddy.opportunities import list_opportunities
    today = date.today().isoformat()
    future_opps = [o for o in list_opportunities()
                   if (o.get("deadline") or "") > today]
    assert future_opps, "no future-dated opportunity in test data"
    target = future_opps[0]
    _authed(client)
    uid = _user_id(app)
    with app.app_context():
        db.session.add(OpportunityBookmark(
            user_id=uid, opp_id=target["id"]))
        db.session.commit()
    items = client.get("/api/career/deadlines").get_json()["deadlines"]
    opp_items = [i for i in items if i["source"] == "opportunity"]
    assert opp_items, items
    assert opp_items[0]["date"] == target["deadline"]


def test_deadlines_pure_function_window():
    from studybuddy.deadlines import upcoming_deadlines
    # No user rows: empty list, never raises — even for garbage input.
    assert upcoming_deadlines(999999) == []
    assert upcoming_deadlines(999999, days="bogus") == []


# ---------- template wiring ----------

def test_tracker_page_shows_deadlines_section(client, app):
    _authed(client)
    resp = client.get("/tracker")
    assert resp.status_code == 200
    assert b"Upcoming deadlines" in resp.data
    assert b"No upcoming deadlines." in resp.data


def test_salary_and_opportunity_pages_show_freshness(client):
    _authed(client)
    sal = client.get("/salary")
    assert sal.status_code == 200
    assert b"Fresh" in sal.data and b"2026-10-26" in sal.data
    opp = client.get("/opportunities")
    assert opp.status_code == 200
    assert b"Fresh" in opp.data and b"2026-10-25" in opp.data
