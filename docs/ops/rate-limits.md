# Rate limiting: storage, limits, and the per-process caveat

## Current state (2026-10-28, verified from code + config)

- Enforcement: Flask-Limiter (`app/extensions.py`). Global defaults
  `200 per day` + `60 per hour`, keyed by user id when authed, else IP
  (after ProxyFix); stricter per-route limits on auth (10–20/hour),
  forgot/reset (5–10/hour), and write endpoints (typically 20–30/hour).
  Clients get `X-RateLimit-Limit/-Remaining/-Reset` headers.
- Storage: `RATELIMIT_STORAGE_URI` from the environment, defaulting to
  `memory://` (in-process). **`render.yaml` does NOT set
  `RATELIMIT_STORAGE_URI`, so production runs on in-memory buckets.**
- No shared store (Redis / Render Key Value / Upstash) is provisioned
  anywhere in the repo or `render.yaml`. None was added by the audit
  fixes: adding unprovisioned infrastructure from config alone would
  break production deploys (the app would fail to connect at boot).

## What the per-process limitation actually means

- With the pre-2026-10-28 shape (2 gunicorn workers), each worker
  enforced its own bucket: effective limits were ~2× the stated
  values, and a deploy/restart reset every bucket. A determined caller
  could also split traffic across workers.
- With the reconciled shape (1 worker, `gunicorn.conf.py` as the single
  source of truth — see `docs/ops/memory.md`), Render runs exactly one
  process, so **limits are exact on Render today**; only the
  reset-on-deploy/restart caveat remains, plus no sharing if a second
  worker or service is ever added.
- Local dev and tests also run `memory://`, which is correct there.

## How to fix it properly (maintainer steps, not done here)

1. Provision a shared store: Upstash Redis (free tier) or Render Key
   Value. Note the connection string; keep it in the Render dashboard
   only, never in the repo.
2. Render dashboard → service `study-buddy` → Environment → set
   `RATELIMIT_STORAGE_URI` to the store URL (for Upstash Redis, the
   `rediss://` URL). Redeploy.
3. Verify: make repeated authed requests past a small per-route limit
   (e.g. the 5/hour forgot-password limit from a test account) and
   confirm a 429 with `Retry-After`; redeploy and confirm the bucket
   survives (still 429, not reset).
4. Only after step 3 passes, optionally record `RATELIMIT_STORAGE_URI`
   as `sync: false` in `render.yaml`'s `envVars` (same pattern as
   `GEMINI_API_KEY`) so the variable's existence is documented without
   its value.
5. If the app ever scales to 2+ workers or services, this store becomes
   mandatory, not optional — per-process buckets would silently
   multiply limits again.
