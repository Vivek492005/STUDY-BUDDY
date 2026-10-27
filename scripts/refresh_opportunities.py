#!/usr/bin/env python3
"""Refresh the opportunity radar dataset (Phase 3).

Marks every entry open/closed deterministically from its deadline, reports
counts, and stamps meta.last_updated / meta.last_refresh. It NEVER deletes
entries, NEVER invents data, and NEVER guesses a deadline: entries without a
deadline stay open (rolling) and are counted separately as no_deadline.

Usage:
    python scripts/refresh_opportunities.py [--check-only] [--data PATH]

Exit codes: 0 = ok, 2 = corrupt/unreadable dataset (nothing written).
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import date

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA = os.path.join(REPO_ROOT, "data", "opportunities.json")


def _parse_deadline(value, opp_id):
    if value is None:
        return None
    try:
        year, month, day = (int(p) for p in str(value).split("-"))
        return date(year, month, day)
    except (ValueError, TypeError):
        raise ValueError(f"Opportunity {opp_id!r} has an invalid deadline.")


def refresh(path, check_only):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"ERROR: dataset unreadable: {exc}", file=sys.stderr)
        return 2
    meta = data.get("meta")
    opps = data.get("opportunities")
    if not isinstance(meta, dict) or not isinstance(opps, list):
        print("ERROR: dataset missing meta or opportunities list.",
              file=sys.stderr)
        return 2
    today = date.today()
    total = len(opps)
    open_count = closed_count = newly_closed = no_deadline = 0
    for o in opps:
        if not isinstance(o, dict) or "id" not in o:
            print("ERROR: dataset contains an entry without an id.",
                  file=sys.stderr)
            return 2
        try:
            dl = _parse_deadline(o.get("deadline"), o["id"])
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        if dl is None:
            no_deadline += 1
            o["status"] = "open"
            open_count += 1
        elif dl < today:
            if o.get("status") != "closed":
                newly_closed += 1
            o["status"] = "closed"
            closed_count += 1
        else:
            o["status"] = "open"
            open_count += 1
    report = {
        "total": total,
        "open": open_count,
        "closed": closed_count,
        "newly_closed": newly_closed,
        "no_deadline": no_deadline,
        "last_updated": meta.get("last_updated"),
        "last_refresh": meta.get("last_refresh"),
    }
    if check_only:
        print("CHECK-ONLY (no changes written):")
    else:
        meta["last_updated"] = today.isoformat()
        meta["last_refresh"] = today.isoformat()
        report["last_updated"] = today.isoformat()
        report["last_refresh"] = today.isoformat()
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(path), prefix=".opps-", suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True,
                          ensure_ascii=False)
                f.write("\n")
            os.replace(tmp_path, path)
        except OSError as exc:
            print(f"ERROR: could not write dataset: {exc}", file=sys.stderr)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return 2
        print(f"Wrote {path}")
    for key in ("total", "open", "closed", "newly_closed", "no_deadline",
                "last_updated", "last_refresh"):
        print(f"{key}: {report[key]}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true",
                        help="Report only; do not write the file.")
    parser.add_argument("--data", default=DEFAULT_DATA,
                        help="Path to opportunities.json.")
    args = parser.parse_args()
    return refresh(args.data, args.check_only)


if __name__ == "__main__":
    sys.exit(main())
