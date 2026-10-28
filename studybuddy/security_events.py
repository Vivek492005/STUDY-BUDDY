"""SEC-14: structured security event logging.

Emits one JSON-ish log line per security-relevant event on the
``studybuddy.security`` logger:

    login_success, login_failure,
    password_reset_request, password_reset_complete,
    rate_limit_hit, ssrf_block, upload_reject, admin_action,
    register_attempt

Privacy rules (hard):
- NEVER log emails, passwords, tokens, raw IPs, full URLs, filenames,
  or file contents. Only hashed identifiers and non-PII categories.
- ``details`` must be a flat dict of codes/counts/booleans — anything
  that could carry PII is dropped by _scrub_details().
"""
from __future__ import annotations

import hashlib
import logging
import time

log = logging.getLogger("studybuddy.security")

# Event types this module emits. Callers should use one of these so
# dashboards/alerts can match on event_type.
EVENT_TYPES = frozenset({
    "login_success",
    "login_failure",
    "password_reset_request",
    "password_reset_complete",
    "rate_limit_hit",
    "ssrf_block",
    "upload_reject",
    "admin_action",
    # SEC-06 (audit Stage 4): registration attempts, with an
    # account_exists boolean for abuse detection. The boolean is
    # internal-only — the user-facing response never reveals it.
    "register_attempt",
})

# Keys that must never appear in details (defense in depth — callers
# shouldn't pass them, but we strip them anyway).
_FORBIDDEN_DETAIL_KEYS = frozenset({
    "email", "password", "token", "secret", "key", "authorization",
    "cookie", "set_cookie", "filename", "file_content", "url",
    "ip", "ip_address", "remote_addr",
})


def _hash(value) -> str:
    """One-way hash for identifiers (user id, IP)."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _current_user_hash():
    """Hashed user id, or 'anon'. Never the raw id."""
    try:
        from flask_login import current_user
        if current_user.is_authenticated:
            return _hash(current_user.get_id())
    except Exception:
        pass
    return "anon"


def _current_ip_hash():
    """Hashed client IP, or 'unknown'. Never the raw IP."""
    try:
        from flask import request
        ip = request.remote_addr
        if ip:
            return _hash(ip)
    except Exception:
        pass
    return "unknown"


def _scrub_details(details):
    """Return a PII-free copy of details. Non-dict/None -> {}."""
    if not isinstance(details, dict):
        return {}
    clean = {}
    for k, v in details.items():
        kl = str(k).lower()
        if kl in _FORBIDDEN_DETAIL_KEYS:
            continue
        # Only scalars; stringify defensively and cap length.
        if isinstance(v, (str, int, float, bool)) or v is None:
            s = str(v)
            clean[str(k)] = s[:200]
        # nested structures are dropped — flat codes only
    return clean


def log_security_event(event_type, user_id=None, ip_hash=None, details=None):
    """Emit a structured security event.

    Args:
        event_type: one of EVENT_TYPES.
        user_id: raw user id (hashed internally) or None to use the
            current request's user. Pass the sentinel ``"anon"``-style
            raw value only if you truly have no user.
        ip_hash: pre-hashed IP, or None to hash request.remote_addr.
        details: flat dict of non-PII codes/counts/booleans.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unknown security event type: {event_type!r}")
    if user_id is None:
        user_ref = _current_user_hash()
    else:
        user_ref = _hash(user_id)
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "event_type": event_type,
        "user": user_ref,
        "ip_hash": ip_hash if ip_hash else _current_ip_hash(),
        "details": _scrub_details(details),
    }
    # Structured single line; the JSON formatter in observability handles
    # production output. Keep the human-readable prefix for dev logs.
    log.warning("security_event %s", payload)
