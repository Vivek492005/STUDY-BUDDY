"""Regression tests for the weekly data-refresh scripts (PR #60 review).

The 2026-11-16 refresh broke two datasets:
- data/opportunities.json was written as a bare list, but
  studybuddy/opportunities.py requires the dict envelope
  (meta + "opportunities") — merging it would have broken /opportunities.
- data/taxonomy/skills_taxonomy.json was rebuilt from a drifted CSV:
  library names (django, flask, ...) landed as Python aliases
  (duplicate-alias failures) and the JavaScript ES-edition aliases
  were dropped (they lived only in the JSON, not the CSV/ENRICH).
These tests pin the builders + the validator so neither recurs.
"""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import build_opportunities  # noqa: E402
import build_skills_taxonomy  # noqa: E402
import validate_data  # noqa: E402


def _entry(oid, deadline="2027-06-01"):
    return {
        "curated": True,
        "deadline": deadline,
        "eligibility": "Students",
        "id": oid,
        "link": f"https://example.com/{oid}",
        "organization": "Example Org",
        "source": "curated",
        "status": "open",
        "tags": ["exam"],
        "title": f"Example {oid}",
        "type": "exam",
    }


def _envelope(items, last_updated="2026-11-16"):
    return {
        "meta": {
            "last_refresh": last_updated,
            "last_updated": last_updated,
            "notice": build_opportunities.META_NOTICE,
            "schema_doc": build_opportunities.META_SCHEMA_DOC,
        },
        "opportunities": items,
    }


@pytest.fixture
def scripts_tmp(tmp_path, monkeypatch):
    """Point both scripts at a throwaway data dir."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(
        build_opportunities, "OPPORTUNITIES_JSON", data_dir / "opportunities.json"
    )
    sources = tmp_path / "sources.md"
    sources.write_text("## Example\nTerms: example.\nhttps://example.com\n")
    monkeypatch.setattr(build_opportunities, "SOURCES_MD", sources)
    monkeypatch.setattr(validate_data, "DATA_DIR", str(data_dir))
    return data_dir


class TestBuildOpportunitiesWrite:
    def test_write_preserves_dict_envelope(self, scripts_tmp):
        path = scripts_tmp / "opportunities.json"
        path.write_text(json.dumps(_envelope([_entry("a"), _entry("b")])))
        build_opportunities.main(["--prune", "--write"])
        data = json.loads(path.read_text())
        assert isinstance(data, dict)
        assert data["meta"]["last_updated"] == date.today().isoformat()
        assert data["meta"]["notice"] == build_opportunities.META_NOTICE
        assert len(data["opportunities"]) == 2

    def test_write_repairs_bare_list_input(self, scripts_tmp):
        path = scripts_tmp / "opportunities.json"
        path.write_text(json.dumps([_entry("a"), _entry("b")]))
        build_opportunities.main(["--prune", "--write"])
        data = json.loads(path.read_text())
        assert isinstance(data, dict)
        assert data["meta"]["last_updated"] == date.today().isoformat()
        assert data["meta"]["schema_doc"] == build_opportunities.META_SCHEMA_DOC
        assert len(data["opportunities"]) == 2

    def test_write_prunes_expired(self, scripts_tmp):
        path = scripts_tmp / "opportunities.json"
        path.write_text(
            json.dumps(_envelope([_entry("fresh"), _entry("old", "2020-01-01")]))
        )
        build_opportunities.main(["--prune", "--write"])
        data = json.loads(path.read_text())
        ids = [o["id"] for o in data["opportunities"]]
        assert ids == ["fresh"]


class TestValidateDataEnvelope:
    def test_rejects_bare_list_opportunities(self, scripts_tmp, monkeypatch):
        (scripts_tmp / "opportunities.json").write_text(json.dumps([_entry("a")]))
        monkeypatch.setattr(sys, "argv", ["validate_data.py"])
        with pytest.raises(SystemExit) as exc:
            validate_data.main()
        assert exc.value.code == 1

    def test_accepts_dict_envelope(self, scripts_tmp, monkeypatch, capsys):
        (scripts_tmp / "opportunities.json").write_text(
            json.dumps(_envelope([_entry("a")]))
        )
        monkeypatch.setattr(sys, "argv", ["validate_data.py"])
        validate_data.main()  # no SystemExit: 0 errors in non-strict mode
        assert "0 errors" in capsys.readouterr().out


class TestBuildSkillsTaxonomy:
    def _rebuild(self, tmp_path, monkeypatch, csv_text=None):
        """Run the real builder against a (possibly modified) CSV copy."""
        src = Path(build_skills_taxonomy.CSV_PATH).read_text(encoding="utf-8")
        csv_path = tmp_path / "skills_db.csv"
        csv_path.write_text(
            csv_text if csv_text is not None else src, encoding="utf-8"
        )
        out_path = tmp_path / "skills_taxonomy.json"
        monkeypatch.setattr(build_skills_taxonomy, "CSV_PATH", str(csv_path))
        monkeypatch.setattr(build_skills_taxonomy, "OUT_PATH", str(out_path))
        build_skills_taxonomy.build()
        return json.loads(out_path.read_text(encoding="utf-8"))

    def test_rebuild_has_no_new_duplicate_aliases(self, tmp_path, monkeypatch):
        tax = self._rebuild(tmp_path, monkeypatch)
        seen = {}
        for s in tax["skills"]:
            for a in s["aliases"]:
                key = a.lower()
                if key in build_skills_taxonomy.KNOWN_DUPES:
                    continue
                assert key not in seen, f"duplicate alias '{a}'"
                seen[key] = s["name"]

    def test_rebuild_keeps_pinned_aliases(self, tmp_path, monkeypatch):
        tax = self._rebuild(tmp_path, monkeypatch)
        by_name = {s["name"]: s for s in tax["skills"]}
        js = by_name["JavaScript"]["aliases"]
        for alias in ("es2016", "es2024", "vanilla javascript", "vanilla js"):
            assert alias in js
        py = by_name["Python"]["aliases"]
        for lib in ("django", "flask", "fastapi", "numpy", "pandas", "pytest",
                    "sqlalchemy", "celery", "pydantic", "poetry"):
            assert lib not in py

    def test_builder_fails_on_duplicate_aliases(self, tmp_path, monkeypatch):
        csv_text = ("skill,category,aliases\n"
                    "Alpha,Programming,shared\n"
                    "Beta,Programming,shared\n")
        csv_path = tmp_path / "skills_db.csv"
        csv_path.write_text(csv_text, encoding="utf-8")
        out_path = tmp_path / "skills_taxonomy.json"
        monkeypatch.setattr(build_skills_taxonomy, "CSV_PATH", str(csv_path))
        monkeypatch.setattr(build_skills_taxonomy, "OUT_PATH", str(out_path))
        with pytest.raises(SystemExit) as exc:
            build_skills_taxonomy.build()
        assert exc.value.code == 1
