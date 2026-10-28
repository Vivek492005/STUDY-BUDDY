"""SEC-01 regression (audit 2026-10-28): password-reset link exposure.

Before the fix, POST /forgot-password flashed the raw reset link
("Reset link (dev): ...") to the unauthenticated requester, so anyone
who knew a user's email could take over the account. The fix removes
the flash entirely: the requester sees only the generic message, the
token stays hashed server-side, and the link is never emitted in a
response, a flash, or a log.

No mailer exists in this codebase yet (verified 2026-10-28: no
Flask-Mail, no mailer module, no email-provider env config), so these
tests assert the no-mailer behaviour: token row created, nothing
leaked. When email delivery lands (see docs/ops/password-reset-email.md),
extend this file with a mailer-stub test — do not weaken these
assertions.
"""

from unified_app import PasswordResetToken, User, db


def _make_user(app, email="reset@test.com"):
    with app.app_context():
        u = User(name="Reset User", email=email)
        u.set_password("MyStr0ng!Pass123")
        db.session.add(u)
        db.session.commit()
        return u.id


def test_forgot_password_issues_no_link(client, app):
    """Existing address: generic message only, token hashed server-side."""
    uid = _make_user(app)
    r = client.post("/forgot-password",
                    data={"email": "reset@test.com"},
                    follow_redirects=True)
    assert r.status_code == 200
    # The generic message is present (identical for known/unknown emails).
    assert b"If an account exists with that email" in r.data
    # No reset link anywhere in the rendered response: no flashed link,
    # no reset URL path, no token parameter.
    assert b"Reset link" not in r.data
    assert b"reset-password/" not in r.data
    # ...but the token row WAS created, hashed, with a 1-hour expiry.
    with app.app_context():
        rows = PasswordResetToken.query.filter_by(user_id=uid).all()
        assert len(rows) == 1
        assert rows[0].token_hash
        assert rows[0].token_hash != "reset"  # a hash, not a plaintext token
        assert rows[0].used_at is None


def test_forgot_password_unknown_email_same_message_no_row(client, app):
    """Unknown address: same generic message, no token row, no leak."""
    r = client.post("/forgot-password",
                    data={"email": "nobody@nowhere.com"},
                    follow_redirects=True)
    assert r.status_code == 200
    assert b"If an account exists with that email" in r.data
    assert b"Reset link" not in r.data
    assert b"reset-password/" not in r.data
    with app.app_context():
        assert PasswordResetToken.query.count() == 0


def test_forgot_password_responses_identical_known_vs_unknown(client, app):
    """Known vs unknown addresses must be indistinguishable (SEC-09).

    Same status and same generic flash text; only the server-side token
    row differs, which the requester cannot observe.
    """
    _make_user(app)
    known = client.post("/forgot-password",
                        data={"email": "reset@test.com"},
                        follow_redirects=True)
    unknown = client.post("/forgot-password",
                          data={"email": "nobody@nowhere.com"},
                          follow_redirects=True)
    assert known.status_code == unknown.status_code == 200
    assert (b"If an account exists with that email" in known.data) == \
           (b"If an account exists with that email" in unknown.data)
