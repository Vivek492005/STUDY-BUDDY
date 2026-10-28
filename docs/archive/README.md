# Archived docs — read this before using anything under `streamlit-era/`

These files were moved here by `git mv` during the October 2026 audit
fixes (Stage 3 docs cleanup). The move preserves history — nothing was
deleted, and `git log --follow` still traces each file.

## What is archived, and why

`streamlit-era/` holds the Streamlit-era research-paper assistant:
`DEPLOYMENT.md` (Streamlit Cloud + Heroku instructions for a
`research-paper-assistant` repo), `STREAMLIT_CLOUD.md`, the old docs
index `README.md` ("Research Paper AI Assistant"), the standalone
frontend (`index.html`, `dashboard.html`, `app.js`, `index.css`),
`home.py`, and the `constructor/` + `deconstructor/` Python trees.

They describe a DIFFERENT app from the current STUDY-BUDDY Flask
monolith and contradict the live deployment (`render.yaml`,
`Dockerfile`, root `README.md`). A new contributor landing on them
first would be misled — that is why they were archived, not left in
place.

## What to use instead

- Deployment: root `README.md` (Installation & Setup, Production,
  Deployment sections) + `render.yaml` (authoritative) +
  `docs/ops/` runbooks.
- Architecture: `docs/PROJECT_OVERVIEW.md`, `docs/ops/`, `docs/api/`.
- Licence: root `LICENSE` (MIT, 2026 Vivek Bartwal). The old
  `docs/LICENSE` copy (2025, different holder) was corrected to match
  the root file during the same cleanup; it is not archived.

## If you need the old code

It still runs as a historical artifact only: check out the files from
`docs/archive/streamlit-era/` and treat them as unmaintained. Do not
reintroduce them into `docs/` without reconciling them against the
current app first.
