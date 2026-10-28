"""WS3-INFRA tests: LLM-03 ledger/quotas, ARC-13 pagination, ARC-14 singletons,
ARC-04/LLM-03 migrations. >=20 tests; must stay green alongside the suite.
"""
import importlib.util
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from unified_app import db

# Import BEFORE the `app` fixture runs create_app() so db.create_all()
# (SQLite dev/test path) also creates llm_usage.
from studybuddy import llm_ledger  # noqa: E402
from studybuddy import arc_state  # noqa: E402
from studybuddy.arc_pagination import (  # noqa: E402
    MAX_PER_PAGE,
    DEFAULT_PER_PAGE,
    normalize_pagination,
    paginate,
    paginated,
)

REPO = Path(__file__).resolve().parent.parent
UTC = timezone.utc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def ledger(app):
    """Fresh ledger state per test; app context active during the test."""
    llm_ledger._alerted.clear()
    with app.app_context():
        db.session.query(llm_ledger.LLMUsage).delete()
        db.session.commit()
        yield llm_ledger
        db.session.query(llm_ledger.LLMUsage).delete()
        db.session.commit()


@pytest.fixture()
def pager():
    """Standalone in-memory DB with 25 items (5 tie groups on `created`)."""
    base = declarative_base()

    class Item(base):
        __tablename__ = "items"
        id = Column(Integer, primary_key=True)
        name = Column(String(50))
        created = Column(Integer)

    engine = create_engine("sqlite:///:memory:")
    base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    for i in range(1, 26):
        session.add(Item(id=i, name=f"item-{i}", created=i // 5))
    session.commit()
    yield session, Item
    session.close()


# ---------------------------------------------------------------------------
# LLM-03: cost math + ledger writes
# ---------------------------------------------------------------------------

def test_estimate_cost_math():
    # groq: 0.05 in / 0.08 out per 1M tokens
    assert llm_ledger.estimate_cost("groq", 1_000_000, 1_000_000) == pytest.approx(0.13)
    assert llm_ledger.estimate_cost("groq", 0, 0) == 0.0
    # unknown provider -> 0.0, never crashes budgeting
    assert llm_ledger.estimate_cost("mystery", 10_000, 10_000) == 0.0
    # local/byok cost nothing server-side
    assert llm_ledger.estimate_cost("local", 5_000_000, 5_000_000) == 0.0


def test_log_llm_action_persists_row(ledger, app):
    with app.app_context():
        row = ledger.log_llm_action(
            7, "resume_feedback", provider="groq", model="llama-3.3-70b-versatile",
            tokens_in=1000, tokens_out=500)
        assert row.id is not None
        assert row.cost_estimate == pytest.approx(
            ledger.estimate_cost("groq", 1000, 500))
        fetched = db.session.get(ledger.LLMUsage, row.id)
        assert fetched.feature == "resume_feedback"
        assert fetched.tokens_in == 1000 and fetched.tokens_out == 500
        assert fetched.created_at is not None


def test_log_llm_action_estimates_tokens_from_chars(ledger, app):
    with app.app_context():
        row = ledger.log_llm_action(
            7, "bullet_rewrite", provider="gemini", model="gemini-x",
            prompt_chars=4000, response_chars=2000)
        # ~4 chars/token heuristic
        assert row.tokens_in == 1000
        assert row.tokens_out == 500


# ---------------------------------------------------------------------------
# LLM-03: quotas
# ---------------------------------------------------------------------------

def test_actions_used_today_counts_current_day_only(ledger, app):
    with app.app_context():
        old = llm_ledger.LLMUsage(
            user_id=42, feature="x", provider="groq", model="m",
            tokens_in=1, tokens_out=1, cost_estimate=0.0,
            created_at=datetime.now(UTC) - timedelta(days=2))
        db.session.add(old)
        db.session.commit()
        ledger.log_llm_action(42, "x", provider="groq", model="m",
                              tokens_in=1, tokens_out=1)
        assert ledger.actions_used_today(42) == 1
        assert ledger.actions_used_today(43) == 0
        assert ledger.actions_used_today(None) == 0


def test_quota_remaining_and_status_line(ledger, app, monkeypatch):
    monkeypatch.setenv("LLM_DAILY_QUOTA", "5")
    with app.app_context():
        assert ledger.quota_remaining(9) == 5
        assert ledger.quota_status_line(9) == "AI actions left today: 5"
        for _ in range(3):
            ledger.log_llm_action(9, "x", provider="groq", model="m")
        assert ledger.quota_remaining(9) == 2
        assert ledger.quota_status_line(9) == "AI actions left today: 2"


def test_daily_quota_env_override(ledger, monkeypatch):
    assert ledger.daily_quota() == 20  # default
    monkeypatch.setenv("LLM_DAILY_QUOTA", "7")
    assert ledger.daily_quota() == 7
    monkeypatch.setenv("LLM_DAILY_QUOTA", "not-a-number")
    assert ledger.daily_quota() == 20  # invalid -> default, never crash


def test_guard_allows_and_returns_remaining(ledger, app, monkeypatch):
    monkeypatch.setenv("LLM_DAILY_QUOTA", "4")
    with app.app_context():
        assert ledger.guard_llm_action(11, "interview") == 4
        ledger.log_llm_action(11, "interview", provider="groq", model="m")
        assert ledger.guard_llm_action(11, "interview") == 3


def test_guard_raises_when_quota_exhausted(ledger, app, monkeypatch):
    monkeypatch.setenv("LLM_DAILY_QUOTA", "2")
    with app.app_context():
        ledger.log_llm_action(12, "x", provider="groq", model="m")
        ledger.log_llm_action(12, "x", provider="groq", model="m")
        with pytest.raises(llm_ledger.QuotaExceeded, match="Daily AI quota"):
            ledger.guard_llm_action(12, "x")
        assert ledger.quota_remaining(12) == 0  # never negative


def test_guard_allows_anonymous_without_user(ledger):
    # No user_id: nothing to meter against; allow (documented).
    assert ledger.guard_llm_action(None) == ledger.daily_quota()


# ---------------------------------------------------------------------------
# LLM-03: monthly budget + auto rule-based fallback
# ---------------------------------------------------------------------------

def _spend(ledger, usd_target):
    # openrouter out = $0.80 / 1M tokens -> tokens for slightly over target
    tokens_out = int(usd_target / 0.80 * 1_000_000) + 10_000
    return ledger.log_llm_action(1, "x", provider="openrouter",
                                 model="google/gemma-4-31b-it:free",
                                 tokens_out=tokens_out)


def test_month_spend_and_budget_not_exceeded_by_default(ledger, app):
    with app.app_context():
        assert ledger.monthly_budget_usd() == 0.0
        _spend(ledger, 50.0)
        assert ledger.month_spend_usd() > 0
        # No budget configured -> never "exceeded" (quota still applies).
        assert ledger.budget_exceeded() is False
        assert ledger.rule_based_fallback_active() is False


def test_budget_exceeded_after_spend(ledger, app, monkeypatch):
    monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "0.05")
    with app.app_context():
        assert ledger.budget_exceeded() is False
        _spend(ledger, 0.10)
        assert ledger.month_spend_usd() >= 0.05
        assert ledger.budget_exceeded() is True
        assert ledger.rule_based_fallback_active() is True


def test_guard_raises_rule_based_when_budget_exceeded(ledger, app, monkeypatch):
    monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "0.05")
    monkeypatch.setenv("LLM_DAILY_QUOTA", "100")
    with app.app_context():
        _spend(ledger, 0.10)
        with pytest.raises(llm_ledger.QuotaExceeded, match="budget"):
            ledger.guard_llm_action(5, "jd_match")


