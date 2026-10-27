"""Ultimate Lab Phase 0: history, envelope, blueprint, per-family limits.

Covers: LabRun recording + XP, envelope labels/next_actions, history
CRUD + ownership, favourites, rerun, dynamic hub counts, blueprint
registration, FEATURE_LABS gating, and per-family vs global rate limits.
"""
from tests.conftest import login, register  # noqa: F401

from studybuddy.ultimate_routes import ALL_TOOLS

TEXT = (
    "I built a REST API in Python with Flask and PostgreSQL serving "
    "500 users daily and reduced response time by 40 percent."
)
REPORT = {"score": 72, "matched_skills": ["python", "flask"],
          "matched_keywords": ["python"], "missing_keywords": ["docker"]}


def _authed(client, email="labphase0@example.com"):
    register(client, email=email)
    login(client, email=email)


def _user_id(app, email):
    from unified_app import User
    with app.app_context():
        return User.query.filter_by(email=email).first().id


def test_run_records_history_and_envelope(client, app):
    _authed(client)
    resp = client.post("/api/ultimate/run/bullets/star-convert",
                       json={"bullet_text": TEXT})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    body = resp.get_json()
    assert body["ok"] and body["run_id"]
    assert body["labels"]["basis"] == "rule-based"
    assert body["labels"]["data_source"]
    kinds = [a["kind"] for a in body["next_actions"]]
    assert "save" in kinds
    from unified_app import LabRun, XPLedger
    uid = _user_id(app, "labphase0@example.com")
    with app.app_context():
        rows = LabRun.query.filter_by(user_id=uid).all()
        assert len(rows) == 1
        assert rows[0].tool_id == "bullets/star-convert"
        assert rows[0].input_hash and len(rows[0].input_hash) == 64
        xp = XPLedger.query.filter_by(user_id=uid,
                                      event_type="lab_run").all()
        assert len(xp) == 1 and xp[0].xp == 5


def test_history_crud_and_ownership(client, app):
    _authed(client)
    r1 = client.post("/api/ultimate/run/bullets/star-convert",
                     json={"bullet_text": TEXT}).get_json()
    r2 = client.post("/api/ultimate/run/github/readme-grade",
                     json={"readme_text": "# Hi\nA readme."}).get_json()
    assert r1["run_id"] != r2["run_id"]
    hist = client.get("/api/ultimate/history").get_json()
    assert hist["ok"] and len(hist["runs"]) == 2
    assert hist["runs"][0]["id"] == r2["run_id"]  # newest first
    assert hist["runs"][0]["saved"] is False
    # Save is idempotent.
    assert client.post(
        f"/api/ultimate/runs/{r1['run_id']}/save").get_json()["saved"] is True
    assert client.post(
        f"/api/ultimate/runs/{r1['run_id']}/save").get_json()["saved"] is True
    hist = client.get("/api/ultimate/history").get_json()
    saved = [r for r in hist["runs"] if r["id"] == r1["run_id"]][0]
    assert saved["saved"] is True
    # Detail carries full input/output.
    detail = client.get(f"/api/ultimate/runs/{r1['run_id']}").get_json()
    assert detail["run"]["input"]["bullet_text"] == TEXT
    # Rerun creates a NEW row and pays XP again (different ref).
    re = client.post(f"/api/ultimate/runs/{r1['run_id']}/rerun").get_json()
    assert re["ok"] and re["run_id"] != r1["run_id"]
    hist = client.get("/api/ultimate/history").get_json()
    assert len(hist["runs"]) == 3
    # Delete one run, then clear the rest.
    assert client.delete(f"/api/ultimate/runs/{r2['run_id']}").get_json()["deleted"] is True
    assert len(client.get("/api/ultimate/history").get_json()["runs"]) == 2
    cleared = client.delete("/api/ultimate/history").get_json()
    assert cleared["deleted"] == 2
    assert client.get("/api/ultimate/history").get_json()["runs"] == []

    # Another user cannot see, save, or delete the first user's runs.
    _authed(client, email="labphase0-two@example.com")
    assert client.get(f"/api/ultimate/runs/{r1['run_id']}").status_code == 404
    assert client.post(f"/api/ultimate/runs/{r1['run_id']}/save").status_code == 404
    assert client.delete(f"/api/ultimate/runs/{r1['run_id']}").status_code == 404
    assert client.get("/api/ultimate/history").get_json()["runs"] == []


