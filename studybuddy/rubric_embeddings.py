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

EMBEDDING_MODEL = "gemini-embedding-001"
_FLAG_ENV = "RUBRIC_EMBEDDINGS"

#: Random-pair baseline for gemini-embedding-001 cosine similarity.
#: Measured 2026-10-31: unrelated calibration pairs score ~0.50, not 0.
#: Raw cosines live in a compressed range, so calibrated_cosine() maps
#: [baseline, 1.0] -> [0.0, 1.0] to make scores mean what they look like.
EMBEDDING_RANDOM_BASELINE = 0.50


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


def calibrated_cosine(va, vb,
                      baseline: float = EMBEDDING_RANDOM_BASELINE) -> float:
    """Embedding cosine shifted so the random-pair baseline maps to 0.

    Raw embedding cosines live in a compressed range (unrelated texts
    score ~0.50 on gemini-embedding-001, not 0). This linearly maps
    [baseline, 1.0] -> [0.0, 1.0], clamping at-baseline-or-below to 0.
    Makes scores mean what they look like: 0 = nothing in common.
    """
    raw = cosine_vectors(va, vb)
    if raw <= baseline:
        return 0.0
    return round((raw - baseline) / (1.0 - baseline), 4)


def calibrated_embedding_similarity(a_text: str | None,
                                    b_text: str | None,
                                    va, vb) -> float:
    """Grading-ready similarity from embedding vectors + texts.

    Two corrections on top of raw cosine (see calibration 2026-10-31):

    1. Baseline shift (calibrated_cosine): unrelated pairs score ~0.50
       raw, so shift the scale to make 0 mean "nothing in common".
    2. Scramble guard: embeddings are order-blind — a shuffled
       word-salad scores 0.84 raw. When unigram overlap is high but
       bigram recall is low (same words, shuffled order), it's keyword
       salad: scale the score down. Paraphrases (low unigram overlap)
       are unaffected by the guard.

    Returns a score in [0, 1]. Deterministic; never raises.
    """
    cal = calibrated_cosine(va, vb)
    if cal <= 0.50:
        return cal
    try:
        from studybuddy.rubric_tfidf import (ngram_order_recall,
                                             unigram_recall)
        uni = unigram_recall(a_text, b_text)
        order = ngram_order_recall(a_text, b_text)
    except Exception:
        return cal
    if uni > 0.60 and order < 0.30:
        # Same words, shuffled: keyword salad. Scale down in proportion
        # to incoherence (order=0 -> 0.25x, order=0.30 -> 1.0x).
        cal = cal * (0.25 + 0.75 * (order / 0.30))
    return round(max(0.0, min(1.0, cal)), 4)


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