def test_budget_alert_levels_and_dedup(ledger, app, monkeypatch, caplog):
    monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "1.00")
    with app.app_context():
        assert ledger.budget_alert_level() == 0
        _spend(ledger, 0.55)  # crosses 50%
        assert ledger.budget_alert_level() == 50
        with caplog.at_level(logging.WARNING, logger="studybuddy.llm_ledger"):
            assert ledger.maybe_alert_budget() == 50
            assert ledger.maybe_alert_budget() == 50  # deduped: no 2nd log
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "50%" in warnings[0].getMessage()
        _spend(ledger, 0.60)  # crosses 80% and 100%
        assert ledger.budget_alert_level() == 100


# ---------------------------------------------------------------------------
# ARC-13: pagination
# ---------------------------------------------------------------------------

def test_normalize_pagination_clamps():
    assert normalize_pagination(2, 10) == (2, 10)
    assert normalize_pagination(0, 10) == (1, 10)      # page floor
    assert normalize_pagination(-3, 10) == (1, 10)
    assert normalize_pagination(1, 500) == (1, MAX_PER_PAGE)  # per_page cap
    assert normalize_pagination(1, 0) == (1, 1)
    assert normalize_pagination(1, -5) == (1, 1)


def test_normalize_pagination_invalid_inputs_fall_back():
    assert normalize_pagination("abc", "xyz") == (1, DEFAULT_PER_PAGE)
    assert normalize_pagination(None, None) == (1, DEFAULT_PER_PAGE)
    assert normalize_pagination("3", "15") == (3, 15)


