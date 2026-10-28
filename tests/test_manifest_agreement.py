"""Audit Stage 4 (DOC-02): pyproject.toml must mirror requirements.in.

The drift that motivated this test: pyproject once disagreed with
requirements.in on python-dotenv (1.0.0 vs 1.2.4) and google-genai
(2.30.1 vs 2.31.0), left Authlib unversioned, and omitted six direct
deps — so `pip install .` built a different app than the deployed
hash-pinned requirements.txt path. requirements.in + requirements.txt
remain authoritative; this test only locks the mirror.
"""

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _normalize(spec: str) -> str:
    spec = spec.strip().lower()
    # Split name (+extras) from the version spec, normalising -/_ in names.
    match = re.match(r"^([a-z0-9_.\-]+)(\[[^\]]+\])?\s*(.*)$", spec)
    name, extras, version = match.groups()
    name = name.replace("-", "_")
    return f"{name}{extras or ''}{version}"


def _requirements_in() -> set[str]:
    specs = set()
    for line in (REPO_ROOT / "requirements.in").read_text(
            encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            specs.add(_normalize(line))
    return specs


def _pyproject() -> set[str]:
    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    return {_normalize(spec) for spec in data["project"]["dependencies"]}


def test_pyproject_mirrors_requirements_in():
    req_in = _requirements_in()
    pyproject = _pyproject()
    assert req_in, "requirements.in parsed empty — parser broke?"
    assert pyproject, "pyproject dependencies parsed empty — parser broke?"
    assert req_in == pyproject, (
        f"pyproject.toml drifted from requirements.in.\n"
        f"Only in requirements.in: {sorted(req_in - pyproject)}\n"
        f"Only in pyproject.toml: {sorted(pyproject - req_in)}\n"
        f"Fix: change requirements.in first, regenerate requirements.txt "
        f"with hashes, then mirror the change in pyproject.toml."
    )
