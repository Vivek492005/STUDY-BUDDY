"""Tests for Wave 5 #29 — Rejection retrospectives.

Covers: content-bank sanity, funnel math, breakdowns, source heuristic,
trust-rule suggestions, wizard auth/isolation, wizard saves + upsert,
completion XP, experiment CRUD + status transitions, unauth redirects,
CSRF rejections, and invalid-input handling.
"""
import pytest

import studybuddy.retrospective_data as retro_data
from tests.conftest import login, register
from unified_app import (
    RETRO_PIPELINE,
    RETRO_SMALL_SAMPLE_N,
    Experiment,
    JobApplication,
    Retrospective,
    User,
    XPLedger,
    create_app,
    db,
    retro_funnel_stats,
    retro_rejection_stage_breakdown,
    retro_role_breakdown,
    retro_source_breakdown,
    retro_source_of,
    retro_suggestions,
)


@pytest.fixture()
def authed_client(client):
    register(client)
    login(client)
    return client


def _uid(app, email="aarav@example.com"):
    with app.app_context():
        return User.query.filter_by(email=email).first().id


def _fake_app(stage, role="Backend Engineer", jd_url=None, notes=None,
              company="Acme"):
    return JobApplication(user_id=1, company=company, role=role,
                          stage=stage, jd_url=jd_url, notes=notes)


def _make_app(app, uid, company="Acme", role="Backend Engineer",
              stage="applied", jd_url=None, notes=None):
    with app.app_context():
        row = JobApplication(user_id=uid, company=company, role=role,
                             stage=stage, jd_url=jd_url, notes=notes)
        db.session.add(row)
        db.session.commit()
        return row.id


# ---------------------------------------------------------------------------
# Content banks: sanity
# ---------------------------------------------------------------------------

def test_bank_starter_count():
    assert len(retro_data.HYPOTHESIS_STARTERS) == 40


def test_bank_template_count():
    assert len(retro_data.EXPERIMENT_TEMPLATES) == 30


def test_bank_ten_categories():
    assert len(retro_data.RETRO_CATEGORIES) == 10
    cats = {k for k, _ in retro_data.RETRO_CATEGORIES}
    assert all(s["category"] in cats for s in retro_data.HYPOTHESIS_STARTERS)
    assert all(t["category"] in cats for t in retro_data.EXPERIMENT_TEMPLATES)


def test_bank_four_starters_per_category():
    from collections import Counter
    counts = Counter(s["category"] for s in retro_data.HYPOTHESIS_STARTERS)
    assert all(v == 4 for v in counts.values()), counts


def test_bank_three_templates_per_category():
    from collections import Counter
    counts = Counter(t["category"] for t in retro_data.EXPERIMENT_TEMPLATES)
    assert all(v == 3 for v in counts.values()), counts


def test_bank_unique_ids():
    s_ids = [s["id"] for s in retro_data.HYPOTHESIS_STARTERS]
    t_ids = [t["id"] for t in retro_data.EXPERIMENT_TEMPLATES]
    assert len(set(s_ids)) == 40
    assert len(set(t_ids)) == 30


def test_bank_starters_phrased_as_hypotheses():
    for s in retro_data.HYPOTHESIS_STARTERS:
        assert s["text"].startswith(("Maybe", "Possibly")), s["id"]


def test_bank_templates_have_all_fields():
    for t in retro_data.EXPERIMENT_TEMPLATES:
        for field in ("hypothesis", "change", "target", "duration"):
            assert t[field] and t[field].strip(), (t["id"], field)


def test_bank_grouping_helpers():
    by_cat = retro_data.starters_by_category()
    assert len(by_cat) == 10
    assert len(by_cat["resume_relevance"]) == 4
    t_by_cat = retro_data.templates_by_category()
    assert len(t_by_cat["interview_prep"]) == 3
    assert retro_data.category_label("followup") == "Follow-up & persistence"
    assert retro_data.category_label("nope") == "nope"


# ---------------------------------------------------------------------------
# Funnel math
# ---------------------------------------------------------------------------

