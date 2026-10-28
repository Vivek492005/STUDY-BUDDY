#!/usr/bin/env python3
"""Retention enforcement (audit fix, Stage 2, 2026-10-28).

Deletes inventory rows older than 90 days (see
studybuddy/retention.py for scope + the "unless you save them"
mapping). DRY-RUN BY DEFAULT: prints per-table counts and deletes
nothing. Pass --apply to actually delete.

Scheduling (maintainer step, not code): create a Render Cron Job
running weekly:
    python scripts/purge_stale_data.py --apply
against the production service, then check its logs for the counts.
Until that job exists, enforcement does not run — see the BLOCKED list
in hidden_work/audit-fixes-progress-20261028.md.

Usage:
    python scripts/purge_stale_data.py [--days 90] [--apply]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=90,
                        help="purge rows older than this many days")
    parser.add_argument("--apply", action="store_true",
                        help="actually delete (default is dry-run)")
    args = parser.parse_args()

    from unified_app import create_app, db
    from studybuddy.retention import purge_stale_analyses

    app = create_app()
    with app.app_context():
        counts = purge_stale_analyses(db, days=args.days,
                                      dry_run=not args.apply)
    mode = "APPLIED" if args.apply else "DRY-RUN (nothing deleted)"
    print(f"{mode}: retention purge, older than {args.days} days")
    print(json.dumps(counts, indent=2, sort_keys=True))
    print(f"total: {sum(counts.values())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
