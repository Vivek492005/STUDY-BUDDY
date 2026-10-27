"""WS4 ARC workstream tests: ARC-01 (app package + auth blueprint),
ARC-05 (typed labs registry), ARC-10 (complexity refactor equivalence),
ARC-11 (background jobs).
"""
import io
import json
import os
import time

import pytest
from flask import url_for

FIXTURES = json.load(
    open(os.path.join(os.path.dirname(__file__), "fixtures",
                      "arc10_characterization.json"))
)


# ===========================================================================
# ARC-01: env-validated config
# ===========================================================================
class TestConfig:
    def test_resolve_testing_override(self):
        from app.config import TestingConfig, resolve_config
        assert resolve_config({"TESTING": True}) is TestingConfig

    def test_resolve_production_env(self, monkeypatch):
        from app.config import ProductionConfig, resolve_config
        monkeypatch.setenv("FLASK_ENV", "production")
        assert resolve_config({}) is ProductionConfig

    def test_resolve_development_default(self, monkeypatch):
        from app.config import DevelopmentConfig, resolve_config
        monkeypatch.delenv("FLASK_ENV", raising=False)
        assert resolve_config({}) is DevelopmentConfig

    def test_production_rejects_missing_secret(self, monkeypatch):
        from app.config import ProductionConfig
        monkeypatch.delenv("SECRET_KEY", raising=False)
        with pytest.raises(RuntimeError, match="SECRET_KEY"):
            ProductionConfig.validate()

    def test_production_rejects_placeholder_secret(self, monkeypatch):
        from app.config import ProductionConfig
        monkeypatch.setenv("SECRET_KEY", "change-me-to-a-long-random-secret")
        with pytest.raises(RuntimeError):
            ProductionConfig.validate()

    def test_production_accepts_real_secret(self, monkeypatch):
        from app.config import ProductionConfig
        monkeypatch.setenv("SECRET_KEY", "a" * 64)
        ProductionConfig.validate()  # must not raise

    def test_extensions_importable_without_monolith(self):
        from app.extensions import csrf, db, limiter, login_manager, oauth
        assert db is not None and limiter is not None
        assert login_manager is not None and csrf is not None
        assert oauth is not None


# ===========================================================================
# ARC-01: auth blueprint — URL identity
# ===========================================================================
class TestAuthBlueprint:
    def test_blueprint_registered(self, app):
        assert "auth" in app.blueprints

    def test_auth_urls_identical(self, app):
        rules = {r.rule: r.endpoint for r in app.url_map.iter_rules()}
        assert rules["/register"] == "auth.register"
        assert rules["/login"] == "auth.login"
        assert rules["/logout"] == "auth.logout"
        methods = {r.rule: r.methods for r in app.url_map.iter_rules()}
        assert methods["/register"] >= {"GET", "POST"}
        assert methods["/login"] >= {"GET", "POST"}

    def test_url_for_builds_identical_paths(self, app):
        with app.test_request_context():
            assert url_for("auth.login") == "/login"
            assert url_for("auth.register") == "/register"
            assert url_for("auth.logout") == "/logout"

    def test_login_view_points_at_blueprint(self, app):
        from app.extensions import login_manager
        assert login_manager.login_view == "auth.login"

    def test_register_login_logout_flow(self, client):
        # register
        r = client.post("/register", data={
            "name": "Arc Test", "email": "arc01@example.com",
            "password": "Str0ng!Pass123", "age_confirm": "on",
        })
        assert r.status_code == 302 and r.headers["Location"] == "/dashboard"
        # logout must be POST
        assert client.get("/logout").status_code == 405
        r = client.post("/logout")
        assert r.status_code == 302
        # login
        r = client.post("/login", data={
            "email": "arc01@example.com", "password": "Str0ng!Pass123"})
        assert r.status_code == 302 and r.headers["Location"] == "/dashboard"
        # bad password still rejected
        client.post("/logout")
        r = client.post("/login", data={
            "email": "arc01@example.com", "password": "wrong"})
        assert b"Invalid email or password" in r.data


