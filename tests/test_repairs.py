"""Regression tests for the 21-issue repair track (P0/P1/P2)."""
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import unified_app
from unified_app import (
    _MODEL_CACHE,
    GeminiError,
    _gemini_api_key,
    _parse_json_safely,
    create_app,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
JS = (REPO_ROOT / "static" / "js" / "research_app.js").read_text()


def _login(client, email="repairs@example.com"):
    """Register + log in (SEC-12: LLM endpoints require login).

    NOTE (audit Stage 4, SEC-06): /register no longer auto-logs in —
    new and existing emails get the same redirect to /login — so the
    login POST below is required, not redundant.
    """
    client.post("/register", data={"name": "Repairs", "email": email,
                                   "password": "Str0ng!Test#2026", "age_confirm": "1"})
    client.post("/login", data={"email": email,
                                "password": "Str0ng!Test#2026"})



@pytest.fixture()
def reset_model_cache():
    _MODEL_CACHE.update({"names": [], "fetched_at": 0.0})
    yield
    _MODEL_CACHE.update({"names": [], "fetched_at": 0.0})


@pytest.fixture()
def csrf_client():
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SECRET_KEY": "csrf-test-secret",
        "WTF_CSRF_ENABLED": True,
    })
    return app.test_client()


# ---------- P0#1: sessionCounter declared ----------

def test_session_counter_declared_before_use():
    decl = re.search(r"\blet\s+sessionCounter\s*=\s*0", JS)
    use = re.search(r"sessionCounter\+\+", JS)
    assert decl, "sessionCounter must be declared"
    assert use, "New Session handler must use sessionCounter"
    assert decl.start() < use.start(), "declaration must precede usage"


# ---------- P0#2: Gemini fail-fast + cached discovery ----------

class _FakeModel:
    def __init__(self, name):
        self.name = name
        self.supported_actions = ["generateContent"]


class _FakeModels:
    def __init__(self, fail_list=False, fail_generate=False):
        self.list_calls = 0
        self.gen_calls = []
        self.fail_list = fail_list
        self.fail_generate = fail_generate

    def list(self):
        self.list_calls += 1
        if self.fail_list:
            raise RuntimeError("boom: discovery failed")
        return [_FakeModel("models/gemini-2.0-flash"),
                _FakeModel("models/gemini-2.0-pro")]

    def generate_content(self, model, contents):
        self.gen_calls.append((model, contents))
        if self.fail_generate:
            raise RuntimeError("boom: generate failed")
        return SimpleNamespace(text="ok from " + model)


class _FakeClient:
    def __init__(self, **kw):
        self.models = _FakeModels(**kw)


def test_gemini_fails_fast_on_discovery_error(monkeypatch, reset_model_cache):
    monkeypatch.setattr(unified_app, "_make_gemini_client",
                        lambda: _FakeClient(fail_list=True))
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    start = time.monotonic()
    with pytest.raises(GeminiError) as exc:
        unified_app.call_gemini_ai("hello")
    elapsed = time.monotonic() - start
    assert elapsed < 10, "must fail fast, not hang"
    assert "try again" in str(exc.value).lower()


def test_gemini_fails_fast_when_all_models_fail(monkeypatch, reset_model_cache):
    monkeypatch.setattr(unified_app, "_make_gemini_client",
                        lambda: _FakeClient(fail_generate=True))
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    start = time.monotonic()
    with pytest.raises(GeminiError):
        unified_app.call_gemini_ai("hello", max_attempts=2)
    assert time.monotonic() - start < 10


def test_gemini_model_list_cached_hourly(monkeypatch, reset_model_cache):
    fake = _FakeClient()
    monkeypatch.setattr(unified_app, "_make_gemini_client", lambda: fake)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    assert unified_app.call_gemini_ai("hi") == "ok from models/gemini-2.0-flash"
    assert unified_app.call_gemini_ai("hi again").startswith("ok from")
    assert fake.models.list_calls == 1, "discovery must be cached"
    # flash prioritized over pro
    assert fake.models.gen_calls[0][0] == "models/gemini-2.0-flash"


def test_gemini_needs_key(monkeypatch):
    monkeypatch.setattr(unified_app, "_make_gemini_client", lambda: None)
    # No provider keys configured -> gateway raises AIError -> GeminiError
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    with pytest.raises(GeminiError) as exc:
        unified_app.call_gemini_ai("hi")
    assert "no ai provider" in str(exc.value).lower()


def test_gemini_key_sentinels_rejected(monkeypatch):
    for sentinel in ["", "your_actual_api_key_here", "<redacted>", "  "]:
        monkeypatch.setenv("GEMINI_API_KEY", sentinel)
        assert _gemini_api_key() is None, sentinel
    monkeypatch.setenv("GEMINI_API_KEY", "real-key-123")
    assert _gemini_api_key() == "real-key-123"


# ---------- P1#4: history reaches the prompt ----------

