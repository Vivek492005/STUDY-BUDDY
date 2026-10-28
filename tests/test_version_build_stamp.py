"""Audit Stage 4 (OPS-04): /version reports the built release, not literals.

``/version`` used to hardcode ``"1.0.0"`` and probe ``.git`` at runtime
(absent on Render, so prod always reported sha ``"unknown"``). The fix
stamps the real tag + SHA at build time via scripts/write_version.py
(wired into render.yaml's buildCommand); /version prefers the stamp
and falls back to the old behaviour when no stamp exists (local dev,
Docker builds whose context excludes .git).

The stamp file is git-ignored and must never be committed: these tests
generate it, assert on it, and delete it again (plus evict it from
sys.modules, so the tests are order-independent).
"""

import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
STAMP = REPO_ROOT / "studybuddy" / "_version.py"


def _remove_stamp():
    if STAMP.exists():
        STAMP.unlink()
    sys.modules.pop("studybuddy._version", None)


def _run_stamp_script():
    subprocess.run(
        [sys.executable, "scripts/write_version.py"],
        cwd=REPO_ROOT, check=True, capture_output=True, text=True,
    )


def _read_stamp():
    ns = {}
    exec(STAMP.read_text(encoding="utf-8"), ns)  # noqa: S102 - test reads own generated file
    return ns["GIT_SHA"], ns["APP_VERSION"]


def test_write_version_script_produces_matching_stamp():
    try:
        _remove_stamp()
        _run_stamp_script()
        assert STAMP.exists()
        sha, version = _read_stamp()
        expected_sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert sha == expected_sha
        assert re.fullmatch(r"dev|\d+\.\d+\.\d+", version)
    finally:
        _remove_stamp()


def test_version_prefers_build_stamp(client):
    try:
        _remove_stamp()
        _run_stamp_script()
        sha, version = _read_stamp()
        body = client.get("/version").get_json()
        assert body["git_sha"] == sha
        assert body["version"] == version
    finally:
        _remove_stamp()


def test_version_falls_back_without_stamp(client):
    _remove_stamp()
    body = client.get("/version").get_json()
    assert body["version"] == "1.0.0"
    assert body["git_sha"]  # runtime probe in dev, "unknown" without .git
    assert body["schema_revision"]


def test_render_build_runs_version_script():
    with open(REPO_ROOT / "render.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    build = data["services"][0]["buildCommand"]
    assert "scripts/write_version.py" in build
    assert build.index("write_version.py") < build.index("pip install")