# ===========================================================================
# ARC-05: typed labs registry
# ===========================================================================
class TestLabsRegistry:
    def test_tools_migrated_count(self):
        from studybuddy.labs_registry import migrated_tool_ids
        # Lab Phase 0 completed the migration: 91 grid tools + 2
        # typed-only aptitude tools (not in the grid by design).
        assert len(migrated_tool_ids()) == 93

    def test_allowlist_rejects_unknown_id(self):
        from studybuddy.labs_registry import run_typed_tool
        result, error = run_typed_tool("nope/evil'; DROP TABLE", {})
        assert result is None and "Unknown tool" in error

    def test_promoted_id_dispatches_through_allowlist(self):
        # Lab Phase 0 promoted every legacy id: a formerly-legacy id now
        # dispatches (empty payload fails validation, never "Unknown tool").
        from studybuddy.labs_registry import is_migrated, run_typed_tool
        assert is_migrated("arxiv/bulk-import")
        result, error = run_typed_tool("arxiv/bulk-import", {})
        assert result is None and error is not None
        assert "Unknown tool" not in error

    def test_input_validation_errors(self):
        from studybuddy.labs_registry import run_typed_tool
        result, error = run_typed_tool("bullets/star-convert", {})
        assert result is None and "bullet_text" in error

    def test_typed_result_matches_legacy_dispatcher(self):
        from studybuddy.labs_registry import run_typed_tool
        from studybuddy.ultimate_routes import run_tool
        payload = {"bullet_text": "Built a Flask API used by 200 students"}
        new_res, new_err = run_typed_tool("bullets/star-convert", payload)
        old_res, old_err = run_tool("bullets/star-convert", payload)
        assert new_err is None
        assert new_res == old_res

    def test_no_dynamic_dispatch_in_registry(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "studybuddy",
                                "labs_registry.py")).read()
        # no dynamic module/function resolution anywhere in the module
        # (the word may appear in prose/docs, but never as code)
        code_lines = [ln for ln in src.splitlines()
                      if ln.strip() and not ln.strip().startswith(("#", '"""', "*"))]
        code = "\n".join(code_lines)
        assert "import importlib" not in code
        assert "getattr(" not in code
        assert "__import__" not in code
        # every handler maps to a directly-imported function (allowlist)
        from studybuddy import labs_registry as lr
        assert len(lr.TOOL_HANDLERS) == 93
        for handler in lr.TOOL_HANDLERS.values():
            assert callable(handler.fn)
            assert handler.meta.requires_login is True
            assert handler.meta.rate_limit

    def test_inventory_csv_marks_all_migrated(self):
        import csv
        rows = list(csv.DictReader(open(os.path.join(
            os.path.dirname(__file__), "..", "docs", "labs-inventory.csv"))))
        assert len(rows) == 91
        migrated = [r for r in rows if "migrated" in r["owner"]
                    and "legacy" not in r["owner"]]
        legacy = [r for r in rows if "legacy-unmigrated" in r["owner"]]
        # Lab Phase 0 completed the migration: no legacy rows remain.
        assert len(migrated) == 91 and len(legacy) == 0


# ===========================================================================
# ARC-11: background jobs
# ===========================================================================
class TestJobs:
    def test_lifecycle_done(self):
        from studybuddy.jobs import JobManager
        mgr = JobManager(max_workers=1)
        try:
            jid = mgr.submit(lambda x: x * 2, 21, name="double")
            info = mgr.status(jid)
            assert info["state"] in ("pending", "running", "done")
            deadline = time.time() + 5
            while mgr.status(jid)["state"] not in ("done", "error") \
                    and time.time() < deadline:
                time.sleep(0.01)
            info = mgr.status(jid)
            assert info["state"] == "done"
            assert info["result"] == 42
            assert mgr.result(jid) == 42
        finally:
            mgr.shutdown()

    def test_error_state(self):
        from studybuddy.jobs import JobManager
        mgr = JobManager(max_workers=1)
        try:
            def boom():
                raise ValueError("kaput")
            jid = mgr.submit(boom)
            deadline = time.time() + 5
            while mgr.status(jid)["state"] not in ("done", "error") \
                    and time.time() < deadline:
                time.sleep(0.01)
            info = mgr.status(jid)
            assert info["state"] == "error"
            assert "ValueError" in info["error"]
            with pytest.raises(RuntimeError, match="failed"):
                mgr.result(jid)
        finally:
            mgr.shutdown()

    def test_unknown_job_id_raises_keyerror(self):
        from studybuddy.jobs import JobManager
        mgr = JobManager(max_workers=1)
        try:
            with pytest.raises(KeyError):
                mgr.status("does-not-exist")
        finally:
            mgr.shutdown()

    def test_result_before_done_raises(self):
        from studybuddy.jobs import JobManager
        import threading as th
        mgr = JobManager(max_workers=1)
        try:
            gate = th.Event()
            jid = mgr.submit(lambda: gate.wait(5), name="blocker")
            with pytest.raises(RuntimeError, match="not finished"):
                mgr.result(jid)
            gate.set()
        finally:
            mgr.shutdown()

    def test_bounded_fifo_eviction(self):
        from studybuddy.jobs import JobManager
        mgr = JobManager(max_workers=1, max_jobs_kept=3)
        try:
            ids = [mgr.submit(lambda: 1, name=f"j{i}") for i in range(5)]
            # only the 3 newest survive
            assert len(mgr.list_jobs(limit=10)) == 3
            with pytest.raises(KeyError):
                mgr.status(ids[0])
            mgr.status(ids[-1])  # newest still there
        finally:
            mgr.shutdown()

    def test_singleton_does_not_start_threads_on_import(self):
        import threading
        before = threading.active_count()
        from studybuddy import jobs  # noqa: F401
        assert threading.active_count() == before


