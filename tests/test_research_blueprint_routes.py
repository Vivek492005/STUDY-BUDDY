"""Monolith split Phase 6 (2026-11-08): Research routes.

The /research/* pages, /api/study/* import + collections APIs, and
/api/cite/* citation APIs moved verbatim from unified_app.register_routes
to studybuddy/research_routes.py. The gate:
* all 35 (rule, methods) pairs exist, unchanged;
* every research endpoint is blueprint-prefixed (research.*) and no
  unprefixed research endpoint remains (Flask blueprint semantics, same
  as the Phase 1 study_library, Phase 2 DSA and Phase 3 learn moves);
* the blueprint module has no TOP-LEVEL unified_app import (the hard
  rule — lazy imports inside functions only), so no import cycle.
"""

import ast

RESEARCH_RULES = {
    ("/research", frozenset({"GET"})): "research.research_home",
    ("/research/import", frozenset({"GET"})): "research.study_import_page",
    ("/api/study/import/arxiv", frozenset({"POST"})): "research.study_import_arxiv",
    ("/api/study/import/doi", frozenset({"POST"})): "research.study_import_doi",
    ("/api/study/import/<path:openalex_id>/graph", frozenset({"GET"})): "research.study_import_graph",
    ("/api/study/import/pdf", frozenset({"POST"})): "research.study_import_pdf",
    ("/api/study/import/search", frozenset({"POST"})): "research.study_import_search",
    ("/api/study/import/full", frozenset({"POST"})): "research.study_import_full",
    ("/api/study/import/<int:paper_id>", frozenset({"DELETE"})): "research.study_import_delete",
    ("/api/study/import/queue", frozenset({"POST"})): "research.study_import_queue",
    ("/api/study/import/citations", frozenset({"GET"})): "research.study_import_citations",
    ("/api/study/collections", frozenset({"POST"})): "research.study_collection_create",
    ("/api/study/collections", frozenset({"GET"})): "research.study_collection_list",
    ("/api/study/collections/<token>/papers", frozenset({"POST"})): "research.study_collection_set_papers",
    ("/research/collections/<token>", frozenset({"GET"})): "research.study_collection_public",
    ("/research/cite", frozenset({"GET"})): "research.cite_page",
    ("/research/trending", frozenset({"GET"})): "research.trending_page",
    ("/research/review", frozenset({"GET"})): "research.litreview_home",
    ("/research/api/review/generate", frozenset({"POST"})): "research.litreview_generate",
    ("/research/review/<int:rid>", frozenset({"GET"})): "research.litreview_view",
    ("/research/review/<int:rid>/export", frozenset({"GET"})): "research.litreview_export",
    ("/research/api/review/<int:rid>", frozenset({"DELETE"})): "research.litreview_delete",
    ("/research/gaps", frozenset({"GET"})): "research.gapfinder_home",
    ("/research/api/gaps/analyze", frozenset({"POST"})): "research.gaps_analyze",
    ("/research/api/gaps/<int:gid>", frozenset({"GET"})): "research.gaps_get",
    ("/research/flashcards", frozenset({"GET"})): "research.flashcards_home",
    ("/research/api/flashcards/generate", frozenset({"POST"})): "research.flashcards_generate",
    ("/research/flashcards/<int:sid>", frozenset({"GET"})): "research.flashcards_review",
    ("/research/api/flashcards/<int:sid>/progress", frozenset({"POST"})): "research.flashcards_progress",
    ("/research/api/flashcards/<int:sid>/quiz", frozenset({"POST"})): "research.flashcards_quiz",
    ("/research/api/flashcards/<int:sid>/stats", frozenset({"GET"})): "research.flashcards_stats",
    ("/api/cite/format", frozenset({"POST"})): "research.cite_format",
    ("/api/cite/bibtex/export", frozenset({"POST"})): "research.cite_bibtex_export",
    ("/api/cite/bibtex/import", frozenset({"POST"})): "research.cite_bibtex_import",
    ("/api/cite/guide/<style>", frozenset({"GET"})): "research.cite_guide",
}

_RESEARCH_FUNCS = {
    "research_home", "study_import_page", "study_import_arxiv",
    "study_import_doi", "study_import_graph", "study_import_pdf",
    "study_import_search", "study_import_full", "study_import_delete",
    "study_import_queue", "study_import_citations",
    "study_collection_create", "study_collection_list",
    "study_collection_set_papers", "study_collection_public",
    "cite_page", "trending_page", "litreview_home",
    "litreview_generate", "litreview_view", "litreview_export",
    "litreview_delete", "gapfinder_home", "gaps_analyze", "gaps_get",
    "flashcards_home", "flashcards_generate", "flashcards_review",
    "flashcards_progress", "flashcards_quiz", "flashcards_stats",
    "cite_format", "cite_bibtex_export", "cite_bibtex_import",
    "cite_guide",
}


def _rule_map(app):
    out = {}
    for rule in app.url_map.iter_rules():
        methods = frozenset((rule.methods or set())
                            - {"HEAD", "OPTIONS"})
        out[(str(rule.rule), methods)] = rule.endpoint
    return out


def test_research_rules_present_with_blueprint_endpoints(app):
    rules = _rule_map(app)
    for (path, methods), endpoint in RESEARCH_RULES.items():
        assert rules.get((path, methods)) == endpoint, \
            f"{path} {sorted(methods)}: got " \
            f"{rules.get((path, methods))}, want {endpoint}"


def test_no_unprefixed_research_endpoints_remain(app):
    endpoints = {r.endpoint for r in app.url_map.iter_rules()}
    leftovers = {e for e in endpoints
                 if e in _RESEARCH_FUNCS}
    assert not leftovers, f"unprefixed research endpoints remain: {leftovers}"


def test_research_blueprint_has_no_top_level_unified_app_import():
    import pathlib
    src = pathlib.Path("studybuddy/research_routes.py").read_text()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "unified_app", \
                    "top-level 'import unified_app' in research_routes.py"
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "unified_app", \
                "top-level 'from unified_app import ...' in research_routes.py"
    # But the lazy imports must exist (models + shared helpers).
    assert "from unified_app import" in src
    assert "PaperImport" in src


def test_research_pages_render(client):
    r = client.get("/research")
    assert r.status_code == 200
    r = client.get("/research/cite")
    assert r.status_code == 200
    r = client.get("/research/trending")
    assert r.status_code == 200
    # flashcards_home is @login_required: anonymous users redirect to login.
    r = client.get("/research/flashcards")
    assert r.status_code == 302
