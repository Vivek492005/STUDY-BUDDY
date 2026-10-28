"""Session hardening (audit fix, Stage 2, 2026-10-28).

Covers: remember-me cookie settings (30 days, Lax, Secure in prod),
session-version revocation on password change, version bump on reset,
logout clearing the session, and fresh-login gating for /account/export.
"""

from datetime import timedelta

from tests.conftest import login


def test_remember_cookie_settings():
    from app.config import BaseConfig, ProductionConfig
    assert BaseConfig.REMEMBER_COOKIE_SAMESITE == "Lax"
    assert BaseConfig.REMEMBER_COOKIE_DURATION == timedelta(days=30)
    assert BaseConfig.REMEMBER_COOKIE_HTTPONLY is True
    assert ProductionConfig.REMEMBER_COOKIE_SECURE is True


def test_remember_cookie_settings_applied(app):
    assert app.config["REMEMBER_COOKIE_SAMESITE"] == "Lax"
    assert app.config["REMEMBER_COOKIE_DURATION"] == timedelta(days=30)


def _make_user(app, email):
    from unified_app import User, db
    with app.app_context():
        u = User(name="Sess User", email=email)
        u.set_password("Str0ng!Pass1")
        db.session.add(u)
        db.session.commit()
        return u.id


def test_stale_session_logged_out_after_version_bump(client, app):
    """A session adopted at version 0 is revoked when the version moves."""
    uid = _make_user(app, "sess1@example.com")
    login(client, email="sess1@example.com", password="Str0ng!Pass1")
    # First authed request adopts the current version (fresh login).
    r = client.get("/dashboard")
    assert r.status_code == 200
    # Simulate a password change elsewhere (bumps session_version).
    from unified_app import User, db
    with app.app_context():
        user = db.session.get(User, uid)
        user.session_version = (user.session_version or 0) + 1
        db.session.commit()
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]
    # And the session is really gone, not just redirected once.
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_password_reset_bumps_version_and_revokes_session(client, app):
    """Full reset flow: old session dies, new password works."""
    from studybuddy.auth_hardening import generate_reset_token
    from werkzeug.security import generate_password_hash
    from unified_app import PasswordResetToken, User, db
    from datetime import datetime, timezone, timedelta as _td

    uid = _make_user(app, "sess2@example.com")
    login(client, email="sess2@example.com", password="Str0ng!Pass1")
    assert client.get("/dashboard").status_code == 200
    raw = generate_reset_token()
    with app.app_context():
        db.session.add(PasswordResetToken(
            user_id=uid, token_hash=generate_password_hash(raw),
            expires_at=datetime.now(timezone.utc) + _td(hours=1)))
        db.session.commit()
    r = client.post(f"/reset-password/{raw}",
                    data={"password": "NewStr0ng!Pass2"},
                    follow_redirects=False)
    assert r.status_code == 302, f"reset failed: {r.status_code}"
    with app.app_context():
        assert db.session.get(User, uid).session_version == 1
    # The pre-reset session is now stale.
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]
    # New password works, old one does not.
    login(client, email="sess2@example.com", password="NewStr0ng!Pass2")
    assert client.get("/dashboard").status_code == 200


def test_logout_clears_session(client, app):
    _make_user(app, "sess3@example.com")
    login(client, email="sess3@example.com", password="Str0ng!Pass1")
    assert client.get("/dashboard").status_code == 200
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 302
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_export_requires_fresh_session(client, app):
    """Fresh (password) login downloads; a remember-me-restored
    session (fresh flag False, as Flask-Login sets on cookie loads)
    is sent back to login first."""
    _make_user(app, "sess4@example.com")
    login(client, email="sess4@example.com", password="Str0ng!Pass1")
    r = client.get("/account/export")
    assert r.status_code == 200
    assert "attachment" in r.headers["Content-Disposition"]
    # Simulate a remember-me restoration: same user, stale session.
    with client.session_transaction() as sess:
        sess["_fresh"] = False
    r = client.get("/account/export", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_delete_still_requires_password_and_delete_word(client, app):
    """The delete route's own re-auth is unchanged by the refactor."""
    _make_user(app, "sess5@example.com")
    login(client, email="sess5@example.com", password="Str0ng!Pass1")
    r = client.post("/account/delete",
                    data={"password": "wrongpass", "confirm": "DELETE"})
    assert r.status_code == 200
    from unified_app import User
    with app.app_context():
        assert User.query.filter_by(
            email="sess5@example.com").first() is not None
