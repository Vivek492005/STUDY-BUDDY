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
