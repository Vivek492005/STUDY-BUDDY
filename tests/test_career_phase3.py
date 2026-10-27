"""Phase 3 surface: radar upgrades + portfolio upgrades."""

import json
from datetime import date


from studybuddy.opportunities import list_opportunities
from tests.conftest import login, register
from tests.test_portfolio import _save
from unified_app import CareerAssessment, Portfolio, User, db


def _authed(client, email="ph3@example.com", name="Phase Three"):
    register(client, email=email, name=name)
    login(client, email=email)


def _user_id(app, email="ph3@example.com"):
    with app.app_context():
        return User.query.filter_by(email=email).first().id


def _open_opp_with_deadline():
    for o in list_opportunities():
        if o.get("deadline"):
            return o
    raise AssertionError("no open opp with a deadline")


def _rolling_opp():
    for o in list_opportunities():
        if not o.get("deadline"):
            return o
    raise AssertionError("no rolling opp")


# ---------- radar APIs ----------

def test_refresh_status_shape(client):
    resp = client.get("/api/opportunities/refresh-status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["total"] == 128 and body["open"] + body["closed"] == 128
    assert body["last_updated"] and body["last_refresh"]


def test_matches_shape_and_reasons(client, app):
    _authed(client)
    uid = _user_id(app)
    with app.app_context():
        db.session.add(Portfolio(user_id=uid, slug="phase-three",
                                 display_name="Phase Three",
                                 skills=["Python", "SQL"]))
        db.session.add(CareerAssessment(
            user_id=uid,
            top_clusters=[{"id": "data_ai", "name": "Data & AI"}]))
        db.session.commit()
    resp = client.get("/api/opportunities/matches")
    assert resp.status_code == 200
    matches = resp.get_json()["matches"]
    assert len(matches) <= 20
    scores = [m["score"] for m in matches]
    assert scores == sorted(scores, reverse=True)
    for m in matches:
        assert 0 < m["score"] <= 100 and m["reasons"]
        assert m["opp_id"] and m["title"] and m["url"]
    assert app.test_client().get(
        "/api/opportunities/matches").status_code == 401


def test_matches_empty_without_profile(client):
    _authed(client)
    body = client.get("/api/opportunities/matches").get_json()
    assert body["matches"] == [] and body.get("note")


def test_alerts_buckets_from_real_deadline(client):
    _authed(client)
    opp = _open_opp_with_deadline()
    days_left = (date.fromisoformat(opp["deadline"]) - date.today()).days
    assert days_left >= 0
    client.post("/opportunities/bookmark",
                data=json.dumps({"opp_id": opp["id"]}),
                content_type="application/json")
    alerts = client.get("/api/opportunities/alerts").get_json()["alerts"]
    assert set(alerts.keys()) == {"due_1d", "due_3d", "due_7d"}
    ids_1 = {a["opp_id"] for a in alerts["due_1d"]}
    ids_3 = {a["opp_id"] for a in alerts["due_3d"]}
    ids_7 = {a["opp_id"] for a in alerts["due_7d"]}
    assert (opp["id"] in ids_1) == (days_left <= 1)
    assert (opp["id"] in ids_3) == (days_left <= 3)
    assert (opp["id"] in ids_7) == (days_left <= 7)


def test_opportunity_detail_checklist_and_bookmarked(client):
    _authed(client)
    opp = _open_opp_with_deadline()
    body = client.get(f"/api/opportunities/{opp['id']}").get_json()
    assert isinstance(body["opportunity"]["eligibility_list"], list)
    assert body["bookmarked"] is False
    client.post("/opportunities/bookmark",
                data=json.dumps({"opp_id": opp["id"]}),
                content_type="application/json")
    body2 = client.get(f"/api/opportunities/{opp['id']}").get_json()
    assert body2["bookmarked"] is True
    assert client.get("/api/opportunities/nope").status_code == 404


def test_apply_to_tracker_dedupe(client, app):
    _authed(client)
    opp = _open_opp_with_deadline()
    r1 = client.post(f"/api/opportunities/{opp['id']}/apply-to-tracker")
    assert r1.status_code == 201
    app_id = r1.get_json()["application_id"]
    r2 = client.post(f"/api/opportunities/{opp['id']}/apply-to-tracker")
    assert r2.status_code == 200
    assert r2.get_json()["application_id"] == app_id
    assert client.post(
        "/api/opportunities/nope/apply-to-tracker").status_code == 404
    assert app.test_client().post(
        f"/api/opportunities/{opp['id']}/apply-to-tracker").status_code == 401


def test_ics_download_and_rolling_400(client):
    _authed(client)
    opp = _open_opp_with_deadline()
    resp = client.get(f"/api/opportunities/{opp['id']}/ics")
    assert resp.status_code == 200
    assert "text/calendar" in resp.content_type
    text = resp.get_data(as_text=True)
    assert "BEGIN:VCALENDAR" in text and "BEGIN:VEVENT" in text
    assert opp["deadline"].replace("-", "") in text
    rolling = _rolling_opp()
    assert client.get(
        f"/api/opportunities/{rolling['id']}/ics").status_code == 400


# ---------- portfolio APIs ----------

def _mk_portfolio(client):
    resp = _save(client)
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    return resp.get_json()["slug"]


def test_save_new_sections_and_contact_list_shape(client, app):
    _authed(client)
    slug = _mk_portfolio(client)
    resp = _save(client, certifications=[{"title": "AWS CLF",
                                          "issuer": "Amazon", "year": "2025"}],
                 experience=[{"title": "Intern", "organization": "Acme",
                              "period": "2025"}],
                 education=[{"institution": "IIT", "degree": "BTech",
                             "period": "2023-2027"}],
                 achievements=[{"title": "Hackathon winner",
                                "description": "Won XYZ"}],
                 contact=[{"label": "Email", "value": "a@b.com"},
                          {"label": "Location", "value": "Delhi"}])
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    html = client.get(f"/p/{slug}").get_data(as_text=True)
    assert "AWS CLF" in html and "a@b.com" in html
    # Object-form contact is rejected: the contract is [{label, value}].
    bad = _save(client, contact={"email": "a@b.com"})
    assert bad.status_code == 400


def test_theme_switch_and_validation(client):
    _authed(client)
    _mk_portfolio(client)
    ok = client.post("/api/portfolio/theme", json={"theme": "dark"})
    assert ok.status_code == 200 and ok.get_json()["theme"] == "dark"
    assert client.post("/api/portfolio/theme",
                       json={"theme": "neon"}).status_code == 400
    stranger = client.application.test_client()
    _authed(stranger, email="ph3b@example.com", name="Phase B")
    assert stranger.post("/api/portfolio/theme",
                         json={"theme": "dark"}).status_code == 404


def test_github_import_preview_and_save(monkeypatch, client):
    _authed(client)
    _mk_portfolio(client)

    class _Resp:
        status_code = 200

        def json(self):
            return [{"name": "demo", "description": "A demo repo",
                     "html_url": "https://github.com/x/demo",
                     "stargazers_count": 42, "language": "Python"},
                    {"name": "other", "description": None,
                     "html_url": "https://github.com/x/other",
                     "stargazers_count": 3, "language": None}]

    import unified_app as ua
    monkeypatch.setattr(ua.requests, "get", lambda *a, **k: _Resp())
    prev = client.post("/api/portfolio/github-import",
                       json={"username": "someone"})
    assert prev.status_code == 200
    repos = prev.get_json()["preview"]
    assert len(repos) == 2 and repos[0]["stars"] == 42
    saved = client.post("/api/portfolio/github-import/save",
                        json={"repos": repos})
    assert saved.status_code == 200 and saved.get_json()["added"] == 2
    again = client.post("/api/portfolio/github-import/save",
                        json={"repos": repos})
    assert again.get_json()["added"] == 0  # deduped by URL
    assert client.post("/api/portfolio/github-import",
                       json={"username": "bad name!"}).status_code == 400


def test_analytics_counts_real_views(client):
    _authed(client)
    slug = _mk_portfolio(client)
    assert client.get("/p/" + slug).status_code == 200
    body = client.get("/api/portfolio/analytics").get_json()
    assert body["total_views"] >= 1
    assert body["views_last_30d"] >= 1
    assert len(body["by_day"]) == 30
    assert sum(d["views"] for d in body["by_day"]) >= 1


def test_review_exactly_25_checks(client):
    _authed(client)
    _mk_portfolio(client)
    body = client.get("/api/portfolio/review").get_json()
    assert body["max"] == 25 and len(body["checks"]) == 25
    assert 0 <= body["score"] <= 25
    for c in body["checks"]:
        assert c["label"]
        if not c["passed"]:
            assert c["hint"]


def test_print_view(client):
    _authed(client)
    slug = _mk_portfolio(client)
    resp = client.get(f"/portfolio/{slug}/print")
    assert resp.status_code == 200
    assert "Aarav Sharma" in resp.get_data(as_text=True)
