"""P1 Labs UX: one generic structured-form renderer for Ultimate Lab.

Before this change every tool field marked json=True (54 of 91 tools)
was a bare textarea with a "must be valid JSON" hint. The server now
ships a form schema per tool in /api/ultimate/tools, derived from the
tool's pydantic input model (migrated tools) or its legacy field defs
(the rest, with shape hints parsed from the label text); the Lab page
renders typed controls from that schema instead of raw JSON textareas.

These tests pin the contract the renderer depends on:
  * every tool carries a form, and no legacy field is dropped;
  * migrated tools' form fields match their pydantic models exactly
    (same payload keys, so validation behaviour cannot change);
  * form-shaped payloads actually run through the HTTP run endpoint —
    one schema tool (object field), one schema tool (list field) and
    one legacy JSON tool (no schema, guided editor with field hints);
  * invalid JSON-object input still fails with a clean field-level
    error, never an opaque "Tool failed".
"""
from tests.conftest import login, register  # noqa: F401

from studybuddy.labs_registry import TOOL_HANDLERS
from studybuddy.ultimate_routes import ALL_TOOLS, form_schema

REPORT = {
    "score": 72,
    "matched_skills": ["python", "flask"],
    "matched_keywords": ["python"],
    "missing_keywords": ["docker", "ci/cd"],
}

TEXT = (
    "I built a REST API in Python with Flask and PostgreSQL serving "
    "500 users daily and reduced response time by 40 percent."
)


def _authed(client):
    register(client)
    login(client)


def _tools_by_id(client):
    _authed(client)
    resp = client.get("/api/ultimate/tools")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] and body["count"] == 91
    return {t["id"]: t for t in body["tools"]}


def test_tools_api_carries_form_for_all_91_and_drops_no_field(client):
    tools = _tools_by_id(client)
    assert len(tools) == 91
    for tool in ALL_TOOLS:
        form = tools[tool["id"]]["form"]
        assert isinstance(form["fields"], list)
        form_names = [f["name"] for f in form["fields"]]
        # No legacy field may disappear in the schema conversion.
        for legacy in tool.get("fields", []):
            assert legacy["name"] in form_names
        # Every form field carries the renderer's contract keys.
        for f in form["fields"]:
            assert f["kind"] in (
                "string", "text", "integer", "number", "boolean",
                "enum", "lines", "object", "json",
                # Eval 3: structured controls replacing raw-JSON textareas.
                "rows", "pairs", "reviewmap")
            assert isinstance(f["required"], bool)


def test_migrated_forms_match_pydantic_models_exactly(client):
    tools = _tools_by_id(client)
    for tool_id, handler in TOOL_HANDLERS.items():
        if tool_id not in tools:
            # Typed-only tools (aptitude pick/score) are not in the
            # 91-tool grid by design; the renderer covers grid tools.
            continue
        form = tools[tool_id]["form"]
        assert form["source"] == "schema"
        assert sorted(f["name"] for f in form["fields"]) == sorted(
            handler.input_model.model_fields.keys())
        by_name = {f["name"]: f for f in form["fields"]}
        for name, finfo in handler.input_model.model_fields.items():
            assert by_name[name]["required"] == bool(finfo.is_required())


def test_form_schema_object_field_has_keys_and_example():
    # Lab Phase 0: portfolio/case-study is typed now; the object keys +
    # example survive the migration (source "schema", not "legacy").
    tool = next(t for t in ALL_TOOLS if t["id"] == "portfolio/case-study")
    form = form_schema(tool)
    assert form["source"] == "schema"
    field = form["fields"][0]
    assert field["kind"] == "object"
    assert field["keys"] == ["title", "description", "tech",
                             "role", "link", "demo_link"]
    assert '"title"' in field["example"]


def test_form_submit_schema_tool_with_object_field(client):
    _authed(client)
    resp = client.post("/api/ultimate/run/jd/prescreen", json={
        "resume_text": TEXT, "report": REPORT})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    body = resp.get_json()
    assert body["ok"] and "score" in body["result"]


def test_form_submit_schema_tool_with_list_field(client):
    _authed(client)
    # What the "lines" control collects: one bullet per line -> array.
    resp = client.post("/api/ultimate/run/bullets/verb-power", json={
        "bullets": ["Built a Flask API serving 500 users daily",
                    "Fixed bugs"]})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    assert resp.get_json()["ok"]


def test_form_submit_legacy_json_tool_with_guided_editor_payload(client):
    _authed(client)
    # What the guided editor produces for portfolio/case-study: the
    # example skeleton filled in, sent as a parsed object (no JSON text).
    resp = client.post("/api/ultimate/run/portfolio/case-study", json={
        "project": {"title": "Study-Buddy", "description": "AI copilot",
                    "tech": ["python", "flask"], "role": "Builder",
                    "link": "https://example.dev",
                    "demo_link": ""}})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    assert resp.get_json()["ok"]


