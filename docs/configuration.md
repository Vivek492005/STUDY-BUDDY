# Configuration

All configuration comes from environment variables, validated in
`studybuddy/app_config.py` (`AppConfig.from_env()`). NOTE (audit OPS-03
correction, 2026-10-28): the old claim that "nothing else reads
`os.environ`" was false — several modules read environment variables
directly (35 keys found by static scan; see `tests/test_env_documented.py`,
which fails if a newly read key is not documented here, in
`.env.example`, or in that test's platform allowlist). The sections
below cover the full set; `RATELIMIT_STORAGE_URI` is read via a dynamic
import in `app/extensions.py`, so the static scan cannot see it — it is
documented under "Rate-limit storage" anyway.

## Core

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | (ephemeral) | Flask session signing. **Set in production** — without it sessions reset on every restart. |
| `DATABASE_URL` | sqlite dev db | SQLAlchemy URL. Render injects the Postgres URL. |
| `FLASK_ENV` | `production` | `production` enables JSON logs, HSTS, secure cookies. |
| `PORT` | `5000` | Dev server port (`python unified_app.py`). |

## LLM providers

| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | — | Google Gemini key (primary). |
| `GROQ_API_KEY` | — | Groq key (fallback 1). |
| `OPENROUTER_API_KEY` | — | OpenRouter key (fallback 2). |
| `LLM_MODE` | `server` | `off` = no LLM calls anywhere (deterministic fallbacks); `byok` = reserved; `server` = use server keys. |

## OAuth

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | — | Google OAuth (optional; password login always works). |

## Limits

| Variable | Default | Description |
|---|---|---|
| `ULTIMATE_RUN_LIMIT` | `30 per hour` | Rate limit for Ultimate Lab tool runs. |
| `MAX_UPLOAD_MB` | `16` | Resume/PDF upload cap. |
| `SKIP_DB_INIT` | — | `1` skips `create_all()` (used when generating migrations). |

## Encryption (user API keys)

| Variable | Default | Description |
|---|---|---|
| `FERNET_KEY` | — | Single base64 Fernet key (44 urlsafe chars) encrypting per-user API keys at rest. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. |
| `FERNET_KEYS` | — | Comma-separated keys, NEWEST FIRST, for rotation. Takes precedence over `FERNET_KEY`. |
| `ENCRYPTION_KEY` | — | Legacy fallback name (used only when neither Fernet variable is set; `SECRET_KEY` is the last-resort fallback in code — do not rely on it). |

## Error tracking (Sentry)

| Variable | Default | Description |
|---|---|---|
| `SENTRY_DSN` | — | Sentry DSN. No-op unless set AND `sentry-sdk` is installed (it is not in `requirements.txt` as of 2026-10-28; see the audit-fixes progress note, BLOCKED B-3, for enablement). Reports are PII-scrubbed by `studybuddy/sentry_init.py`. |
| `SENTRY_ENVIRONMENT` / `SENTRY_RELEASE` / `SENTRY_TRACES_RATE` / `SENTRY_PROFILES_RATE` | — | Optional Sentry tuning; read only when a DSN is set. |

## Rate-limit storage

| Variable | Default | Description |
|---|---|---|
| `RATELIMIT_STORAGE_URI` | `memory://` | Flask-Limiter backend. In-process by default (exact on Render with its single worker; resets on deploy/restart). Set to a Redis URL for a shared store — full procedure in `docs/ops/rate-limits.md`. |

## Runtime + refresh flags

| Variable | Default | Description |
|---|---|---|
| `CSP_REPORT_ONLY` | — | Set to `1` to run Content-Security-Policy in report-only mode (violations reported, not enforced). Test/dev use. |
| `SKIP_DB_INIT` | — | Set to skip database initialisation at app startup (used by some test/boot paths). |
| `LIVE_REFRESH_DISABLED` | — | Set to disable live data refreshes (opportunity board auto-refresh). |
| `OPP_NO_AUTO_REFRESH` | — | Set to disable the opportunity board's automatic refresh specifically. |
| `STUDYBUDDY_NO_PARSE_SANDBOX` | — | Set to `1` to parse uploads in-process instead of the sandboxed subprocess (test/dev use; the sandbox is the production path). |
| `HTTPS_PROXY` / `HTTP_PROXY` / `SSL_CERT_FILE` | — | Standard proxy/certificate environment honored by outbound HTTP clients; not app-specific configuration. |
| `DYNO` / `RENDER` | — | Platform-detection flags set by Heroku / Render themselves (not user-configured); the app uses them only to detect its host environment. |

## Admins + misc

| Variable | Default | Description |
|---|---|---|
| `STUDYBUDDY_ADMINS` | — | Comma-separated admin emails (mentor verification, admin views). |
| `RESEARCH_CONTACT_EMAIL` | — | Contact shown on research pages (optional). |
| `RAG_EMBEDDER` | — | RAG embedder backend for the study library (optional). |

## Internationalization (Hindi) — honest scope

- The landing page (`/`) is fully translated: 50 strings in
  `translations/hi/LC_MESSAGES/messages.po`, rendered via the stdlib
  `.po` reader in `studybuddy/i18n.py` (no Flask-Babel required).
  Strings with no Hindi entry fall back to English.
- The shared navigation and all other page bodies remain English.
  The switcher (EN | हिंदी, in the shared nav) therefore changes the
  landing page's language and persists the choice in a `lang` cookie;
  it does not translate the rest of the site. This scope is stated
  here and in the switcher's tooltip — not implied otherwise.
- Locale resolution: `?lang` (validated; invalid values never set a
  cookie) > `lang` cookie > `Accept-Language` header. Switching
  preserves the page's other query parameters (the switch links
  rebuild the current URL with `lang` replaced, they do not drop the
  query string). `<html lang>` follows the resolved locale on every
  template.

