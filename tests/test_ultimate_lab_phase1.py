"""Ultimate Lab Phase 1: rooms, flagship paths, intent search.

Covers: room pages (30 rooms, deep links, 404), path list/detail,
verified tool-step completion (wrong-tool/wrong-user rejection, XP 10
once), page steps (progress, no XP), Resume -> Offer finish landing a
real tracker wishlist row (25 XP once, no double row), milestone
finishes, intent search ordering, FEATURE_LABS gating, hub sections.
"""
from tests.conftest import login, register  # noqa: F401

RUN_PAYLOADS = {
    "jd/prescreen": {"resume_text": "Python developer with Flask",
                     "report": {}},
    "bullets/star-convert": {
        "bullet_text": "Built a Flask API used by 200 students"},
    "jd/keyword-plan": {"resume_text": "Python developer", "report": {}},
    "versions/diff": {"text_a": "a", "text_b": "b"},
    "jd/offer-odds": {"report": {}},
    "mock/voice-prep": {
        "question": {"question": "Tell me about yourself"},
        "answer_text": "I build apps", "seconds_taken": 60},
    "assessment/delta": {
        "old_results": [{"id": "c1", "name": "Backend", "match": 50}],
        "new_results": [{"id": "c1", "name": "Backend", "match": 60}]},
    "quizzes/weakness-drill": {"skill": "python", "past_results": {}},
    "dsa/pattern-mastery": {"solved_ids": []},
    "star/story-bank": {"stories": ["Built a Flask API"]},
    "mock/company-intel": {"company": "Acme"},
    "coach/hr-round": {},
    "peermocks/score-rubric": {"scores": {}},
    "bullets/verb-power": {"bullets": ["Built a thing"]},
}

RESUME_OFFER_TOOL_STEPS = [
    ("r2", "jd/prescreen"),
    ("r3", "bullets/star-convert"),
    ("r4", "jd/keyword-plan"),
    ("r5", "versions/diff"),
    ("r6", "jd/offer-odds"),
    ("r7", "mock/voice-prep"),
]

INTERVIEW_TOOL_STEPS = [
    ("i2", "assessment/delta"),
    ("i3", "quizzes/weakness-drill"),
    ("i4", "dsa/pattern-mastery"),
    ("i5", "star/story-bank"),
    ("i6", "mock/company-intel"),
    ("i7", "coach/hr-round"),
    ("i8", "peermocks/score-rubric"),
]


def _authed(client, email="labphase1@example.com"):
    register(client, email=email)
    login(client, email=email)


def _run_ok(client, tool_id):
    resp = client.post("/api/ultimate/run/" + tool_id,
                       json=RUN_PAYLOADS[tool_id])
    assert resp.status_code == 200, (tool_id, resp.get_data(as_text=True)[:300])
    body = resp.get_json()
    assert body["ok"] and body.get("run_id")
    return body["run_id"]


def _complete_step(client, path, step, run_id=None):
    body = {} if run_id is None else {"run_id": run_id}
    return client.post("/api/ultimate/paths/%s/steps/%s/complete"
                       % (path, step), json=body)


def _xp_rows(app, email, event_type):
    from unified_app import User, XPLedger
    with app.app_context():
        user = User.query.filter_by(email=email).first()
        return XPLedger.query.filter_by(user_id=user.id,
                                        event_type=event_type).all()


def test_room_page_and_404(client):
    assert client.get("/ultimate/rooms/jd-matched-ats-scoring").status_code == 302
    _authed(client)
    resp = client.get("/ultimate/rooms/jd-matched-ats-scoring")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "JD-Matched ATS Scoring" in html
    assert "Guided workflow" in html
    assert "/ultimate/lab?tool=jd/prescreen" in html
    assert client.get("/ultimate/rooms/no-such-room").status_code == 404


def test_paths_list_and_detail(client):
    _authed(client)
    body = client.get("/api/ultimate/paths").get_json()
    assert body["ok"] and len(body["paths"]) == 3
    ids = [p["id"] for p in body["paths"]]
    assert ids == ["resume-offer", "placement-sprint", "interview-ready"]
    totals = {p["id"]: p["total"] for p in body["paths"]}
    assert totals == {"resume-offer": 7, "placement-sprint": 12,
                      "interview-ready": 8}
    detail = client.get("/api/ultimate/paths/resume-offer").get_json()["path"]
    assert len(detail["steps"]) == 7 and detail["done"] == 0
    assert detail["completed"] is False
    kinds = {s["id"]: s["kind"] for s in detail["steps"]}
    assert kinds["r1"] == "page" and kinds["r2"] == "tool"
    assert client.get("/api/ultimate/paths/nope").status_code == 404
    assert client.get("/api/ultimate/paths").status_code == 200


def test_tool_step_verification_and_xp(client, app):
    _authed(client)
    good_run = _run_ok(client, "bullets/star-convert")
    other_run = _run_ok(client, "jd/prescreen")
    # Wrong tool's run, missing run id, unknown step/path: all rejected.
    assert _complete_step(client, "resume-offer", "r3",
                          run_id=other_run).status_code == 400
    assert _complete_step(client, "resume-offer", "r3").status_code == 400
    assert _complete_step(client, "resume-offer", "zz").status_code == 404
    assert _complete_step(client, "nope", "r3").status_code == 404
    # Matching run: verified, 10 XP once.
    done = _complete_step(client, "resume-offer", "r3",
                          run_id=good_run).get_json()
    assert done["ok"] and done["xp_awarded"] is True
    assert len(_xp_rows(app, "labphase1@example.com", "lab_path_step")) == 1
    assert _xp_rows(app, "labphase1@example.com",
                    "lab_path_step")[0].xp == 10
    again = _complete_step(client, "resume-offer", "r3",
                           run_id=good_run).get_json()
    assert again["already"] is True and again["xp_awarded"] is False
    assert len(_xp_rows(app, "labphase1@example.com", "lab_path_step")) == 1
    # Page step: progress, but no XP.
    page = _complete_step(client, "resume-offer", "r1").get_json()
    assert page["ok"] and page["xp_awarded"] is False
    assert len(_xp_rows(app, "labphase1@example.com", "lab_path_step")) == 1


