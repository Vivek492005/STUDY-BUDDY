# Password reset: email integration follow-up (audit SEC-01)

Status (2026-10-28): **self-service reset is intentionally non-functional
until an email channel exists.** This is the honest state, not a bug to
work around by exposing the link.

## What changed and why

Before 2026-10-28, `POST /forgot-password` flashed the raw reset link
(`Reset link (dev): …`) to the unauthenticated requester. Anyone who knew
a user's email could obtain a valid reset link and take over the account,
which defeated the generic "If an account exists with that email, a reset
link has been sent." message. The flash was removed (SEC-01).

## Current behaviour (deliberate)

- `POST /forgot-password` always returns the same generic message and
  redirects to login, whether or not the address exists (no enumeration).
- When the address exists, a `PasswordResetToken` row is still created
  with the token stored **hashed** (never plaintext) and a 1-hour expiry,
  exactly as before — so the token-issuance path keeps working and a
  future mailer only has to deliver the link.
- The link itself is **discarded**: it is not flashed, not returned in
  any response, and not written to logs. Raw reset tokens in log storage
  would recreate the same exposure class (anyone with log access could
  redeem them), so logging the link is intentionally not done.
- No mailer exists in this codebase (no Flask-Mail, no mailer module, no
  email-provider env config was found on 2026-10-28). No provider was
  invented to fill the gap.

## What the maintainer must do to restore self-service reset

1. Choose an email provider (transactional: e.g. Resend, SendGrid, or
   Amazon SES — pick one; do not add more than one).
2. Add the provider's SDK to `requirements.in`, regenerate
   `requirements.txt` with hashes (`uv pip compile requirements.in
   --generate-hashes`), and keep the hash-pinned install path.
3. Add a mailer module (e.g. `studybuddy/mailer.py`) that reads its API
   key and sender address from environment variables only — never commit
   a key, and set the values in the Render dashboard (`sync: false` in
   `render.yaml`, same pattern as `GEMINI_API_KEY`).
4. In `forgot_password` (`unified_app.py`), after the token row is
   committed, send the link via the mailer instead of discarding it.
   Keep the generic response for both existing and unknown addresses,
   and keep the "link never in a flash/response/log" invariant.
5. Add a regression test: with the mailer stubbed, assert the reset
   email is addressed to the user's email and contains a link, and that
   no response body or flashed message contains the token. Extend
   `tests/test_sec01_reset_leak.py` rather than weakening it.
6. Verify end to end on staging: request a reset for a real address,
   click the emailed link, set a new password, and confirm the old
   sessions are revoked (see the session-hardening notes in the audit
   fixes progress note).

## Until then

Users who cannot log in need maintainer-assisted recovery (verify
identity out of band, then use the reset flow once email exists, or a
manual password set via the documented admin path). Do not restore the
dev flash to "make reset work" — that reintroduces SEC-01.
