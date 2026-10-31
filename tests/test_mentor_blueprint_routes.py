"""Monolith split Phase 5 (2026-10-31): Mentor routes.

The /mentors/* and /api/mentors/* routes (mentor directory, office-hour
booking, Q&A, reviews, admin verify) moved verbatim from
unified_app.register_routes to studybuddy/mentor_routes.py. The gate:
* all 21 (rule, methods) pairs exist, unchanged;
* every mentor endpoint is blueprint-prefixed (mentors.*) and no unprefixed
  mentor endpoint remains (Flask blueprint semantics, same as the
  Phase 1–4 moves);
* the blueprint module has no TOP-LEVEL unified_app import (the hard
  rule — lazy imports inside functions only), so no import cycle.
"""

import ast

MENTOR_RULES = {
    ("/mentors", frozenset({"GET"})): "mentors.mentors_hub",
    ("/mentors/<int:mentor_id>", frozenset({"GET"})): "mentors.mentor_profile",
    ("/mentors/apply", frozenset({"GET"})): "mentors.mentor_apply_form",
    ("/mentors/api/apply", frozenset({"POST"})): "mentors.mentor_apply_submit",
    ("/mentors/api/office-hours/<int:oh_id>/book", frozenset({"POST"})): "mentors.mentor_book_session",
    ("/mentors/api/bookings/<int:booking_id>/cancel", frozenset({"POST"})): "mentors.mentor_cancel_booking",
    ("/mentors/api/bookings/<int:booking_id>/attend", frozenset({"POST"})): "mentors.mentor_mark_attended",
    ("/mentors/api/bookings/<int:booking_id>/note", frozenset({"POST"})): "mentors.mentor_save_note",
    ("/mentors/api/bookings/<int:booking_id>/feedback", frozenset({"POST"})): "mentors.mentor_submit_feedback",
    ("/mentors/api/my-bookings", frozenset({"GET"})): "mentors.mentor_my_bookings",
    ("/mentors/api/report", frozenset({"POST"})): "mentors.mentor_report",
    ("/mentors/api/admin/verify/<int:mentor_id>", frozenset({"POST"})): "mentors.mentor_admin_verify",
    ("/mentors/api/admin/office-hours", frozenset({"POST"})): "mentors.mentor_admin_create_session",
    ("/mentors/api/admin/applications/<int:application_id>/approve", frozenset({"POST"})): "mentors.mentor_admin_approve",
    ("/api/mentors/matches", frozenset({"GET"})): "mentors.mentors_matches_api",
    ("/api/mentors/qa", frozenset({"GET"})): "mentors.mentors_qa_api",
    ("/api/mentors/qa/ask", frozenset({"POST"})): "mentors.mentors_qa_ask_api",
    ("/api/mentors/qa/mine", frozenset({"GET"})): "mentors.mentors_qa_mine_api",
    ("/api/mentors/<int:mentor_id>/reviews", frozenset({"POST"})): "mentors.mentors_review_create_api",
    ("/api/mentors/<int:mentor_id>/reviews", frozenset({"GET"})): "mentors.mentors_review_list_api",
    ("/api/mentors/dashboard", frozenset({"GET"})): "mentors.mentors_dashboard_api",
}

_MENTOR_FUNCS = {
    "mentors_hub", "mentor_profile", "mentor_apply_form", "mentor_apply_submit",
    "mentor_book_session", "mentor_cancel_booking", "mentor_mark_attended",
    "mentor_save_note", "mentor_submit_feedback", "mentor_my_bookings",
    "mentor_report", "mentor_admin_verify", "mentor_admin_create_session",
    "mentor_admin_approve", "mentors_matches_api", "mentors_qa_api",
    "mentors_qa_ask_api", "mentors_qa_mine_api", "mentors_review_create_api",
    "mentors_review_list_api", "mentors_dashboard_api",
}


def _rule_map(app):
    out = {}
    for rule in app.url_map.iter_rules():
        methods = frozenset((rule.methods or set())
                            - {"HEAD", "OPTIONS"})
        out[(str(rule.rule), methods)] = rule.endpoint
    return out


def test_mentor_rules_present_with_blueprint_endpoints(app):
    rules = _rule_map(app)
    for (path, methods), endpoint in MENTOR_RULES.items():
        assert rules.get((path, methods)) == endpoint, \
            f"{path} {sorted(methods)}: got " \
            f"{rules.get((path, methods))}, want {endpoint}"


def test_no_unprefixed_mentor_endpoints_remain(app):
    endpoints = {r.endpoint for r in app.url_map.iter_rules()}
    leftovers = {e for e in endpoints
                 if e in _MENTOR_FUNCS}
    assert not leftovers, f"unprefixed mentor endpoints remain: {leftovers}"


def test_mentor_blueprint_has_no_top_level_unified_app_import():
    import pathlib
    src = pathlib.Path("studybuddy/mentor_routes.py").read_text()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "unified_app", \
                    "top-level 'import unified_app' in mentor_routes.py"
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "unified_app", \
                "top-level 'from unified_app import ...' in mentor_routes.py"
    # But the lazy imports must exist (models + career hook).
    assert "from unified_app import" in src
    assert "Mentor" in src


def test_mentor_pages_redirect_anonymous(client):
    # auth-gated pages redirect to login (302), never 404
    r = client.get("/mentors")
    assert r.status_code == 302
    r = client.get("/mentors/apply")
    assert r.status_code == 302
    r = client.get("/api/mentors/matches")
    assert r.status_code in (302, 401)