def test_chat_history_included_in_prompt(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(unified_app, "_ai_ready", lambda: True)
    captured = {}
    monkeypatch.setattr(unified_app, "call_gemini_ai",
                        lambda prompt: captured.setdefault("prompt", prompt) or "answer")
    history = [{"role": "user", "content": "what is attention?"},
               {"role": "assistant", "content": "attention is a mechanism"}]
    resp = client.post("/api/chat_research",
                       json={"message": "explain it deeper",
                             "document_text": "doc",
                             "history": history})
    assert resp.status_code == 200
    prompt = captured["prompt"]
    assert "CONVERSATION SO FAR" in prompt
    assert "what is attention?" in prompt
    assert "explain it deeper" in prompt


def test_chat_missing_message_rejected(client, monkeypatch):
    _login(client)
    # P2#17: never send the literal string "None" to the model.
    monkeypatch.setattr(unified_app, "_ai_ready", lambda: True)
    called = []
    monkeypatch.setattr(unified_app, "call_gemini_ai",
                        lambda prompt: called.append(prompt) or "x")
    for payload in [{}, {"message": ""}, {"message": "   "}, {"document_text": "doc"}]:
        resp = client.post("/api/chat_research", json=payload)
        assert resp.status_code == 400
    assert called == [], "model must not be called without a message"


def test_chat_gemini_failure_is_502_not_500(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(unified_app, "_ai_ready", lambda: True)
    monkeypatch.setattr(unified_app, "call_gemini_ai",
                        lambda prompt: (_ for _ in ()).throw(GeminiError("down")))
    resp = client.post("/api/chat_research",
                       json={"message": "hi", "document_text": "doc"})
    assert resp.status_code == 502
    assert "down" in resp.get_json()["error"]


# ---------- P1#3 / P1#5 / P2#20: constructor ----------

_FAKE_REPO = {
    "full_name": "octo/hello", "description": "demo repo", "stars": 12,
    "forks": 3, "primary_language": "Python",
    "top_languages": [("Python", 90.0), ("HTML", 10.0)],
    "file_count": 42, "top_dirs": ["src", "tests"],
    "sample_files": ["src/app.py", "README.md"],
    "has_tests": True, "has_docker": False, "has_ci": True,
    "readme": "hello world readme", "complexity": "Focused",
}


def test_constructor_rejects_empty_repo_url(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(unified_app, "_ai_ready", lambda: True)
    for payload in [{}, {"repo_url": ""}, {"repo_url": "   "}]:
        resp = client.post("/api/constructor_generate", json=payload)
        assert resp.status_code == 400
        assert "repository url" in resp.get_json()["error"].lower()


def test_constructor_rejects_non_github_url(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(unified_app, "_ai_ready", lambda: True)
    resp = client.post("/api/constructor_generate",
                       json={"repo_url": "https://gitlab.com/a/b"})
    assert resp.status_code == 400


def test_constructor_malformed_json_never_500s(client, monkeypatch):
    _login(client)
    # P1#5: model returns garbage twice -> friendly 502, never 500.
    monkeypatch.setattr(unified_app, "_ai_ready", lambda: True)
    monkeypatch.setattr(unified_app, "_fetch_repo_data", lambda o, r: _FAKE_REPO)
    monkeypatch.setattr(unified_app, "call_gemini_ai", lambda prompt: "not json at all {{{")
    resp = client.post("/api/constructor_generate",
                       json={"repo_url": "https://github.com/octo/hello"})
    assert resp.status_code == 502
    assert "unreadable" in resp.get_json()["error"].lower()


def test_constructor_parses_fenced_json_and_uses_real_stats(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(unified_app, "_ai_ready", lambda: True)
    monkeypatch.setattr(unified_app, "_fetch_repo_data", lambda o, r: _FAKE_REPO)
    captured = {}
    def fake_call(prompt):
        captured["prompt"] = prompt
        return '```json\n{"title": "T", "abstract": "A", "introduction": "I", ' \
               '"lit_review": "L", "architecture": "Ar", "implementation": "Im", ' \
               '"results": "R", "conclusion": "C", ' \
               '"stats": {"files": 42, "complexity": "Focused", "confidence": "High"}}\n```'
    monkeypatch.setattr(unified_app, "call_gemini_ai", fake_call)
    resp = client.post("/api/constructor_generate",
                       json={"repo_url": "https://github.com/octo/hello"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["analysis"]["title"] == "T"
    # P1#3: real repo facts are injected into the prompt.
    assert "octo/hello" in captured["prompt"]
    assert "42" in captured["prompt"]


def test_constructor_github_failure_is_502(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(unified_app, "_ai_ready", lambda: True)
    def boom(o, r):
        raise unified_app.GithubRepoError("Repository not found on GitHub.")
    monkeypatch.setattr(unified_app, "_fetch_repo_data", boom)
    resp = client.post("/api/constructor_generate",
                       json={"repo_url": "https://github.com/nope/missing"})
    assert resp.status_code == 502
    assert "not found" in resp.get_json()["error"].lower()


def test_parse_json_safely():
    assert _parse_json_safely('{"a": 1}') == {"a": 1}
    assert _parse_json_safely('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json_safely('```\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        _parse_json_safely("nope {{{")
    with pytest.raises(ValueError):
        _parse_json_safely("")


# ---------- P1#7: SECRET_KEY policy ----------

def test_secret_key_required_in_production(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("DYNO", raising=False)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app({"TESTING": True,
                    "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})


def test_secret_key_ephemeral_in_dev(monkeypatch, caplog):
    # ARC-08: the ephemeral-key warning now goes through logging, not print().
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    with caplog.at_level("WARNING"):
        app = create_app({"TESTING": True,
                          "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    assert len(app.secret_key) >= 32
    assert any("ephemeral key" in r.message for r in caplog.records)


# ---------- P2#11: CSRF ----------

def test_csrf_blocks_form_without_token(csrf_client):
    resp = csrf_client.post("/login", data={"email": "a@b.c",
                                            "password": "password123"})
    assert resp.status_code == 400


def test_csrf_allows_form_with_token(csrf_client):
    html = csrf_client.get("/login").data.decode()
    token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    resp = csrf_client.post("/login", data={"email": "a@b.c",
                                            "password": "password123",
                                            "csrf_token": token})
    # invalid credentials -> re-rendered form (200), not a 400 CSRF rejection
    assert resp.status_code == 200
    assert b"Invalid email or password" in resp.data


# ---------- P2#11 follow-up: token-based CSRF on fetch-driven endpoints ----------

def _meta_csrf_token(client, page):
    """Extract the csrf-token meta tag from a rendered page (same client keeps the session)."""
    html = client.get(page).data.decode()
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, f"{page} must render a csrf-token meta tag for fetch-driven POSTs"
    return m.group(1)


def test_csrf_blocks_analyze_without_token(csrf_client):
    resp = csrf_client.post("/analyze")
    assert resp.status_code == 400
    assert b"Security check failed" in resp.data


def test_csrf_allows_analyze_with_token(csrf_client):
    token = _meta_csrf_token(csrf_client, "/resume")
    resp = csrf_client.post("/analyze", headers={"X-CSRFToken": token})
    # CSRF passed: reached app logic, which rejects the missing file.
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "No file uploaded"


def test_csrf_blocks_chat_research_without_token(csrf_client):
    resp = csrf_client.post("/api/chat_research", json={"message": "hello"})
    assert resp.status_code == 400
    assert resp.get_json()["error"].startswith("Security check failed")


def test_csrf_allows_chat_research_with_token(csrf_client):
    token = _meta_csrf_token(csrf_client, "/research")
    resp = csrf_client.post("/api/chat_research", json={},
                            headers={"X-CSRFToken": token})
    # CSRF passed (not 400 "Security check failed"); SEC-12 auth denies
    # anonymous callers with 401.
    assert resp.status_code == 401
    assert "log in" in resp.get_json()["error"]


def test_csrf_blocks_constructor_without_token(csrf_client):
    resp = csrf_client.post("/api/constructor_generate",
                            json={"repo_url": "https://github.com/a/b"})
    assert resp.status_code == 400
    assert resp.get_json()["error"].startswith("Security check failed")


def test_csrf_allows_constructor_with_token(csrf_client):
    token = _meta_csrf_token(csrf_client, "/research")
    resp = csrf_client.post("/api/constructor_generate", json={},
                            headers={"X-CSRFToken": token})
    # CSRF passed (not 400 "Security check failed"); SEC-12 auth denies
    # anonymous callers with 401.
    assert resp.status_code == 401
    assert "log in" in resp.get_json()["error"]


# ---------- P2#10: error handlers ----------

def test_api_404_is_json(client):
    resp = client.get("/api/does-not-exist")
    assert resp.status_code == 404
    assert resp.is_json
    assert "error" in resp.get_json()


def test_page_404_renders_error_page(client):
    resp = client.get("/no-such-page")
    assert resp.status_code == 404
    assert b"Back to the Hub" in resp.data


# ---------- P2#15 / P2#16: JS cleanup ----------

def test_no_fabricated_citation_count():
    assert "Math.random()" not in JS, "citation counts must never be fabricated"


def test_dead_qa_engine_removed():
    # No definitions or calls may remain (an explanatory comment is fine).
    code = "\n".join(line for line in JS.splitlines()
                     if not line.strip().startswith("//"))
    assert "function generateDeconstructorResponse" not in code
    assert "function findBestMatch" not in code
    assert "function formatResponse" not in code
    assert "generateDeconstructorResponse(" not in code
    assert "findBestMatch(" not in code
    assert "extractSection" in code, "still used by analyzePDF"


def test_new_session_button_wired():
    assert 'getElementById(\'btn-new-session\')' in JS or \
           'getElementById("btn-new-session")' in JS


# ---------- P2#18: timezone-aware datetimes ----------

def test_updated_at_writes_timezone_aware():
    # SQLite strips tzinfo on round-trip, so assert on what the app *writes*:
    # the column default/onupdate callables must produce tz-aware datetimes
    # (previously datetime.utcnow -> naive).
    from unified_app import ResearchSession
    col = ResearchSession.__table__.c.updated_at
    assert col.default.arg(None).tzinfo is not None
    assert col.onupdate.arg(None).tzinfo is not None
