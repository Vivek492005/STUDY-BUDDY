# Rubric embeddings — decision note (issue #55 / GFI #6)

## Decision (maintainer, 2026-10-28 — not re-litigated here)

- **API embeddings via Gemini** behind a feature flag.
- **TF-IDF stays the DEFAULT scoring path.** Flag off = byte-identical output.
- **Local model rejected** for the Render free tier (RAM + cold-start budget).

## Flag

- Env var: `RUBRIC_EMBEDDINGS` (`1`/`true`/`yes`/`on`); app config field
  `AppConfig.rubric_embeddings` (default `False`).
- Read via `studybuddy.rubric_embeddings.embeddings_enabled()`; the embed
  callable comes from `get_rubric_embed()` (None when flag off or Gemini
  unconfigured — callers then use TF-IDF and make no network call).

## Implementation

- New module `studybuddy/rubric_embeddings.py` (stdlib-only top-level
  imports; unified_app imported lazily inside `gemini_embed`, the
  study_library.py pattern — no cycle, no new dependency: the google-genai
  SDK is already used by `unified_app._make_gemini_client`).
- Embedding model: `text-embedding-004` via `client.models.embed_content`.
- Wiring:
  - `mock_interview.score_answer(..., embed=...)` — new optional param;
    similarity via `_key_point_similarity_with_signal` (all-or-nothing per
    call: "embeddings" only if at least one embedding cosine was actually
    computed, else TF-IDF for the whole call and signal "tfidf").
  - `eng_interview_v2.score_answer(..., embed=...)` — param already existed;
    now labelled: "embeddings" if any key point used the embedder, else
    "tfidf". Note: v2 falls back PER key point inside `semantic_similarity`
    (pre-existing design, unchanged); the label means "embeddings used for
    at least one point, lexical fallback for any failed pair".
  - Routes pass `embed=get_rubric_embed()` (mock submit + score-v2 API).
- Per-score-call memoization: the answer is embedded once, not once per
  key point (`memoized_embed`, local cache, failed embeds cached too).

## Fallback discipline

- `gemini_embed()` returns None on ANY failure and never raises.
- Any embed failure → TF-IDF values, signal "tfidf". Scoring never 500s
  because of embeddings and never blocks beyond the SDK's 30s timeout.
- No key configured → no network attempt at all (flag-on but keyless
  behaves exactly like flag-off, plus the "tfidf" label).

## Honest labelling

- Score dicts gain `similarity_signal` ONLY when an embed callable was
  passed (flag on). Flag-off output is byte-identical to before.
- TF-IDF is never described as embeddings anywhere (UI copy, comments,
  docs, or the calibration report).
- The calibration test's stub embedder is a clearly-labelled TEST DOUBLE
  (hand-written concept groups). It proves wiring, not semantics — it is
  ALSO fooled by the scrambled pair, exactly like TF-IDF. Only real Gemini
  embeddings could distinguish that pair.

## Calibration

- 8 hand-written, human-labelled pairs in
  `tests/test_rubric_embeddings_calibration.py` (PAIRS). TF-IDF column is
  real and deterministic; the live Gemini column requires GEMINI_API_KEY
  (absent in this environment — `scripts/rubric_calibration_report.py`
  reports "not configured" rather than inventing numbers).
- No live Gemini before/after numbers were produced in this environment.
  Run the report script with a key to fill the after column.

## Deliberately left out

- No UI surface changes: no template renders similarity today, so the
  JSON `similarity_signal` key is the surfacing mechanism. A future UI
  pass can display it.
- No run-result caching by input hash (noted as a future free-tier
  optimisation, same as the Lab plan).
- No local-model code path (rejected by the decision).
- `resume_interview.py`'s use of `score_answer` is untouched (embed=None
  default) — flag-on wiring covers the mock submit route and the v2 API.
