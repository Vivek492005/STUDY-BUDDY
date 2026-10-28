# STUDY-BUDDY audit fixes — progress note (2026-10-28)

Source audit: `hidden_work/audit-20261028.md` (read-only; not modified).
Base: remote main `5e5c218`. All commits below are LOCAL ONLY — nothing
pushed. Pushing is a separate step via the user's secure storage
mechanism. Test env: `~/workspace/acra/.venv/bin/python` with
`CSP_REPORT_ONLY=1`; ruff via `~/.local/bin/uvx ruff@latest check
--no-cache` (never on `.js` files).

## STAGE 1 — Critical fixes — DONE, gates green

### SEC-01 — password-reset link flashed to unauthenticated requesters
- Fix: `unified_app.py::forgot_password` — the `Reset link (dev)` flash
  is removed entirely. No mailer exists in the codebase (verified:
  no Flask-Mail, no mailer module, no email-provider env config), so no
  provider was invented. Behaviour now: generic message only (identical
  for known/unknown addresses), token still stored hashed server-side
  with 1-hour expiry, link discarded — not flashed, not returned, not
  logged (raw tokens in log storage would recreate the same exposure;
  this is a deliberate deviation from a literal "log it server-side"
  reading, recorded here and in the code comment).
- Follow-up doc: `docs/ops/password-reset-email.md` (provider choice,
  mailer module, dashboard-only keys, stubbed-mailer test, staging
  verification). Until that lands, self-service reset is intentionally
  non-functional; do NOT restore the dev flash to "make it work".
- Considered and left alone: the sibling `verify_email` dev flash is
  shown only to the already-authenticated user for their own link —
  not an unauthenticated leak, out of SEC-01 scope.
- Regression tests: `tests/test_sec01_reset_leak.py` (3 tests: known
  address issues no link but creates a hashed token row; unknown
  address gets the same message and creates no row; known vs unknown
  responses indistinguishable).

### SEC-03 — duplicate `envVars` in `render.yaml`
- Fix: merged into a single `envVars` block; `PYTHON_VERSION: 3.12.0`
  pin kept, plus `SECRET_KEY` (generated), `FLASK_ENV`, `GEMINI_API_KEY`,
  `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GITHUB_TOKEN`
  (`sync: false`), `DATABASE_URL` from `acra-db`.
- Regression tests: `tests/test_render_yaml.py` (strict loader fails on
  any duplicate key; pin + keys asserted; `acra-db` block asserted
  present until the Neon cutover).

### SEC-02 — production-database discrepancy (config-level only)
- NOT migrated live (needs dashboard access + backup/restore proof,
  impossible from this checkout). `render.yaml` still wires `acra-db`;
  it was NOT re-pointed (a Neon target may not exist yet) and the block
  was NOT deleted (production may still use it).
- Doc: `docs/ops/neon-database-decision.md` — exact dashboard steps:
  confirm the effective `DATABASE_URL` + `acra-db` expiry date; if on
  Render free, pg_dump → restore to Neon → verify row counts → set
  dashboard `DATABASE_URL` → redeploy → verify → only then remove the
  `databases:` block; if already on Neon, remove the stale block after
  a fresh backup; backup/restore proof procedure included.
- See BLOCKED list below for the user steps.

### TEST-01 — spec drift / suite red on main
- Fix: `docs/api/openapi.yaml` extended with the 9 missing PUBLIC paths
  (`/api/career/freshness`, `/api/dsa/progress`,
  `/api/interview/intel/{company}`, `/api/mentors/qa`,
  `/api/mentors/{mentor_id}/reviews`,
  `/api/opportunities/refresh-status`,
  `/api/retrospectives/learnings`, `/api/salary/curve`,
  `/api/salary/scripts`). Every `x-response-keys` list was probed from
  the live handler (anonymous test client), not written from memory.
  The mentors-reviews GET is documented; its POST is
  session-authenticated (`@login_required`, booking required) and stays
  covered by `test_route_policy.py` / `test_mentors.py`, noted in a spec
  comment. Spec now 33 paths (24 + 9).
