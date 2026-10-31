"""RAG-03: dense retrieval + hybrid fusion (BM25 x dense via RRF).

Three embedder implementations selectable by config (``RAG_EMBEDDER`` env
or an explicit argument):

- ``"gemini"`` — Gemini embedding API (pinned model
  ``models/text-embedding-004``, 768 dims). The roadmap notes that
  ``gemini-embedding-2`` and ``gemini-embedding-001`` live in incompatible
  vector spaces, so the model id AND dims are stored alongside every
  vector and changing models requires re-embedding.
- ``"local"`` — documented stub: a small local ONNX model via fastembed
  (e.g. bge-small class) was evaluated on paper, but resident memory on
  the 512 MB tier has not been measured yet, so it is NOT adopted here.
  Instantiating it is fine; calling ``embed`` raises a clear error.
- ``"none"`` (default) — no dense retrieval; the pure-Python lexical path
  (BM25 + TF-IDF from :mod:`studybuddy.pdf_qa`) is the whole retriever and
  the graceful fallback when dense is unavailable or fails.

Fusion is Reciprocal Rank Fusion: ``score = sum(1 / (k + rank_i))`` with
``k = 60``, ``rank_i`` 1-based, summed over the lexical and dense ranked
lists. Every vector is tagged ``{"model", "dims"}`` so a model change is
detectable at read time.

Embeddings are cached in-process by SHA-256 of the text so a chunk is
never embedded twice in one session; batching (32 texts/call) keeps
free-tier RPM usage low. No keys are logged or stored — the Gemini key is
read from ``GEMINI_API_KEY`` at call time.
"""

from __future__ import annotations

import abc
import hashlib
import math
import os
from typing import Any

from studybuddy.pdf_qa import retrieve as lexical_retrieve

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Pinned Gemini embedding model + dims (changing either requires
#: re-embedding: vector spaces are NOT compatible across models).
GEMINI_EMBEDDING_MODEL = "models/gemini-embedding-001"
GEMINI_EMBEDDING_DIMS = 3072

#: Texts per embedding API call (free-tier RPM is the binding constraint).
EMBED_BATCH_SIZE = 32

#: RRF constant from the roadmap: score = sum(1 / (RRF_K + rank)).
RRF_K = 60

#: How deep each side goes before fusion (fusion needs ranks, not scores).
FUSION_CANDIDATE_DEPTH = 50

_CONFIG_VALUES = ("gemini", "local", "none")

#: In-process embedding cache: sha256(text) -> (model, dims, vector).
_EMBED_CACHE: dict[str, tuple[str, int, list[float]]] = {}


# ---------------------------------------------------------------------------
# 1. Embedder interface + implementations
# ---------------------------------------------------------------------------

class Embedder(abc.ABC):
    """Dense-embedding provider. ``embed`` returns one vector per text."""

    model_name: str = "unknown"
    dims: int = 0

    @abc.abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts``; returns a list of ``dims``-length float vectors."""

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string."""
        vecs = self.embed([text or ""])
        return vecs[0] if vecs else [0.0] * self.dims

    def provenance(self) -> dict[str, Any]:
        """``{"model", "dims"}`` — stored with every vector."""
        return {"model": self.model_name, "dims": self.dims}


class GeminiEmbedder(Embedder):
    """Gemini embedding API (pinned model, 3072 dims).

    The API key is read from ``GEMINI_API_KEY`` on every call and never
    stored or logged. Calls are batched; failures raise ``RuntimeError``
    with a redacted message so callers can fall back to lexical retrieval.
    """

    model_name = GEMINI_EMBEDDING_MODEL
    dims = GEMINI_EMBEDDING_DIMS

    def __init__(self, batch_size: int = EMBED_BATCH_SIZE) -> None:
        self.batch_size = max(1, int(batch_size))

    def _client(self):
        try:
            from google import genai
        except ImportError as exc:
            raise RuntimeError(
                "google-genai is not installed; cannot use the Gemini "
                "embedder. Set RAG_EMBEDDER=none for lexical-only retrieval."
            ) from exc
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set; cannot use the Gemini embedder. "
                "Set RAG_EMBEDDER=none for lexical-only retrieval."
            )
        return genai.Client(api_key=api_key)

    def embed(self, texts: list[str]) -> list[list[float]]:
        texts = [t if isinstance(t, str) else "" for t in (texts or [])]
        if not texts:
            return []
        client = self._client()
        vectors: list[list[float]] = []
        try:
            for i in range(0, len(texts), self.batch_size):
                batch = texts[i:i + self.batch_size]
                resp = client.models.embed_content(
                    model=self.model_name, contents=batch)
                for emb in resp.embeddings or []:
                    values = list(emb.values or [])
                    if len(values) != self.dims:
                        raise RuntimeError(
                            "Gemini returned %d dims, expected %d — the "
                            "embedding model may have changed; re-embed "
                            "with the pinned model." % (len(values),
                                                         self.dims))
                    vectors.append([float(v) for v in values])
        except RuntimeError:
            raise
        except Exception as exc:
            # Never leak the key: the message carries only the error type.
            raise RuntimeError(
                "Gemini embedding call failed (%s); falling back to "
                "lexical retrieval is safe." % type(exc).__name__) from exc
        if len(vectors) != len(texts):
            raise RuntimeError(
                "Gemini returned %d vectors for %d texts."
                % (len(vectors), len(texts)))
        return vectors