def _funnel_apps():
    # 3 wishlist, 4 applied, 2 oa, 1 interview, 0 offer, 2 rejected
    apps = []
    apps += [_fake_app("wishlist") for _ in range(3)]
    apps += [_fake_app("applied") for _ in range(4)]
    apps += [_fake_app("oa") for _ in range(2)]
    apps += [_fake_app("interview") for _ in range(1)]
    apps += [_fake_app("rejected") for _ in range(2)]
    return apps


def test_funnel_empty():
    f = retro_funnel_stats([])
    assert f["total"] == 0
    assert f["rejected"] == 0
    assert all(s["count"] == 0 and s["reached"] == 0 for s in f["stages"])
    assert all(c["rate"] is None for c in f["conversions"])


def test_funnel_counts_and_reached():
    f = retro_funnel_stats(_funnel_apps())
    assert f["total"] == 12
    assert f["rejected"] == 2
    by_id = {s["id"]: s for s in f["stages"]}
    assert by_id["wishlist"]["count"] == 3
    assert by_id["applied"]["count"] == 4
    assert by_id["wishlist"]["reached"] == 10   # every app passes wishlist
    assert by_id["applied"]["reached"] == 7     # 4 + 2 + 1
    assert by_id["oa"]["reached"] == 3          # 2 + 1
    assert by_id["interview"]["reached"] == 1
    assert by_id["offer"]["reached"] == 0


def test_funnel_conversion_rates():
    f = retro_funnel_stats(_funnel_apps())
    conv = {(c["from"], c["to"]): c for c in f["conversions"]}
    assert conv[("wishlist", "applied")]["rate"] == 70.0
    assert conv[("applied", "oa")]["rate"] == round(100.0 * 3 / 7, 1)
    assert conv[("oa", "interview")]["rate"] == round(100.0 * 1 / 3, 1)
    assert conv[("interview", "offer")]["rate"] == 0.0
    assert conv[("wishlist", "applied")]["numerator"] == 7
    assert conv[("wishlist", "applied")]["denominator"] == 10


def test_funnel_zero_denominator_is_none():
    f = retro_funnel_stats([_fake_app("rejected")])
    # nothing reached any pipeline stage
    assert all(c["rate"] is None for c in f["conversions"])
    assert f["rejected"] == 1


def test_funnel_unknown_stage_ignored():
    apps = [_fake_app("applied"), _fake_app("weird-stage")]
    f = retro_funnel_stats(apps)
    assert f["total"] == 2
    by_id = {s["id"]: s for s in f["stages"]}
    assert by_id["applied"]["count"] == 1


def test_funnel_stage_labels():
    f = retro_funnel_stats([])
    labels = {s["id"]: s["label"] for s in f["stages"]}
    assert labels["oa"] == "OA / Assignment"
    assert labels["wishlist"] == "Wishlist"
    assert [s["id"] for s in f["stages"]] == list(RETRO_PIPELINE)


# ---------------------------------------------------------------------------
# Role breakdown
# ---------------------------------------------------------------------------

def test_role_breakdown_grouping_case_insensitive():
    apps = ([_fake_app("applied", role="Backend Engineer") for _ in range(3)]
            + [_fake_app("rejected", role="backend engineer")]
            + [_fake_app("oa", role="Data Analyst") for _ in range(2)])
    rows = retro_role_breakdown(apps)
    assert len(rows) == 2
    be = rows[0]
    assert be["applications"] == 4
    assert be["rejected"] == 1
    assert be["rejection_rate"] == 25.0
    assert be["response_rate"] == 0.0
    da = rows[1]
    assert da["responded"] == 2
    assert da["response_rate"] == 100.0


def test_role_breakdown_sorted_by_volume():
    apps = ([_fake_app("applied", role="B") for _ in range(5)]
            + [_fake_app("applied", role="A") for _ in range(2)])
    rows = retro_role_breakdown(apps)
    assert rows[0]["role"] == "B"
    assert rows[1]["role"] == "A"


def test_role_breakdown_small_sample_flag():
    apps = [_fake_app("applied", role="Solo") for _ in range(4)]
    rows = retro_role_breakdown(apps)
    assert rows[0]["small_sample"] is True
    apps += [_fake_app("applied", role="Solo")]
    rows = retro_role_breakdown(apps)
    assert rows[0]["small_sample"] is False
    assert RETRO_SMALL_SAMPLE_N == 5


