"""Regression test for the dev-server double-import bug.

Bug: running `python unified_app.py` loaded the module twice (`__main__` and
`unified_app`), creating two SQLAlchemy instances. studybuddy/* lazy imports
(`import unified_app`) resolved to the *second* copy, so /review 500'd with
"The current Flask app is not registered with this 'SQLAlchemy' instance"
(and momentum silently fell back to empty summaries).

Fix: alias sys.modules["unified_app"] to __main__ when run as a script.
This test boots the real dev server in a subprocess and proves /review
returns 200 there. (Under gunicorn the module loads once, unaffected.)
"""
import http.cookiejar
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

REPO = os.path.join(os.path.dirname(__file__), "..")
APP = os.path.join(REPO, "unified_app.py")
# Prefer the repo venv when present (local dev); fall back to the running
# interpreter (CI installs deps into the system python, no .venv there).
_VENV_PY = os.path.join(REPO, ".venv", "bin", "python")
PY = _VENV_PY if os.path.exists(_VENV_PY) else sys.executable


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_healthy(base, timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=3) as r:
                if r.status == 200:
                    return
        except Exception:  # noqa
            pass
        time.sleep(1)
    raise RuntimeError("dev server did not become healthy in time")


def test_dev_server_review_200():
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    db_path = os.path.join(tempfile.mkdtemp(prefix="sb-devsrv-"), "t.db")
    env = dict(os.environ,
               PORT=str(port),
               DATABASE_URL=f"sqlite:///{db_path}",
               SECRET_KEY="devsrv-test-secret",
               OPP_NO_AUTO_REFRESH="1",
               PYTHONPATH=REPO)
    proc = subprocess.Popen(
        [PY, APP], env=env, cwd=REPO,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_healthy(base)

        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))

        def get(path):
            with opener.open(base + path, timeout=10) as r:
                return r.status, r.read().decode("utf-8", "replace")

        def post(path, data):
            body = urllib.parse.urlencode(data).encode()
            req = urllib.request.Request(base + path, data=body)
            with opener.open(req, timeout=10) as r:
                return r.status, r.read().decode("utf-8", "replace")

        # Register via the real UI (CSRF token from the form).
        _s, html = get("/register")
        m = re.search(r'name="csrf_token" value="([^"]+)"', html)
        assert m, "CSRF token not found on /register"
        _s, _html = post("/register", {
            "csrf_token": m.group(1),
            "name": "Dev Srv",
            "email": "devsrv@example.com",
            "password": "devsrv-pass-123",
        })

        # The double-import bug made this 500 on `python unified_app.py`.
        status, _html = get("/review")
        assert status == 200, f"/review returned {status} on dev server"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # noqa
            proc.kill()


if __name__ == "__main__":
    test_dev_server_review_200()
    print("dev-server double-import regression test passed")
