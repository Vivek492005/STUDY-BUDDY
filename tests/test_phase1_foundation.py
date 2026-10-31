"""Phase 1 Batch 2: OPS-01 reproducible builds, ARC-02 packaging."""
import os
import re

REPO = os.path.join(os.path.dirname(__file__), "..")


class TestReproducibleBuilds:
    """OPS-01: locked, hash-verified requirements.txt + PYTHON_VERSION pin."""

    def test_requirements_txt_has_hashes(self):
        path = os.path.join(REPO, "requirements.txt")
        text = open(path).read()
        # every pinned package must have at least one sha256 hash
        pkgs = re.findall(r"^([a-zA-Z0-9_.-]+)==[^\s\\]+", text, re.M)
        assert pkgs, "no pinned packages found"
        # count hash lines vs package lines
        hash_blocks = len(re.findall(r"--hash=sha256:", text))
        assert hash_blocks >= len(pkgs), \
            f"{len(pkgs)} packages but only {hash_blocks} hash lines"

    def test_requirements_in_exists(self):
        assert os.path.exists(os.path.join(REPO, "requirements.in"))

    def test_python_version_pinned_render(self):
        text = open(os.path.join(REPO, "render.yaml")).read()
        assert "PYTHON_VERSION" in text, "render.yaml must pin PYTHON_VERSION"
        assert ".python-version" in os.listdir(REPO)

    def test_security_pins_survive_lock(self):
        """SEC-07 pins must be present in the compiled lockfile."""
        text = open(os.path.join(REPO, "requirements.txt")).read()
        for pkg, ver in [("flask", "3.1.3"), ("werkzeug", "3.1.9"),
                         ("gunicorn", "26.2.0"), ("pypdf", "6.21.4")]:
            assert re.search(rf"^{pkg}=={re.escape(ver)}\b", text, re.M), \
                f"{pkg}=={ver} missing from lockfile"


class TestCIFoundation:
    """TEST-01/TEST-12: CI workflow and pre-commit config exist."""

    def test_ci_workflow_exists(self):
        path = os.path.join(REPO, ".github", "workflows", "ci.yml")
        assert os.path.exists(path)
        text = open(path).read()
        for job in ("lint:", "test-sqlite:", "test-postgres:", "security:"):
            assert job in text, f"CI job {job} missing"

    def test_precommit_config_exists(self):
        assert os.path.exists(os.path.join(REPO, ".pre-commit-config.yaml"))

    def test_pyproject_exists(self):
        assert os.path.exists(os.path.join(REPO, "pyproject.toml"))


class TestMigrations:
    """ARC-03: Alembic baseline — upgrade/downgrade on a fresh database."""

    def _run_flask_db(self, *args, db_url):
        import os
        import subprocess
        import sys
        env = dict(os.environ, DATABASE_URL=db_url, SKIP_DB_INIT="1",
                   OPP_NO_AUTO_REFRESH="1", FLASK_APP="unified_app",
                   SECRET_KEY="migration-test")
        # sys.executable -m flask works in venvs AND in CI (no .venv there).
        return subprocess.run(
            [sys.executable, "-m", "flask", "db", *args],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=120)

    def test_baseline_upgrade_creates_tables(self, tmp_path):
        db_url = f"sqlite:///{tmp_path}/mig.db"
        r = self._run_flask_db("upgrade", db_url=db_url)
        assert r.returncode == 0, r.stderr[-500:]
        import sqlite3
        con = sqlite3.connect(str(tmp_path / "mig.db"))
        tables = {row[0] for row in con.execute(
            "select name from sqlite_master where type='table'")}
        assert "user" in tables and "resume_analysis" in tables
        assert "alembic_version" in tables
        assert len(tables) > 40  # full schema, not a delta

    def test_baseline_downgrade_reverts(self, tmp_path):
        db_url = f"sqlite:///{tmp_path}/mig2.db"
        assert self._run_flask_db("upgrade", db_url=db_url).returncode == 0
        r = self._run_flask_db("downgrade", "base", db_url=db_url)
        assert r.returncode == 0, r.stderr[-500:]
        import sqlite3
        con = sqlite3.connect(str(tmp_path / "mig2.db"))
        tables = {row[0] for row in con.execute(
            "select name from sqlite_master where type='table'"
            " and name not like 'alembic%' and name != 'sqlite_sequence'")}
        assert tables == set()

    def test_no_ddl_at_boot_for_postgres(self, monkeypatch):
        """Production (Postgres URL) must not run create_all at worker boot."""
        import unified_app
        calls = []
        monkeypatch.setattr(unified_app.db, "create_all",
                            lambda *a, **k: calls.append(1))
        _app = unified_app.create_app({
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "postgresql+psycopg2://u:p@localhost/db",
            "SECRET_KEY": "x", "WTF_CSRF_ENABLED": False,
        })
        assert calls == [], "create_all ran at boot with a Postgres URL"