class LocalEmbedder(Embedder):
    """Documented stub for a local ONNX embedder (fastembed, bge-small).

    NOT adopted: the roadmap requires measuring resident memory on the
    512 MB tier before adopting a local model, and that measurement has
    not been done. Instantiating is harmless; ``embed`` raises with
    guidance. This keeps the config surface honest — "local" is a real
    option in the config schema, not a silent no-op.
    """

    model_name = "fastembed/bge-small (not adopted)"
    dims = 384

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError(
            "Local ONNX embedding is not enabled: resident memory on the "
            "512 MB tier has not been measured (see RAG-03). Install "
            "fastembed, measure RSS, then implement this class. Until then "
            "use RAG_EMBEDDER=gemini or RAG_EMBEDDER=none (lexical-only).")


def select_embedder(name: str | None = None) -> Embedder | None:
    """Return the configured embedder, or None for lexical-only mode.

    ``name`` overrides the ``RAG_EMBEDDER`` env var (default ``"none"``).
    Unknown values raise ``ValueError`` — a misconfigured retriever must
    fail loudly, not silently degrade.
    """
    raw = name if name is not None else os.environ.get("RAG_EMBEDDER", "none")
    key = str(raw).strip().lower()
    if key == "none":
        return None
    if key == "gemini":
        return GeminiEmbedder()
    if key == "local":
        return LocalEmbedder()
    raise ValueError(
        "Unknown RAG_EMBEDDER=%r; expected one of %s."
        % (raw, ", ".join(_CONFIG_VALUES)))


# ---------------------------------------------------------------------------
# 2. Embedding cache (content-hash keyed, in-process)
# ---------------------------------------------------------------------------

def _cache_key(text: str, model: str, dims: int) -> str:
    return hashlib.sha256(
        "|".join([model, str(dims), text]).encode("utf-8")).hexdigest()


def embed_texts_cached(embedder: Embedder,
                       texts: list[str]) -> list[list[float]]:
    """Embed ``texts`` reusing the in-process content-hash cache.

    Returns vectors aligned with ``texts``; each vector is tagged by the
    embedder's provenance at the call site (see :func:`hybrid_retrieve`).
    """
    prov = embedder.provenance()
    keys = [_cache_key(t, prov["model"], prov["dims"]) for t in texts]
    missing_idx = [i for i, k in enumerate(keys) if k not in _EMBED_CACHE]
    if missing_idx:
        fresh = embedder.embed([texts[i] for i in missing_idx])
        for i, vec in zip(missing_idx, fresh, strict=True):
            _EMBED_CACHE[keys[i]] = (prov["model"], prov["dims"],
                                     [float(v) for v in vec])
    return [_EMBED_CACHE[k][2] for k in keys]


def clear_embed_cache() -> None:
    """Drop the in-process embedding cache (tests / model changes)."""
    _EMBED_CACHE.clear()


# ---------------------------------------------------------------------------
# 3. Pure-Python cosine + dense ranking
# ---------------------------------------------------------------------------

