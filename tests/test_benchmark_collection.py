"""GFI #5: opt-in benchmark collection — consent, redaction, delete,
aggregate-only reporting.

Hard rules pinned here: no storage without consent, no raw text stored,
no per-case data in any --real-dir report, aggregates suppressed below
5 contributions. Nothing in this file invents a real resume: the texts
below are short synthetic fixtures written for the tests.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import benchmark_resume  # noqa: E402
from studybuddy.benchmark_collection import store_contribution  # noqa: E402
from studybuddy.benchmark_redaction import redact_resume_text  # noqa: E402
from tests.conftest import register  # noqa: E402
from unified_app import BenchmarkContribution  # noqa: E402

PII_TEXT = """John Doe
Senior Software Engineer
john.doe@example.com | +91 98765 43210 | linkedin.com/in/johndoe

Address: 12 MG Road, Bengaluru 560001
Date of Birth: 1 Jan 1998
PAN: ABCDE1234F

Experience
Built Python tools for data teams.

Skills
Python, SQL, Git
"""

CLEAN_TEXT = """Summary
Developer with experience building web applications.

Skills
Python, SQL
"""


# ---------------------------------------------------------------- redactor
class TestRedactor:
    def test_email_phone_url_stripped(self):
        red, report = redact_resume_text(PII_TEXT)
        assert "john.doe@example.com" not in red
        assert "98765" not in red
        assert "linkedin.com/in/johndoe" not in red
        assert "[EMAIL_1]" in red and "[PHONE_1]" in red and "[URL_1]" in red
        assert report["counts"]["email"] == 1
        assert report["counts"]["phone"] == 1
        assert report["counts"]["url"] == 1

    def test_name_address_dob_ids_stripped(self):
        red, report = redact_resume_text(PII_TEXT)
        assert "John Doe" not in red and "[NAME]" in red
        assert "12 MG Road" not in red and "560001" not in red
        assert "Address: [ADDRESS]" in red
        assert "1 Jan 1998" not in red and "Date of Birth: [DOB]" in red
        assert "ABCDE1234F" not in red and "[PAN]" in red
        assert report["counts"]["name"] >= 1
        assert report["counts"]["address"] == 1
        assert report["counts"]["dob"] == 1
        assert report["counts"]["pan"] == 1

    def test_name_with_separators_and_title_line_kept(self):
        red, _ = redact_resume_text(
            "Jane Smith | jane@example.com\nSummary\nWorked on APIs.")
        assert "Jane Smith" not in red and "[NAME]" in red
        red2, _ = redact_resume_text(
            "Senior Software Engineer\nBuilt Python tools.")
        assert "Senior Software Engineer" in red2  # a title, not a name

    def test_clean_text_unchanged(self):
        red, report = redact_resume_text(CLEAN_TEXT)
        assert red == CLEAN_TEXT
        assert report == {"counts": {}, "redacted_any": False}

    def test_report_contains_counts_only_never_values(self):
        _red, report = redact_resume_text(PII_TEXT)
        blob = json.dumps(report)
        for secret in ("John Doe", "john.doe@example.com", "98765",
                       "ABCDE1234F", "MG Road"):
            assert secret not in blob


# ------------------------------------------------------------------- store
class TestStore:
    def test_store_refuses_without_consent(self, app):
        with app.app_context():
            assert BenchmarkContribution.query.count() == 0
            with pytest.raises(ValueError):
                store_contribution(1, "redacted text", consent=False)
            assert BenchmarkContribution.query.count() == 0

    def test_store_refuses_empty_text(self, app):
        with app.app_context():
            with pytest.raises(ValueError):
                store_contribution(1, "   ", consent=True)
            assert BenchmarkContribution.query.count() == 0

    def test_store_with_consent_persists_redacted_only(self, app):
        red, _ = redact_resume_text(PII_TEXT)
        with app.app_context():
            row = store_contribution(1, red, consent=True)
            assert row.consent is True
            assert row.redacted_text == red.strip()
            assert "john.doe@example.com" not in row.redacted_text


# -------------------------------------------------------------------- flow
def _count(app):
    with app.app_context():
        return BenchmarkContribution.query.count()


class TestContributeFlow:
    def test_anonymous_contribute_page_redirects(self, client):
        assert client.get("/benchmark/contribute").status_code == 302

    def test_preview_stores_nothing(self, client, app):
        register(client)
        resp = client.post("/benchmark/contribute/preview",
                           data={"resume_text": PII_TEXT})
        assert resp.status_code == 200
        assert b"[EMAIL_1]" in resp.data
        assert b"john.doe@example.com" not in resp.data
        assert _count(app) == 0

    def test_confirm_without_consent_stores_nothing(self, client, app):
        register(client)
        resp = client.post("/benchmark/contribute/confirm",
                           data={"resume_text": PII_TEXT})
        assert resp.status_code == 302
        assert _count(app) == 0

    def test_confirm_with_consent_stores_redacted(self, client, app):
        register(client)
        red, _ = redact_resume_text(PII_TEXT)
        resp = client.post("/benchmark/contribute/confirm",
                           data={"resume_text": red, "consent": "on"})
        assert resp.status_code == 302
        assert _count(app) == 1
        with app.app_context():
            row = BenchmarkContribution.query.first()
            assert row.consent is True
            assert "john.doe@example.com" not in row.redacted_text
            assert "John Doe" not in row.redacted_text

    def test_confirm_re_redacts_retyped_pii(self, client, app):
        # The contributor re-types their email into the editable preview.
        register(client)
        red, _ = redact_resume_text(PII_TEXT)
        sneaky = red.replace("[EMAIL_1]", "john.doe@example.com")
        client.post("/benchmark/contribute/confirm",
                    data={"resume_text": sneaky, "consent": "on"})
        with app.app_context():
            row = BenchmarkContribution.query.first()
            assert "john.doe@example.com" not in row.redacted_text

    def test_delete_own_contribution(self, client, app):
        register(client)
        client.post("/benchmark/contribute/confirm",
                    data={"resume_text": CLEAN_TEXT, "consent": "on"})
        assert _count(app) == 1
        with app.app_context():
            row_id = BenchmarkContribution.query.first().id
        resp = client.post(
            f"/benchmark/contribute/{row_id}/delete")
        assert resp.status_code == 302
        assert _count(app) == 0

    def test_delete_other_users_contribution_404s(self, client, app):
        register(client)  # user A contributes
        client.post("/benchmark/contribute/confirm",
                    data={"resume_text": CLEAN_TEXT, "consent": "on"})
        with app.app_context():
            row_id = BenchmarkContribution.query.first().id
        other = app.test_client()  # fresh client = user B
        register(other, name="Second User", email="second@example.com")
        resp = other.post(f"/benchmark/contribute/{row_id}/delete")
        assert resp.status_code == 404
        assert _count(app) == 1


# ------------------------------------------------------- aggregate reports
def _write_real_dir(tmp_path, n):
    for i in range(n):
        (tmp_path / f"person-{i}-resume.txt").write_text(
            f"Summary\nDeveloper with {i + 1} years of experience.\n\n"
            f"Experience\nBuilt Python tools.\n\nSkills\nPython, SQL, Git\n",
            encoding="utf-8")
    return tmp_path


class TestAggregateOnlyReporting:
    def test_real_dir_reports_aggregates_only(self, tmp_path, capsys):
        d = _write_real_dir(tmp_path, 6)
        assert benchmark_resume.main(["--real-dir", str(d)]) == 0
        out = capsys.readouterr().out
        assert "contributions: 6" in out
        assert "mean:" in out
        assert "score distribution:" in out
        for i in range(6):
            assert f"person-{i}" not in out  # no filenames, no per-case rows
        assert ".txt" not in out

    def test_real_dir_json_has_no_results(self, tmp_path, capsys):
        d = _write_real_dir(tmp_path, 6)
        assert benchmark_resume.main(["--real-dir", str(d), "--json"]) == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "results" not in data
        assert data["aggregate"]["n"] == 6
        assert data["aggregate"]["suppressed"] is False
        assert "person-0" not in out

    def test_real_dir_suppressed_below_five(self, tmp_path, capsys):
        d = _write_real_dir(tmp_path, 2)
        assert benchmark_resume.main(["--real-dir", str(d)]) == 0
        out = capsys.readouterr().out
        assert "contributions: 2" in out
        assert "suppressed" in out
        assert "mean:" not in out  # no aggregate leaks for tiny sets

    def test_aggregate_summary_has_no_per_case_keys(self):
        results = benchmark_resume.run_cases(
            benchmark_resume.SYNTHETIC_CASES[:6])
        summary = benchmark_resume.aggregate_summary(results)
        blob = json.dumps(summary)
        assert "syn-strong" not in blob
        assert "Aarav" not in blob
        assert summary["n"] == 6