def test_role_breakdown_top_n():
    apps = [_fake_app("applied", role="Role-%d" % i) for i in range(12)]
    rows = retro_role_breakdown(apps, top_n=8)
    assert len(rows) == 8


def test_role_breakdown_blank_role():
    rows = retro_role_breakdown([_fake_app("applied", role="  ")])
    assert rows[0]["role"] == "(no role)"


# ---------------------------------------------------------------------------
# Source heuristic
# ---------------------------------------------------------------------------

def test_source_from_jd_url_domain():
    a = _fake_app("applied", jd_url="https://www.linkedin.com/jobs/view/123")
    assert retro_source_of(a) == "linkedin.com"


def test_source_from_jd_url_no_www():
    a = _fake_app("applied", jd_url="https://boards.greenhouse.io/acme/jobs/1")
    assert retro_source_of(a) == "boards.greenhouse.io"


def test_source_from_notes_keyword():
    a = _fake_app("applied", notes="Applied via LinkedIn referral from a friend")
    assert retro_source_of(a) == "linkedin"


def test_source_unknown_fallback():
    a = _fake_app("applied")
    assert retro_source_of(a) == "unknown"
    a2 = _fake_app("applied", jd_url="not a url", notes="nothing useful")
    assert retro_source_of(a2)  # never raises, always a string


def test_source_breakdown_flags_best_effort():
    apps = [_fake_app("applied", jd_url="https://x.example.com/j") for _ in range(3)]
    rows = retro_source_breakdown(apps)
    assert rows[0]["source"] == "x.example.com"
    assert rows[0]["best_effort"] is True
    assert rows[0]["small_sample"] is True


# ---------------------------------------------------------------------------
# Rejection-stage breakdown
# ---------------------------------------------------------------------------

def _fake_retro(stage_reached):
    return Retrospective(user_id=1, application_id=1, stage_reached=stage_reached)


def test_rejection_stage_breakdown_grouping():
    retros = [_fake_retro("interview"), _fake_retro("interview"),
              _fake_retro("oa"), _fake_retro(None), _fake_retro("bogus")]
    b = retro_rejection_stage_breakdown(retros)
    rows = {r["stage"]: r for r in b["rows"]}
    assert rows["interview"]["rejections"] == 2
    assert rows["oa"]["rejections"] == 1
    assert rows["applied"]["rejections"] == 0
    assert b["unknown"] == 2
    assert b["total"] == 5


def test_rejection_stage_breakdown_small_sample():
    b = retro_rejection_stage_breakdown([_fake_retro("offer")])
    rows = {r["stage"]: r for r in b["rows"]}
    assert rows["offer"]["small_sample"] is True


# ---------------------------------------------------------------------------
# Trust-rule suggestions
# ---------------------------------------------------------------------------

def _suggest_inputs():
    funnel = retro_funnel_stats(_funnel_apps())
    apps = _funnel_apps() + [_fake_app("rejected", role="Backend Engineer")
                             for _ in range(3)]
    roles = retro_role_breakdown(apps)
    rej = retro_rejection_stage_breakdown(
        [_fake_retro("interview") for _ in range(3)] + [_fake_retro("oa")])
    sources = retro_source_breakdown(
        [_fake_app("applied", jd_url="https://a.example.com") for _ in range(3)]
        + [_fake_app("rejected", jd_url="https://b.example.com") for _ in range(3)])
    return funnel, roles, rej, sources


def test_suggestions_empty_data():
    cards = retro_suggestions(retro_funnel_stats([]), [], 
                              retro_rejection_stage_breakdown([]), [])
    assert cards == []


def test_suggestions_all_cards_have_three_labels():
    cards = retro_suggestions(*_suggest_inputs())
    assert len(cards) >= 3
    for c in cards:
        assert c["observed_pattern"] and isinstance(c["observed_pattern"], str)
        assert c["plausible_cause"] and isinstance(c["plausible_cause"], str)
        assert c["recommended_test"] and isinstance(c["recommended_test"], str)
        assert "sample_size" in c and "small_sample" in c


def test_suggestions_never_state_facts():
    cards = retro_suggestions(*_suggest_inputs())
    for c in cards:
        assert "Hypothesis" in c["plausible_cause"] or "hypothesis" in c["plausible_cause"].lower()