def test_resume_offer_finish_lands_tracker_row(client, app):
    _authed(client)
    early = client.post("/api/ultimate/paths/resume-offer/complete", json={})
    assert early.status_code == 400
    assert early.get_json()["missing"]
    _complete_step(client, "resume-offer", "r1")
    for step, tool in RESUME_OFFER_TOOL_STEPS:
        _complete_step(client, "resume-offer", step,
                       run_id=_run_ok(client, tool))
    finish = client.post("/api/ultimate/paths/resume-offer/complete",
                         json={"role": "Backend intern", "company": ""})
    body = finish.get_json()
    assert finish.status_code == 200, body
    assert body["completed"] is True and body["tracker_created"] is True
    assert body["application_id"]
    from unified_app import JobApplication, User
    with app.app_context():
        user = User.query.filter_by(email="labphase1@example.com").first()
        rows = JobApplication.query.filter_by(user_id=user.id).all()
        assert len(rows) == 1
        assert rows[0].stage == "wishlist" and rows[0].role == "Backend intern"
    assert len(_xp_rows(app, "labphase1@example.com",
                        "lab_path_complete")) == 1
    assert _xp_rows(app, "labphase1@example.com",
                    "lab_path_complete")[0].xp == 25
    # Re-finish: idempotent — no second tracker row, no second XP.
    again = client.post("/api/ultimate/paths/resume-offer/complete",
                        json={"role": "Other role"}).get_json()
    assert again["already"] is True and again["tracker_created"] is False
    with app.app_context():
        user = User.query.filter_by(email="labphase1@example.com").first()
        assert JobApplication.query.filter_by(user_id=user.id).count() == 1
    assert len(_xp_rows(app, "labphase1@example.com",
                        "lab_path_complete")) == 1


def test_interview_ready_milestone_finish(client, app):
    _authed(client)
    _complete_step(client, "interview-ready", "i1")
    for step, tool in INTERVIEW_TOOL_STEPS:
        _complete_step(client, "interview-ready", step,
                       run_id=_run_ok(client, tool))
    body = client.post("/api/ultimate/paths/interview-ready/complete",
                       json={}).get_json()
    assert body["completed"] is True and body["tracker_created"] is False
    assert any(link["href"] == "/mentors"
               for link in body["finish_links"])
    assert len(_xp_rows(app, "labphase1@example.com",
                        "lab_path_complete")) == 1


def test_sprint_partial_and_intent_search(client):
    _authed(client)
    detail = client.get("/api/ultimate/paths/placement-sprint").get_json()["path"]
    assert len(detail["steps"]) == 12
    done = _complete_step(client, "placement-sprint", "s1",
                          run_id=_run_ok(client, "bullets/verb-power"))
    assert done.get_json()["ok"] is True
    search = client.get("/api/ultimate/search",
                        query_string={"q": "improve my resume"}).get_json()
    assert search["ok"]
    assert search["intent"][0]["id"] == "bullets/star-convert"
    assert "jd/keyword-plan" in [t["id"] for t in search["intent"]]
    salary = client.get("/api/ultimate/search",
                        query_string={"q": "salary"}).get_json()
    assert "salary/evaluate" in [t["id"] for t in salary["intent"]]
    empty = client.get("/api/ultimate/search",
                       query_string={"q": "zzznomatch"}).get_json()
    assert empty["intent"] == [] and empty["matches"] == []


def test_path_ownership_and_anon(client):
    assert client.get("/api/ultimate/paths").status_code == 401
    assert client.get("/api/ultimate/search",
                      query_string={"q": "resume"}).status_code == 401
    _authed(client, email="labphase1a@example.com")
    run_id = _run_ok(client, "bullets/star-convert")
    _complete_step(client, "resume-offer", "r1")
    # Second user: fresh progress, and cannot use the first user's run.
    # (Registering while logged in does NOT switch accounts, so log
    # out first — the same rule the conftest helpers document.)
    client.post("/logout")
    _authed(client, email="labphase1b@example.com")
    body = client.get("/api/ultimate/paths").get_json()
    assert body["paths"][0]["done"] == 0
    assert _complete_step(client, "resume-offer", "r3",
                          run_id=run_id).status_code == 404


def test_hub_shows_paths_and_rooms(client):
    _authed(client)
    html = client.get("/ultimate/lab").get_data(as_text=True)
    assert "Guided paths" in html
    assert "Resume → Offer" in html
    assert "/ultimate/paths/resume-offer" in html
    assert "Feature rooms" in html
    assert "JD-Matched ATS Scoring" in html
    assert "/ultimate/rooms/jd-matched-ats-scoring" in html


def test_paths_flag_off_blocks_apis(client, monkeypatch):
    import dataclasses as _dc
    from studybuddy import app_config
    monkeypatch.setattr(app_config, "config",
                        _dc.replace(app_config.config, feature_labs=False))
    _authed(client)
    assert client.get("/api/ultimate/paths").status_code == 403
    assert client.get("/api/ultimate/search",
                      query_string={"q": "resume"}).status_code == 403
    assert _complete_step(client, "resume-offer", "r1").status_code == 403