def test_form_submit_legacy_json_array_tool(client):
    _authed(client)
    resp = client.post("/api/ultimate/run/versions/performance", json={
        "applications": [{"version_id": "v1", "version_name": "A",
                          "stage": "applied"}]})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    assert resp.get_json()["ok"]


def test_form_submit_bad_object_field_fails_cleanly(client):
    _authed(client)
    resp = client.post("/api/ultimate/run/jd/prescreen", json={
        "resume_text": TEXT, "report": "not json at all"})
    assert resp.status_code == 400
    body = resp.get_json()
    assert not body["ok"] and "report" in body["error"]
    assert "JSON object" in body["error"]
    assert not body["error"].startswith("Tool failed")


def test_lab_page_ships_structured_form_renderer(client):
    _authed(client)
    resp = client.get("/ultimate/lab")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="lab-grid"' in html
    # Renderer markers: guided-editor example button + validity note.
    assert "Insert example" in html
    assert "lab-json-note" in html
    assert "legacyToForm" in html


# ---------------------------------------------------------------------------
# Eval 3: structured-form payloads run end-to-end.
#
# These payloads are exactly what the new renderer composes from its typed
# controls (per-key inputs, repeatable rows, (name, value) pairs) — no
# hand-written JSON. Each asserts the tool's real output.
# ---------------------------------------------------------------------------

def _run(client, tool_id, payload):
    _authed(client)
    resp = client.post("/api/ultimate/run/%s" % tool_id, json=payload)
    assert resp.status_code == 200, resp.get_data(as_text=True)[:400]
    body = resp.get_json()
    assert body["ok"], body.get("error")
    return body["result"]


def test_eval3_rows_number_coercion_deadline_alerts(client):
    result = _run(client, "opportunities/deadline-alerts", {
        "opportunities": [
            {"title": "Hackathon", "organization": "Unstop", "days_left": 1},
            {"title": "Internship", "organization": "Acme", "days_left": 5},
        ]})
    assert result["critical"] and result["upcoming"]


def test_eval3_object_fieldset_fit_score(client):
    result = _run(client, "opportunities/fit-score", {
        "opportunity": {"title": "Backend Intern",
                        "eligibility": "python flask",
                        "type": "internship"},
        "skills": ["python", "flask"]})
    assert result["score"] > 0 and "verdict" in result


def test_eval3_pairs_action_nudges(client):
    result = _run(client, "health/action-nudges", {
        "current_scores": {"applications": 2, "learning": 50},
        "previous_scores": {"applications": 30}})
    assert "nudges" in result and result["nudges"]


def test_eval3_rows_exam_sprint(client):
    result = _run(client, "flashcards/exam-sprint", {
        "cards": [{"id": "c1", "question": "What is X?", "answer": "Y"}],
        "seconds_per_card": 20})
    assert result["rounds"] and result["rounds"][0]["question"] == "What is X?"


def test_eval3_rows_kvdict_weak_card_loop(client):
    result = _run(client, "flashcards/weak-card-loop", {
        "card_stats": {"c1": {"wrong": 3, "right": 7}},
        "cards": [{"id": "c1", "question": "Q?", "answer": "A"}]})
    assert result["queue"] and result["queue"][0]["id"] == "c1"


def test_eval3_rows_weekly_planner(client):
    result = _run(client, "roadmaps/weekly-planner", {
        "roadmap_stages": [{"stage": "Python", "weeks": 2,
                            "project": "CLI tool"}],
        "hours_per_week": 8})
    assert result["weeks"] and result["weeks"][0]["focus"] == "Python"


def test_eval3_object_verify_citations(client):
    result = _run(client, "pdfqa/verify-citations", {
        "answer_dict": {"answer": 'She said "hello world today" [1].'}})
    assert result["claims"] == 1 and result["ok"] is True


def test_eval3_lines_feasibility(client):
    result = _run(client, "gaps/feasibility", {
        "gap_text": "Students struggle to find good project ideas for resumes",
        "skills": ["python", "flask"]})
    assert "score" in result and "verdict" in result


def test_eval3_rows_personalized_feed(client):
    result = _run(client, "trending/personalized-feed", {
        "papers": [{"title": "Attention is all you need",
                    "abstract": "Transformers for nlp",
                    "link": "https://example.dev"}],
        "skills": ["nlp"], "limit": 5})
    assert result["total"] == 1


def test_eval3_object_bool_video_presence(client):
    result = _run(client, "coach/video-presence", {
        "self_scores": {"eye_contact": True, "framing": True,
                        "background": False, "posture": False,
                        "gestures": False, "audio": False,
                        "attire": False}})
    assert result["score"] == 35 and len(result["checks"]) == 7


