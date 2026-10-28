# OPS-09: Worker sizing and 512 MB memory discipline

## Gunicorn config

`gunicorn.conf.py` (repo root, added 2026-10-19):

| Setting | Value | Why |
|---|---|---|
| `workers` | **1** | 512 MB cannot afford 2 full worker processes; threads share one process's memory |
| `worker_class` | `gthread` | I/O-bound Flask app — threads beat processes under the GIL |
| `threads` | 4 | concurrent uploads while one worker parses |
| `timeout` | 60 | hung worker killed after 60 s — long jobs belong in background (ARC-11), not requests |
| `graceful_timeout` | 30 | in-flight requests get 30 s on SIGTERM |
| `max_requests` / `max_requests_jitter` | 1000 / 100 | recycle workers to cap slow memory growth; jitter avoids thundering restart |
| `preload_app` | off | only after a thread-safety audit of every module (caches, singletons) |

## ✅ Reconciled 2026-10-28 (audit fix): one source of truth

Previously `render.yaml` ran `gunicorn unified_app:app --workers 2
--threads 4 --timeout 120` while this file prescribed 1 worker / 60 s —
**2 workers × ~130 MB ≈ 260 MB+ baseline** on a 512 MB instance, plus a
120 s timeout letting a hung worker pin memory for 2 minutes. Both
`render.yaml` (`startCommand: gunicorn -c gunicorn.conf.py
unified_app:app`) and the `Dockerfile` CMD now invoke gunicorn with
`-c gunicorn.conf.py` and no inline flags; values change only in the
conf file. `tests/test_render_yaml.py` locks the render command and
`tests/test_ws6_ops.py::test_gunicorn_config_values` locks the conf
values (1 worker / gthread / 4 threads / 60 s).
The 120 s timeout existed to cover the resume pipeline + Gemini calls; with
`timeout=60`, LLM calls must keep their own 30 s per-attempt fail-fast and
long pipelines must move to background jobs (PERF-05) — this is the correct
trade, not a regression. Side benefit: with a single worker process,
Flask-Limiter's in-memory buckets are exact on Render (see
`docs/ops/rate-limits.md`) — the old 2-worker shape enforced limits
per-process (~2× nominal).

## Measured numbers (2026-10-19, this VM)

- Boot + `/healthz` + `/readyz` + `/version`: **127 MB peak RSS**, ~9 s.
- CI budget (`scripts/check_memory.py`, PERF-09): fail if peak RSS > **400 MB** —
  headroom for PDF parsing + skills extraction + readiness scoring on fixtures.

## Profiling guidance

When hunting growth: `tracemalloc` for pure-Python, `memray` for native
(resume PDF parsing, embeddings, the skills automaton). Snapshot before/after
a single resume parse; anything retained per-request is a leak.
