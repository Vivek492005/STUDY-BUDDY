"""Stage 4 regression: remember-cookie clearing vs session.clear().

Root cause (caught by the e2e delete-account test, 2026-10-28):
Flask-Login's logout_user() does not delete the remember cookie
immediately — it sets session["_remember"] = "clear", and the actual
deletion happens in an after_request handler. Both the logout route
and the session-version hook called session.clear() AFTER
logout_user(), wiping that marker, so the remember cookie survived
every logout and every revocation. A browser presenting the surviving
cookie re-restored the same version-less session on the next request,
and the hook rejected it again — an infinite /login redirect loop for
any remember-me user (the login form checks "Keep me logged in" by
default, so this hit every browser login).

The fix (auth_bp.logout + the session-version hook) restores the
marker after clearing, when a remember cookie was presented. These
tests lock both halves: logout deletes the cookie, and a revoked
remember-restored session terminates instead of looping.
"""

from tests.conftest import register


def _login_with_remember(client, email):
    register(client, email=email)
    resp = client.post("/login", data={
        "email": email, "password": "Str0ng!Test#2026", "remember": "1",
    }, follow_redirects=False)
    assert resp.status_code == 302
    return resp


def _remember_cookie_from_response(resp):
    for header in resp.headers.getlist("Set-Cookie"):
        if header.startswith("remember_token="):
            return header
    return None


def test_logout_deletes_remember_cookie(client):
    resp = _login_with_remember(client, "logout-remember@example.com")
    assert _remember_cookie_from_response(resp) is not None
    resp = client.post("/logout", follow_redirects=False)
    assert resp.status_code == 302
    cleared = _remember_cookie_from_response(resp)
    assert cleared is not None, "logout must clear the remember cookie"
    assert "remember_token=;" in cleared
    assert "Max-Age=0" in cleared or "1970" in cleared


def test_revoked_remember_session_terminates_instead_of_looping(client):
    resp = _login_with_remember(client, "loop-remember@example.com")
    cookie = _remember_cookie_from_response(resp)
    assert cookie is not None
    value = cookie.split(";", 1)[0].split("=", 1)[1]

    # The same client presenting ONLY the remember cookie (no session):
    # Flask-Login restores the user, the hook rejects the version-less
    # session, and the response must delete the cookie — so the next
    # request renders the login page instead of redirecting again.
    client.delete_cookie("session")
    client.delete_cookie("remember_token")
    client.set_cookie("remember_token", value)
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    cleared = _remember_cookie_from_response(resp)
    assert cleared is not None, "revocation must clear the remember cookie"
    assert "remember_token=;" in cleared
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code == 200, "second request must render login, not loop"
