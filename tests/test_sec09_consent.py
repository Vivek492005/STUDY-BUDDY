"""Audit Stage 4 (SEC-09): LLM consent re-consent on policy change.

What is covered here (honest scope):
- The version check itself: consent recorded under an old policy
  version does NOT count once the version bumps; re-consenting under
  the new version counts again.
- The surfacing: signed-in users without current consent see a
  "Review AI consent" prompt in the shared nav; anonymous users never
  do (consent is a per-account record).

What is NOT covered here: route-level enforcement. No AI route
currently checks consent (``_llm_consent_given`` is only read by the
template context processor), and the LLM surface has no central choke
point (``call_gemini_ai`` sites + ``ai_gateway`` + per-module clients),
so per-route gating needs an AI-route inventory first. See the
audit-fixes progress note for the exact follow-up. This file must not
be read as claiming every AI route enforces consent.
"""

from tests.conftest import login, register


def _authed(client, email="consent@example.com"):
    register(client, email=email)
    login(client, email=email)


def test_missing_consent_shows_nav_prompt(client):
    _authed(client)
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Review AI consent" in resp.data
    assert b"/consent/llm" in resp.data


def test_current_consent_hides_nav_prompt(client):
    _authed(client)
    resp = client.post("/consent/llm", data={"agree": "yes"},
                       follow_redirects=False)
    assert resp.status_code == 302
    resp = client.get("/")
    assert b"Review AI consent" not in resp.data


def test_stale_version_consent_does_not_count(client, monkeypatch):
    _authed(client)
    client.post("/consent/llm", data={"agree": "yes"})
    assert b"Review AI consent" not in client.get("/").data

    # Policy version bumps: the v1 consent must stop counting.
    monkeypatch.setattr("unified_app.CONSENT_POLICY_VERSION", "v2")
    resp = client.get("/")
    assert b"Review AI consent" in resp.data

    # Re-consenting under the new version counts again.
    client.post("/consent/llm", data={"agree": "yes"})
    assert b"Review AI consent" not in client.get("/").data


def test_withdraw_shows_prompt_again(client):
    _authed(client)
    client.post("/consent/llm", data={"agree": "yes"})
    assert b"Review AI consent" not in client.get("/").data
    resp = client.post("/consent/llm/withdraw", follow_redirects=False)
    assert resp.status_code == 302
    assert b"Review AI consent" in client.get("/").data


def test_anonymous_users_see_no_prompt(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Review AI consent" not in resp.data
