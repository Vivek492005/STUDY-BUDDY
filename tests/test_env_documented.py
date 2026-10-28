"""Audit Stage 4 (OPS-03): every env key read in app code is documented.

The gap that motivated this test: `.env.example` once omitted the
fallback-LLM keys, feature flags, Fernet, Sentry, admin, and quota
variables, and `docs/configuration.md` falsely claimed nothing else
read `os.environ` — while 35 keys were read across modules. This test
statically scans unified_app.py + studybuddy/ + app/ for
`os.environ` / `os.getenv` / `environ[...]` reads and requires each key
to appear in `.env.example`, `docs/configuration.md`, or the platform
allowlist below.

Known limitation (documented, not hidden): dynamically read keys
bypass a static scan — `RATELIMIT_STORAGE_URI` is read via a dynamic
`__import__("os")` call in `app/extensions.py`, so it is documented
in `docs/configuration.md` but invisible here. A dynamically read key
must still be documented by hand; this test catches everything else.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Platform-detection flags set by the host itself, not user-configured.
# They are documented in docs/configuration.md too, but allowed here so
# the test does not depend on that section's wording.
PLATFORM_ALLOWLIST = {"DYNO", "RENDER"}

_SCAN = re.compile(
    r"""(?:os\.environ(?:\.get)?|os\.getenv|getenv)\(\s*["']([A-Z][A-Z0-9_]*)["']"""
    r"""|os\.environ\[\s*["']([A-Z][A-Z0-9_]*)["']"""
)


def _scanned_keys() -> set[str]:
    keys = set()
    paths = [REPO_ROOT / "unified_app.py"]
    paths += sorted((REPO_ROOT / "studybuddy").glob("*.py"))
    paths += sorted((REPO_ROOT / "app").rglob("*.py"))
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in _SCAN.finditer(text):
            keys.add(match.group(1) or match.group(2))
    return keys


def _docs_text() -> str:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    text += "\n" + (REPO_ROOT / "docs" / "configuration.md").read_text(
        encoding="utf-8")
    return text


def test_every_env_key_is_documented():
    docs = _docs_text()
    undocumented = [
        key for key in sorted(_scanned_keys())
        if key not in PLATFORM_ALLOWLIST
        and not re.search(rf"\b{key}\b", docs)
    ]
    assert undocumented == [], (
        f"Env keys read in code but documented nowhere: {undocumented}\n"
        f"Fix: document each in .env.example or docs/configuration.md "
        f"(or add it to PLATFORM_ALLOWLIST with a comment, if the host "
        f"sets it and users never configure it)."
    )