class TestObservability:
    """ARC-08: request IDs; OPS-05: health/readiness/version."""

    def test_request_id_header(self, client):
        resp = client.get("/")
        assert "X-Request-ID" in resp.headers
        assert len(resp.headers["X-Request-ID"]) >= 8

    def test_request_id_echo(self, client):
        resp = client.get("/", headers={"X-Request-ID": "test-123"})
        assert resp.headers["X-Request-ID"] == "test-123"

    def test_healthz(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

    def test_readyz(self, client):
        resp = client.get("/readyz")
        data = resp.get_json()
        assert "ready" in data and "checks" in data
        assert data["checks"]["db"] == "ok"
        # never leak secrets
        assert "SECRET" not in resp.get_data(as_text=True).upper().replace(
            "SECRET_KEY", "")

    def test_version(self, client):
        resp = client.get("/version")
        data = resp.get_json()
        assert data["app"] == "study-buddy"
        assert data["git_sha"] and data["schema_revision"]


class TestFeatureFlags:
    """ARC-12: validated config + feature flags."""

    def test_bad_llm_mode_falls_back(self, monkeypatch):
        monkeypatch.setenv("LLM_MODE", "nonsense")
        from studybuddy.app_config import AppConfig
        assert AppConfig.from_env().llm_mode == "server"

    def test_llm_off_disables(self, monkeypatch):
        monkeypatch.setenv("LLM_MODE", "off")
        from studybuddy.app_config import AppConfig
        assert not AppConfig.from_env().llm_enabled()

    def test_labs_flag_off_gives_403(self, client, monkeypatch):
        import dataclasses
        from studybuddy import app_config
        # AppConfig is frozen; swap in a modified copy.
        monkeypatch.setattr(app_config, "config",
                            dataclasses.replace(app_config.config,
                                                feature_labs=False))
        # need a logged-in user (SEC-01: anonymous gets 401 first)
        from tests.conftest import register, login
        register(client, email="flagtest@example.com")
        login(client, email="flagtest@example.com")
        resp = client.get("/api/ultimate/tools")
        assert resp.status_code == 403


class TestSafeXmlParsing:
    """SEC-10: defusedxml blocks billion-laughs; html.escape for escaping."""

    def test_billion_laughs_blocked(self):
        from defusedxml.ElementTree import fromstring
        from defusedxml.common import EntitiesForbidden
        payload = ('<?xml version="1.0"?>'
                   '<!DOCTYPE lolz ['
                   '<!ENTITY lol "lollollollollollollollollollol">'
                   '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
                   ']>'
                   '<lolz>&lol2;</lolz>')
        import pytest
        with pytest.raises(EntitiesForbidden):
            fromstring(payload)

    def test_arxiv_uses_defusedxml(self):
        import studybuddy.arxiv_import as m
        assert "defusedxml" in open(m.__file__).read()

    def test_trending_uses_defusedxml(self):
        import studybuddy.trending as m
        assert "defusedxml" in open(m.__file__).read()

    def test_leetcode_uses_html_escape(self):
        import studybuddy.leetcode_integration as m
        src = open(m.__file__).read()
        assert "from html import escape" in src
        assert "xml.sax.saxutils" not in src
