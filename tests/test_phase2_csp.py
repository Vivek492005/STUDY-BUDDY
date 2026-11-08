"""Phase 2 — SEC-05 CSP enforced + nonces.

Enforced by default since the 2026-10-25 nonce rollout (all inline
blocks nonced, handlers converted, enforce-mode browser sweep clean).
CSP_REPORT_ONLY=1 is only a temporary opt-out; the default-mode tests
below assert the enforced header, and one test covers the opt-out.
"""
class TestCSPHeaders:
    def test_csp_enforced_header_present(self, client):
        r = client.get("/")
        # Enforced by default (CSP_REPORT_ONLY not set); the report-only
        # header must NOT appear in the production mode.
        assert "Content-Security-Policy" in r.headers
        assert "Content-Security-Policy-Report-Only" not in r.headers
        csp = r.headers["Content-Security-Policy"]
        assert "report-uri /csp-report" in csp
        assert "object-src 'none'" in csp
        assert "nonce-" in csp  # per-request nonce present

    def test_csp_report_only_when_env_set(self, client, monkeypatch):
        # The CSP_REPORT_ONLY=1 opt-out still works: the enforced header
        # is replaced by the report-only one (header is chosen per
        # request in _set_security_headers, so monkeypatch applies).
        monkeypatch.setenv("CSP_REPORT_ONLY", "1")
        r = client.get("/")
        assert "Content-Security-Policy-Report-Only" in r.headers
        assert "Content-Security-Policy" not in r.headers
        assert "nonce-" in r.headers["Content-Security-Policy-Report-Only"]

    def test_permissions_policy_present(self, client):
        r = client.get("/")
        assert "Permissions-Policy" in r.headers
        assert "camera=()" in r.headers["Permissions-Policy"]

    def test_csp_report_endpoint(self, client):
        r = client.post("/csp-report",
                        json={"csp-report": {"violated-directive": "script-src",
                                            "blocked-uri": "https://evil.com/x.js"}})
        assert r.status_code == 204

    def test_csp_nonce_available_in_templates(self, client):
        # nonce changes per request (not static)
        r1 = client.get("/")
        r2 = client.get("/")
        n1 = r1.headers["Content-Security-Policy"]
        n2 = r2.headers["Content-Security-Policy"]
        # extract nonce values
        import re
        m1 = re.search(r"nonce-([A-Za-z0-9_-]+)", n1)
        m2 = re.search(r"nonce-([A-Za-z0-9_-]+)", n2)
        assert m1 and m2
        assert m1.group(1) != m2.group(1), "nonce must be per-request"