- Contract test honestly rescoped (`tests/test_ws5_contracts.py`):
  exact public coverage (every public policy path in spec), no phantom
  spec paths, spec `x-auth` must equal policy classification in both
  directions, auth-op floor of 8 (the 2026-10-28 count) so selective
  auth documentation cannot silently shrink. `EXAMPLE_URLS` extended
  (intel/google, mentors/1/reviews, salary curve with a valid role x
  tier); `test_public_get_response_shapes` seeds one Mentor row (id 1)
  so the reviews probe honestly returns 200 — fixture setup, not a
  weakening. Well-formed count updated 24 → 33.
- Found during the fix: 5 policy entries were stale — `/api/cite/*`
  (bibtex export/import, format, guide) and `/api/parse_preview` were
  classified `auth` in `tests/route_policy.yaml` while the handlers
  answer anonymously (proven by the pre-existing public POST probes).
  Policy corrected to `public` (policy is a test fixture only; no
  production code reads it). This is a correction to match verified
  behaviour, not a weakening: the handlers never rejected anonymous
  callers.
- Stage 1 gate evidence: `pytest tests/test_ws5_contracts.py
  tests/test_route_policy.py tests/test_sec09_auth.py
  tests/test_sec01_reset_leak.py tests/test_render_yaml.py -q` →
  31 passed. Ruff on changed files → all checks passed.
- Commit (local, unpushed): `c09bf6e` — "fix(audit stage 1): SEC-01
  reset-link leak, render.yaml merge, spec public coverage".
- Pre-existing working-tree changes deliberately NOT touched by this
  commit: rubric-embeddings work (`studybuddy/rubric_embeddings.py`,
  `studybuddy/app_config.py`, `studybuddy/eng_interview_v2.py`,
  `studybuddy/mock_interview.py`, calibration script + test),
  `docs/launch-posts-final.md` (modified before this work; forbidden
  file, left alone), untracked launch docs (`docs/reddit-refresh-*`,
  `docs/show-hn-refresh-*`) and `hidden_work/`.

## STAGE 2 — Engineering foundation — DONE, gates green

### Session hardening
- Remember-me (audit: 365 days, Secure False, SameSite None):
  `app/config.py` now sets `REMEMBER_COOKIE_HTTPONLY=True`,
  `REMEMBER_COOKIE_SAMESITE="Lax"`,
  `REMEMBER_COOKIE_DURATION=30 days` in `BaseConfig`, and
  `REMEMBER_COOKIE_SECURE=True` in `ProductionConfig` (dev stays
  non-Secure over plain http). Verified applied via `create_app` config.
- Revocation on password change: new `User.session_version` column
  (`unified_app.py`) + Alembic revision
  `migrations/versions/e5f6a7b8c9d0_audit_session_version.py`
  (down_revision `d4e5f6a7b8c9`, the previous head; Postgres needs
  this migration — dev SQLite gets the column via `create_all`).
  A `before_request` hook in `create_app` compares the session's
  stored version against the column: equal passes; stale logs the
  user out, clears the session, and redirects to login (401 JSON for
  `/api/*`). Adoption rules need no per-login-path edits: a fresh
  session without a version adopts the current one (grandfathers
  pre-deploy sessions; covers form, register, and Google OAuth logins),
  while a remember-me cookie restoration without a version is
  rejected (it cannot prove it predates the last password change —
  one-time re-login for remember-me users after this deploy).
- `reset_password` bumps `session_version` on every successful reset.
- Logout (`app/blueprints/auth_bp.py`) now does `session.clear()`
  after `logout_user()` (previously session data survived logout).
- Re-auth for sensitive actions: `/account/export` now requires a
  fresh session (`@fresh_login_required`) — a remember-me-restored
  session is sent back to login first. `/account/delete` already had
  password + type-DELETE confirmation, so it needed no extra gate.
  Found while wiring this: `login_manager.refresh_view` was unset, so
  `@fresh_login_required` aborted 401 with no login path (a dead end
  for browser users); it is now set to `auth.login` in `create_app`.
- Tests: `tests/test_session_hardening.py` (7 tests: cookie settings
  on classes + applied config, stale-session logout, full reset flow
  with version bump + old-session revocation + new-password login,
  logout clears session, export fresh-gate, delete re-auth unchanged).

