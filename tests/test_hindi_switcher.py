"""Hindi switcher (GFI #1): landing page renders in Hindi, choice persists.

Acceptance: toggle works end-to-end on the landing page; a test asserts a
Hindi string renders; no other page regresses (default stays English).
"""


def test_landing_default_is_english(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Learn" in resp.data
    assert "सीखें".encode("utf-8") not in resp.data
    assert b"?lang=hi" in resp.data  # toggle link present


def test_landing_hindi_via_param(client):
    resp = client.get("/?lang=hi")
    assert resp.status_code == 200
    assert "सीखें".encode("utf-8") in resp.data
    assert "छात्रों के लिए".encode("utf-8") in resp.data
    assert 'lang="hi"'.encode("utf-8") in resp.data


def test_lang_param_sets_persistent_cookie(client):
    resp = client.get("/?lang=hi")
    assert resp.status_code == 200
    assert "lang=hi" in resp.headers.get("Set-Cookie", "")


def test_lang_cookie_persists_across_requests(client):
    client.set_cookie("lang", "hi")
    resp = client.get("/")
    assert resp.status_code == 200
    assert "सीखें".encode("utf-8") in resp.data


def test_lang_cookie_can_switch_back_to_english(client):
    client.set_cookie("lang", "hi")
    resp = client.get("/?lang=en")
    assert resp.status_code == 200
    assert "सीखें".encode("utf-8") not in resp.data
    assert b"Learn" in resp.data


def test_invalid_lang_falls_back_to_english(client):
    resp = client.get("/?lang=xx")
    assert resp.status_code == 200
    assert b"Learn" in resp.data
    assert "सीखें".encode("utf-8") not in resp.data
    # Invalid values must never set a lang cookie.
    assert "lang=" not in resp.headers.get("Set-Cookie", "")


# --- Audit Stage 3: query-preserving switch links + dynamic <html lang> ---


def test_switch_link_preserves_query_params(client):
    # Old switcher used {{ request.path }}?lang=..., dropping the page's
    # other query parameters (e.g. /dsa?topic=arrays lost the filter).
    resp = client.get("/aptitude?topic=arrays")
    assert resp.status_code == 200
    assert b"topic=arrays&amp;lang=hi" in resp.data
    assert b"topic=arrays&amp;lang=en" in resp.data


def test_switch_link_replaces_existing_lang_param(client):
    import re

    resp = client.get("/aptitude?lang=en&topic=arrays")
    assert resp.status_code == 200
    match = re.search(
        rb'<a href="([^"]+)"[^>]*>' + "हिंदी".encode("utf-8") + rb"</a>",
        resp.data,
    )
    assert match is not None
    href = match.group(1)
    assert b"topic=arrays" in href
    assert b"lang=hi" in href
    assert b"lang=en" not in href


def test_html_lang_follows_locale_on_standalone_and_base_pages(client):
    # Standalone template (own <html> tag) ...
    resp = client.get("/aptitude?lang=hi")
    assert resp.status_code == 200
    assert b'<html lang="hi"' in resp.data
    # ... and a base-extending template (inherits base.html's <html>).
    resp = client.get("/learn/roadmaps?lang=hi")
    assert resp.status_code == 200
    assert b'<html lang="hi"' in resp.data


def test_switcher_states_hindi_scope_in_ui(client):
    # Honest scope: Hindi covers the landing page; the rest of the site
    # stays in English. The switcher's tooltip says so in the UI itself,
    # not just in docs/configuration.md.
    resp = client.get("/")
    assert resp.status_code == 200
    assert (
        b"Hindi translation currently covers the landing page"
        in resp.data
    )
