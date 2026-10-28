"""Audit Stage 4 (PERF-01): upload caps aligned + documented in the UI.

The cap stack, locked here so the three layers cannot drift silently:
- Web ceiling: Flask MAX_CONTENT_LENGTH = 16 MB (rejects larger bodies
  before any handler runs).
- Parsing layer: resumes 5 MB (stricter than the ceiling, so it binds
  on web too), papers 30 MB (binds only for direct/library calls —
  web papers hit the 16 MB ceiling first).
- PDF-QA layer: 25 MB (direct calls; web uploads hit the 16 MB ceiling).
- UI: the resume form states 5 MB, the research form states 16 MB.
"""

import pytest

from studybuddy.parsing import (
    MAX_PAPER_BYTES,
    MAX_RESUME_BYTES,
    ParseError,
    validate_upload,
)
from studybuddy.pdf_qa import MAX_UPLOAD_BYTES


def test_web_ceiling_is_16mb(app):
    assert app.config["MAX_CONTENT_LENGTH"] == 16 * 1024 * 1024


def test_parsing_and_pdfqa_caps():
    assert MAX_RESUME_BYTES == 5 * 1024 * 1024
    assert MAX_PAPER_BYTES == 30 * 1024 * 1024
    assert MAX_UPLOAD_BYTES == 25 * 1024 * 1024


def test_resume_over_5mb_rejected_by_parsing():
    # Size is checked before content type, so a blob of zeros suffices.
    with pytest.raises(ParseError, match="too large"):
        validate_upload(b"\x00" * (5 * 1024 * 1024 + 1),
                        filename="big.pdf", kind="resume")


def test_paper_over_30mb_rejected_by_parsing():
    with pytest.raises(ParseError, match="too large"):
        validate_upload(b"\x00" * (30 * 1024 * 1024 + 1),
                        filename="big.pdf", kind="paper")


def test_resume_form_states_5mb_limit(client):
    resp = client.get("/resume")
    assert resp.status_code == 200
    assert b"Max 5 MB per file." in resp.data


def test_research_form_states_16mb_limit(client):
    resp = client.get("/research")
    assert resp.status_code == 200
    assert b"Max 16 MB per PDF." in resp.data
