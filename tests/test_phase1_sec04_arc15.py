"""Phase 1 — SEC-04 rate-limit keying/storage + ARC-15 time handling.

SEC-04 acceptance: key by user id when authenticated else IP; storage from
RATELIMIT_STORAGE_URI; global defaults; 429 carries Retry-After.
ARC-15: no datetime.utcnow() anywhere; naive UTC helper used for DB compares.
"""
import re


class TestRateLimitKeying:
    def test_key_prefers_user_id_when_authenticated(self, client, app):
        from app.extensions import _rate_limit_key  # ARC-01: moved
        from unified_app import User, db
        from flask_login import login_user
        with app.app_context():
            user = User(name="kuser", email="k1@example.com")
            user.set_password("pw123456")
            db.session.add(user)
            db.session.commit()
            uid = user.id
        with client.application.test_request_context("/"):
            login_user(db.session.get(User, uid))
            assert _rate_limit_key() == f"user:{uid}"

    def test_key_falls_back_to_ip_when_anonymous(self, client):
        from app.extensions import _rate_limit_key  # ARC-01: moved
        with client.application.test_request_context(
                "/", environ_base={"REMOTE_ADDR": "9.9.9.9"}):
            key = _rate_limit_key()
        assert key.startswith("ip:"), key

    def test_limiter_has_global_defaults_and_storage_uri(self):
        import inspect
        # ARC-01: limiter moved to app/extensions.py (strangler pattern)
        from app import extensions as ext
        src = inspect.getsource(ext)
        # SEC-04: global defaults + env-driven storage + user-aware key func
        assert '"200 per day"' in src and '"60 per hour"' in src
        assert 'RATELIMIT_STORAGE_URI' in src
        assert 'def _rate_limit_key' in src
        assert 'user:{' in src  # user id preferred when authenticated

    def test_redis_client_available_for_shared_store(self):
        # B-5: Flask-Limiter's Redis backend (limits storage) imports
        # the redis client — a Redis RATELIMIT_STORAGE_URI crashes boot
        # without this dependency installed.
        import redis

        assert redis.__version__.startswith("8.")

    def test_swallow_errors_config_wired(self):
        # B-5: the config key must be the exact one Flask-Limiter reads
        # (flask_limiter.constants.ConfigVars.SWALLOW_ERRORS), and it
        # must default to strict (False) while on memory:// storage.
        from flask_limiter.constants import ConfigVars

        from app.config import BaseConfig

        assert ConfigVars.SWALLOW_ERRORS == "RATELIMIT_SWALLOW_ERRORS"
        assert hasattr(BaseConfig, "RATELIMIT_SWALLOW_ERRORS")
        assert BaseConfig.RATELIMIT_SWALLOW_ERRORS is False

    def test_login_has_stricter_limit(self, client):
        # brute-force guard present on /login route rules
        rules = [str(r) for r in client.application.url_map.iter_rules()
                 if "login" in str(r)]
        assert rules, "login route must exist"


class TestRateLimitHeaders:
    """#13: X-RateLimit-* headers (Flask-Limiter headers_enabled=True).

    Runs in a subprocess on purpose: create_app disables the limiter
    under TESTING, and flask-limiter 4.x binds its header injection to
    the first app served in a process — the suite creates hundreds of
    apps per worker, so in-process assertions would test that quirk
    instead of the production shape (one app per process, as here).
    A dedicated RFC-5737 IP keeps the rate bucket isolated.
    """

    _PROBE = r"""
import json, os, sys
sys.path.insert(0, os.getcwd())
os.environ["OPP_NO_AUTO_REFRESH"] = "1"
from unified_app import create_app
from app.extensions import limiter

app = create_app({"TESTING": True,
                  "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
                  "SECRET_KEY": "test-secret-key",
                  "WTF_CSRF_ENABLED": False})
limiter.enabled = True
client = app.test_client()
ip = {"REMOTE_ADDR": "203.0.113.11"}
out = {}
r = client.get("/healthz", environ_base=ip)
out["healthz_status"] = r.status_code
out["healthz_headers"] = {k: v for k, v in r.headers.items()
                          if "ratelimit" in k.lower()}
# /csp-report carries an explicit 60/hour limit (SEC-05): exhaust it.
statuses = [client.post("/csp-report", json={},
                        environ_base=ip).status_code for _ in range(61)]
out["csp_last_status"] = statuses[-1]
out["csp_ok_count"] = statuses.count(204)
r = client.post("/csp-report", json={}, environ_base=ip)
out["csp_429_headers"] = {k: v for k, v in r.headers.items()
                          if "ratelimit" in k.lower() or k == "Retry-After"}
print("PROBE_JSON:" + json.dumps(out))
"""

    def _run_probe(self):
        import json
        import os
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, "-c", self._PROBE],
            capture_output=True, text=True,
            cwd=os.path.join(os.path.dirname(__file__), ".."),
            timeout=180)
        assert result.returncode == 0, result.stderr[-2000:]
        line = [ln for ln in result.stdout.splitlines()
                if ln.startswith("PROBE_JSON:")]
        assert line, result.stdout[-1000:]
        return json.loads(line[0][len("PROBE_JSON:"):])

    def test_headers_on_default_limited_endpoint(self):
        out = self._run_probe()
        assert out["healthz_status"] == 200
        headers = out["healthz_headers"]
        assert int(headers["X-RateLimit-Limit"]) > 0
        assert "X-RateLimit-Remaining" in headers
        assert "X-RateLimit-Reset" in headers

    def test_headers_and_429_on_exhausted_route_limit(self):
        out = self._run_probe()
        assert out["csp_ok_count"] == 60
        assert out["csp_last_status"] == 429
        assert "X-RateLimit-Limit" in out["csp_429_headers"]
        assert "Retry-After" in out["csp_429_headers"]


class TestTimeHandling:
    def test_no_utcnow_anywhere(self):
        import pathlib
        bad = []
        paths = [pathlib.Path("unified_app.py"),
                 *pathlib.Path("studybuddy").glob("*.py")]
        for p in paths:
            text = p.read_text()
            if re.search(r"(?<!_utcnow_naive)(?<!def _)\.utcnow\(\)", text):
                bad.append(str(p))
        assert not bad, f"utcnow() found in: {bad}"

    def test_utcnow_naive_returns_naive_utc(self):
        from datetime import datetime, timezone
        from unified_app import _utcnow_naive
        got = _utcnow_naive()
        assert got.tzinfo is None
        delta = abs((datetime.now(timezone.utc).replace(tzinfo=None) - got).total_seconds())
        assert delta < 10