### Deletion completeness + export completeness
- New canonical registry `studybuddy/user_data.py`: 62 plain
  `user_id` models (61 in `unified_app.py` + `LLMUsage` from
  `studybuddy/llm_ledger.py`) + 5 linked models with non-`user_id`
  user columns (`PeerRoom` host/guest/interviewer, `PeerFeedback`
  reviewer/reviewee, `PeerBlock` blocker/blocked, `TeamLeague`
  created_by, `OHReport` reporter_user_id). The old handlers covered
  14 (export) / 19 (delete) models; the rest silently survived.
- Documented edge rules: `Mentor` profiles are ANONYMIZED (name →
  "Deleted user", PII cleared, `user_id` → NULL), never deleted, so
  other users' reviews/bookings keep referential integrity; rooms the
  user hosts/interviews in are deleted with their answers/feedback;
  rooms merely guested in survive with `guest_id` cleared; leagues
  the user created are deleted with their memberships (`created_by`
  is NOT NULL, so they cannot be detached). Deletes run children-first
  via a topological sort over SQLAlchemy FK metadata, and fail LOUDLY
  (raised, transaction rolled back, user kept) if any rows remain —
  the old per-model `try/except: pass` swallowing is gone.
- `account_delete` and `account_export` in `unified_app.py` now use
  the registry (export keys for the original 14 unchanged;
  `LLMConsent` keeps its special `llm_consent` key).
- Tests: `tests/test_account_delete_completeness.py` (2 tests: seeds
  one row in EVERY user-owned + linked table via dependency passes,
  deletes through the real route, asserts zero rows + anonymized
  mentor + detached guest room + control user's data untouched; plus
  a registry-drift guard that re-derives all `user_id` models from
  the SQLAlchemy registry and fails if any is unregistered).

### Retention purge
- New `studybuddy/retention.py`: purges exactly the 10 inventory
  tables with a 90-day line (resume/JD/bullet/LinkedIn analyses,
  interview sessions, communication analyses, aptitude + quiz
  attempts, career assessments, experiments) by `created_at` age.
  Saved artifacts (`ResumeVersion`, `CoverDoc`, `Portfolio`) are
  explicitly NOT purged — that is the "unless you save them" half of
  the promise. "Inactivity" is approximated by row age (no per-user
  activity clock exists in schema; documented in the module).
- New `scripts/purge_stale_data.py`: dry-run by default, `--apply`
  to delete, prints per-table counts. Scheduling is a dashboard step
  (see BLOCKED B-4).
- Tests: `tests/test_retention_purge.py` (dry-run deletes nothing;
  apply deletes exactly the >90d rows and keeps recent + saved rows).

