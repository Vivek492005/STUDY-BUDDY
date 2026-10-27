"""Phase 1 tracker spine: interview kits, offer compare, referrals,
timeline, digest, CSV import."""

import json
from datetime import UTC, date, datetime, timedelta

from unified_app import JobApplication, User, db


def _authed(client, email="trk1@example.com", name="Tracker One"):
    client.post("/register", data={
        "name": name, "email": email, "password": "Str0ng!Pass1",
        "age_confirm": "1"}, follow_redirects=False)
    client.post("/login", data={"email": email, "password": "Str0ng!Pass1"},
                follow_redirects=False)


def _user_id(app, email="trk1@example.com"):
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


# ---------- interview kit ----------

def test_kit_company_match(client):
    _authed(client)
    app_id = _add_app(client, company="Google", role="SDE")
    resp = client.get(f"/api/tracker/{app_id}/kit")
    assert resp.status_code == 200
    kit = resp.get_json()["kit"]
    assert kit["company"] == "Google"
    assert kit["question_source"] == "company"
    assert 1 <= len(kit["questions"]) <= 10
    assert kit["questions"][0]["question"]
    assert len(kit["checklist"]) >= 5


def test_kit_role_fallback_and_no_match(client):
    _authed(client)
    app_id = _add_app(client, company="ZzzNoSuchCo", role="Data Analyst")
    kit = client.get(f"/api/tracker/{app_id}/kit").get_json()["kit"]
    assert kit["question_source"] == "role"
    assert kit["roadmap"] is not None
    app_id2 = _add_app(client, company="ZzzNoSuchCo", role="ZzzNoSuchRole")
    kit2 = client.get(f"/api/tracker/{app_id2}/kit").get_json()["kit"]
    assert kit2["questions"] == [] and kit2["note"]
    assert client.get("/api/tracker/999999/kit").status_code == 404
    stranger = client.application.test_client()
    _authed(stranger, email="trk1b@example.com", name="Tracker B")
    assert stranger.get(f"/api/tracker/{app_id}/kit").status_code == 404


# ---------- offer compare ----------

def test_compare_ranks_and_band(client):
    _authed(client)
    resp = client.post("/api/tracker/offers/compare", json={
        "role": "Software Development Engineer", "tier": "tier1",
        "offers": [
            {"title": "A Corp", "base_lpa": 18, "bonus_lpa": 2,
             "esop_lpa": 0, "location": "Bengaluru"},
            {"title": "B Corp", "base_lpa": 22, "bonus_lpa": 1,
             "esop_lpa": 3, "location": "Pune"},
        ]})
    assert resp.status_code == 200
    body = resp.get_json()
    offers = body["offers"]
    assert offers[0]["title"] == "B Corp" and offers[0]["rank"] == 1
    assert offers[0]["total_lpa"] == 26 and offers[1]["total_lpa"] == 20
    assert body["band"] is not None and body["band"]["median_lpa"] > 0
    assert body["verdict"] and "B Corp" in body["verdict"]
    no_band = client.post("/api/tracker/offers/compare", json={
        "offers": [{"title": "A", "base_lpa": 10},
                   {"title": "B", "base_lpa": 12}]})
    assert no_band.get_json()["band"] is None


def test_compare_validation(client):
    _authed(client)
    base = "/api/tracker/offers/compare"
    one = {"offers": [{"title": "A", "base_lpa": 10}]}
    assert client.post(base, json=one).status_code == 400
    four = {"offers": [{"title": str(i), "base_lpa": 10}
                       for i in range(4)]}
    assert client.post(base, json=four).status_code == 400
    neg = {"offers": [{"title": "A", "base_lpa": -5},
                      {"title": "B", "base_lpa": 10}]}
    assert client.post(base, json=neg).status_code == 400
    boolean = {"offers": [{"title": "A", "base_lpa": True},
                          {"title": "B", "base_lpa": 10}]}
    assert client.post(base, json=boolean).status_code == 400
    assert client.application.test_client().post(
        base, json={"offers": [{"title": "A", "base_lpa": 1},
                               {"title": "B", "base_lpa": 2}]}).status_code == 401


# ---------- referrals ----------