def test_paginate_envelope_and_beyond_last_page(pager):
    session, Item = pager
    order = (Item.created.desc(), Item.id.desc())
    first = paginate(1, 10, session.query(Item), order_by=order)
    assert first["page"] == 1 and first["per_page"] == 10
    assert first["total"] == 25 and first["pages"] == 3
    assert first["has_next"] is True and first["has_prev"] is False
    assert [i.id for i in first["items"]] == [25, 24, 23, 22, 21, 20, 19, 18, 17, 16]

    empty = paginate(9, 10, session.query(Item), order_by=order)
    assert empty["items"] == []
    assert empty["total"] == 25 and empty["has_next"] is False
    assert empty["has_prev"] is True


def test_paginate_stable_ordering_with_ties(pager):
    # 5 rows share each `created` value; the id tiebreak must make pages
    # deterministic across repeated calls.
    session, Item = pager
    order = (Item.created.desc(), Item.id.desc())
    def run():
        return [i.id for i in
                paginate(2, 7, session.query(Item), order_by=order)["items"]]

    assert run() == run() == [18, 17, 16, 15, 14, 13, 12]
    # every id appears exactly once across all pages (no dupes, no drops)
    seen = []
    for p in (1, 2, 3, 4):
        seen += [i.id for i in
                 paginate(p, 7, session.query(Item), order_by=order)["items"]]
    assert sorted(seen) == list(range(1, 26))


def test_paginated_decorator_requires_order_by():
    with pytest.raises(ValueError, match="order_by"):
        paginated()  # stable ordering is mandatory (ARC-13)


def test_paginated_decorator_paginates_and_serializes(pager):
    from flask import Flask
    session, Item = pager
    flask_app = Flask(__name__)

    @paginated(order_by=(Item.created.desc(), Item.id.desc()),
               serialize=lambda i: {"id": i.id, "name": i.name})
    def view():
        return session.query(Item)

    with flask_app.test_request_context("/?page=2&per_page=10"):
        result = view()
    assert result["page"] == 2
    assert len(result["items"]) == 10
    assert result["items"][0] == {"id": 15, "name": "item-15"}

    # per_page above the max is clamped by the decorator too
    with flask_app.test_request_context("/?per_page=500"):
        result = view()
    assert result["per_page"] == MAX_PER_PAGE


# ---------------------------------------------------------------------------
# ARC-14: lock-protected singletons
# ---------------------------------------------------------------------------

def test_get_or_init_runs_factory_once_under_threads():
    calls = []

    def factory():
        time.sleep(0.02)  # widen the race window
        calls.append(1)
        return {"built": True}

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(
            lambda _: arc_state.get_or_init("ws3.test.race", factory),
            range(32)))
    assert len(calls) == 1
    assert all(r is results[0] for r in results)
    arc_state.reset_singleton("ws3.test.race")


def test_lazy_singleton_reset_rebuilds():
    holder = arc_state.get_holder("ws3.test.reset", lambda: object())
    first = holder.get()
    assert holder.is_initialized is True
    holder.reset()
    assert holder.is_initialized is False
    assert holder.get() is not first  # factory re-ran


