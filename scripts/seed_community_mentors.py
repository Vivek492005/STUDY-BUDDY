#!/usr/bin/env python3
"""Real mentor supply report (Phase 5) — READ-ONLY, never fabricates people.

STUDY-BUDDY's real mentors come ONLY from the application pipeline:

  1. A student applies at /mentors/apply
     (POST /mentors/api/apply — name, headline, path, bio, ...).
  2. An admin approves via POST
     /mentors/api/admin/applications/<application_id>/approve,
     which creates the real Mentor row (verified=True, is_seed=False,
     linked to the applicant's account).

The 14 seed mentors are ILLUSTRATIVE profiles (is_seed=True,
verified=False, never bookable). They exist so the directory is not
empty on first load — they are not real mentors and must never be
presented as such.

This script reports the current state so a community lead can see how
far real supply has grown, and prints the exact approve calls for any
pending applications. It inserts NOTHING.

Usage (from the repo root, with the app venv):

    .venv/bin/python scripts/seed_community_mentors.py

    .venv/bin/python scripts/seed_community_mentors.py --pending
        # also list pending applications with approve instructions
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--pending", action="store_true",
                        help="List pending applications + approve calls.")
    args = parser.parse_args()

    from unified_app import Mentor, MentorApplication, create_app

    app = create_app()
    with app.app_context():
        real = Mentor.query.filter_by(is_seed=False).count()
        seeds = Mentor.query.filter_by(is_seed=True).count()
        verified_real = Mentor.query.filter_by(
            is_seed=False, verified=True).count()
        pending = (MentorApplication.query.filter_by(status="pending")
                   .order_by(MentorApplication.created_at.asc()).all())
        approved = MentorApplication.query.filter_by(
            status="approved").count()

    print("STUDY-BUDDY mentor supply (real people only count as supply)")
    print("------------------------------------------------------------")
    print(f"Illustrative seed profiles : {seeds} (never bookable, never verified)")
    print(f"Real mentor rows           : {real} ({verified_real} verified)")
    print(f"Applications approved      : {approved}")
    print(f"Applications pending       : {len(pending)}")
    print()
    if real == 0:
        print("No real mentors yet. Real supply grows ONLY through the")
        print("application + admin-approval pipeline above — there is no")
        print("bulk import of invented mentors, by design.")
    if args.pending:
        print()
        for row in pending:
            print(f"Pending #{row.id}: {row.name} — {row.headline} "
                  f"({row.path})")
            print("  Approve with: POST "
                  f"/mentors/api/admin/applications/{row.id}/approve")
            print("  (requires an admin account; CSRF via X-CSRFToken)")
    print()
    print("NOTE: this script is read-only. It never inserts mentor rows.")


if __name__ == "__main__":
    main()
