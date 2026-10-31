"""Monolith split Phase 4 (2026-10-31): Career tracker routes.

The /tracker/*, /api/tracker/*, /api/career/* and /assessment/* routes
moved verbatim from unified_app.register_routes to
studybuddy/career_routes.py. The gate:
* all 23 (rule, methods) pairs exist, unchanged;
* every career endpoint is blueprint-prefixed (career.*) and no unprefixed
  career endpoint remains (Flask blueprint semantics, same as the
  Phase 1/2/3 moves);
* the blueprint module has no TOP-LEVEL unified_app import (the hard
  rule — lazy imports inside functions only), so no import cycle.
"""

import ast

CAREER_RULES = {
    ("/tracker", frozenset({"GET"})): "career.tracker_board",
    ("/tracker/add", frozenset({"POST"})): "career.tracker_add",
    ("/tracker/<int:app_id>/move", frozenset({"POST"})): "career.tracker_move",
    ("/tracker/<int:app_id>/edit", frozenset({"POST"})): "career.tracker_edit",
    ("/tracker/<int:app_id>/delete", frozenset({"POST"})): "career.tracker_delete",
    ("/tracker/stats", frozenset({"GET"})): "career.tracker_stats",
    ("/api/tracker/<int:app_id>/kit", frozenset({"GET"})): "career.tracker_kit",
    ("/api/tracker/offers/compare", frozenset({"POST"})): "career.tracker_offers_compare",
    ("/api/tracker/referrals", frozenset({"GET"})): "career.tracker_referrals_list",
    ("/api/tracker/referrals", frozenset({"POST"})): "career.tracker_referrals_add",
    ("/api/tracker/referrals/<int:ref_id>", frozenset({"PATCH"})): "career.tracker_referrals_patch",
    ("/api/tracker/referrals/<int:ref_id>", frozenset({"DELETE"})): "career.tracker_referrals_delete",
    ("/api/tracker/<int:app_id>/timeline", frozenset({"GET"})): "career.tracker_timeline",
    ("/api/tracker/digest", frozenset({"GET"})): "career.tracker_digest",
    ("/api/tracker/import", frozenset({"POST"})): "career.tracker_import",
    ("/api/career/snapshot", frozenset({"GET"})): "career.career_snapshot_api",
    ("/api/career/freshness", frozenset({"GET"})): "career.career_freshness_api",
    ("/api/career/deadlines", frozenset({"GET"})): "career.career_deadlines_api",
    ("/tracker/<int:app_id>/deadline", frozenset({"POST"})): "career.tracker_set_deadline",
    ("/assessment", frozenset({"GET"})): "career.assessment_quiz",
    ("/assessment/submit", frozenset({"POST"})): "career.assessment_submit",
    ("/assessment/<int:assessment_id>", frozenset({"GET"})): "career.view_assessment",
    ("/api/career/assessment/history", frozenset({"GET"})): "career.assessment_history",
}

_CAREER_FUNCS = {
    "tracker_board", "tracker_add", "tracker_move", "tracker_edit",
    "tracker_delete", "tracker_stats", "tracker_kit", "tracker_offers_compare",
    "tracker_referrals_list", "tracker_referrals_add", "tracker_referrals_patch",
    "tracker_referrals_delete", "tracker_timeline", "tracker_digest",
    "tracker_import", "career_snapshot_api", "career_freshness_api",
    "career_deadlines_api", "tracker_set_deadline", "assessment_quiz",
    "assessment_submit", "view_assessment", "assessment_history",
}


def _rule_map(app):
    out = {}
    for rule in app.url_map.iter_rules():
        methods = frozenset((rule.methods or set())
                            - {"HEAD", "OPTIONS"})
        out[(str(rule.rule), methods)] = rule.endpoint
    return out


def test_career_rules_present_with_blueprint_endpoints(app):
    rules = _rule_map(app)
    for (path, methods), endpoint in CAREER_RULES.items():
        assert rules.get((path, methods)) == endpoint, \
            f"{path} {sorted(methods)}: got " \
            f"{rules.get((path, methods))}, want {endpoint}"


def test_no_unprefixed_career_endpoints_remain(app):
    endpoints = {r.endpoint for r in app.url_map.iter_rules()}
    leftovers = {e for e in endpoints
                 if e in _CAREER_FUNCS}
    assert not leftovers, f"unprefixed career endpoints remain: {leftovers}"


def test_career_blueprint_has_no_top_level_unified_app_import():
    import pathlib
    src = pathlib.Path("studybuddy/career_routes.py").read_text()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "unified_app", \
                    "top-level 'import unified_app' in career_routes.py"
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "unified_app", \
                "top-level 'from unified_app import ...' in career_routes.py"
    # But the lazy imports must exist (models + constants + helpers).
    assert "from unified_app import" in src
    assert "JobApplication" in src
    assert "TRACKER_STAGES" in src


def test_career_pages_render(client):
    # anonymous: auth-gated pages redirect to login (302), never 404
    r = client.get("/tracker")
    assert r.status_code == 302
    r = client.get("/assessment")
    assert r.status_code == 302
    r = client.get("/api/career/freshness")
    assert r.status_code == 200
