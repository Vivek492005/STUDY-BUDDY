"""Regression tests for PERF-08 Postgres pooling (docs/ops/postgres-pooling.md).

Neon free scales to zero after 5 min idle and pooled connections held
across the sleep are dead. Until 2026-11-17 the app ran SQLAlchemy
defaults (no pool_pre_ping / pool_recycle), and the first DB request
after idle 500d: the public service's login POST flapped 500/200/500
while the control service answered fine. These tests pin the engine
options so the wiring can never silently drop again.
"""

import pytest

from app.extensions import db
from unified_app import create_app

PG_URL = "postgresql://user:pass@127.0.0.1:1/studybuddy"

EXPECTED_OPTIONS = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
    "pool_size": 3,
    "max_overflow": 5,
    "pool_timeout": 30,
    "connect_args": {
        "connect_timeout": 10,
        "options": "-c statement_timeout=30000",
    },
}


@pytest.fixture()
def pg_app(monkeypatch):
    """An app bound to a (never contacted) Postgres URL.

    SKIP_DB_INIT=1 skips _ensure_prod_schema at boot; db.init_app is
    lazy, so no connection is attempted.
    """
    monkeypatch.setenv("SKIP_DB_INIT", "1")
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": PG_URL,
        "SECRET_KEY": "test-secret-key",
        "WTF_CSRF_ENABLED": False,
    })
    yield app


def test_postgres_gets_pool_options(pg_app):
    assert pg_app.config.get("SQLALCHEMY_ENGINE_OPTIONS") == EXPECTED_OPTIONS


def test_postgres_engine_pool_size(pg_app):
    with pg_app.app_context():
        assert db.engine.pool.size() == 3


def test_sqlite_gets_no_pool_options(app):
    # The Postgres-only options (statement_timeout, pool sizing) must
    # never leak into SQLite dev/test: the connect_args alone would
    # break SQLite. Flask-SQLAlchemy itself defaults the key to {},
    # so assert it stays empty rather than absent.
    assert app.config.get("SQLALCHEMY_ENGINE_OPTIONS") in (None, {})
