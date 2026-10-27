"""Dataset freshness (Phase 0 foundations).

Answers "how old is this curated data, and should the student trust it?"
for the three curated datasets the Career section serves. Pure reads, never
raises: unreadable data yields last_updated=None and stale=False with an
explanatory note, never an exception.
"""

from __future__ import annotations

import json
import os
from datetime import date
from typing import Any

_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# Days after which each dataset counts as stale.
_STALE_AFTER_DAYS = {"salary": 120, "opportunities": 30, "jobs": 90}
_LABELS = {"salary": "Salary", "opportunities": "Opportunity", "jobs": "Jobs"}


def _read_json_meta(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            meta = data.get("meta")
            return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def _parse_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def freshness_status(dataset: str, today: date | None = None) -> dict[str, Any]:
    """Freshness for one dataset: salary | opportunities | jobs."""
    today = today or date.today()
    label = _LABELS.get(dataset, dataset)
    last_updated: date | None = None
    note: str | None = None

    if dataset == "salary":
        last_updated = _parse_date(
            _read_json_meta(os.path.join(_DATA_DIR, "salary_ranges.json"))
            .get("last_updated"))
    elif dataset == "opportunities":
        last_updated = _parse_date(
            _read_json_meta(os.path.join(_DATA_DIR, "opportunities.json"))
            .get("last_updated"))
    elif dataset == "jobs":
        csv_path = os.path.join(_DATA_DIR, "jobs.csv")
        try:
            mtime = os.path.getmtime(csv_path)
            last_updated = date.fromtimestamp(mtime)
        except OSError:
            note = "No date available for jobs data"
    else:
        note = f"Unknown dataset: {dataset}"

    window = _STALE_AFTER_DAYS.get(dataset, 90)
    stale = False
    if last_updated is not None:
        stale = (today - last_updated).days > window
        if stale:
            message = (f"{label} data updated "
                       f"{last_updated.isoformat()} — may be out of date")
        else:
            message = f"Updated {last_updated.isoformat()}"
    else:
        message = note or f"{label} data has no recorded update date"

    return {
        "dataset": dataset,
        "last_updated": last_updated.isoformat() if last_updated else None,
        "stale": stale,
        "message": message,
    }


def all_freshness(today: date | None = None) -> dict[str, dict[str, Any]]:
    """Freshness for all three datasets, keyed by dataset name."""
    return {name: freshness_status(name, today=today)
            for name in ("salary", "opportunities", "jobs")}
