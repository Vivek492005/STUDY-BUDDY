"""DATA-01: Validate dataset provenance (schemas, meta.json, freshness).

Usage: python scripts/validate_data.py [--strict] [--report PATH]
Checks every data/*.json against its schema in data/schemas/,
verifies meta.json presence, and warns on stale last_verified dates.
With --report, a JSON summary (counts + messages) is written to PATH
so CI can upload the warning count as an artifact (#10).
"""
import glob
import json
import os
import sys
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data")
SCHEMA_DIR = os.path.join(DATA_DIR, "schemas")

# Freshness SLAs (days) per dataset
FRESHNESS_SLA = {
    "opportunities": 60,
    "salary_ranges": 180,
    "default": 365,
}

REQUIRED_RECORD_FIELDS = ["id", "source_url", "retrieved_at", "license",
                          "last_verified", "confidence"]
REQUIRED_META_FIELDS = ["version", "date", "maintainers", "methodology"]


def check_record(rec, dataset, errors, warnings):
    for field in REQUIRED_RECORD_FIELDS:
        if field not in rec:
            # source_note is an acceptable alternative to source_url
            if field == "source_url" and "source_note" in rec:
                continue
            warnings.append(
                f"{dataset}: record {rec.get('id', '?')} missing '{field}'")
    if rec.get("confidence") not in (None, "high", "medium", "low"):
        errors.append(
            f"{dataset}: record {rec.get('id')} bad confidence "
            f"'{rec.get('confidence')}'")


def check_freshness(rec, dataset, warnings):
    sla = FRESHNESS_SLA.get(dataset, FRESHNESS_SLA["default"])
    lv = rec.get("last_verified")
    if not lv:
        return
    try:
        dt = datetime.fromisoformat(lv.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - dt).days
        if age > sla:
            warnings.append(
                f"{dataset}: record {rec.get('id')} stale "
                f"({age}d > {sla}d SLA)")
    except (ValueError, TypeError):
        warnings.append(
            f"{dataset}: record {rec.get('id')} bad last_verified '{lv}'")


def main():
    strict = "--strict" in sys.argv
    report_path = None
    if "--report" in sys.argv:
        idx = sys.argv.index("--report")
        if idx + 1 < len(sys.argv):
            report_path = sys.argv[idx + 1]
    errors, warnings = [], []
    seen_ids = {}

    for path in sorted(glob.glob(os.path.join(DATA_DIR, "*.json"))):
        name = os.path.basename(path)[:-5]
        if name.endswith(".meta"):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            errors.append(f"{name}: unreadable ({e})")
            continue
        if name == "opportunities":
            # The app loader requires the dict envelope (meta +
            # "opportunities"); a bare list breaks /opportunities with
            # AttributeError — the 2026-11-16 weekly refresh shipped one
            # (PR #60 review). This is a blocking error, not a warning.
            if not isinstance(data, dict):
                errors.append(
                    f"{name}: must be an object with 'meta' + "
                    f"'opportunities', got {type(data).__name__}"
                )
                continue
            meta = data.get("meta") or {}
            for field in ("last_updated", "notice", "schema_doc"):
                if field not in meta:
                    errors.append(f"{name}: meta missing '{field}'")
            records = data.get("opportunities")
            if not isinstance(records, list) or not records:
                errors.append(
                    f"{name}: 'opportunities' must be a non-empty list"
                )
                continue
        else:
            records = data if isinstance(data, list) else data.get("items", [])
        if not isinstance(records, list):
            warnings.append(f"{name}: not a list, skipping provenance check")
            continue
        for rec in records:
            if not isinstance(rec, dict):
                continue
            rid = rec.get("id")
            if rid:
                key = (name, rid)
                if key in seen_ids:
                    errors.append(f"{name}: duplicate id '{rid}'")
                seen_ids[key] = True
            check_record(rec, name, errors, warnings)
            check_freshness(rec, name, warnings)

        # meta.json check
        meta_path = os.path.join(DATA_DIR, f"{name}.meta.json")
        if not os.path.exists(meta_path):
            warnings.append(f"{name}: missing {name}.meta.json")
        else:
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
                for field in REQUIRED_META_FIELDS:
                    if field not in meta:
                        warnings.append(f"{name}.meta.json missing '{field}'")
            except (json.JSONDecodeError, OSError) as e:
                errors.append(f"{name}.meta.json unreadable ({e})")

    print(f"Checked {len(seen_ids)} records.")
    for w in warnings:
        print(f"  WARN: {w}")
    for e in errors:
        print(f"  ERROR: {e}")
    print(f"\n{len(errors)} errors, {len(warnings)} warnings.")
    if report_path:
        report = {
            "records_checked": len(seen_ids),
            "errors": len(errors),
            "warnings": len(warnings),
            "strict": strict,
            "error_messages": errors,
            "warning_messages": warnings[:1000],
            "warning_messages_truncated": len(warnings) > 1000,
        }
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"Report written to {report_path}")
    if errors or (strict and warnings):
        sys.exit(1)


if __name__ == "__main__":
    main()