## Feature flags

| Variable | Default | Description |
|---|---|---|
| `FEATURE_LABS` | `1` | Ultimate Lab (`/ultimate/*`). Set `0` to disable. |
| `FEATURE_PEER_MOCKS` | `1` | Peer mock interviews. |
| `FEATURE_COMMUNITY` | `1` | Community mentors board. |

## Health endpoints

- `GET /healthz` — liveness, no DB. Render health check points here.
- `GET /readyz` — readiness: DB reachable, migrations at head, secret key set. 503 when not ready. Never exposes secrets.
- `GET /version` — git SHA, schema revision, Python version.
- ## LLM Configuration

STUDY-BUDDY supports three LLM modes controlled by the `LLM_MODE` environment variable.

| Variable | Default | Description |
|---|---|---|
| `LLM_MODE` | `server` | Selects the LLM operating mode: `off`, `byok`, or `server`. |
| `LLM_BASE_URL` | Not set | Base URL of a local OpenAI-compatible LLM endpoint, such as Ollama. |
| `LLM_MODEL` | `llama3.2` | Model name sent to the local LLM endpoint. |

### LLM Modes

- **`off`** — Disables LLM calls. The application uses rule-based fallbacks where available.
- **`byok`** — Uses the user's own API key configured in Profile → AI Settings. Requests are sent through OpenRouter.
- **`server`** — Uses configured providers in this order: local endpoint, Gemini, Groq, then OpenRouter. Providers without configured keys are skipped.

### Local Setup with Ollama

1. Install Ollama from [https://ollama.com/](https://ollama.com/).
2. Download the model and start the Ollama server:

   ```bash
   ollama pull llama3.2
   ollama serve
   ```

3. Set the environment variables.

**Linux/macOS:**

```bash
export LLM_MODE=server
export LLM_BASE_URL=http://localhost:11434/v1
export LLM_MODEL=llama3.2
```

**Windows PowerShell:**

```powershell
$env:LLM_MODE="server"
$env:LLM_BASE_URL="http://localhost:11434/v1"
$env:LLM_MODEL="llama3.2"
```

4. Keep Ollama running while using STUDY-BUDDY. The application sends chat-completion requests to the local endpoint.

### Important Notes

- `LLM_BASE_URL` should point to the OpenAI-compatible API base URL, including `/v1` for a standard Ollama setup.
- `LLM_MODEL` must match a model available in Ollama.
- In `server` mode, configured cloud providers can be used as fallbacks if the local endpoint fails.
- For local-only operation, leave cloud provider API keys unset and ensure Ollama is running with the selected model available.
- `byok` and `off` modes do not use the server-mode provider chain.
- Environment variables set in a terminal apply to processes launched from that terminal.

