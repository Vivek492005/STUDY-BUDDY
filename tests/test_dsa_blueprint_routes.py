"""Monolith split Phase 2 (audit fix, Stage 2, 2026-10-28): DSA routes.

The /dsa/* + /api/dsa/* routes moved verbatim from
unified_app.register_routes to studybuddy/dsa_routes.py. The gate:
* all 12 (rule, methods) pairs exist, unchanged;
* every DSA endpoint is blueprint-prefixed (dsa.*) and no unprefixed
  dsa_* endpoint remains (Flask blueprint semantics, same as the
  Phase 1 study_library move);
* the blueprint module has no TOP-LEVEL unified_app import (the hard
  rule — lazy imports inside functions only), so no import cycle.
"""

import ast

DSA_RULES = {
    ("/dsa", frozenset({"GET"})): "dsa.dsa_home",
    ("/api/dsa/problems", frozenset({"GET"})): "dsa.dsa_problem_list",
    ("/api/dsa/progress", frozenset({"GET"})): "dsa.dsa_progress_api",
    ("/dsa/diagnosis", frozenset({"GET"})): "dsa.dsa_diagnosis",
    ("/dsa/a2z", frozenset({"GET"})): "dsa.dsa_a2z_page",
    ("/dsa/a2z/<problem_id>/learn", frozenset({"GET"})): "dsa.dsa_a2z_learn",
    ("/dsa/tracker", frozenset({"GET"})): "dsa.dsa_tracker_page",
    ("/dsa/<problem_id>/untrack", frozenset({"POST"})): "dsa.dsa_untrack",
    ("/dsa/<problem_id>", frozenset({"GET"})): "dsa.dsa_problem_page",
    ("/dsa/<problem_id>/hint", frozenset({"POST"})): "dsa.dsa_hint",
    ("/dsa/<problem_id>/approach", frozenset({"POST"})): "dsa.dsa_approach",
    ("/dsa/<problem_id>/status", frozenset({"POST"})): "dsa.dsa_status",
}


def _rule_map(app):
    out = {}
    for rule in app.url_map.iter_rules():
        methods = frozenset((rule.methods or set())
                            - {"HEAD", "OPTIONS"})
        out[(str(rule.rule), methods)] = rule.endpoint
    return out


def test_dsa_rules_present_with_blueprint_endpoints(app):
    rules = _rule_map(app)
    for (path, methods), endpoint in DSA_RULES.items():
        assert rules.get((path, methods)) == endpoint, \
            f"{path} {sorted(methods)}: got " \
            f"{rules.get((path, methods))}, want {endpoint}"


def test_no_unprefixed_dsa_endpoints_remain(app):
    endpoints = {r.endpoint for r in app.url_map.iter_rules()}
    leftovers = {e for e in endpoints
                 if e.startswith("dsa_") and not e.startswith("dsa.")}
    assert not leftovers, f"unprefixed DSA endpoints remain: {leftovers}"


def test_dsa_blueprint_has_no_top_level_unified_app_import():
    import pathlib
    src = pathlib.Path("studybuddy/dsa_routes.py").read_text()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "unified_app", \
                    "top-level 'import unified_app' in dsa_routes.py"
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "unified_app", \
                "top-level 'from unified_app import ...' in dsa_routes.py"
    # But the lazy imports must exist (models + momentum hook).
    assert "from unified_app import DSAProgress" in src
    assert "from unified_app import _momentum_hook" in src


def test_dsa_pages_render(client):
    r = client.get("/dsa")
    assert r.status_code == 200
    r = client.get("/api/dsa/problems")
    assert r.status_code == 200
    assert r.is_json and "problems" in r.get_json()
    r = client.get("/api/dsa/progress")
    assert r.status_code == 200
    assert r.get_json()["is_authed"] is False
