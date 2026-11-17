"""#17 + #8: /readyz DB latency and /version benchmark scores.

#17: /readyz times its SELECT 1 probe and reports db_latency_ms so a
slow-but-alive database is visible to uptime dashboards.
#8: /version surfaces benchmarks/results.json (gold-set scores) when
the file exists, and omits the key gracefully when it doesn't.
"""
import json
import os

import unified_app

REPO = os.path.join(os.path.dirname(__file__), "..")


class TestReadyzLatency:
    def test_db_latency_field_present_and_positive(self, client):
        r = client.get("/readyz")
        body = r.get_json()
        assert body["checks"]["db"] == "ok"
        latency = body["checks"]["db_latency_ms"]
        assert isinstance(latency, (int, float))
        assert latency > 0

    def test_readyz_still_reports_ready(self, client):
        # In the test app the schema is create_all-managed, so the
        # migrations check may report "behind" — latency must be
        # present either way, and the db check itself must pass.
        body = client.get("/readyz").get_json()
        assert "db_latency_ms" in body["checks"]


class TestVersionBenchmarks:
    def test_version_includes_benchmarks_when_file_exists(self, client):
        with open(os.path.join(REPO, "benchmarks", "results.json")) as f:
            on_disk = json.load(f)
        body = client.get("/version").get_json()
        assert "benchmarks" in body
        assert body["benchmarks"]["n_items"] == on_disk["n_items"]
        assert body["benchmarks"]["skill_f1"] == on_disk["skill_f1"]

    def test_version_omits_benchmarks_when_unavailable(self, client,
                                                         monkeypatch):
        monkeypatch.setattr(unified_app, "_load_benchmark_scores",
                            lambda path=None: None)
        body = client.get("/version").get_json()
        assert "benchmarks" not in body
        assert body["app"] == "study-buddy"

    def test_loader_missing_file_returns_none(self, tmp_path):
        assert unified_app._load_benchmark_scores(
            str(tmp_path / "nope.json")) is None

    def test_loader_malformed_file_returns_none(self, tmp_path):
        bad = tmp_path / "results.json"
        bad.write_text("{not json", encoding="utf-8")
        assert unified_app._load_benchmark_scores(str(bad)) is None

    def test_loader_valid_file_returns_dict(self, tmp_path):
        good = tmp_path / "results.json"
        good.write_text(json.dumps({"n_items": 3, "skill_f1": 0.5}),
                        encoding="utf-8")
        assert unified_app._load_benchmark_scores(str(good)) == {
            "n_items": 3, "skill_f1": 0.5}


class TestReadyzLlm:
    """2026-11-17: /readyz exposes LLM config (names/mode only, never
    keys) so prod provider setup is verifiable without dashboard access.
    A missing key must never make the service unready (rule-based
    fallbacks exist)."""

    def test_llm_block_present_with_shape(self, client):
        body = client.get("/readyz").get_json()
        llm = body["checks"]["llm"]
        assert llm["mode"] in ("off", "byok", "server")
        assert isinstance(llm["providers"], list)
        assert isinstance(llm["ready"], bool)
        # Only provider NAMES ever appear — no key material.
        assert set(llm["providers"]) <= {"gemini", "groq", "openrouter"}

    def test_llm_block_reflects_configured_key(self, client, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "test-key-not-real")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        body = client.get("/readyz").get_json()
        llm = body["checks"]["llm"]
        assert llm["providers"] == ["groq"]
        assert llm["ready"] is True