def test_suggestions_weakest_conversion_first():
    cards = retro_suggestions(*_suggest_inputs())
    first = cards[0]
    # oa -> interview at 33.3% is weakest among conversions with n >= 2
    assert "33.3%" in first["observed_pattern"]
    assert "OA / Assignment" in first["observed_pattern"]


def test_suggestions_role_card():
    cards = retro_suggestions(*_suggest_inputs())
    role_cards = [c for c in cards if "rejection rate" in c["observed_pattern"]]
    assert role_cards
    assert "Backend Engineer" in role_cards[0]["observed_pattern"]


def test_suggestions_cluster_card():
    cards = retro_suggestions(*_suggest_inputs())
    cluster = [c for c in cards if "reaching the" in c["observed_pattern"]]
    assert cluster
    assert "Interview" in cluster[0]["observed_pattern"]


def test_suggestions_source_card_best_effort():
    cards = retro_suggestions(*_suggest_inputs())
    src = [c for c in cards if "best-effort" in c["observed_pattern"]]
    assert src


# ---------------------------------------------------------------------------
# Auth: unauthenticated users get 302 redirects
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,url", [
    ("get", "/retrospectives"),
    ("get", "/retrospectives/new/1"),
    ("get", "/retrospectives/api/funnel"),
    ("get", "/retrospectives/api/experiments"),
    ("post", "/retrospectives/api/save"),
    ("post", "/retrospectives/api/complete/1"),
    ("post", "/retrospectives/api/experiments"),
    ("post", "/retrospectives/api/experiments/1/result"),
])
def test_unauth_redirects(client, method, url):
    r = getattr(client, method)(url, json={} if method == "post" else None)
    assert r.status_code == 302, (method, url)


# ---------------------------------------------------------------------------
# Hub page
# ---------------------------------------------------------------------------

def test_hub_renders(authed_client, app):
    uid = _uid(app)
    _make_app(app, uid, company="Acme", role="Backend Engineer", stage="rejected",
              jd_url="https://jobs.example.com/1")
    r = authed_client.get("/retrospectives")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Rejection Retrospectives" in html
    assert "Observed pattern" in html or "Suggestions" in html
    assert "/retrospectives" in html  # nav link present
    assert "csrf-token" in html


def test_hub_empty_state(authed_client):
    r = authed_client.get("/retrospectives")
    assert r.status_code == 200
    assert "No applications tracked yet" in r.get_data(as_text=True)


def test_hub_shows_suggestion_trust_labels(authed_client, app):
    uid = _uid(app)
    for i in range(3):
        _make_app(app, uid, company="C%d" % i, role="Backend Engineer",
                  stage="rejected")
        _make_app(app, uid, company="D%d" % i, role="Backend Engineer",
                  stage="applied")
    r = authed_client.get("/retrospectives")
    html = r.get_data(as_text=True)
    assert "Observed pattern" in html
    assert "Plausible cause (hypothesis)" in html
    assert "Recommended test" in html


# ---------------------------------------------------------------------------
# Wizard page
# ---------------------------------------------------------------------------

def test_wizard_renders_for_own_application(authed_client, app):
    uid = _uid(app)
    aid = _make_app(app, uid, company="Acme", role="Backend Engineer",
                    stage="rejected")
    r = authed_client.get("/retrospectives/new/%d" % aid)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Acme" in html
    assert "Maybe my resume" in html  # hypothesis starter rendered
    assert "One controlled change" in html  # experiment template rendered
    assert "csrf-token" in html


def test_wizard_404_for_other_users_application(authed_client, app):
    uid = _uid(app)
    aid = _make_app(app, uid, stage="rejected")
    authed_client.post("/logout", follow_redirects=False)
    register(authed_client, name="Second", email="second@example.com")
    login(authed_client, email="second@example.com")
    r = authed_client.get("/retrospectives/new/%d" % aid)
    assert r.status_code == 404


def test_wizard_404_for_missing_application(authed_client):
    r = authed_client.get("/retrospectives/new/999999")
    assert r.status_code == 404


