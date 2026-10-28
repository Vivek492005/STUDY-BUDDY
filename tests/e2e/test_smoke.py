"""TEST-07: Playwright end-to-end smoke tests for the critical journeys.

  1. signup -> upload resume -> readiness (ATS) report page
  2. application tracker: add -> move stage
  3. delete account

Runs against a seeded local instance (see conftest.live_server).
Screenshots are saved on failure (see docs/testing/e2e.md).
"""
import os
import tempfile
import uuid

import pytest

pytest.importorskip("playwright")


def _make_resume_pdf_bytes():
    """Minimal valid single-page PDF resume (no network, no fixtures)."""
    lines = [
        "Aarav Sharma",
        "aarav.sharma@example.com | +91-9876543210",
        "SUMMARY",
        "Computer Science undergraduate with strong Python and machine learning skills.",
        "EDUCATION",
        "B.Tech Computer Science, Graphic Era Hill University, 2023 - 2027",
        "EXPERIENCE",
        "Machine Learning Intern, 6 months building Flask APIs and data pipelines.",
        "SKILLS",
        "Python, Java, SQL, Machine Learning, Flask, Git, Data Analysis, HTML, CSS, JavaScript",
        "PROJECTS",
        "Built a resume analyzer web app with Flask and NLP.",
    ]
    content = b"BT /F1 12 Tf 72 740 Td 14 TL\n"
    for line in lines:
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content += b"(" + safe.encode("latin-1", "ignore") + b") Tj T*\n"
    content += b"ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref_pos = len(pdf)
    pdf += b"xref\n0 %d\n" % (len(objects) + 1)
    pdf += b"0000000000 65535 f \n"
    for off in offsets:
        pdf += b"%010d 00000 n \n" % off
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (
        len(objects) + 1, xref_pos)
    return pdf


def _unique_email(prefix="e2e"):
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


def _signup(page, base, name="E2E User", email=None, password="<redacted>"):
    """Drive the real /register + /login forms; returns the email used."""
    email = email or _unique_email()
    page.goto(f"{base}/register")
    page.fill("#name", name)
    page.fill("#email", email)
    page.fill("#password", password)
    page.check('input[name="age_confirm"]')
    page.click('button[type="submit"]')
    # SEC-06 (audit Stage 4): /register no longer auto-logs in — new and
    # existing emails get the same redirect to /login — so log in here.
    page.wait_for_url("**/login**", timeout=15000)
    page.fill("#email", email)
    page.fill("#password", password)
    page.click('button[type="submit"]')
    page.wait_for_url("**/dashboard**", timeout=15000)
    return email


PASSWORD = "<redacted>"


def test_signup_upload_resume_readiness(page, live_server):
    """signup -> /resume upload -> /analyze -> readiness report page."""
    _signup(page, live_server, email=_unique_email("resume"))

    page.goto(f"{live_server}/resume")
    assert "resume" in page.url.lower()

    pdf_path = os.path.join(tempfile.gettempdir(), "e2e-resume.pdf")
    with open(pdf_path, "wb") as f:
        f.write(_make_resume_pdf_bytes())

    page.set_input_files("#fileInput", pdf_path)
    page.wait_for_selector("#analyzeBtn.show", timeout=10000)
    page.click("#analyzeBtn")
    # fetch POST /analyze then JS redirect to /results/<id>
    page.wait_for_url("**/results/**", timeout=120000)

    body = page.inner_text("body")
    lowered = body.lower()
    # readiness/ATS report rendered (dashboard.html shows the analysis)
    assert "readiness" in lowered or "ats" in lowered, \
        "readiness report page does not show an ATS/readiness score"


def test_tracker_add_and_move(page, live_server):
    """tracker: add an application, then move it across stages."""
    _signup(page, live_server, email=_unique_email("tracker"))

    page.goto(f"{live_server}/tracker")
    assert "tracker" in page.url.lower()

    # Add via the page's own session (same cookie jar as the browser).
    added = page.evaluate("""async () => {
        const r = await fetch('/tracker/add', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                company: 'E2E Corp', role: 'SDE Intern', stage: 'wishlist',
            }),
        });
        return {status: r.status, body: await r.json()};
    }""")
    assert added["status"] == 201, added
    app_id = added["body"]["application"]["id"]

    moved = page.evaluate("""async (appId) => {
        const r = await fetch(`/tracker/${appId}/move`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({stage: 'applied'}),
        });
        return {status: r.status, body: await r.json()};
    }""", app_id)
    assert moved["status"] == 200, moved
    assert moved["body"]["application"]["stage"] == "applied"

    # Board reflects the move.
    page.reload()
    assert "E2E Corp" in page.inner_text("body")


def test_delete_account(page, live_server):
    """delete account: password re-auth + DELETE confirmation -> logged out."""
    _signup(page, live_server, email=_unique_email("delacct"))

    page.goto(f"{live_server}/account/delete")
    page.fill('input[name="password"]', PASSWORD)
    page.fill("#confirm", "DELETE")
    # NOTE: 'button[type="submit"]' is ambiguous here — the nav logout
    # form's button comes first in DOM order and was clicked instead
    # (logging the user out, then looping on /login via the remember
    # cookie). Click the delete button by its text.
    page.click('button:has-text("Permanently Delete My Account")')
    # After deletion the user is logged out and sent to the landing page.
    page.wait_for_url(live_server + "/", timeout=15000)
    assert "login" in page.inner_text("body").lower() or \
        "sign" in page.inner_text("body").lower()

    # Deleted users can no longer reach protected pages.
    page.goto(f"{live_server}/tracker")
    assert "login" in page.url.lower(), \
        "deleted user can still reach a protected page"
