"""Phase 2 direction: discovery expansion + salary expansion."""

import json

from studybuddy.career_assessment import CLUSTERS, QUESTIONS
from studybuddy.role_cards import role_cards_for_clusters
from studybuddy.salary_data import city_query, load_dataset
from tests.conftest import login, register


def _authed(client, email="ph2@example.com", name="Phase Two"):
    register(client, email=email, name=name)
    login(client, email=email)


def _submit(client):
    answers = {q["id"]: 0 for q in QUESTIONS}
    resp = client.post("/assessment/submit",
                       data=json.dumps({"answers": answers}),
                       content_type="application/json",
                       headers={"X-CSRFToken": "test"})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    return resp.get_json()


# ---------- discovery bank ----------

def test_bank_counts_and_cluster_coverage():
    assert len(QUESTIONS) == 28
    assert len(CLUSTERS) == 14
    for cid in ("devops", "mobile", "qa_testing", "embedded",
                "game_dev", "data_engineering"):
        assert cid in CLUSTERS
    for cid in CLUSTERS:
        earning = 0
        for q in QUESTIONS:
            if any(o.get("weights", {}).get(cid, 0) > 0
                   for o in q["options"]):
                earning += 1
        assert earning >= 2, cid


def test_role_cards_total_and_shape():
    cards = role_cards_for_clusters(list(CLUSTERS.keys()))
    total = sum(len(v) for v in cards.values())
    assert total >= 60, total
    for _cid, lst in cards.items():
        assert len(lst) <= 12
        for card in lst:
            assert card["title"] and card["description"]
            assert "salary" not in card and "company" not in card


def test_submit_enriches_handoffs(client):
    _authed(client)
    body = _submit(client)
    assert body["result"]["total_questions"] == 28
    clusters = body["result"]["clusters"]
    assert len(clusters) == 5
    for c in clusters:
        assert isinstance(c["role_cards"], list) and c["role_cards"]
        assert c["roadmap_url"] is None or \
            c["roadmap_url"].startswith("/learn/roadmaps/")
        assert c["quiz_url"] is None or c["quiz_url"] == "/learn/quizzes"


def test_history_newest_first_and_auth(client):
    _authed(client)
    _submit(client)
    _submit(client)
    resp = client.get("/api/career/assessment/history")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["count"] == 2 and len(body["attempts"]) == 2
    for a in body["attempts"]:
        assert a["top_clusters"] and a["top_cluster_names"]
        assert a["completed_at"]
    assert client.application.test_client().get(
        "/api/career/assessment/history").status_code == 401


def test_saved_result_page_embeds_handoffs(client):
    _authed(client)
    body = _submit(client)
    aid = body["assessment_id"]
    html = client.get(f"/assessment/{aid}").get_data(as_text=True)
    assert "__ASSESSMENT_RESULT__" in html
    assert "asHistory" in html
    assert body["result"]["clusters"][0]["role_cards"][0]["title"] in html


# ---------- tracker wishlist relaxation ----------

def test_tracker_blank_company_wishlist_only(client):
    _authed(client)
    ok = client.post("/tracker/add",
                     data=json.dumps({"company": "", "role": "SDE",
                                      "stage": "wishlist"}),
                     content_type="application/json",
                     headers={"X-CSRFToken": "test"})
    assert ok.status_code == 201
    bad_stage = client.post("/tracker/add",
                            data=json.dumps({"company": "", "role": "SDE",
                                             "stage": "applied"}),
                            content_type="application/json",
                            headers={"X-CSRFToken": "test"})
    assert bad_stage.status_code == 400
    bad_role = client.post("/tracker/add",
                           data=json.dumps({"company": "", "role": "",
                                            "stage": "wishlist"}),
                           content_type="application/json",
                           headers={"X-CSRFToken": "test"})
    assert bad_role.status_code == 400


# ---------- salary dataset ----------

