"""Audit Stage 4 (SEC-06): registration must not reveal account existence.

The old flow flashed "An account with this email already exists" (and
auto-logged-in new accounts) — an attacker could enumerate registered
emails. The fixed flow returns an identical response for new and
existing emails: same status, same redirect target, same flash, no
auto-login either way. There is no mailer yet, so no "check your
inbox" promise is made; the shared message is true for both cases.
"""


def _register(client, email):
    return client.post("/register", data={
        "name": "Case", "email": email,
        "password": "Str0ng!Test#2026", "age_confirm": "1",
    }, follow_redirects=False)


def test_new_and_existing_email_responses_are_indistinguishable(client):
    first = _register(client, "uniform@example.com")
    assert first.status_code == 302
    assert first.headers["Location"].endswith("/login")
    first_page = client.get("/login")
    assert b"Your account is ready. Please log in to continue." in first_page.data

    second = _register(client, "uniform@example.com")
    assert second.status_code == first.status_code
    assert second.headers["Location"] == first.headers["Location"]
    second_page = client.get("/login")
    assert b"Your account is ready. Please log in to continue." in second_page.data
    assert b"already exists" not in second_page.data


def test_new_account_is_not_auto_logged_in(client):
    _register(client, "noauto@example.com")
    # Not authenticated: dashboard bounces to login.
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    # The created account does log in with its password.
    resp = client.post("/login", data={
        "email": "noauto@example.com", "password": "Str0ng!Test#2026",
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert client.get("/dashboard").status_code == 200


def test_validation_errors_still_reported(client):
    # Uniformity applies to the existence branch only; validation
    # errors (which fire before the existence check, identically for
    # new and existing emails) are unchanged.
    resp = client.post("/register", data={
        "name": "A", "email": "weak@example.com",
        "password": "short", "age_confirm": "1",
    })
    assert resp.status_code == 200
    assert b"at least 10 characters" in resp.data