def test_referral_crud_and_ownership(client, app):
    _authed(client)
    app_id = _add_app(client)
    resp = client.post("/api/tracker/referrals", json={
        "contact_name": "Priya", "application_id": app_id,
        "channel": "linkedin", "notes": "Batchmate"})
    assert resp.status_code == 201, resp.get_data(as_text=True)[:300]
    ref = resp.get_json()["referral"]
    assert ref["status"] == "asked" and ref["company"] == "Acme"
    rid = ref["id"]
    lst = client.get("/api/tracker/referrals").get_json()["referrals"]
    assert len(lst) == 1
    patched = client.patch(f"/api/tracker/referrals/{rid}",
                           json={"status": "referred"})
    assert patched.get_json()["referral"]["status"] == "referred"
    assert client.patch(f"/api/tracker/referrals/{rid}",
                        json={"status": "bogus"}).status_code == 400
    stranger = app.test_client()
    _authed(stranger, email="trk1b@example.com", name="Tracker B")
    assert stranger.patch(f"/api/tracker/referrals/{rid}",
                          json={"status": "asked"}).status_code == 404
    assert stranger.delete(f"/api/tracker/referrals/{rid}").status_code == 404
    assert client.post("/api/tracker/referrals", json={
        "contact_name": "X", "application_id": 999999}).status_code == 400
    assert client.post("/api/tracker/referrals",
                       json={"contact_name": " "}).status_code == 400
    assert client.delete(f"/api/tracker/referrals/{rid}").status_code == 200
    assert client.get("/api/tracker/referrals").get_json()["referrals"] == []


# ---------- timeline ----------

def test_timeline_derived_events(client, app):
    _authed(client)
    app_id = _add_app(client, stage="interview")
    with app.app_context():
        row = db.session.get(JobApplication, app_id)
        row.created_at = datetime.now(UTC) - timedelta(days=10)
        row.stage_updated_at = datetime.now(UTC)
        row.deadline = date.today() + timedelta(days=4)
        row.deadline_kind = "interview"
        db.session.commit()
    events = client.get(f"/api/tracker/{app_id}/timeline").get_json()["timeline"]
    names = [e["event"] for e in events]
    assert names[0] == "Added to tracker"
    assert "Moved to Interview" in names
    assert "Deadline: interview" in names
    assert [e["date"] for e in events] == sorted(e["date"] for e in events)


# ---------- digest ----------

def test_digest_stale_and_upcoming(client, app):
    _authed(client)
    app_id = _add_app(client, stage="applied")
    with app.app_context():
        row = db.session.get(JobApplication, app_id)
        row.stage_updated_at = datetime.now(UTC) - timedelta(days=20)
        row.deadline = date.today() + timedelta(days=3)
        row.deadline_kind = "oa"
        db.session.commit()
    digest = client.get("/api/tracker/digest").get_json()["digest"]
    assert digest["total"] == 1 and len(digest["stale"]) == 1
    assert len(digest["upcoming_7d"]) == 1
    assert digest["suggested_actions"], digest
    assert app.test_client().get("/api/tracker/digest").status_code == 401


# ---------- CSV import ----------

CSV_GOOD = ("company,role,stage,applied_date\n"
            "Acme,SDE,applied,2026-10-01\n"
            "Beta,Data Analyst,wishlist,\n"
            ",NoCompany,applied,\n"
            "Gamma,PM,bogus-stage,\n"
            "Delta,QA,applied,not-a-date\n")


def test_csv_import_mixed_rows(client, app):
    _authed(client)
    resp = client.post("/api/tracker/import", json={"csv": CSV_GOOD})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    body = resp.get_json()
    assert body["imported"] == 3  # Acme, Beta, Gamma (stage -> wishlist)
    assert body["skipped"] == 2  # missing company, bad date
    assert len(body["notes"]) == 1  # unknown stage note
    with app.app_context():
        rows = JobApplication.query.filter_by(
            user_id=_user_id(app)).all()
        by_company = {r.company: r.stage for r in rows}
    assert by_company["Gamma"] == "wishlist"
    assert "Delta" not in by_company


def test_csv_import_validation(client):
    _authed(client)
    assert client.post("/api/tracker/import",
                       json={"csv": "name,title\nA,B\n"}).status_code == 400
    big = "company,role,stage,applied_date\n" + \
        "".join(f"C{i},R{i},applied,\n" for i in range(201))
    assert client.post("/api/tracker/import",
                       json={"csv": big}).status_code == 400
    assert client.application.test_client().post(
        "/api/tracker/import", json={"csv": CSV_GOOD}).status_code == 401