### Sentry — NOT wired (BLOCKED, see B-3)
- Verified: `sentry_sdk` is absent from `requirements*.txt` and the
  venv; `studybuddy/sentry_init.py` exists but is dormant ("REPORTED,
  not applied") and `init_sentry` is never called. Per the task's
  condition (wire ONLY if the SDK is already present), nothing was
  wired and no dependency was added.

### Gunicorn reconciliation — one source of truth
- `render.yaml` startCommand is now exactly
  `gunicorn -c gunicorn.conf.py unified_app:app` (was inline 2 workers
  / 120 s); `Dockerfile` CMD is now
  `gunicorn -c gunicorn.conf.py unified_app:app` (was inline 2 / 120);
  `gunicorn.conf.py` header rewritten to state it is the single source
  of truth (values unchanged: 1 worker / gthread / 4 threads / 60 s,
  so `tests/test_ws6_ops.py::test_gunicorn_config_values` still passes
  unmodified); `docs/ops/memory.md` updated from "should change" to
  reconciled, including the side benefit that a single worker makes
  Flask-Limiter's in-memory buckets exact on Render.
- Tests: `tests/test_render_yaml.py` extended (exact startCommand +
  Dockerfile conf-flag assertions, healthcheck `--timeout` excluded
  from the check).

### Rate-limit store — documented, not provisioned
- Verified: `app/extensions.py` reads `RATELIMIT_STORAGE_URI` (default
  `memory://`); `render.yaml` does not set it; no shared store is
  provisioned anywhere. No store was added from config alone (the app
  would fail to connect at boot).
- Doc: `docs/ops/rate-limits.md` — current state, what per-process
  means now (exact on Render with 1 worker; resets on deploy/restart;
  breaks again if workers are ever added), and the exact provisioning
  + verification steps (see BLOCKED B-5).

### Monolith split Phase 2 — DSA routes moved (issue #53)
- New `studybuddy/dsa_routes.py`: all 12 DSA routes moved verbatim
  (`/dsa`, `/api/dsa/problems`, `/api/dsa/progress`, `/dsa/diagnosis`,
  `/dsa/a2z`, `/dsa/a2z/<id>/learn`, `/dsa/tracker`,
  `/dsa/<id>/untrack`, `/dsa/<id>`, `/dsa/<id>/hint`,
  `/dsa/<id>/approach`, `/dsa/<id>/status`) plus `_dsa_progress_row`.
  (The audit's "10 routes" was approximate; the url_map shows 12.)
  Helpers come from `studybuddy/dsa_practice.py` +
  `studybuddy/a2z_lessons.py` (top-level, no cycle); `DSAProgress`
  and `_momentum_hook` are lazy imports (phase-1 pattern).
- `unified_app.py`: 227-line DSA block excised (replaced by a pointer
  comment); blueprint registered next to `study_library`; the 16
  now-unused DSA helper imports removed (only `a2z_problems` kept —
  still used by the landing-page stats). Net: the god module shrinks
  and the DSA surface is independently testable.
- Templates: 21 `url_for('dsa_*')` call sites across 10 templates
  updated to `dsa.*` (including the two kwargs-style calls in
  `dsa_a2z.html` / `dsa_diagnosis.html` that a naive pattern missed
  on the first pass); zero unprefixed references remain.
- Route-diff gate (evidence, not just a test): full url_map dumped
  before/after — 311 rules both times, (rule, methods) diff EMPTY;
  only the 12 endpoint names gained the `dsa.` prefix, as Flask
  blueprint semantics require (same as Phase 1).
- Tests: `tests/test_dsa_blueprint_routes.py` (12 rules + endpoints,
  no unprefixed endpoints, no top-level `unified_app` import in the
  blueprint, page/API smoke). Existing DSA-adjacent suites re-run
  green (see gate evidence below).
- Stage 2 gate evidence: `test_account_delete_completeness` +
  `test_session_hardening` + `test_phase2_privacy` → 16 passed;
  `test_retention_purge` + `test_render_yaml` + ws6 gunicorn-values →
  24 passed in the combined run; `test_dsa_blueprint_routes` +
  `test_dsa` + `test_route_policy` + `test_authz_matrix` → 45 passed;
  career/momentum/interview/data04/eval/health/peer suites → 205
  passed. Ruff clean on all Stage 2 files.
- Commit (local, unpushed): `d4af269` — "fix(audit stage 2): session
  hardening, delete completeness, retention, gunicorn, DSA split".

## STAGE 3 — Product quality — DONE, gates green

### Docs cleanup
- Legacy docs archived via `git mv` (history preserved) to
  `docs/archive/streamlit-era/` + `docs/archive/README.md` explaining
  what was archived and why: `DEPLOYMENT.md`, `STREAMLIT_CLOUD.md`,
  `README.md`, `home.py`, `app.js`, `dashboard.html`, `index.html`,
  `index.css`, `constructor/`, `deconstructor/`. NOT archived:
  `docs/data/` and `docs/assets/` (referenced by README/docs) and
  `docs/docs/` (architecture PNGs, out of audit scope). No in-repo
  references to the archived files were found in `.py` / `.html` /
  `.md` before the move.
- LICENSE conflict reconciled: `docs/LICENSE` (MIT, 2025, different
  holder — legacy artifact) overwritten with the root `LICENSE`
  content (MIT, 2026 Vivek Bartwal); file kept so links do not break.
  `pyproject.toml` already declared MIT, consistent with the root.
  Engineering cleanup, not a legal verdict.
- `pyproject.toml` drift fixed to mirror `requirements.in` exactly:
  `python-dotenv` 1.0.0 → 1.2.4, `google-genai` 2.30.1 → 2.31.0,
  `Authlib` now pinned 1.9.1, `flask-migrate` moved from dev-optional
  to main dependencies, and the previously omitted `defusedxml`,
  `pypdfium2`, `pdfplumber`, `psycopg[binary]`, `pyyaml` added. A
  header comment states `requirements.in` + hash-pinned
  `requirements.txt` remain the authoritative install source.
- `.env.example` completed: LLM fallback keys + `LLM_MODE` /
  `LLM_BASE_URL` / `LLM_MODEL`, `FERNET_KEY(S)` (+ `ENCRYPTION_KEY`
  legacy note), `SENTRY_DSN` (disabled-by-default note),
  `RATELIMIT_STORAGE_URI` (memory-default note),
  `STUDYBUDDY_ADMINS`, `RESEARCH_CONTACT_EMAIL`, `RAG_EMBEDDER`.
  `SECRET_KEY` is now generation-command-only (the old
  `change-me-to-a-long-random-secret` fill-in is a value production
  boot blocklists). Google redirect URI corrected to the current
  prod host `https://study-buddy-3m16.onrender.com/auth/google/callback`.
- `docs/configuration.md` extended: Encryption (FERNET), Error
  tracking (Sentry), Rate-limit storage, Admins + misc sections, and
  an Internationalization (Hindi) section stating the honest scope
  (landing fully translated; nav + page bodies English; `?lang`
  validated; query strings preserved on switch; `<html lang>`
  follows the resolved locale).
- README corrected from data files, not memory: Quick Start now
  includes `cp .env.example .env` + the Python 3.12 requirement;
  opportunities 83 → 124 open (128 total in the current file);
  collected tests ~9,600 → 9,786 (measured 2026-10-28 via
  `pytest --collect-only`); `unified_app.py` 9,000+ → ~11,900 lines
  (measured 11,881); `studybuddy/` description corrected (Flask
  blueprints + network modules live there, not pure logic only);
  Testing section defers to `CONTRIBUTING.md` as authoritative;
  Run + Deployment sections now show the hash-pinned install and the
  conf-based gunicorn command (`gunicorn -c gunicorn.conf.py
  unified_app:app`); `DATABASE_URL` wording points at the Neon
  decision doc instead of asserting Render Postgres.

### Hindi switcher — honestly scoped (no nav translation)
- DECISION: the ~40 nav labels were NOT translated. Translation
  quality for 40 labels cannot be verified by tests, and a
  half-translated nav (Hindi labels, English bodies) is arguably
  worse UX than a clearly-scoped landing translation. Instead the
  scope is stated in the UI (switcher tooltip) and in
  `docs/configuration.md`.
- Fixes that DID land: `templates/base.html` `<html lang="en">` →
  `<html lang="{{ current_locale }}">`, and the same change applied
  repo-wide — all 33 full-page templates now render a dynamic lang
  (0 hardcoded `lang="en"` remain; verified by grep). The switcher
  in `templates/_site_nav.html` now uses a `switch_url(locale)`
  helper (added to the `_i18n_processor` context processor in
  `unified_app.py`) that rebuilds the current URL with `lang`
  replaced and every other query parameter preserved — the old
  `{{ request.path }}?lang=...` links dropped filters like
  `/dsa?topic=arrays`. The switcher's Hindi link carries
  `title="Hindi translation currently covers the landing page;
  other pages stay in English."`.
- Tests: `tests/test_hindi_switcher.py` extended (query preservation,
  lang replacement without duplication, `lang="hi"` on a standalone
  page AND a base-extending page, scope tooltip present). The
  existing 6 Hindi tests still pass unchanged.

### Mobile overflow guard + 390px re-evaluation (UX-01)
- Guard added to `templates/base.html` (inherited by 47 templates)
  and to all 32 standalone full-page templates (33 `<html>`
  templates total): media/table `max-width: 100%`, `pre`
  horizontal scrolling, `html, body { overflow-x: clip }` (clip, not
  hidden, so sticky positioning keeps working). No `display: block`
  on tables (breaks layout).
- Evaluation: local headless Chromium (Playwright) at 390×844
  against a local dev server — NOT the live browser, NOT prod.
  Reusable script `scripts/eval_overflow_390.py`; before/after JSON
  reports + full write-up in
  `hidden_work/overflow-390px-20261028.md`.
- Honest result: NO page had document-level overflow before the
  guard (scrollWidth 390 on all 16 evaluated pages) — the audit's
  "can overflow" was a risk statement, and the measured hub pages
  were already contained. Element overhangs found were clipped
  decorative (ticker marquee, cursor glow, SVG chart internals) or
  scroll-container-contained (salary city table inside
  `.cp-table-scroll { overflow-x: auto }` — scrollable, not cut
  off). The guard changed nothing on these pages (before/after
  identical) and is a preventive safety net for unevaluated pages.
- ONE real defect found and fixed: `/learn/roadmaps` skill-card grid
  was hardcoded to 3 columns, squeezing cards to ~120px at phone
  widths so 9 level `<select>` elements overflowed their cards.
  Fix: `gap-skills-grid` class (3 columns desktop, 1 column below
  640px), inline style removed; re-evaluation confirms the selects
  no longer overhang. Same hardcoded-3-column pattern in
  `certs.html:41`, `profile.html:93`, `study_cite.html:39` was NOT
  evaluated (auth-gated pages were out of this QA run) — flagged in
  the evidence doc for the next 390px pass, not silently claimed.
- Tests: `tests/test_mobile_overflow_guard.py` (guard present in
  base + every standalone page, no hardcoded `lang="en"`, roadmaps
  grid rule). Candidate paths `/roadmaps` / `/quizzes` 404'd (wrong
  guesses); real routes `/learn/roadmaps` / `/learn/quizzes` were
  evaluated instead.
- Stage 3 gate evidence: `test_hindi_switcher` +
  `test_mobile_overflow_guard` + `test_route_policy` +
  `test_dsa_blueprint_routes` + `test_dsa` + `test_authz_matrix` +
  `test_ws5_contracts` → 67 passed. Ruff clean on all Stage 3
  Python files. Commit (local, unpushed): see Stage 3 commit SHA in
  the report.

## STAGE 4 — Differentiation & growth (code-feasible only) — NOT STARTED

## BLOCKED items (need the user; never fabricated)

- **B-1 — Production database cutover (SEC-02).** See
  `docs/ops/neon-database-decision.md` for the exact dashboard steps
  (confirm effective `DATABASE_URL` + `acra-db` expiry; pg_dump →
  Neon restore → row-count verification → dashboard `DATABASE_URL`
  cutover → redeploy → verify → then remove the `databases:` block).
- **B-2 — Password-reset email delivery (SEC-01 follow-up).** See
  `docs/ops/password-reset-email.md`: choose a transactional provider,
  add its SDK via `requirements.in` + hash-pinned `requirements.txt`,
  add a mailer module reading keys from the environment (dashboard
  only), wire delivery in `forgot_password`, add a stubbed-mailer test,
  verify end to end on staging.
- **B-3 — Sentry wiring (Stage 2).** `sentry_sdk` is not in
  `requirements*.txt` and not installed. To enable: add `sentry-sdk`
  to `requirements.in`, regenerate `requirements.txt` with hashes
  (`uv pip compile requirements.in --generate-hashes`), set
  `SENTRY_DSN` in the Render dashboard (never in the repo), then wire
  the existing dormant module — add `from studybuddy.sentry_init
  import init_sentry; init_sentry(app)` inside `create_app` in
  `unified_app.py` (the module is a no-op without a DSN and scrubs
  PII/resume text/API keys from reports). Verify with a staging error
  and confirm it appears in the Sentry dashboard.
- **B-4 — Retention purge scheduling (Stage 2).** Code + tests are
  done (`studybuddy/retention.py`, `scripts/purge_stale_data.py`
  dry-run by default); enforcement runs only when scheduled. Create a
  Render Cron Job (weekly) running
  `python scripts/purge_stale_data.py --apply` against the production
  service, then check its logs for per-table counts. Until then, old
  analyses keep accumulating.
- **B-5 — Shared rate-limit store (Stage 2).** See
  `docs/ops/rate-limits.md`: provision Upstash Redis or Render Key
  Value, set `RATELIMIT_STORAGE_URI` in the Render dashboard,
  redeploy, verify a 429 survives a redeploy (bucket not reset).
  Optional follow-up: record the variable as `sync: false` in
  `render.yaml`. Not urgent while Render runs 1 worker (limits are
  exact there); mandatory if workers/services ever scale out.