def cosine_sim(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two dense vectors (0.0 on zero vectors)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def rank_dense(num_chunks: int, query_vec: list[float],
               chunk_vecs: list[list[float]]) -> list[tuple[int, float]]:
    """Rank chunks by cosine similarity: ``[(chunk_pos, score)]`` best-first.

    Ties break on chunk position ascending (deterministic). Zero query
    vectors yield ``[]`` — the caller then falls back to lexical only.
    """
    if not query_vec or all(v == 0.0 for v in query_vec):
        return []
    scored = [(i, cosine_sim(query_vec, vec))
              for i, vec in enumerate(chunk_vecs)]
    scored = [(i, s) for i, s in scored if s > 0.0]
    scored.sort(key=lambda p: (-p[1], p[0]))
    return scored


# ---------------------------------------------------------------------------
# 4. Reciprocal Rank Fusion
# ---------------------------------------------------------------------------

def reciprocal_rank_fusion(rankings: list[list[Any]],
                           k: int = RRF_K) -> dict[Any, float]:
    """Fuse ranked lists: ``score(key) = sum(1 / (k + rank_i))``.

    ``rankings`` are best-first key lists (``rank_i`` is 1-based). Keys
    present in several lists accumulate; a key in one list still scores.
    Deterministic. Never raises on bad input (yields ``{}``).
    """
    try:
        k = int(k)
    except (TypeError, ValueError):
        return {}
    scores: dict[Any, float] = {}
    if not isinstance(rankings, (list, tuple)):
        return scores
    for ranking in rankings:
        if not isinstance(ranking, (list, tuple)):
            continue
        for rank, key in enumerate(ranking, start=1):
            try:
                scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            except TypeError:
                continue  # unhashable key: skip, never crash
    return scores


# ---------------------------------------------------------------------------
# 5. Hybrid retrieve
# ---------------------------------------------------------------------------

def hybrid_retrieve(
    chunks: list[dict],
    query: str,
    top_k: int = 5,
    embedder: Embedder | None = None,
    rrf_k: int = RRF_K,
    candidate_depth: int = FUSION_CANDIDATE_DEPTH,
) -> list[dict]:
    """Hybrid BM25/TF-IDF + dense retrieval fused with RRF.

    ``chunks`` are dicts with at least ``"text"`` (chunk position is the
    fusion key). The lexical side always runs (pure-Python, zero config);
    the dense side runs only when ``embedder`` is given AND succeeds — any
    embedder failure degrades gracefully to lexical-only instead of
    raising.

    Returns up to ``top_k`` dicts, each a copy of the chunk plus::

        {"rrf_score": float, "lexical_rank": int | None,
         "dense_rank": int | None, "embedding_model": str | None,
         "embedding_dims": int | None}

    ``lexical_rank``/``dense_rank`` are 1-based (None when the chunk was
    not in that side's candidate list). Deterministic; never raises.
    """
    try:
        if not isinstance(chunks, (list, tuple)) or not chunks:
            return []
        if not isinstance(query, str) or not query.strip():
            return []
        top_k = max(1, int(top_k))
    except (TypeError, ValueError):
        return []

    texts = [c.get("text", "") if isinstance(c, dict) else "" for c in chunks]

    lexical_hits = lexical_retrieve(chunks, query, top_k=candidate_depth)
    # Map lexical hits back to chunk positions: retrieve() returns copies,
    # so match on text in order (first unused match wins).
    lex_positions: list[int] = []
    used = [False] * len(chunks)
    for hit in lexical_hits:
        htext = hit.get("text", "")
        for i, t in enumerate(texts):
            if not used[i] and t == htext:
                used[i] = True
                lex_positions.append(i)
                break
    lexical_ranking = lex_positions

    dense_positions: list[int] = []
    emb_model: str | None = None
    emb_dims: int | None = None
    if embedder is not None:
        try:
            query_vec = embedder.embed_query(query)
            chunk_vecs = embed_texts_cached(embedder, texts)
            ranked = rank_dense(len(chunks), query_vec, chunk_vecs)
            dense_positions = [i for i, _ in ranked[:candidate_depth]]
            prov = embedder.provenance()
            emb_model, emb_dims = prov["model"], prov["dims"]
        except Exception:
            dense_positions = []  # graceful degradation: lexical only

    rankings = [r for r in (lexical_ranking, dense_positions) if r]
    if not rankings:
        return []
    fused = reciprocal_rank_fusion(rankings, k=rrf_k)

    lex_rank_of = {pos: r + 1 for r, pos in enumerate(lexical_ranking)}
    dense_rank_of = {pos: r + 1 for r, pos in enumerate(dense_positions)}
    ordered = sorted(fused, key=lambda p: (-fused[p], p))
    results: list[dict] = []
    for pos in ordered[:top_k]:
        chunk = chunks[pos]
        out = dict(chunk) if isinstance(chunk, dict) else {"text": texts[pos]}
        out["rrf_score"] = fused[pos]
        out["lexical_rank"] = lex_rank_of.get(pos)
        out["dense_rank"] = dense_rank_of.get(pos)
        out["embedding_model"] = emb_model
        out["embedding_dims"] = emb_dims
        results.append(out)
    return results
