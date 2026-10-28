#!/usr/bin/env python3
"""Build-time version stamp (audit OPS-04).

``/version`` used to report a hardcoded ``"1.0.0"`` and a ``git_sha``
probed from ``.git`` at runtime — but Render's runtime has no ``.git``,
so production always reported sha ``"unknown"`` and the literal version
no matter which tag was deployed.

This script runs at BUILD time (when ``.git`` exists) and writes
``studybuddy/_version.py`` with the real short SHA and the nearest
release tag (``v`` prefix stripped; ``"dev"`` when no tag is
reachable). ``/version`` prefers these values and falls back to the
old behaviour when the stamp is absent (local dev, Docker builds —
the Dockerfile context excludes ``.git`` by design, so Docker images
keep reporting the fallback; that limitation is documented here, not
hidden).

The script NEVER fails the build: any git error writes the
``unknown`` / ``dev`` fallbacks and exits 0. Stdlib only. The generated
file is git-ignored (see ``.gitignore``) and must never be committed
— a committed stamp would go stale on the next release.

Wiring: ``render.yaml`` buildCommand runs this before ``pip install``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT = REPO_ROOT / "studybuddy" / "_version.py"

HEADER = (
    "# GENERATED at build time by scripts/write_version.py — do not edit\n"
    "# by hand and do not commit (git-ignored). Regenerate on every build.\n"
)


def _git(*args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=15,
            cwd=REPO_ROOT,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001 — stamp must never fail the build
        return ""


def main() -> int:
    sha = _git("rev-parse", "--short", "HEAD") or "unknown"
    tag = _git("describe", "--tags", "--abbrev=0")
    version = tag[1:] if tag.startswith("v") else (tag or "dev")
    OUT.write_text(
        f"{HEADER}GIT_SHA = {sha!r}\nAPP_VERSION = {version!r}\n",
        encoding="utf-8",
    )
    print(f"version stamp: {version} @ {sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
