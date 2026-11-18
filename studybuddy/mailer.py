"""B-2: provider-agnostic SMTP mailer (stdlib only, no new dependency).

Reads all credentials from environment variables only — never commit a
key. Designed for Brevo's free SMTP relay by default (smtp-relay.brevo.com:587)
but works with any SMTP provider via env overrides.

Env vars:
    SMTP_HOST     Relay host (default: smtp-relay.brevo.com).
    SMTP_PORT     Relay port (default: 587). Port 465 uses implicit TLS.
    SMTP_USER     Relay username (Brevo: login email). Required to send.
    SMTP_PASSWORD Relay password (Brevo: SMTP key). Required to send.
    MAIL_FROM     Verified sender address. Required to send.
    SMTP_USE_TLS  "1" (default) = STARTTLS on ports other than 465.

Every public function is total: it returns False (and logs) instead of
raising, so a mail failure can never break a request flow. No recipient
address or token is ever logged.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger(__name__)

DEFAULT_SMTP_HOST = "smtp-relay.brevo.com"
DEFAULT_SMTP_PORT = 587
SMTP_TIMEOUT_SECONDS = 10


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def mail_config() -> dict:
    """Return the effective SMTP config (password included — never log it)."""
    try:
        port = int(_env("SMTP_PORT", str(DEFAULT_SMTP_PORT)))
    except ValueError:
        log.warning("SMTP_PORT is not a number; using default %s.",
                    DEFAULT_SMTP_PORT)
        port = DEFAULT_SMTP_PORT
    return {
        "host": _env("SMTP_HOST", DEFAULT_SMTP_HOST),
        "port": port,
        "user": _env("SMTP_USER"),
        "password": _env("SMTP_PASSWORD"),
        "from_addr": _env("MAIL_FROM"),
        "use_tls": _env("SMTP_USE_TLS", "1") != "0",
    }


def is_mail_configured() -> bool:
    """True when sending is possible (user + password + sender all set)."""
    cfg = mail_config()
    return bool(cfg["user"] and cfg["password"] and cfg["from_addr"])


def send_mail(to_addr: str, subject: str, text_body: str,
              html_body: str | None = None) -> bool:
    """Send one email via the configured SMTP relay.

    Returns True on accepted delivery, False otherwise (not configured,
    bad config, or relay error). Never raises; never logs the recipient
    address or any message content.
    """
    if not to_addr or not subject or not text_body:
        log.warning("send_mail called with missing to/subject/body.")
        return False
    cfg = mail_config()
    if not (cfg["user"] and cfg["password"] and cfg["from_addr"]):
        log.info("Mailer not configured (SMTP_USER/SMTP_PASSWORD/MAIL_FROM "
                 "missing); email skipped.")
        return False
    msg = EmailMessage()
    msg["From"] = cfg["from_addr"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    try:
        if cfg["port"] == 465:
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"],
                                      timeout=SMTP_TIMEOUT_SECONDS)
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"],
                                  timeout=SMTP_TIMEOUT_SECONDS)
        with server as smtp:
            if cfg["port"] != 465 and cfg["use_tls"]:
                smtp.starttls()
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
    except Exception:
        log.warning("SMTP send failed (host=%s port=%s).",
                    cfg["host"], cfg["port"], exc_info=True)
        return False
    log.info("Email sent (subject=%r).", subject)
    return True


RESET_SUBJECT = "Reset your STUDY-BUDDY password"


def send_password_reset_email(to_addr: str, reset_link: str) -> bool:
    """Send the password-reset email. Returns False instead of raising.

    The link is passed in by the caller (built with url_for _external);
    it is never logged here.
    """
    if not to_addr or not reset_link:
        log.warning("send_password_reset_email called without address/link.")
        return False
    text_body = (
        "Hi,\n\n"
        "You asked to reset your STUDY-BUDDY password. Use the link below "
        "to choose a new one. The link expires in 1 hour and works once.\n\n"
        f"{reset_link}\n\n"
        "If you did not ask for this, ignore this email — your password "
        "stays unchanged.\n\n"
        "— STUDY-BUDDY"
    )
    html_body = (
        "<p>Hi,</p>"
        "<p>You asked to reset your STUDY-BUDDY password. Use the link "
        "below to choose a new one. The link expires in 1 hour and works "
        "once.</p>"
        f'<p><a href="{reset_link}">Reset your password</a></p>'
        "<p>If you did not ask for this, ignore this email — your password "
        "stays unchanged.</p>"
        "<p>— STUDY-BUDDY</p>"
    )
    return send_mail(to_addr, RESET_SUBJECT, text_body, html_body)