def test_skills_taxonomy_singleton_threadsafe_and_api_stable():
    from studybuddy import skills_taxonomy
    for key in ("skills_taxonomy.taxonomy", "skills_taxonomy.indexes",
                "skills_taxonomy.compiled_re"):
        arc_state.reset_singleton(key)
    with ThreadPoolExecutor(max_workers=8) as pool:
        taxonomies = list(pool.map(lambda _: skills_taxonomy.load_taxonomy(),
                                   range(16)))
    assert all(t is taxonomies[0] for t in taxonomies)
    # Public API unchanged after the ARC-14 migration.
    assert skills_taxonomy.normalize_skill("reactjs") == "React"
    assert skills_taxonomy.normalize_skill("  REACT.JS  ") == "React"
    assert skills_taxonomy.normalize_skill("not-a-real-skill-xyz") is None
    found = skills_taxonomy.extract_skills_fast("I know reactjs and python.")
    assert any(r["name"] == "React" for r in found)
    assert skills_taxonomy.get_category("React")
    # Reset drops the cache; next load rebuilds (equal content, new object).
    arc_state.reset_singleton("skills_taxonomy.taxonomy")
    assert skills_taxonomy.load_taxonomy() is not taxonomies[0]


# ---------------------------------------------------------------------------
# Migrations: chain + upgrade/downgrade on SQLite
# ---------------------------------------------------------------------------

