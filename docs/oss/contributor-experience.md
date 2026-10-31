# Contributor experience (OSS-10)

## 15-minute architecture tour

```
unified_app.py          Flask app factory (routes only — logic lives in studybuddy/)
studybuddy/             feature modules, one concern each:
  parsing.py            PDF/DOCX text + field extraction (own parser)
  ai_gateway.py         LLM provider abstraction (Groq/OpenRouter/Ollama)
  jd_matcher.py         resume↔JD requirement coverage
  skills_taxonomy.py    skill matching against data/skills_db.csv
  ux_explain.py         "why this score" renderer (UX-07)
  ux_export.py          Markdown/PDF export + private share links (UX-09)
  prod_packs.py         community data packs, schema-checked (PROD-10)
data/                   versioned datasets (see CONTRIBUTING-DATA.md)
benchmarks/             gold sets + run.py harness (ENG-09)
tests/                  9,820 tests; conftest.py has shared fixtures
```

Start: read `docs/PROJECT_OVERVIEW.md`, then `studybuddy/parsing.py`
(the parser is the heart of the product), then the module you want to touch.

## Getting started

- `make setup` (or follow `docs/self-hosting.md` for Docker).
- Devcontainer/Codespaces config: `.devcontainer/devcontainer.json` (planned).
- Run the fast subset: `pytest tests/ -q -x -k "not slow"`.

## Review SLAs

- First response within **48 hours** (maintainer or bot triage).
- Data PRs (no Python needed): reviewed within **72 hours**.
- Stale PRs get a nudge at 7 days, never silent-closed.

## Labelled tasks

- `good first issue` — 20 data tasks seeded (OSS-04); each has acceptance criteria.
- `help wanted` / `p1` / `p2` — difficulty + priority labelled.

## Recognition

- All contributors listed via the all-contributors bot.
- Release notes credit every contributor by name.
- Pair new contributors with **data tasks first** (fastest path to a merged PR).
