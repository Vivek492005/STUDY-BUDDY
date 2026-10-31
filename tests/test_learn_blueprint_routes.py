"""Monolith split Phase 3 (2026-10-31): Learn routes.

The /learn/* routes (learning hub, skill roadmaps, skill quizzes,
certification guides) moved verbatim from unified_app.register_routes to
studybuddy/learn_routes.py. The gate:
* all 20 (rule, methods) pairs exist, unchanged;
* every learn endpoint is blueprint-prefixed (learn.*) and no unprefixed
  learn endpoint remains (Flask blueprint semantics, same as the Phase 1
  study_library and Phase 2 DSA moves);
* the blueprint module has no TOP-LEVEL unified_app import (the hard
  rule — lazy imports inside functions only), so no import cycle.
"""

import ast

LEARN_RULES = {
    ("/learn", frozenset({"GET"})): "learn.learn_hub",
    ("/learn/roadmaps", frozenset({"GET"})): "learn.roadmaps_home",
    ("/learn/roadmaps/<skill_id>", frozenset({"GET"})): "learn.roadmap_detail",
    ("/learn/roadmaps/gap", frozenset({"POST"})): "learn.roadmaps_gap",
    ("/learn/roadmaps/progress", frozenset({"POST"})): "learn.roadmaps_progress",
    ("/learn/roadmaps/dashboard", frozenset({"GET"})): "learn.roadmaps_dashboard",
    ("/learn/quizzes", frozenset({"GET"})): "learn.quizzes_home",
    ("/learn/quizzes/start", frozenset({"POST"})): "learn.quizzes_start",
    ("/learn/quizzes/<int:attempt_id>/check", frozenset({"POST"})): "learn.quizzes_check",
    ("/learn/quizzes/<int:attempt_id>/submit", frozenset({"POST"})): "learn.quizzes_submit",
    ("/learn/quizzes/<int:attempt_id>", frozenset({"GET"})): "learn.view_quiz_result",
    ("/learn/quizzes/history", frozenset({"GET"})): "learn.quizzes_history",
    ("/learn/quizzes/mistakes", frozenset({"GET"})): "learn.quizzes_mistakes",
    ("/learn/quizzes/drill", frozenset({"POST"})): "learn.quizzes_drill",
    ("/learn/certs", frozenset({"GET"})): "learn.certs_home",
    ("/learn/certs/browse", frozenset({"GET"})): "learn.certs_browse",
    ("/learn/certs/<cert_id>", frozenset({"GET"})): "learn.cert_detail",
    ("/learn/certs/recommend", frozenset({"POST"})): "learn.certs_recommend",
    ("/learn/certs/plan", frozenset({"POST"})): "learn.certs_plan",
    ("/learn/certs/plans", frozenset({"GET"})): "learn.certs_plans",
}

_LEARN_FUNCS = {
    "learn_hub", "roadmaps_home", "roadmap_detail", "roadmaps_gap",
    "roadmaps_progress", "roadmaps_dashboard", "quizzes_home", "quizzes_start",
    "quizzes_check", "quizzes_submit", "view_quiz_result", "quizzes_history",
    "quizzes_mistakes", "quizzes_drill", "certs_home", "certs_browse",
    "cert_detail", "certs_recommend", "certs_plan", "certs_plans",
}


def _rule_map(app):
    out = {}
    for rule in app.url_map.iter_rules():
        methods = frozenset((rule.methods or set())
                            - {"HEAD", "OPTIONS"})
        out[(str(rule.rule), methods)] = rule.endpoint
    return out


def test_learn_rules_present_with_blueprint_endpoints(app):
    rules = _rule_map(app)
    for (path, methods), endpoint in LEARN_RULES.items():
        assert rules.get((path, methods)) == endpoint, \
            f"{path} {sorted(methods)}: got " \
            f"{rules.get((path, methods))}, want {endpoint}"


def test_no_unprefixed_learn_endpoints_remain(app):
    endpoints = {r.endpoint for r in app.url_map.iter_rules()}
    leftovers = {e for e in endpoints
                 if e in _LEARN_FUNCS}
    assert not leftovers, f"unprefixed learn endpoints remain: {leftovers}"


def test_learn_blueprint_has_no_top_level_unified_app_import():
    import pathlib
    src = pathlib.Path("studybuddy/learn_routes.py").read_text()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "unified_app", \
                    "top-level 'import unified_app' in learn_routes.py"
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "unified_app", \
                "top-level 'from unified_app import ...' in learn_routes.py"
    # But the lazy imports must exist (models + shared helpers).
    assert "from unified_app import" in src
    assert "RoadmapProgress" in src


def test_learn_pages_render(client):
    r = client.get("/learn")
    assert r.status_code == 200
    r = client.get("/learn/roadmaps")
    assert r.status_code == 200
    r = client.get("/learn/quizzes")
    assert r.status_code == 200
    r = client.get("/learn/certs")
    assert r.status_code == 200
