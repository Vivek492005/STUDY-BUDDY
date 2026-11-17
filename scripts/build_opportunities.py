"""CI-buildable opportunities pipeline stub (DATA-03).

Validates docs/data/sources.md entries, de-duplicates opportunities,
expires past deadlines, and tags India relevance. Full ingestion from
legal sources (official APIs/RSS, partner feeds, user-submitted PRs) is
future work; this pipeline guarantees the committed dataset is always
clean, whatever its origin.

Exit codes: 0 = clean, 1 = validation errors, 2 = expired entries found
(and --prune was not given).

Wiring to WS4's data-refresh workflow: the refresh job should invoke
`python scripts/build_opportunities.py --prune --write` AFTER fetching and
BEFORE committing, so every dataset commit passes CI validation.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES_MD = ROOT / "docs" / "data" / "sources.md"
OPPORTUNITIES_JSON = ROOT / "data" / "opportunities.json"

# The dict envelope studybuddy/opportunities.py requires (OPPS_SCHEMA):
# _load_from_disk() does data.get("meta") / data.get("opportunities") and
# raises on a bare list. The --write path must preserve this envelope —
# the 2026-11-16 weekly refresh wrote the bare list and broke
# /opportunities (PR #60 review).
META_NOTICE = (
    "Curated starter set for student guidance. Deadlines are indicative — "
    "always verify dates and eligibility on the official website before applying."
)
META_SCHEMA_DOC = (
    "See opportunities.py OPPS_SCHEMA for the documented JSON schema used "
    "to extend this dataset."
)

TAGS = {"internship", "hackathon", "scholarship", "exam", "fellowship", "job"}
INDIA_MARKERS = re.compile(r"india|indian|inr|₹|delhi|mumbai|bengaluru|bangalore|hyderabad|chennai|pune|kolkata|ahmedabad|remote", re.I)


def validate_sources_md(path: Path | None = None) -> list[str]:
    """Each source entry must name its terms/license (DATA-03 legal-sources rule)."""
    path = path or SOURCES_MD
    errors: list[str] = []
    if not path.is_file():
        return [f"sources file missing: {path}"]
    text = path.read_text(encoding="utf-8")
    entries = re.findall(r"^#{2,3}\s+(.+)$", text, re.M)
    if not entries:
        errors.append("no source entries found (expected ## headings)")
    lowered = text.lower()
    for entry in entries:
        # crude per-entry check: the entry's section must mention terms/license
        section = text.split(entry, 1)[-1].split("\n## ", 1)[0].lower()
        if "terms" not in section and "license" not in section and "licence" not in section:
            errors.append(f"source '{entry.strip()}': no terms/license recorded")
    if "http" not in lowered:
        errors.append("sources.md contains no URLs at all")
    return errors


def load_opportunities(path: Path | None = None) -> list[dict]:
    # NB: resolved at call time (not as a default arg) so tests can
    # monkeypatch OPPORTUNITIES_JSON.
    path = path or OPPORTUNITIES_JSON
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("opportunities", [])


def _envelope(items: list[dict], today: date | None = None) -> dict:
    """Wrap pruned items in the dict envelope the app loader requires.

    Preserves the current file's meta (if it is a dict) and stamps
    last_refresh/last_updated to today; synthesizes the standard meta
    when the current file is a bare list (legacy broken state).
    """
    today = today or date.today()
    stamp = today.isoformat()
    meta: dict = {
        "last_refresh": stamp,
        "last_updated": stamp,
        "notice": META_NOTICE,
        "schema_doc": META_SCHEMA_DOC,
    }
    try:
        current = json.loads(OPPORTUNITIES_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = None
    if isinstance(current, dict) and isinstance(current.get("meta"), dict):
        meta = {**current["meta"], **meta}
    return {"meta": meta, "opportunities": items}


def dedupe(items: list[dict]) -> tuple[list[dict], int]:
    seen: set[str] = set()
    out: list[dict] = []
    for item in items:
        key = (str(item.get("title", "")).strip().lower(),
               str(item.get("url", "")).strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out, len(items) - len(out)


def find_expired(items: list[dict], *, today: date | None = None) -> list[dict]:
    today = today or date.today()
    expired = []
    for item in items:
        dl = (item.get("deadline") or "").strip()
        if not dl:
            continue
        try:
            if date.fromisoformat(dl) < today:
                expired.append(item)
        except ValueError:
            pass  # malformed deadlines are a validation concern elsewhere
    return expired


def india_relevance(item: dict) -> str:
    """Tag: 'india' | 'remote-ok' | 'global' | 'unknown'."""
    text = " ".join(
        str(item.get(k, "")) for k in ("title", "description", "eligibility", "location")
    )
    if INDIA_MARKERS.search(text):
        return "india"
    if re.search(r"remote", text, re.I):
        return "remote-ok"
    if any(k in text.lower() for k in ("worldwide", "global", "anywhere")):
        return "global"
    return "unknown"


def normalize_tags(item: dict) -> list[str]:
    tags = {str(t).lower() for t in item.get("tags", []) if t}
    return sorted(tags & TAGS) or ["unknown"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate/prune the opportunities dataset.")
    ap.add_argument("--prune", action="store_true", help="drop expired entries")
    ap.add_argument("--write", action="store_true", help="write cleaned dataset back")
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    args = ap.parse_args(argv)

    errors = validate_sources_md()
    items = load_opportunities()
    items, n_dupes = dedupe(items)
    expired = find_expired(items)

    for item in items:
        item.setdefault("india_relevance", india_relevance(item))
        item["tags"] = normalize_tags(item)

    if args.prune:
        expired_ids = {id(e) for e in expired}
        items = [i for i in items if id(i) not in expired_ids]

    if args.write:
        envelope = _envelope(items)
        OPPORTUNITIES_JSON.write_text(
            json.dumps(envelope, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    report = {
        "sources_errors": errors,
        "total": len(items),
        "duplicates_removed": n_dupes,
        "expired": len(expired),
        "pruned": args.prune,
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"opportunities: {report['total']} | dupes removed: {n_dupes} | "
              f"expired: {len(expired)} | sources.md errors: {len(errors)}")
        for e in errors:
            print(f"  ! {e}")

    if errors:
        return 1
    if expired and not args.prune:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
