# Production database: Render free Postgres vs Neon (audit SEC-02)

Status (2026-10-28): **UNRESOLVED — needs a dashboard check by the maintainer.**
This document does not migrate anything. It records the discrepancy and the
exact steps to resolve it, because changing `render.yaml` from config alone
can either break production deploys or silently re-point production at the
wrong database.

## The discrepancy

- `docs/ops/hosting-plan.md` (2026-10-19) records a decision to use **Neon
  free Postgres**, because **Render free Postgres expires 30 days after
  creation** (14-day grace, then deletion). Neon free has no expiry.
- `render.yaml` (as committed) still wires `DATABASE_URL` from the Render
  free database **`acra-db`** (`databases:` block, `plan: free`).
- The Render dashboard may already override `DATABASE_URL` with a Neon
  connection string (dashboard env vars take precedence over `render.yaml`
  on redeploy only if the dashboard value is set — a `render.yaml` change
  can still re-point it). Which database production actually uses is
  **unverified from the repo alone**.

If production runs on Render free Postgres, all accounts and data face a
documented deletion risk. If it was already migrated to Neon, `render.yaml`
is stale and a future redeploy could re-point at Render.

## Step 1 — Confirm what production uses today (dashboard, ~5 minutes)

1. Render dashboard → service `study-buddy` → **Environment** → read the
   effective `DATABASE_URL`. Note whether it is a `*.render.com` /
   `*.oregon-postgres.render.com` host (Render) or a `*.neon.tech` host
   (Neon), and whether it is marked as coming from the linked database
   or as a manually set value.
2. Render dashboard → **PostgreSQL** → open `acra-db` → check the creation
   date and any expiry banner. If it is a free instance, note the expiry
   date: that is the hard deadline for the migration below.
3. Record the finding (host type + expiry date, if any) in this document
   and in the release notes before proceeding.

## Step 2A — If production is on Render free Postgres: migrate to Neon

1. Create a Neon project (free tier) and a database for STUDY-BUDDY.
   Note the Neon connection string; keep it in the Render dashboard only,
   never in the repo.
2. Take a full backup of the Render database first:
   `pg_dump "$RENDER_DATABASE_URL" > studybuddy-prod-backup-YYYYMMDD.sql`
   (get the Render URL from Dashboard → PostgreSQL → `acra-db` →
   Connections; do not paste it into chat, docs, or issues).
3. Restore into Neon: `psql "$NEON_DATABASE_URL" < studybuddy-prod-backup-YYYYMMDD.sql`.
4. Verify the restore before cutover: row counts on the user-owned tables
   (`user`, `resume_analysis`, `dsa_progress`, `roadmap_progress`,
   `lab_run`, `xp_ledger`, `benchmark_contribution`) match the source;
   spot-check that a known account and its dashboard data load from a
   scratch app pointed at Neon.
5. Cut over: Render dashboard → service `study-buddy` → **Environment** →
   set `DATABASE_URL` to the Neon connection string (dashboard values
   override `render.yaml`). Redeploy the service.
6. Verify production after the redeploy: `/version` returns 200, login
   works, and a known account's dashboard data is present.
7. Only after steps 5–6 pass, commit the follow-up config change:
   remove the `databases:` block and the `DATABASE_URL fromDatabase`
   entry from `render.yaml`, and replace them with a comment pointing at
   this document and the dashboard-managed `DATABASE_URL`. Push that
   commit only after production is verified on Neon.
8. Keep the Render `acra-db` instance until the Neon cutover has run
   cleanly for at least one week; then delete it from the dashboard.

## Step 2B — If production is already on Neon

1. Confirm the dashboard `DATABASE_URL` is the Neon connection string
   (Step 1). Keep a fresh `pg_dump` backup from Neon before any change.
2. Commit the same follow-up as Step 2A.7: remove the `databases:` block
   and the `DATABASE_URL fromDatabase` entry from `render.yaml`, so a
   future redeploy cannot re-point production at Render free Postgres.
3. Verify production after the redeploy (`/version` 200, login works,
   dashboard data present), then delete the stale `acra-db` instance if
   it is unused.

## Step 3 — Backup / restore proof (required before any beta cohort)

A runbook reference is not proof. After the cutover, execute and record:

1. `pg_dump` from the live production database (Neon) to a dated file.
2. Restore that dump into a scratch Neon branch/database.
3. Point a local app at the scratch database and verify: login, dashboard,
   and at least one user-owned record per major table load correctly.
4. Record the date, the dump file name, and the verification result here.

## What was NOT done from the repo (2026-10-28)

- No live migration was attempted: it needs dashboard access plus the
  backup/restore proof above, neither of which can be done from this
  checkout.
- `render.yaml` was NOT re-pointed at Neon: pointing at a Neon database
  that may not exist yet would break production deploys.
- The Render `acra-db` block was NOT deleted: deleting it before a
  verified cutover would destroy the database production may still use.
