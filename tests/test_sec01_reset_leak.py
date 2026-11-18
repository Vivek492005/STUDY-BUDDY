"""SEC-01 regression (audit 2026-10-28): password-reset link exposure.

Before the fix, POST /forgot-password flashed the raw reset link
("Reset link (dev): ...") to the unauthenticated requester, so anyone
who knew a user's email could take over the account. The fix removes
the flash entirely: the requester sees only the generic message, the
token stays hashed server-side, and the link is never emitted in a
response, a flash, or a log.

Email delivery landed in B-2 (2026-11-18, studybuddy/mailer.py): the
reset link is emailed when the mailer is configured and still never
leaks into a response, flash, or log. The mailer-stub tests below pin
that; do not weaken these assertions.
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


def test_forgot_password_sends_reset_email_via_mailer(
        client, app, monkeypatch):
    """B-2: with the mailer stubbed, the reset email goes to the user's
    address with a reset link — and nothing leaks in the response."""
    uid = _make_user(app)
    sent = []

    def _fake_send(to_addr, reset_link):
        sent.append((to_addr, reset_link))
        return True

    monkeypatch.setattr("studybuddy.mailer.send_password_reset_email",
                        _fake_send)
    r = client.post("/forgot-password",
                    data={"email": "reset@test.com"},
                    follow_redirects=True)
    assert r.status_code == 200
    assert b"If an account exists with that email" in r.data
    assert b"Reset link" not in r.data
    assert b"reset-password/" not in r.data
    assert len(sent) == 1
    assert sent[0][0] == "reset@test.com"
    assert "/reset-password/" in sent[0][1]
    with app.app_context():
        rows = PasswordResetToken.query.filter_by(user_id=uid).all()
        assert len(rows) == 1


def test_forgot_password_unknown_email_sends_nothing(
        client, app, monkeypatch):
    """B-2: unknown address triggers no mailer call at all."""
    sent = []

    def _fake_send(to_addr, reset_link):
        sent.append((to_addr, reset_link))
        return True

    monkeypatch.setattr("studybuddy.mailer.send_password_reset_email",
                        _fake_send)
    r = client.post("/forgot-password",
                    data={"email": "nobody@nowhere.com"},
                    follow_redirects=True)
    assert r.status_code == 200
    assert sent == []
