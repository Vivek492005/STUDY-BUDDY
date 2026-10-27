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
                "enum", "lines", "object", "json")
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
