# Monolith split plan: unified_app.py (9,373 lines) → blueprints

Status: IN PROGRESS — 9 blueprints extracted (see "Completed phases"
below); unified_app.py was 9,928 lines before Phase 6 (2026-11-08) and
9,027 lines after it. The plan below was drafted Oct 2026 when the file
was 9,373 lines; the phase order proved aspirational — the actual order
followed cohesion in practice (study_library → dsa → learn → career →
mentors → ultimate → benchmark → auth → research).

## Why phased, not big-bang

- All SQLAlchemy models live in `unified_app.py` itself. Any new blueprint
  module that imports them at top level creates a circular import
  (`unified_app` imports the blueprint at registration time).
- Routes share `limiter`, `csrf`, `db`, and helper closures defined in
  `register_routes`. Moving a route means moving (or lazy-importing) its
  dependencies too.
- The full test suite (~9,820 tests) plus CI (12 jobs) is the gate for
  every phase. A phase that goes red is reverted, not patched forward.

## The one hard rule

Blueprint modules MUST NOT import `unified_app` at top level. Models and
shared helpers are imported lazily inside functions (the codebase already
does this in `studybuddy/health_review.py::_models`), or passed in at
registration time. Any phase that introduces a top-level circular import
is rejected in review.

## Phase order (most cohesive, least shared-state first)

1. **Study library** (`/research/library`, `/study/api/*`) — smallest
   coherent group, few cross-route dependencies. Template for all later
   phases. (Drafted as good-first-issue #2.)
2. **DSA / A2Z** (`/dsa/*`) — large but self-contained via
   `studybuddy/dsa_practice.py`.
3. **Learn** (`/learn/*`: roadmaps, quizzes, certs) — shares quiz/roadmap
   helpers; extract after DSA proves the pattern.
4. **Career** (`/career/*`, tracker, opportunities, salary, mentors) —
   shares the most helpers; goes last among feature groups.
5. **Platform routes** (auth-adjacent pages, account, export) — only
   after feature groups are out, since everything touches them.

Explicitly NOT in scope for the split: renaming URLs, changing response
shapes, or "improving" a route while moving it. A move is a pure move;
behavior changes ship separately.

## Per-phase verification gate (all must pass before merge)

1. `pytest` full suite green locally (or the documented subset when the
   full suite flakes under load) AND the pushed commit's CI 12/12 green.
2. Route inventory unchanged: every URL before the phase exists after it
   (compare the route table, not just tests).
3. Browser spot-check of the moved group's pages (logged-in + anon) with
   zero new console errors.
4. `ruff check` clean on moved files.

## Complexity hotspots (do NOT refactor in the same PR as a move)

The eval flagged 47 D–F complexity blocks, worst: `generate_review` (41),
`validate_portfolio_payload` (41), `cluster_themes` (40). These get
their own small, tested refactors AFTER their routes move — never bundled
with a move, so a regression is attributable to exactly one change.

## Done when

`unified_app.py` holds only app creation, extension setup, and blueprint
registration — no route handlers. Until then, every phase report states
the current line count honestly; a phase that only moves code without
shrinking the file has failed.

## Completed phases

- **Phase 1 — Study library** (`studybuddy/study_library.py`).
- **Phase 2 — DSA / A2Z** (`studybuddy/dsa_routes.py`).
- **Phase 3 — Learn** (`studybuddy/learn_routes.py`).
- **Phase 4 — Career** (`studybuddy/career_routes.py`).
- **Phase 5 — Mentors, Ultimate Lab, Benchmark, Auth**
  (`studybuddy/mentor_routes.py`, `studybuddy/ultimate_routes.py`,
  `studybuddy/benchmark_collection.py`, `app/blueprints/auth_bp.py`).
- **Phase 6 — Research (2026-11-08):** `/research/*` pages,
  `/api/study/*` import + collections APIs, `/api/cite/*` citation APIs
  — 35 routes — moved verbatim to `studybuddy/research_routes.py`
  (models/helpers still lazy-imported from `unified_app` inside
  functions, same pattern as Phase 3). Route inventory verified
  unchanged (312 rules before = 312 after, endpoint-for-endpoint);
  gate test `tests/test_research_blueprint_routes.py`. unified_app.py
  9,928 → 9,027 lines. Stale monkeypatch targets in
  `tests/test_study_routes.py` / `tests/test_import_world.py` retargeted
  from `unified_app.<helper>` to `studybuddy.research_routes.<helper>`
  (the old attribute still exists on unified_app but is no longer read
  by the moved views, so the patches had silently stopped working).