def _load_migration(filename):
    path = REPO / "migrations" / "versions" / filename
    spec = importlib.util.spec_from_file_location("ws3_mig", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ops(engine):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    conn = engine.connect()
    ctx = MigrationContext.configure(conn)
    return Operations(ctx), conn


def test_alembic_single_linear_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    cfg = Config()
    cfg.set_main_option("script_location", str(REPO / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    assert list(script.get_heads()) == ["e5f6a7b8c9d0"]
    sess_ver = script.get_revision("e5f6a7b8c9d0")
    assert sess_ver.down_revision == "d4e5f6a7b8c9"  # audit Stage 2
    arc04 = script.get_revision("d4e5f6a7b8c9")
    assert arc04.down_revision == "c3d4e5f6a7b8"
    llm03 = script.get_revision("c3d4e5f6a7b8")
    assert llm03.down_revision == "b2c3d4e5f6a7"  # RAG-05 (concurrent WS)
    rag05 = script.get_revision("b2c3d4e5f6a7")
    assert rag05.down_revision == "01c271a81d47"


def test_llm03_migration_upgrade_downgrade(tmp_path, monkeypatch):
    mod = _load_migration("c3d4e5f6a7b8_llm03_usage.py")
    engine = create_engine(f"sqlite:///{tmp_path}/llm03.db")
    with engine.begin() as c:
        c.execute(sa.text('CREATE TABLE "user" (id INTEGER PRIMARY KEY)'))
    ops, conn = _ops(engine)
    monkeypatch.setattr(mod, "op", ops)

    mod.upgrade()
    conn.commit()
    insp = sa.inspect(engine)
    assert "llm_usage" in insp.get_table_names()
    cols = {c["name"]: c for c in insp.get_columns("llm_usage")}
    assert set(cols) == {"id", "user_id", "feature", "provider", "model",
                         "tokens_in", "tokens_out", "cost_estimate",
                         "created_at"}
    assert isinstance(cols["cost_estimate"]["type"], sa.Float)
    idx = insp.get_indexes("llm_usage")
    assert any(i["name"] == "ix_llm_usage_user_created"
               and list(i["column_names"]) == ["user_id", "created_at"]
               for i in idx)
    with engine.begin() as c:
        c.execute(sa.text(
            "INSERT INTO llm_usage (user_id, feature, provider, model,"
            " tokens_in, tokens_out, cost_estimate, created_at)"
            " VALUES (1, 'x', 'groq', 'm', 10, 20, 0.001,"
            " '2026-10-19 10:00:00')"))

    mod.downgrade()
    conn.commit()
    assert "llm_usage" not in sa.inspect(engine).get_table_names()
    conn.close()


_ARC04_TABLES = {
    "paper_import": "(id INTEGER PRIMARY KEY, user_id INTEGER,"
                    " fetched_at VARCHAR(32), created_at DATETIME)",
    "goal": "(id INTEGER PRIMARY KEY, user_id INTEGER, created_at DATETIME)",
    "job_application": "(id INTEGER PRIMARY KEY, user_id INTEGER,"
                       " created_at DATETIME)",
    "health_review": "(id INTEGER PRIMARY KEY, user_id INTEGER,"
                     " created_at DATETIME)",
    "resume_analysis": "(id INTEGER PRIMARY KEY, user_id INTEGER,"
                       " created_at DATETIME)",
    "jd_match_report": "(id INTEGER PRIMARY KEY, user_id INTEGER,"
                       " created_at DATETIME)",
    "bullet_rewrite": "(id INTEGER PRIMARY KEY, user_id INTEGER,"
                      " created_at DATETIME)",
    "linked_in_report": "(id INTEGER PRIMARY KEY, user_id INTEGER,"
                       " created_at DATETIME)",
}


def test_arc04_migration_upgrade_downgrade(tmp_path, monkeypatch):
    mod = _load_migration("d4e5f6a7b8c9_arc04_schema.py")
    engine = create_engine(f"sqlite:///{tmp_path}/arc04.db")
    with engine.begin() as c:
        for table, ddl in _ARC04_TABLES.items():
            c.execute(sa.text(f"CREATE TABLE {table} {ddl}"))
        # ISO w/ offset, canonical sqlite form, garbage, NULL
        c.execute(sa.text(
            "INSERT INTO paper_import (id, user_id, fetched_at, created_at)"
            " VALUES (1, 1, '2026-10-19T10:30:00+00:00',"
            " '2026-10-19 10:00:00')"))
        c.execute(sa.text(
            "INSERT INTO paper_import (id, user_id, fetched_at, created_at)"
            " VALUES (2, 1, '2026-10-19 11:00:00', '2026-10-19 10:00:00')"))
        c.execute(sa.text(
            "INSERT INTO paper_import (id, user_id, fetched_at, created_at)"
            " VALUES (3, 1, 'not-a-date', '2026-10-19 10:00:00')"))
        c.execute(sa.text(
            "INSERT INTO paper_import (id, user_id, fetched_at, created_at)"
            " VALUES (4, 1, NULL, '2026-10-19 10:00:00')"))
    ops, conn = _ops(engine)
    monkeypatch.setattr(mod, "op", ops)

    mod.upgrade()
    conn.commit()
    insp = sa.inspect(engine)

    # (a) composite indexes on the four report tables
    for table in ("resume_analysis", "jd_match_report", "bullet_rewrite",
                  "linked_in_report"):
        idx = insp.get_indexes(table)
        assert any(i["name"] == f"ix_{table}_user_created"
                   and list(i["column_names"]) == ["user_id", "created_at"]
                   for i in idx), table

    # (b) fetched_at is now DateTime; values normalized to canonical UTC
    cols = {c["name"]: c for c in insp.get_columns("paper_import")}
    assert isinstance(cols["fetched_at"]["type"], sa.DateTime)
    with engine.connect() as c:
        vals = dict(c.execute(
            sa.text("SELECT id, fetched_at FROM paper_import")).fetchall())
    assert vals[1] == "2026-10-19 10:30:00"
    assert vals[2] == "2026-10-19 11:00:00"
    assert vals[3] is None  # unparseable -> nulled, never crashes a read
    assert vals[4] is None

    # (c) updated_at added to the three main tables
    for table in ("goal", "job_application", "health_review"):
        tcols = {c["name"] for c in insp.get_columns(table)}
        assert "updated_at" in tcols, table

    mod.downgrade()
    conn.commit()
    insp = sa.inspect(engine)
    for table in ("resume_analysis", "jd_match_report", "bullet_rewrite",
                  "linked_in_report"):
        assert not [i for i in insp.get_indexes(table)
                    if i["name"] == f"ix_{table}_user_created"], table
    cols = {c["name"]: c for c in insp.get_columns("paper_import")}
    assert isinstance(cols["fetched_at"]["type"], sa.String)
    for table in ("goal", "job_application", "health_review"):
        assert "updated_at" not in {c["name"]
                                    for c in insp.get_columns(table)}, table
    conn.close()


def test_arc04_migration_is_thread_local_safe_attr():
    # The migration module must not touch the Flask app or network at import.
    mod = _load_migration("d4e5f6a7b8c9_arc04_schema.py")
    assert mod.revision == "d4e5f6a7b8c9"
    assert mod.down_revision == "c3d4e5f6a7b8"
    assert mod._parse_iso("2026-10-19T10:30:00+00:00") is not None
    assert mod._parse_iso("garbage") is None
    assert mod._parse_iso(None) is None
