# Good-first-issue drafts (P1/P2 leftovers) — DRAFTS, NOT YET POSTED

These 10 issues were drafted after the 2026-10-24 Claude evaluation's P1/P2
pass (Oct 2026). They are **drafts for maintainer review** — post them to
GitHub only after review, since issues are public. Each one is real,
scoped work; none of them requires inventing users, data, or results.

## 1. Hindi switcher: wire translations into a real language toggle
- **Problem:** `studybuddy/i18n.py` and `translations/hi/` exist, but no
  template links to them and `?lang=hi` does nothing. The README correctly
  lists vernacular support as unchecked roadmap, not shipped.
- **Work:** pick one high-traffic page (landing), mark its strings, ship a
  working toggle that persists the choice, and prove it with a test that
  renders the page in Hindi. Do NOT machine-translate the whole site in one
  PR — one page, fully correct, is the goal.
- **Accept:** toggle works end-to-end on the landing page; test asserts a
  Hindi string renders; no other page regresses.

## 2. Monolith split, phase 1: extract the study-library routes
- **Problem:** `unified_app.py` is 9,373 lines. A big-bang split is unsafe.
- **Work:** move ONLY the study-library routes (`/research/library`,
  `/study/api/*`) into a `studybuddy/library_routes.py` blueprint, with
  lazy model imports (models live in `unified_app` — a top-level import
  would be circular). Keep every URL, response shape, and test green.
- **Accept:** full study-library test file green; no route changes; the
  blueprint registers from `register_routes` with zero behavior delta.

## 3. Monolith split, phase 2: extract the DSA routes
- Same pattern as #2, for the DSA/A2Z route group. Depends on #2's pattern
  being merged first so the two extractions don't collide.

## 4. CSP soak: watch /csp-report for two weeks, fix stragglers
- **Problem:** CSP is enforced (Oct 2026 nonce rollout), but rare pages and
  flows may still trip it.
- **Work:** review `/csp-report` logs weekly for two weeks; every real
  violation gets a fix (nonce or listener), every false positive gets
  documented. No `unsafe-inline` reintroduction.
- **Accept:** two weekly reports posted as comments; violation count
  trending to zero.

## 5. Real-benchmark collection: 100 opt-in real resumes
- **Problem:** the benchmark harness (`scripts/benchmark_resume.py`) runs
  on a clearly-labeled SYNTHETIC set. Real resumes would calibrate it.
- **Work:** build an opt-in submission flow (explicit consent, PII
  redaction preview, delete-anytime) and a `--real-dir` run that reports
  aggregate bands only — never individual resumes.
- **Accept:** consent + redaction + deletion all work; aggregate report
  runs; zero PII stored without consent.

## 6. Semantic embeddings for the interview rubric (infra decision first)
- **Problem:** the rubric's similarity signal is lexical TF-IDF cosine,
  explicitly NOT embeddings (a no-shared-words paraphrase scores 0).
- **Work:** first decide the infra (local model vs API embeddings, RAM and
  cold-start budget on the free tier), then implement behind a flag with a
  human-labelled calibration set. Do NOT relabel TF-IDF as embeddings.
- **Accept:** decision written up; flagged implementation; calibration set
  with before/after scores.

## 7. RAG stopword handling (known miss: abs-02)
- **Problem:** the RAG harness shows `abs-02` (sourdough) failing
  abstention because `_tokenize` doesn't strip stopwords, so "how"
  matched "show exactly how…" at score 1.0.
- **Work:** add stopword handling to retrieval, re-run
  `scripts/benchmark_rag.py`, and keep accuracy ≥ 0.96 while fixing
  abstention. Engine change, so it needs the full RAG-adjacent suites
  green, not just the harness.
- **Accept:** harness re-run with abstention fixed and no accuracy drop.

## 8. Cursor glow: move the 390px fix from /resume to cursor.css
- **Problem:** the 520px `.sb-cursor-glow` overflows narrow viewports
  site-wide; /resume got a page-scoped `@media` fix, but every other
  page still carries the glow (harmless for scroll, flagged by strict
  bounding-box checks).
- **Work:** fix it once in `static/css/cursor.css` (hide on touch-width
  screens), then re-measure /resume + 3 other pages at 390px.
- **Accept:** zero overflowing elements at 390px on all measured pages.

## 9. Live axe re-crawl (residual unnamed-button question)
- **Problem:** the eval's "50/56 pages unnamed buttons" did not reproduce
  in a static template audit (0 truly unnamed found); the remainder, if
  any, lives in JS-generated or runtime-empty states.
- **Work:** run a live axe-core crawl (logged-in) over all routes and
  file one issue per real finding with a repro. No speculative fixes.
- **Accept:** crawl report committed; every real finding has an issue.

## 10. Invalid HTML: stray <footer> inside <head> on 3 report pages
- **Problem:** `coach_report`, `dsa_diagnosis`, `interview_report` contain
  a `<footer>` block inside `<head>` (browsers hoist it, but it is
  invalid and fragile for landmark placement).
- **Work:** move each footer into the body in the correct landmark order,
  keeping rendered output identical (screenshot before/after).
- **Accept:** pages validate; rendered output unchanged; report-page
  tests green.