def test_salary_dataset_counts_and_monotonicity():
    data = load_dataset()
    assert len(data["entries"]) == 408 and len(data["roles"]) == 34
    assert len(city_query()) == 60
    by_key = {(e["role"], e["tier"], e["experience_band"]): e
              for e in data["entries"]}
    bands = ["0-1", "1-3", "3-5", "5+"]
    for role in data["roles"]:
        for tier in ("tier1", "tier2", "tier3"):
            meds = [by_key[(role, tier, b)]["median_lpa"] for b in bands]
            assert meds == sorted(meds), (role, tier, meds)
            for b in bands:
                e = by_key[(role, tier, b)]
                assert e["min_lpa"] <= e["median_lpa"] <= e["max_lpa"]
        for b in bands:
            t1 = by_key[(role, "tier1", b)]["median_lpa"]
            t2 = by_key[(role, "tier2", b)]["median_lpa"]
            t3 = by_key[(role, "tier3", b)]["median_lpa"]
            assert t1 >= t2 >= t3, (role, b)


def test_salary_page_city_table(client):
    html = client.get("/salary").get_data(as_text=True)
    assert "City comparison" in html
    assert "Show all cities (60)" in html
    assert "Mysuru" in html


# ---------- salary APIs ----------

def test_curve_shape_and_errors(client):
    resp = client.get("/api/salary/curve", query_string={
        "role": "Software Development Engineer", "tier": "tier1"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert [c["band"] for c in body["curve"]] == ["0-1", "1-3", "3-5", "5+"]
    meds = [c["median_lpa"] for c in body["curve"]]
    assert meds == sorted(meds) and body["disclaimer"]
    assert client.get("/api/salary/curve", query_string={
        "role": "Astronaut", "tier": "tier1"}).status_code == 400
    assert client.get("/api/salary/curve", query_string={
        "role": "Software Development Engineer",
        "tier": "tier9"}).status_code == 400


def test_evaluate_bands_city_and_scripts(client):
    _authed(client)
    base = "/api/salary/evaluate"
    payload = {"role": "Software Development Engineer", "tier": "tier1",
               "band": "0-1"}
    below = client.post(base, json={**payload, "offer_lpa": 2})
    assert "below" in below.get_json()["evaluation"]["percentile_note"]
    within = client.post(base, json={**payload, "offer_lpa": 8})
    assert "within" in within.get_json()["evaluation"]["percentile_note"]
    above = client.post(base, json={**payload, "offer_lpa": 20})
    assert "above" in above.get_json()["evaluation"]["percentile_note"]
    city = client.post(base, json={**payload, "offer_lpa": 8,
                                   "city": "Mumbai"})
    ev = city.get_json()["evaluation"]
    assert ev["city_adjusted"] is not None
    assert ev["city_adjusted"]["multiplier"] > 0
    assert len(ev["suggested_scripts"]) == 3
    gotham = client.post(base, json={**payload, "offer_lpa": 8,
                                     "city": "Gotham"})
    assert gotham.get_json()["evaluation"]["city_adjusted"] is None
    assert client.post(base, json={**payload,
                                   "offer_lpa": -1}).status_code == 400
    assert client.post(base, json={**payload,
                                   "offer_lpa": True}).status_code == 400
    assert client.post(base, json={**payload, "offer_lpa": 8,
                                   "band": "99-100"}).status_code == 400
    assert client.application.test_client().post(
        base, json={**payload, "offer_lpa": 8}).status_code == 401


def test_scripts_all_filtered_unknown(client):
    all_r = client.get("/api/salary/scripts")
    assert len(all_r.get_json()["scripts"]) == 15
    filt = client.get("/api/salary/scripts",
                      query_string={"situation": "below-band"})
    assert len(filt.get_json()["scripts"]) == 3
    unknown = client.get("/api/salary/scripts",
                         query_string={"situation": "nope"})
    assert unknown.status_code == 200
    assert unknown.get_json()["scripts"] == []
