"""Rubric embeddings (issue #55 / GFI #6): HYBRID scoring — API + improved TF-IDF.

Decision (maintainer, 2026-10-28; hybrid confirmed by user 2026-10-31):
Gemini API embeddings when the RUBRIC_EMBEDDINGS flag is on AND a key is
configured; otherwise the improved lexical scorer in
studybuddy.rubric_tfidf (n-grams, sublinear TF, better stemming) as the
fallback. A local model was rejected for the Render free tier (RAM +
cold-start budget).

Precedence:
- Flag OFF (default) -> get_rubric_embed() returns None -> callers score
  with the LEGACY TF-IDF exactly as before (byte-identical output, no
  similarity_signal key). This is the safety guarantee.
- Flag ON + key -> memoized Gemini embedder -> "embeddings"/"gemini".
- Flag ON + no key -> null embedder (callable returning None) -> callers
  take the flag-on path with the improved TF-IDF fallback, labelled
  "tfidf". No network attempt is made without a key.

Rules this module enforces:
- Any gateway failure -> gemini_embed() returns None -> callers fall back
  to improved TF-IDF silently. Scoring never 500s and never blocks on the
  network beyond the embed call itself (the SDK's 30s HTTP timeout, same
  as generation).
- Honest labelling: TF-IDF is NEVER described as embeddings, and the
  embeddings signal is labelled "embeddings"/"gemini" wherever it
  surfaces (score dicts carry "similarity_signal" and "similarity_method"
  keys when the flag is on).

Top-level imports are stdlib only. unified_app is imported lazily inside
gemini_embed (the study_library.py pattern) because importing it at top
level would boot the whole app (and create a cycle: unified_app imports
the rubric scorers, which import this module).
"""

from __future__ import annotations

import math
import os

EMBEDDING_MODEL = "text-embedding-004"
_FLAG_ENV = "RUBRIC_EMBEDDINGS"


def embeddings_enabled() -> bool:
    """True only when the RUBRIC_EMBEDDINGS flag is on (default OFF).

    Reads the app config singleton (loaded from env at boot, the declared
    source of truth); falls back to the env var directly if the config
    cannot be imported (e.g. standalone scripts).
    """
    try:
        from studybuddy.app_config import config

        return bool(config.rubric_embeddings)
    except Exception:
        pass
    return (os.environ.get(_FLAG_ENV) or "").strip().lower() in (
        "1", "true", "yes", "on")


def cosine_vectors(va, vb) -> float:
    """Cosine similarity of two embedding vectors, clamped to [0, 1]."""
    if not va or not vb:
        return 0.0
    dot = sum(x * y for x, y in zip(va, vb, strict=False))
    na = math.sqrt(sum(x * x for x in va))
    nb = math.sqrt(sum(y * y for y in vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


def gemini_embed(text: str) -> list[float] | None:
    """Embed one text via Gemini. Returns None on ANY failure (never raises).

    Reuses the existing google-genai client (unified_app._make_gemini_client
    — the same SDK and key already used for generation); no new provider
    SDK, no new dependency. A None client (no key / no SDK) means None,
    which callers treat as "fall back to TF-IDF".
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        import unified_app as app_mod

        client = app_mod._make_gemini_client()
        if client is None:
            return None
        resp = client.models.embed_content(
            model=EMBEDDING_MODEL, contents=text)
        embeddings = getattr(resp, "embeddings", None) or []
        if not embeddings:
            return None
        values = getattr(embeddings[0], "values", None) or []
        if not values:
            return None
        return [float(v) for v in values]
    except Exception:
        return None


def memoized_embed(embed_fn, limit: int = 512):
    """Per-score-call cache: the answer is embedded once, not once per key
    point (a mock answer with N key points would otherwise burn N+1 API
    calls). Failed embeds (None) are cached too, so one call never
    retries a failing provider in a loop. The cache is local to the
    returned callable — no cross-request state.
    """
    cache: dict[str, list[float] | None] = {}

    def cached(text: str):
        if text not in cache:
            if len(cache) >= limit:
                cache.clear()
            cache[text] = embed_fn(text)
        return cache[text]

    return cached


def get_rubric_embed():
    """Embed callable for rubric scoring, or None.

    None ONLY when the flag is off (default) — callers then use the
    legacy TF-IDF with byte-identical output and make no network call.

    When the flag is on, always returns a callable:
    - Key configured -> memoized Gemini embedder (API path).
    - No key -> a null embedder (returns None for every text). Callers
      treat this as "flag on, API unavailable": they use the improved
      TF-IDF fallback and label the signal "tfidf". No network attempt
      is ever made without a key.
    """
    if not embeddings_enabled():
        return None
    try:
        from studybuddy.ai_gateway import gemini_configured

        has_key = bool(gemini_configured())
    except Exception:
        has_key = False
    if has_key:
        return memoized_embed(gemini_embed)
    return memoized_embed(_null_embed)


def _null_embed(text: str) -> None:
    """Null embedder: always None. Signals 'flag on, no key' to callers,
    which then use the improved TF-IDF fallback (never the network)."""
    return None
