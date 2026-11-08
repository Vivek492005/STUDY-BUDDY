"""Regression: CSP report pipeline + inline-script nonces.

Found via Lighthouse best-practices (0.93 vs 0.95 budget) on 2026-10-22:
browsers POST violation reports to /csp-report, but CSRFProtect 400'd
every one (reports cannot carry a token), and the nonce-less inline
scripts in base/index templates generated a violation on every page.
"""
import re

import pytest

from unified_app import create_app


@pytest.fixture()
def csrf_app():
    return create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SECRET_KEY": "test-secret-key",
        "WTF_CSRF_ENABLED": True,  # production condition
        "RATELIMIT_ENABLED": False,
    })


@pytest.fixture()
def csrf_client(csrf_app):
    return csrf_app.test_client()


def test_csp_report_accepts_browser_reports_without_csrf(csrf_client):
    payload = {
        "csp-report": {
            "violated-directive": "script-src 'self'",
            "blocked-uri": "inline",
        }
    }
    r = csrf_client.post("/csp-report", json=payload)
    assert r.status_code == 204


def test_audited_pages_have_no_bare_inline_scripts(csrf_client):
    for path in ("/", "/login", "/resume"):
        html = csrf_client.get(path).get_data(as_text=True)
        assert "<script>" not in html, f"bare <script> left on {path}"


def test_inline_script_nonce_matches_csp_header(csrf_client):
    r = csrf_client.get("/")
    # Enforced by default; fall back to the report-only header only when
    # the CSP_REPORT_ONLY=1 opt-out is active.
    header = (r.headers.get("Content-Security-Policy")
              or r.headers.get("Content-Security-Policy-Report-Only") or "")
    m = re.search(r"'nonce-([^']+)'", header)
    assert m, "CSP header must carry a per-request nonce"
    html = r.get_data(as_text=True)
    assert f'nonce="{m.group(1)}"' in html


def test_csp_beacon_fallback_snippet_present(csrf_client):
    # #19: when the browser's native report-uri POST is blocked (e.g.
    # by an adblocker), the base template's securitypolicyviolation
    # listener re-sends the report via fetch, falling back to
    # navigator.sendBeacon — so violations still get logged.
    for path in ("/", "/login", "/resume"):
        html = csrf_client.get(path).get_data(as_text=True)
        assert "securitypolicyviolation" in html, path
        assert "sendBeacon" in html, path
        assert "/csp-report" in html, path