def test_favorites_roundtrip_and_unknown_tool(client):
    _authed(client)
    assert client.get("/api/ultimate/favorites").get_json()["favorites"] == []
    assert client.post("/api/ultimate/favorites/bullets/star-convert").get_json()["favorite"] is True
    assert client.post("/api/ultimate/favorites/bullets/star-convert").get_json()["favorite"] is True
    favs = client.get("/api/ultimate/favorites").get_json()["favorites"]
    assert len(favs) == 1 and favs[0]["tool_id"] == "bullets/star-convert"
    assert favs[0]["name"]
    assert client.post("/api/ultimate/favorites/nope/nope").status_code == 400
    assert client.delete("/api/ultimate/favorites/bullets/star-convert").get_json()["favorite"] is False
    assert client.get("/api/ultimate/favorites").get_json()["favorites"] == []


def test_hub_counts_are_dynamic(client):
    _authed(client)
    html = client.get("/ultimate/lab").get_data(as_text=True)
    n_tools = len(ALL_TOOLS)
    n_features = len({t["feature"] for t in ALL_TOOLS})
    assert f'id="lab-hero-count">{n_tools}<' in html
    assert f"across {n_features} features" in html
    assert "My Lab" in html


def test_blueprint_registered_and_anon_denied(app, client):
    assert "ultimate._ultimate_run" in app.view_functions
    assert "ultimate._ultimate_tools" in app.view_functions
    assert client.get("/api/ultimate/history").status_code == 401
    assert client.post("/api/ultimate/runs/1/save").status_code == 401
    assert client.get("/api/ultimate/favorites").status_code == 401
    assert client.get("/ultimate/lab").status_code == 302


def test_labs_flag_off_blocks_run_and_history(client, monkeypatch):
    import dataclasses as _dc
    from studybuddy import app_config
    monkeypatch.setattr(app_config, "config",
                        _dc.replace(app_config.config, feature_labs=False))
    _authed(client)
    assert client.post("/api/ultimate/run/bullets/star-convert",
                       json={"bullet_text": TEXT}).status_code == 403
    assert client.get("/api/ultimate/history").status_code == 403


def test_per_family_and_global_limits():
    """Family buckets are independent; the global bucket bounds total.

    Uses a throwaway app with tiny limits + the shared limiter flipped
    on (TESTING disables it at creation). Reset in finally so no other
    test in this process ever runs limited.
    """
    from unified_app import create_app
    from app.extensions import limiter
    app = create_app({"TESTING": True,
                      "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
                      "SECRET_KEY": "test-secret-key",
                      "WTF_CSRF_ENABLED": False,
                      "ULTIMATE_RUN_LIMIT": "2 per hour",
                      "ULTIMATE_RUN_GLOBAL_LIMIT": "5 per hour"})
    client = app.test_client()
    limiter.enabled = True
    try:
        register(client, email="labphase0-limits@example.com")
        login(client, email="labphase0-limits@example.com")

        def run(tool, payload):
            return client.post(f"/api/ultimate/run/{tool}", json=payload).status_code

        # Family "AI Bullet Rewriter": 2 ok, 3rd is family-limited.
        assert run("bullets/star-convert", {"bullet_text": TEXT}) == 200
        assert run("bullets/star-convert", {"bullet_text": TEXT}) == 200
        assert run("bullets/star-convert", {"bullet_text": TEXT}) == 429
        # A different family still works after the first family's lockout
        # (Flask-Limiter counts even rejected attempts toward the global
        # bucket, so the global limit needs headroom for this proof).
        assert run("github/readme-grade", {"readme_text": "# Hi"}) == 200
        assert run("github/readme-grade", {"readme_text": "# Hi"}) == 200
        # 6th evaluated request trips a bucket (family or global).
        assert run("github/readme-grade", {"readme_text": "# Hi"}) == 429
    finally:
        limiter.enabled = False