def test_wizard_prefills_saved_facts(authed_client, app):
    uid = _uid(app)
    aid = _make_app(app, uid, stage="rejected")
    authed_client.post("/retrospectives/api/save", json={
        "application_id": aid, "step": "facts",
        "facts": "Got the OA, then silence.",
        "timeline": "Applied 2 Oct",
        "stage_reached": "oa"})
    r = authed_client.get("/retrospectives/new/%d" % aid)
    html = r.get_data(as_text=True)
    assert "Got the OA, then silence." in html
    assert "Applied 2 Oct" in html


# ---------------------------------------------------------------------------
# Wizard saves (upsert)
# ---------------------------------------------------------------------------

def _save(client, aid, step, **kw):
    body = {"application_id": aid, "step": step}
    body.update(kw)
    return client.post("/retrospectives/api/save", json=body)


def _get_retro(app, aid):
    with app.app_context():
        return Retrospective.query.filter_by(application_id=aid).first()


def test_save_facts_upserts(authed_client, app):
    uid = _uid(app)
    aid = _make_app(app, uid, stage="rejected")
    r = _save(authed_client, aid, "facts", facts="The facts.",
              timeline="Applied 1 Oct", stage_reached="interview")
    assert r.status_code == 200
    assert r.get_json()["success"] is True
    retro = _get_retro(app, aid)
    assert retro.stage_reached == "interview"
    assert retro.facts.startswith("Timeline: Applied 1 Oct")
    assert "The facts." in retro.facts
    # second save upserts the same row
    first_id = retro.id
    _save(authed_client, aid, "facts", facts="Updated facts.")
    retro2 = _get_retro(app, aid)
    assert retro2.id == first_id
    assert "Updated facts." in retro2.facts


def test_save_patterns_and_hypotheses(authed_client, app):
    uid = _uid(app)
    aid = _make_app(app, uid, stage="rejected")
    assert _save(authed_client, aid, "patterns",
                 observed_patterns="Third OA-stage rejection.").status_code == 200
    assert _save(authed_client, aid, "hypotheses",
                 hypotheses="Maybe my timed practice is thin.").status_code == 200
    retro = _get_retro(app, aid)
    assert retro.observed_patterns == "Third OA-stage rejection."
    assert retro.hypotheses == "Maybe my timed practice is thin."


def test_save_experiment_step_creates_experiment(authed_client, app):
    uid = _uid(app)
    aid = _make_app(app, uid, stage="rejected")
    r = _save(authed_client, aid, "experiment", experiment={
        "hypothesis": "Maybe my answers ramble.",
        "change": "Practice 5 STAR answers.",
        "target": "Structure score >= 7 on next 3 interviews.",
        "duration": "3 weeks"})
    assert r.status_code == 200
    with app.app_context():
        exps = Experiment.query.filter_by(user_id=uid).all()
        assert len(exps) == 1
        assert exps[0].status == "planned"
        assert exps[0].linked_application_id == aid
        assert exps[0].duration == "3 weeks"


def test_save_invalid_step(authed_client, app):
    uid = _uid(app)
    aid = _make_app(app, uid, stage="rejected")
    r = _save(authed_client, aid, "bogus")
    assert r.status_code == 400


def test_save_invalid_stage_reached(authed_client, app):
    uid = _uid(app)
    aid = _make_app(app, uid, stage="rejected")
    r = _save(authed_client, aid, "facts", facts="x", stage_reached="nope")
    assert r.status_code == 400


def test_save_other_users_application_404(authed_client, app):
    uid = _uid(app)
    aid = _make_app(app, uid, stage="rejected")
    authed_client.post("/logout", follow_redirects=False)
    register(authed_client, name="Second", email="second@example.com")
    login(authed_client, email="second@example.com")
    r = _save(authed_client, aid, "facts", facts="hijack")
    assert r.status_code == 404


def test_save_missing_experiment_fields(authed_client, app):
    uid = _uid(app)
    aid = _make_app(app, uid, stage="rejected")
    r = _save(authed_client, aid, "experiment",
              experiment={"hypothesis": "Maybe…", "change": "", "target": ""})
    assert r.status_code == 400


def test_save_non_json_body(authed_client, app):
    uid = _uid(app)
    _make_app(app, uid, stage="rejected")
    r = authed_client.post("/retrospectives/api/save", data="not json",
                           content_type="text/plain")
    assert r.status_code == 400


