"""OPS-06: optional Sentry error tracking with PII scrubbing.

Disabled by default. Set ``SENTRY_DSN`` in the environment to enable.
Never enable without the scrubber below — resumes, API keys and auth
tokens must never leave the server in an error report (PRIV-01).

Wiring (in unified_app.py, inside create_app — applied in B-3, 2026-11-18):

    from studybuddy.sentry_init import init_sentry
    init_sentry(app)   # no-op unless SENTRY_DSN is set
"""

from __future__ import annotations

import logging
import os
import re

log = logging.getLogger(__name__)

# Keys/values that must never be shipped to Sentry.
_SENSITIVE_KEY_RE = re.compile(
    r"password|passwd|secret|api[_-]?key|auth|token|session|cookie|"
    r"client[_-]?secret|private[_-]?key|fernet|resume[_-]?text|"
    r"interview[_-]?answer|linkedin",
    re.IGNORECASE,
)
_SENSITIVE_HEADER_RE = re.compile(
    r"authorization|cookie|set-cookie|x-api-key|proxy-authorization",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _redact_value(value):
    if isinstance(value, str) and _EMAIL_RE.search(value):
        return _EMAIL_RE.sub("[email-redacted]", value)
    return "[redacted]"


def _scrub_dict(mapping):
    """Recursively redact sensitive keys in a dict-like structure."""
    if not isinstance(mapping, dict):
        return mapping
    scrubbed = {}
    for key, value in mapping.items():
        if isinstance(key, str) and _SENSITIVE_KEY_RE.search(key):
            scrubbed[key] = "[redacted]"
        elif isinstance(value, dict):
            scrubbed[key] = _scrub_dict(value)
        elif isinstance(value, list):
            scrubbed[key] = [_scrub_dict(v) for v in value]
        elif isinstance(value, str):
            scrubbed[key] = _redact_value(value)
        else:
            scrubbed[key] = value
    return scrubbed


def scrub_event(event, hint=None):  # noqa: ARG001 - Sentry before_send signature
    """Sentry ``before_send`` hook: strip PII/secrets from an error event.

    Pure function — no sentry_sdk import needed, fully unit-testable.
    """
    if not isinstance(event, dict):
        return event

    # 1. Request data: drop bodies (may contain resumes/uploads), keep path.
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("data", None)  # raw POST bodies / form payloads
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {
                k: ("[redacted]" if _SENSITIVE_HEADER_RE.search(str(k)) else v)
                for k, v in headers.items()
            }
        request.pop("cookies", None)
        if isinstance(request.get("query_string"), str):
            # tokens sometimes leak into query strings
            request["query_string"] = _EMAIL_RE.sub(
                "[email-redacted]", request["query_string"])
        event["request"] = request

    # 2. User: keep only an opaque id, drop email/username/IP PII.
    user = event.get("user")
    if isinstance(user, dict):
        safe_user = {}
        if "id" in user:
            safe_user["id"] = user["id"]
        event["user"] = safe_user
        event.pop("user", None) if not safe_user else None

    # 3. Extra / contexts / tags: redact sensitive keys, scrub emails.
    for section in ("extra", "contexts", "tags"):
        if section in event:
            event[section] = _scrub_dict(event[section])

    # 4. Breadcrumbs and exception messages: scrub email addresses.
    for crumb in event.get("breadcrumbs", {}).get("values", []) or []:
        if isinstance(crumb, dict) and isinstance(crumb.get("message"), str):
            crumb["message"] = _EMAIL_RE.sub(
                "[email-redacted]", crumb["message"])
    for exc in event.get("exception", {}).get("values", []) or []:
        if isinstance(exc, dict):
            for field in ("value", "type"):
                val = exc.get(field)
                if isinstance(val, str):
                    exc[field] = _EMAIL_RE.sub("[email-redacted]", val)

    # 5. Never ship server-side env as tags.
    event.pop("server_name", None)
    return event


def init_sentry(app=None):
    """Initialize Sentry if SENTRY_DSN is set; otherwise a no-op.

    Returns the sentry_sdk module on success, None when disabled, when
    sentry_sdk is not installed, or when init fails (bad DSN, bad sample
    rates). Never raises: a misconfigured DSN must not break boot.
    """
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        log.info("Sentry disabled (SENTRY_DSN not set).")
        return None
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
    except ImportError:
        log.warning("SENTRY_DSN set but sentry_sdk not installed; "
                    "error tracking disabled.")
        return None

    try:
        traces_rate = float(os.environ.get("SENTRY_TRACES_RATE", "0.05"))
        profiles_rate = float(
            os.environ.get("SENTRY_PROFILES_RATE", "0.0"))
        sentry_sdk.init(
            dsn=dsn,
            before_send=scrub_event,
            integrations=[FlaskIntegration()],
            # No send_default_pii option: in sentry-sdk 2.75 the option is
            # deprecated (passing it warns) and omitting it maps to False
            # internally (client.py: send_default_pii defaults to False
            # when unset), so PII stays off with no warning.
            traces_sample_rate=traces_rate,
            profiles_sample_rate=profiles_rate,
            environment=os.environ.get(
                "SENTRY_ENVIRONMENT",
                os.environ.get("FLASK_ENV", "production")),
            release=os.environ.get("SENTRY_RELEASE", "study-buddy@1.0.0"),
        )
    except Exception:
        # Bad DSN (BadDsn) or bad tuning values must never break boot.
        log.warning("Sentry init failed; error tracking disabled.",
                    exc_info=True)
        return None
    if app is not None and hasattr(app, "logger"):
        app.logger.info("Sentry error tracking enabled (PII scrubbing on).")
    return sentry_sdk
