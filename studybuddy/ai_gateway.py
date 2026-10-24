"""AI Gateway — multi-provider LLM access with automatic fallback.

Provider chain (all free tiers):
    1. Gemini  (GEMINI_API_KEY)      — google-genai SDK
    2. Groq    (GROQ_API_KEY)        — OpenAI-compatible, llama-3.3-70b-versatile
    3. OpenRouter (OPENROUTER_API_KEY) — OpenAI-compatible, free :free models
    4. Local   (LLM_BASE_URL)        — OpenAI-compatible local endpoint (Ollama)

PRIV-05 modes (via LLM_MODE env):
    - "server" (default): use server-configured provider keys
    - "off": no LLM calls — raise AIError("disabled") so callers use rule-based fallbacks
    - "byok": caller must pass user_key; used for that request only, never logged

generate() tries each configured provider in order and returns the first
successful response. Every provider failure is logged; only if ALL fail
does AIError raise. Callers never need to know which provider served them.

Keys are read from the environment on every call (never cached), so key
rotation via Render env vars takes effect without a restart.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 45

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_MODEL = "llama-3.3-70b-versatile"

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_MODEL = "google/gemma-4-31b-it:free"  # verified in :free list 2026-10-15

# PRIV-05: local OpenAI-compatible endpoint (e.g. Ollama at http://localhost:11434/v1)
_LOCAL_URL = os.getenv("LLM_BASE_URL", "").rstrip("/") + "/chat/completions" \
    if os.getenv("LLM_BASE_URL") else None
_LOCAL_MODEL = os.getenv("LLM_MODEL", "llama3.2")

_KEY_SENTINELS = frozenset({
    "", "your_actual_api_key_here", "<redacted>", "change-me",
    "YOUR_API_KEY", "paste-your-key-here",
})


class AIError(Exception):
    """All providers failed; message is safe to show to users."""


class RateLimited(AIError):
    """Provider returned 429."""


class ProviderDown(AIError):
    """Provider returned 5xx or is unreachable."""


class BadOutput(AIError):
    """Provider returned unusable output."""


# LLM-01: Circuit breaker state (per provider)
_CIRCUIT = {}  # provider -> {"failures": int, "open_until": float}
_CIRCUIT_THRESHOLD = 5
_CIRCUIT_COOLDOWN = 300  # seconds

# LLM-04: Simple in-memory cache (prompt hash -> response)
# For production, use Redis. This is a pragmatic in-process cache.
_CACHE = {}  # sha256(prompt) -> (response, timestamp)
_CACHE_TTL = 3600  # 1 hour
_CACHE_MAX = 1000


def _cache_get(prompt: str) -> str | None:
    """LLM-04: Get cached response if fresh."""
    import hashlib
    import time
    key = hashlib.sha256(prompt.encode()).hexdigest()
    entry = _CACHE.get(key)
    if entry:
        response, ts = entry
        if time.time() - ts < _CACHE_TTL:
            log.info("llm_cache_hit")
            return response
        del _CACHE[key]
    return None


def _cache_put(prompt: str, response: str) -> None:
    """LLM-04: Cache a response."""
    import hashlib
    import time
    if len(_CACHE) >= _CACHE_MAX:
        # Evict oldest
        oldest = min(_CACHE, key=lambda k: _CACHE[k][1])
        del _CACHE[oldest]
    key = hashlib.sha256(prompt.encode()).hexdigest()
    _CACHE[key] = (response, time.time())


def _circuit_open(provider: str) -> bool:
    """Check if circuit breaker is open for provider."""
    import time
    state = _CIRCUIT.get(provider, {})
    open_until = state.get("open_until", 0)
    if time.time() < open_until:
        return True
    # Half-open: reset failures after cooldown
    if open_until and time.time() >= open_until:
        _CIRCUIT[provider] = {"failures": 0, "open_until": 0}
    return False


def _circuit_record(provider: str, success: bool) -> None:
    """Record success/failure for circuit breaker."""
    import time
    state = _CIRCUIT.setdefault(provider, {"failures": 0, "open_until": 0})
    if success:
        state["failures"] = 0
        state["open_until"] = 0
    else:
        state["failures"] += 1
        if state["failures"] >= _CIRCUIT_THRESHOLD:
            state["open_until"] = time.time() + _CIRCUIT_COOLDOWN
            log.warning("circuit_open provider=%s", provider)


def _retry_with_backoff(
    fn: Callable[[], str],
    provider: str,
    max_retries: int = 3,
) -> str:
    """LLM-01: Exponential backoff with jitter on 429/5xx."""
    import random
    import time
    last_exc = None
    for attempt in range(max_retries):
        if _circuit_open(provider):
            raise ProviderDown(f"{provider}: circuit breaker open")
        try:
            result = fn()
            _circuit_record(provider, True)
            return result
        except requests.HTTPError as e:
            status = e.response.status_code if e.response else 0
            _circuit_record(provider, False)
            last_exc = e
            if status == 429:
                raise RateLimited(f"{provider}: rate limited")
            if 500 <= status < 600 and attempt < max_retries - 1:
                delay = (2 ** attempt) + random.uniform(0, 1)
                log.info("retry provider=%s attempt=%d delay=%.1f",
                         provider, attempt + 1, delay)
                time.sleep(delay)
                continue
            raise ProviderDown(f"{provider}: HTTP {status}")
        except (requests.ConnectionError, requests.Timeout) as e:
            _circuit_record(provider, False)
            last_exc = e
            if attempt < max_retries - 1:
                delay = (2 ** attempt) + random.uniform(0, 1)
                time.sleep(delay)
                continue
            raise ProviderDown(f"{provider}: connection failed")
    raise ProviderDown(f"{provider}: all retries failed: {last_exc}")


def _key(name: str) -> str | None:
    key = (os.getenv(name) or "").strip().strip('"').strip("'")
    return key if key and key not in _KEY_SENTINELS else None


def gemini_configured() -> bool:
    return _key("GEMINI_API_KEY") is not None


def groq_configured() -> bool:
    return _key("GROQ_API_KEY") is not None


def openrouter_configured() -> bool:
    return _key("OPENROUTER_API_KEY") is not None


def ai_ready() -> bool:
    """True when at least one provider key is configured."""
    return gemini_configured() or groq_configured() or openrouter_configured() \
        or local_configured()


def local_configured() -> bool:
    """PRIV-05: True when LLM_BASE_URL points at a local OpenAI-compatible endpoint."""
    return bool(os.getenv("LLM_BASE_URL", "").strip())


def llm_mode() -> str:
    """PRIV-05: current LLM mode — off|byok|server (from env, default server)."""
    mode = os.getenv("LLM_MODE", "server").strip().lower()
    return mode if mode in ("off", "byok", "server") else "server"


def _local_generate(prompt: str, api_key: str | None = None) -> str:
    """PRIV-05: generate via local OpenAI-compatible endpoint (Ollama etc.)."""
    base = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
    if not base:
        raise AIError("Local LLM not configured (LLM_BASE_URL)")
    return _openai_compatible_generate(
        base + "/chat/completions", api_key or "ollama",
        os.getenv("LLM_MODEL", "llama3.2"), prompt, "local")


def providers_configured() -> list[str]:
    """Ordered list of configured provider names (for status pages)."""
    out = []
    if gemini_configured():
        out.append("gemini")
    if groq_configured():
        out.append("groq")
    if openrouter_configured():
        out.append("openrouter")
    return out


def _openai_compatible_generate(
    url: str,
    key: str,
    model: str,
    prompt: str,
    provider: str,
) -> str:
    """POST a chat-completion request to an OpenAI-compatible endpoint."""
    def _call() -> str:
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        if provider == "openrouter":
            headers["HTTP-Referer"] = "https://study-buddy.app"
            headers["X-Title"] = "STUDY-BUDDY"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
        }
        r = requests.post(url, headers=headers, json=payload,
                          timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        text = (data["choices"][0]["message"]["content"] or "").strip()
        if not text:
            raise BadOutput(f"{provider}: empty response")
        return text
    return _retry_with_backoff(_call, provider)


def _groq_generate(prompt: str) -> str:
    key = _key("GROQ_API_KEY")
    if not key:
        raise AIError("Groq key not configured")
    return _openai_compatible_generate(_GROQ_URL, key, _GROQ_MODEL, prompt, "groq")


def _openrouter_generate(prompt: str) -> str:
    key = _key("OPENROUTER_API_KEY")
    if not key:
        raise AIError("OpenRouter key not configured")
    return _openai_compatible_generate(_OPENROUTER_URL, key, _OPENROUTER_MODEL,
                                       prompt, "openrouter")


def _gemini_generate(prompt: str) -> str:
    """Delegate to the existing Gemini implementation in unified_app."""
    # Local import to avoid a hard dependency cycle at module load.
    import unified_app as app_mod
    return app_mod.call_gemini_direct(prompt)


def _model_for(provider_name: str) -> str:
    """Map a gateway provider key to the model id for the LLM ledger.

    Gemini's model is discovered dynamically in unified_app, so we record
    the provider key there (the ledger treats it as budgeting math anyway).
    """
    return {
        "local": _LOCAL_MODEL,
        "gemini": "gemini",  # dynamically discovered in unified_app
        "groq": _GROQ_MODEL,
        "openrouter": _OPENROUTER_MODEL,
        "byok": _OPENROUTER_MODEL,
    }.get(provider_name, provider_name)


def generate(
    prompt: str,
    user_key: str | None = None,
    redact_pii: bool = True,
    _llm_user_id: int | None = None,
    _llm_feature: str = "generate",
) -> str:
    """Generate text via the first working provider.

    PRIV-05 modes:
    - LLM_MODE=off: raises AIError("disabled") — callers use rule-based fallbacks.
    - LLM_MODE=byok: uses user_key (the user's own API key) via OpenRouter;
      raises if no key provided.
    - default: server chain — local (LLM_BASE_URL) -> Gemini -> Groq -> OpenRouter.

    user_key is never logged.

    LLM-06: PII is redacted from the prompt before sending to the provider
    (unless redact_pii=False and the caller has a specific need).

    LLM-03: pass _llm_user_id (int) + _llm_feature (str) to enforce the
    daily quota / monthly budget ledger. Raises QuotaExceeded when exhausted
    (callers catch it and render the rule-based UX). When _llm_user_id is
    None (system call / not logged in) the guard is a no-op.
    """
    # LLM-03: quota/budget guard (no-op when _llm_user_id is None)
    from studybuddy.llm_ledger import guard_llm_action, log_llm_action
    guard_llm_action(_llm_user_id, _llm_feature)
    # LLM-06: Pre-LLM redaction
    redaction_map = {}
    if redact_pii:
        from studybuddy.pii_redaction import redact_for_llm
        prompt, redaction_map = redact_for_llm(prompt)
        if redaction_map:
            log.info("llm_prompt_redacted count=%d", len(redaction_map))
    # LLM-04: Check cache (skip for BYOK — user keys differ)
    use_cache = user_key is None
    if use_cache:
        cached = _cache_get(prompt)
        if cached:
            if redaction_map:
                from studybuddy.pii_redaction import unredact
                cached = unredact(cached, redaction_map)
            return cached
    mode = llm_mode()
    if mode == "off":
        raise AIError(
            "AI features are disabled (LLM_MODE=off). "
            "Rule-based results are shown instead.")
    if mode == "byok":
        if not user_key or user_key in _KEY_SENTINELS:
            raise AIError(
                "BYOK mode: add your own API key in Profile → AI Settings "
                "to use AI features.")
        # Use the user's key against OpenRouter (OpenAI-compatible)
        return _openai_compatible_generate(
            _OPENROUTER_URL, user_key, _OPENROUTER_MODEL, prompt, "byok")

    chain = []
    if local_configured():
        chain.append(("local", _local_generate))
    if gemini_configured():
        chain.append(("gemini", _gemini_generate))
    if groq_configured():
        chain.append(("groq", _groq_generate))
    if openrouter_configured():
        chain.append(("openrouter", _openrouter_generate))
    if not chain:
        raise AIError(
            "No AI provider is configured. Add GEMINI_API_KEY, GROQ_API_KEY, "
            "or OPENROUTER_API_KEY to your environment.")

    errors = []
    for name, fn in chain:
        try:
            import time
            start = time.time()
            text = fn(prompt)
            latency_ms = int((time.time() - start) * 1000)
            if text:
                # LLM-08: observability — provider, latency, prompt size
                # (never log prompt content or user keys)
                log.info("ai_request provider=%s latency_ms=%d "
                         "prompt_chars=%d response_chars=%d cached=%s",
                         name, latency_ms, len(prompt), len(text),
                         False)
                # LLM-06: reinsert redacted PII into response
                if redaction_map:
                    from studybuddy.pii_redaction import unredact
                    text = unredact(text, redaction_map)
                # LLM-04: cache successful responses
                if use_cache:
                    _cache_put(prompt, text)
                # LLM-03: ledger — record the successful call for quota/budget
                log_llm_action(_llm_user_id, _llm_feature, provider=name,
                               model=_model_for(name),
                               prompt_chars=len(prompt),
                               response_chars=len(text))
                return text
            errors.append(f"{name}: empty response")
        except Exception as exc:
            log.warning("AI gateway: %s failed: %r", name, exc)
            errors.append(f"{name}: {exc}")
    raise AIError(
        "All AI providers failed just now. Please try again in a moment. "
        f"({'; '.join(errors)[:200]})")


# LLM-02: Structured outputs with pydantic validation
def generate_structured(
    prompt: str,
    schema_cls: type[Any],
    user_key: str | None = None,
    max_repair: int = 1,
    _llm_user_id: int | None = None,
    _llm_feature: str = "generate_structured",
) -> Any:
    """Generate JSON conforming to a pydantic schema.

    Args:
        prompt: base prompt (schema instructions auto-appended)
        schema_cls: pydantic BaseModel class
        user_key: optional BYOK key
        max_repair: retries with repair prompt on validation failure

    Returns:
        Validated schema instance. Raises AIError if all fail.
    """
    import json
    schema_json = schema_cls.model_json_schema()
    full_prompt = (
        f"{prompt}\n\n"
        f"Respond with ONLY valid JSON matching this schema:\n"
        f"{json.dumps(schema_json, indent=2)}\n"
        f"No markdown, no explanation, just the JSON."
    )
    last_err = None
    for attempt in range(max_repair + 1):
        try:
            text = generate(full_prompt, user_key=user_key,
                            _llm_user_id=_llm_user_id,
                            _llm_feature=_llm_feature)
            # Strip markdown code fences if present
            text = text.strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text)
            data = json.loads(text)
            return schema_cls.model_validate(data)
        except Exception as e:
            last_err = e
            log.warning("structured_output attempt %d failed: %r",
                        attempt + 1, e)
            if attempt < max_repair:
                full_prompt += (
                    f"\n\nPrevious output was invalid: {e}. "
                    f"Return ONLY valid JSON."
                )
    raise BadOutput(f"Structured output failed: {last_err}")