def test_save_bad_application_id(authed_client):
    r = authed_client.post("/retrospectives/api/save",
                           json={"application_id": "abc", "step": "facts"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Completion awards XP
# ---------------------------------------------------------------------------

def _ledger(app, uid, event_type):
    with app.app_context():
        return XPLedger.query.filter_by(user_id=uid,
                                        event_type=event_type).all()


def test_complete_awards_xp_once(authed_client, app):
    uid = _uid(app)
    aid = _make_app(app, uid, stage="rejected")
    r = authed_client.post("/retrospectives/api/complete/%d" % aid, json={})
    assert r.status_code == 200
    data = r.get_json()
    assert data["success"] is True
    assert data["first_completion"] is True
    assert data["xp"] == 15
    rows = _ledger(app, uid, "retrospective_logged")
    assert len(rows) == 1
    assert rows[0].xp == 15
    with app.app_context():
        retro = Retrospective.query.filter_by(application_id=aid).first()
        assert retro.completed is True
        assert retro.completed_at is not None


def test_complete_is_idempotent(authed_client, app):
    uid = _uid(app)
    aid = _make_app(app, uid, stage="rejected")
    authed_client.post("/retrospectives/api/complete/%d" % aid, json={})
    r = authed_client.post("/retrospectives/api/complete/%d" % aid, json={})
    data = r.get_json()
    assert data["first_completion"] is False
    assert data["xp"] == 0
    # momentum dedupe: still exactly one XP row for the day
    assert len(_ledger(app, uid, "retrospective_logged")) == 1


def test_complete_other_users_application_404(authed_client, app):
    uid = _uid(app)
    aid = _make_app(app, uid, stage="rejected")
    authed_client.post("/logout", follow_redirects=False)
    register(authed_client, name="Second", email="second@example.com")
    login(authed_client, email="second@example.com")
    r = authed_client.post("/retrospectives/api/complete/%d" % aid, json={})
    assert r.status_code == 404


def test_complete_missing_application_404(authed_client):
    r = authed_client.post("/retrospectives/api/complete/999999", json={})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Experiments: CRUD + status transitions
# ---------------------------------------------------------------------------

def _create_exp(client, **kw):
    body = {"hypothesis": "Maybe my resume buries outcomes.",
            "change": "Lead bullets with numbers.",
            "target": "2 responses on next 10 apps.",
            "duration": "3 weeks"}
    body.update(kw)
    return client.post("/retrospectives/api/experiments", json=body)


def test_experiment_create_201(authed_client, app):
    uid = _uid(app)
    aid = _make_app(app, uid, stage="rejected")
    r = _create_exp(authed_client, linked_application_id=aid)
    assert r.status_code == 201
    data = r.get_json()["experiment"]
    assert data["status"] == "planned"
    assert data["linked_application_id"] == aid
    assert data["linked_company"] == "Acme"


def test_experiment_create_without_link(authed_client):
    r = _create_exp(authed_client)
    assert r.status_code == 201
    assert r.get_json()["experiment"]["linked_application_id"] is None


def test_experiment_create_validation(authed_client):
    r = authed_client.post("/retrospectives/api/experiments",
                           json={"hypothesis": "Maybe…", "change": "", "target": ""})
    assert r.status_code == 400
    r = authed_client.post("/retrospectives/api/experiments",
                           json={"hypothesis": "x" * 2001, "change": "y",
                                 "target": "z"})
    assert r.status_code == 400


def test_experiment_create_other_users_link_404(authed_client, app):
    uid = _uid(app)
    aid = _make_app(app, uid, stage="rejected")
    authed_client.post("/logout", follow_redirects=False)
    register(authed_client, name="Second", email="second@example.com")
    login(authed_client, email="second@example.com")
    r = _create_exp(authed_client, linked_application_id=aid)
    assert r.status_code == 404


def test_experiment_list(authed_client):
    _create_exp(authed_client)
    _create_exp(authed_client, hypothesis="Maybe timing matters.")
    r = authed_client.get("/retrospectives/api/experiments")
    assert r.status_code == 200
    items = r.get_json()["experiments"]
    assert len(items) == 2
    assert items[0]["hypothesis"] == "Maybe timing matters."  # newest first


def _set_result(client, exp_id, status, result=""):
    return client.post("/retrospectives/api/experiments/%d/result" % exp_id,
                       json={"status": status, "result": result})


def test_experiment_status_planned_to_running(authed_client):
    exp_id = _create_exp(authed_client).get_json()["experiment"]["id"]
    r = _set_result(authed_client, exp_id, "running")
    assert r.status_code == 200
    assert r.get_json()["experiment"]["status"] == "running"


def test_experiment_status_running_to_done_with_result(authed_client):
    exp_id = _create_exp(authed_client).get_json()["experiment"]["id"]
    _set_result(authed_client, exp_id, "running")
    r = _set_result(authed_client, exp_id, "done",
                    result="Response rate rose from 10% to 30%.")
    assert r.status_code == 200
    data = r.get_json()["experiment"]
    assert data["status"] == "done"
    assert "30%" in data["result"]


def test_experiment_status_planned_to_done_allowed(authed_client):
    exp_id = _create_exp(authed_client).get_json()["experiment"]["id"]
    r = _set_result(authed_client, exp_id, "done", result="Skipped the run.")
    assert r.status_code == 200


def test_experiment_status_backward_transition_rejected(authed_client):
    exp_id = _create_exp(authed_client).get_json()["experiment"]["id"]
    _set_result(authed_client, exp_id, "running")
    _set_result(authed_client, exp_id, "done")
    r = _set_result(authed_client, exp_id, "running")
    assert r.status_code == 400
    assert "Cannot move" in r.get_json()["error"]


def test_experiment_status_invalid(authed_client):
    exp_id = _create_exp(authed_client).get_json()["experiment"]["id"]
    r = _set_result(authed_client, exp_id, "archived")
    assert r.status_code == 400


def test_experiment_result_too_long(authed_client):
    exp_id = _create_exp(authed_client).get_json()["experiment"]["id"]
    r = _set_result(authed_client, exp_id, "done", result="x" * 5001)
    assert r.status_code == 400


def test_experiment_result_other_users_404(authed_client, app):
    exp_id = _create_exp(authed_client).get_json()["experiment"]["id"]
    authed_client.post("/logout", follow_redirects=False)
    register(authed_client, name="Second", email="second@example.com")
    login(authed_client, email="second@example.com")
    r = _set_result(authed_client, exp_id, "running")
    assert r.status_code == 404


def test_experiment_result_missing_404(authed_client):
    r = _set_result(authed_client, 999999, "running")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Funnel API
# ---------------------------------------------------------------------------

def test_funnel_api(authed_client, app):
    uid = _uid(app)
    _make_app(app, uid, stage="applied")
    _make_app(app, uid, stage="rejected")
    r = authed_client.get("/retrospectives/api/funnel")
    assert r.status_code == 200
    funnel = r.get_json()["funnel"]
    assert funnel["total"] == 2
    assert funnel["rejected"] == 1
    assert len(funnel["stages"]) == 5


# ---------------------------------------------------------------------------
# CSRF: POSTs without a token are rejected (400) on a CSRF-enabled app
# ---------------------------------------------------------------------------

@pytest.fixture()
def csrf_app():
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SECRET_KEY": "retro-csrf-test",
        "WTF_CSRF_ENABLED": True,
    })
    with app.app_context():
        db.create_all()
    yield app


def test_csrf_rejected_on_retro_posts(csrf_app):
    # CSRFProtect rejects token-less POSTs (400) before auth is even
    # checked — no login needed to exercise the rejection.
    client = csrf_app.test_client()
    cases = [
        "/retrospectives/api/save",
        "/retrospectives/api/complete/1",
        "/retrospectives/api/experiments",
        "/retrospectives/api/experiments/1/result",
    ]
    bodies = [
        {"application_id": 1, "step": "facts"},
        {},
        {"hypothesis": "h", "change": "c", "target": "t"},
        {"status": "running"},
    ]
    for url, body in zip(cases, bodies, strict=False):
        r = client.post(url, json=body)
        assert r.status_code == 400, url


def test_csrf_rejected_on_complete(csrf_app):
    client = csrf_app.test_client()
    r = client.post("/retrospectives/api/complete/123", json={})
    assert r.status_code == 400
