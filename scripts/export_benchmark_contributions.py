#!/usr/bin/env python3
"""Export consented benchmark contributions for aggregate-only scoring.

Reads ONLY BenchmarkContribution rows with consent=True, re-redacts each
text as defense-in-depth, and writes one .txt file per contribution
into --out. Filenames are contribution ids only — no usernames, emails,
or any contributor-identifying information ever touches the output.

The exported directory is the input to:
    python scripts/benchmark_resume.py --real-dir DIR
which reports AGGREGATE BANDS ONLY (suppressed below 5 contributions).

Usage (run with the app's environment, e.g. the production DATABASE_URL):
    python scripts/export_benchmark_contributions.py --out /tmp/bench-real

Refusals: a row whose re-redacted text still matches an email/phone/URL
pattern is SKIPPED with a warning (this should never happen — the
storage path already redacts — but the export never ships suspect text).
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}")
_URL_RE = re.compile(r"https?://[^\s)>\"]+|(?:www\.|linkedin\.com/|github\.com/)[^\s)>\"]*")


def _still_has_pii(text: str) -> bool:
    return bool(_EMAIL_RE.search(text) or _PHONE_RE.search(text)
                or _URL_RE.search(text))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True,
                        help="Directory to write one .txt per contribution")
    args = parser.parse_args(argv)

    from unified_app import BenchmarkContribution, create_app
    from studybuddy.benchmark_redaction import redact_resume_text

    app = create_app()
    os.makedirs(args.out, exist_ok=True)
    exported = skipped = 0
    with app.app_context():
        rows = (BenchmarkContribution.query
                .filter_by(consent=True)
                .order_by(BenchmarkContribution.id).all())
        for row in rows:
            redacted, _report = redact_resume_text(row.redacted_text or "")
            if _still_has_pii(redacted):
                print(f"SKIP id={row.id}: re-redacted text still matches "
                      f"a PII pattern", file=sys.stderr)
                skipped += 1
                continue
            path = os.path.join(args.out, f"{row.id}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(redacted)
            exported += 1
    print(f"exported={exported} skipped={skipped} dir={args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