def test_eval3_object_numbers_score_rubric(client):
    result = _run(client, "peermocks/score-rubric", {
        "scores": {"clarity": 4, "depth": 3}})
    assert result["total"] == 3.5 and result["average"] == 3.5


def test_eval3_reviewmap_monthly_report(client):
    result = _run(client, "health/monthly-report", {
        "review": {"month": "October 2026",
                   "dimensions": {"learning": {"score": 70,
                                               "evidence": "finished module 3"},
                                  "networking": {"score": 20,
                                                 "evidence": "no outreach"}}}})
    assert "highlights" in result and "concerns" in result


def test_eval3_pairs_and_kvlist_peer_benchmarks(client):
    result = _run(client, "health/peer-benchmarks", {
        "my_scores": {"learning": 70},
        "peer_scores": [{"learning": 60}, {"learning": 80}]})
    assert result["benchmarks"] and result["benchmarks"][0]["percentile"] == 50.0


def test_eval3_rows_bool_weakness_drill(client):
    result = _run(client, "quizzes/weakness-drill", {
        "skill": "dsa", "n": 5,
        "past_results": [{"topic": "knapsack", "correct": True},
                         {"topic": "lis", "correct": False}]})
    assert result["target_topics"] and result["questions"]


def test_eval3_nested_rows_recruiter_preview(client):
    result = _run(client, "portfolio/recruiter-preview", {
        "portfolio": {"display_name": "Aarav", "headline": "Backend dev",
                      "skills": ["python", "flask"],
                      "projects": [{"title": "SB", "description": "AI copilot",
                                    "demo_link": "https://example.dev"}]}})
    assert result["score"] > 0 and result["scan"]


def test_eval3_normalized_keys_project_milestones(client):
    result = _run(client, "roadmaps/project-milestones", {
        "roadmap_stages": [{"stage": "Python", "weeks": 3}]})
    assert result["stages"][0]["stage"] == "Python"


def test_eval3_rows_lines_subinput_answer_across_docs(client):
    result = _run(client, "pdfqa/answer-across-docs", {
        "query": "What is attention?",
        "docs": [{"title": "Paper",
                  "chunks": ["attention is all you need",
                             "transformers use attention"]}]})
    assert "answer" in result


def test_eval3_report_guided_json_and_integer_offer_odds(client):
    result = _run(client, "jd/offer-odds", {
        "report": {"score": 72, "matched_skills": ["python"],
                   "missing_skills": ["docker"],
                   "missing_keywords": ["ci/cd"]},
        "applicants_estimate": 150})
    assert "checklist" in result


def test_eval3_form_schema_curated_shapes(client):
    tools = _tools_by_id(client)

    def by_name(tid):
        return {f["name"]: f for f in tools[tid]["form"]["fields"]}

    assert by_name("gaps/feasibility")["skills"]["kind"] == "lines"
    assert by_name("jd/offer-odds")["applicants_estimate"]["kind"] == "integer"
    assert by_name("health/action-nudges")["current_scores"]["kind"] == "pairs"
    assert by_name("health/monthly-report")["review"]["kind"] == "reviewmap"
    assert by_name("flashcards/weak-card-loop")["card_stats"]["kind"] == "rows"
    assert by_name("flashcards/weak-card-loop")["card_stats"]["compose"] == "kvdict"
    assert by_name("roadmaps/weekly-planner")["roadmap_stages"]["kind"] == "rows"
    assert by_name("coach/video-presence")["self_scores"]["keys"] == [
        "eye_contact", "framing", "background", "posture",
        "gestures", "audio", "attire"]
    assert by_name("peermocks/score-rubric")["scores"]["keys"] == [
        "clarity", "depth", "communication", "problem_solving", "behavioral"]
    # No tool still needs raw JSON except the jd ATS-report paste fields.
    raw = [(tid, f["name"]) for tid, t in tools.items()
           for f in t["form"]["fields"]
           if f["kind"] in ("object", "json") and not f.get("keys")]
    assert sorted(raw) == [
        ("jd/keyword-plan", "report"),
        ("jd/offer-odds", "report"),
        ("jd/prescreen", "report")]
    for tid, name in raw:
        f = by_name(tid)[name]
        assert f["example"] and f["hint"]


def test_lab_page_ships_eval3_controls(client):
    _authed(client)
    resp = client.get("/ultimate/lab")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    for marker in ("labRowsEditor", "labObjectEditor", "labPairsEditor",
                   "labReviewEditor", "lab-row", "lab-fieldset",
                   "labComposeRows"):
        assert marker in html, marker