# ===========================================================================
# ARC-10: characterization — refactored functions == pre-refactor outputs
# ===========================================================================
class _FakeUpload:
    def __init__(self, filename, data, mimetype=None, stream=None):
        self.filename = filename
        self.mimetype = mimetype
        self.stream = (io.BytesIO(data) if stream is None and data is not None
                       else stream)


class _NoRead:
    filename = "x.pdf"
    mimetype = "application/pdf"
    stream = None


def _big_tell_stream():
    from studybuddy.pdf_qa import MAX_UPLOAD_BYTES

    class BigTell(io.BytesIO):
        def __init__(self):
            super().__init__(b"%PDF-1.4 fake")

        def seek(self, *a):
            return 0

        def tell(self):
            return MAX_UPLOAD_BYTES + 1

    return BigTell()


class TestCharacterization:
    def test_validate_upload_equivalence(self):
        from studybuddy.pdf_qa import validate_upload
        cases = {
            "none": None,
            "no_filename": _FakeUpload("", b"%PDF-1.4 x"),
            "wrong_ext": _FakeUpload("notes.txt", b"%PDF-1.4 x"),
            "bad_mime": _FakeUpload("a.pdf", b"%PDF-1.4 x",
                                    mimetype="image/png"),
            "octet_mime_ok": _FakeUpload(
                "a.pdf", b"%PDF-1.4 x", mimetype="application/octet-stream"),
            "no_mime": _FakeUpload("a.pdf", b"%PDF-1.4 x"),
            "empty": _FakeUpload("a.pdf", b""),
            "too_big": _FakeUpload("a.pdf", b"%PDF",
                                   stream=_big_tell_stream()),
            "bad_magic": _FakeUpload("a.pdf", b"NOT A PDF AT ALL" * 100),
            "good": _FakeUpload("a.pdf", b"%PDF-1.4\nrest of pdf"),
            "good_upper_ext": _FakeUpload("A.PDF", b"%PDF-1.7\n..."),
            "no_stream": _NoRead(),
            "magic_late": _FakeUpload("a.pdf", b" " * 2000 + b"%PDF-1.4"),
        }
        expected = FIXTURES["validate_upload"]
        for name, fu in cases.items():
            assert list(validate_upload(fu)) == expected[name], name

    def test_star_convert_equivalence(self):
        from studybuddy.bullet_rewriter import star_convert
        data = FIXTURES["star_convert"]
        for i, bullet in enumerate(data["inputs"]):
            try:
                got = star_convert(bullet)
            except ValueError as e:
                got = {"ValueError": str(e)}
            assert got == data["outputs"][f"b{i}"], f"b{i}"

    def test_weak_topic_diagnosis_equivalence(self):
        from studybuddy.dsa_practice import weak_topic_diagnosis
        data = FIXTURES["weak_topic_diagnosis"]
        assert weak_topic_diagnosis(data["rows"]) == data["output"]
        assert weak_topic_diagnosis([]) == \
            FIXTURES["weak_topic_diagnosis_empty"]["output"]

    def test_reading_queue_equivalence(self):
        from studybuddy.arxiv_import import reading_queue
        data = FIXTURES["reading_queue"]
        got = reading_queue(data["papers"], interests=data["interests"],
                            minutes_per_paper=30)
        assert got == data["output"]

    def test_suggest_next_papers_equivalence(self):
        from studybuddy.gap_finder import suggest_next_papers
        graph = {
            "nodes": [
                {"id": "c", "title": "Center Paper", "is_center": True},
                {"id": "n1", "title": "Neighbor One", "year": 2022},
                {"id": "n2", "title": "Neighbor Two", "year": "2021"},
                {"id": "n3", "title": "  ", "year": 2020},
                {"id": "n4", "title": "Center Paper"},
            ],
            "edges": [
                {"from": "c", "to": "n1"},
                {"from": "n2", "to": "c"},
                {"from": "n1", "to": "n2"},
                "junk",
            ],
        }
        imports = [
            {"user_id": 1, "title": "Center Paper", "data": {"graph": graph}},
            {"user_id": 2, "title": "Other User Paper",
             "data": {"graph": graph}},
            {"user_id": 1, "title": "No Graph", "data": {}},
            "junk-import",
        ]

        class Obj:
            def __init__(self):
                self.user_id = 1
                self.title = "Center Paper"
                self.data = {"graph": graph}

        exp = FIXTURES["suggest_next_papers"]
        assert suggest_next_papers(1, imports, ["Some Library Title"]) == \
            exp["output_dicts"]
        assert suggest_next_papers(1, imports + [Obj()],
                                   "Single Title String") == exp["output_mixed"]
        assert suggest_next_papers(None, imports, []) == exp["output_none_user"]
        assert suggest_next_papers(1, imports, [],
                                   max_n="bogus") == exp["output_bad_max"]
