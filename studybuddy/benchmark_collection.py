"""GFI #5: opt-in benchmark resume collection.

Real resumes calibrate the ATS benchmark harness
(scripts/benchmark_resume.py), but they may only come from real users
who opt in — never fabricated, never scraped, never seeded. This module
is the collection half of that guarantee:

  1. Explicit consent: the contributor reads a plain-language
     explanation of what is stored and why, then checks a consent box.
  2. PII redaction preview: the contributor sees exactly what will be
     stored (studybuddy/benchmark_redaction.redact_resume_text) in an
     EDITABLE textarea, so anything the auto-redactor missed can be
     struck before confirming.
  3. Delete-anytime: a contributor's row is hard-deleted on request,
     so future aggregate reports cannot include it.
  4. Zero PII without consent: `store_contribution` refuses to persist
     anything unless consent is True, and the raw pasted text is never
     stored, logged, or returned anywhere.

Flow (all routes login-gated, form posts CSRF-protected):
  GET  /benchmark/contribute                 form + own contributions
  POST /benchmark/contribute/preview         redacted preview, NO storage
  POST /benchmark/contribute/confirm         consent + re-redact + store
  POST /benchmark/contribute/<id>/delete     owner-only hard delete

Hard rule (monolith-split pattern, same as study_library.py): this
module MUST NOT import unified_app at top level. Models come from the
lazy _models() import; db comes from app.extensions.

The stored text is redacted TWICE by construction: once for the preview
the user sees, and again on confirm (so text the user re-typed into the
preview is still redacted before storage). The preview step itself
stores nothing — a preview without confirm leaves no trace.
"""

from __future__ import annotations

import logging

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from studybuddy.benchmark_redaction import redact_resume_text

log = logging.getLogger("acra")

benchmark_bp = Blueprint("benchmark_collection", __name__)

CONSENT_VERSION = "v1"
MAX_RESUME_CHARS = 100_000


def _models():
    """Lazy import of unified_app models (avoids circular import)."""
    from unified_app import BenchmarkContribution

    return BenchmarkContribution


def store_contribution(user_id: int, redacted_text: str, consent: bool,
                       consent_version: str = CONSENT_VERSION):
    """Persist one redacted contribution. Refuses without consent.

    Raises ValueError (and stores nothing) when consent is falsy or
    the text is empty. Callers must pass ALREADY-REDACTED text; the
    confirm view re-redacts before calling, so this is the second gate,
    not the only one.
    """
    if not consent:
        raise ValueError("Consent is required to store a benchmark contribution.")
    if not redacted_text or not redacted_text.strip():
        raise ValueError("Cannot store an empty contribution.")
    BenchmarkContribution = _models()
    row = BenchmarkContribution(
        user_id=user_id,
        redacted_text=redacted_text.strip(),
        consent=True,
        consent_version=consent_version,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _own_contributions():
    BenchmarkContribution = _models()
    return (BenchmarkContribution.query
            .filter_by(user_id=current_user.id)
            .order_by(BenchmarkContribution.created_at.desc()).all())


def _render_contribute(preview_text=None, preview_report=None):
    rows = _own_contributions()
    return render_template(
        "benchmark_contribute.html",
        contributions=[{
            "id": r.id,
            "created_at": r.created_at,
            "word_count": len((r.redacted_text or "").split()),
            "snippet": (r.redacted_text or "")[:200],
        } for r in rows],
        preview_text=preview_text,
        preview_report=preview_report,
    )


@benchmark_bp.route("/benchmark/contribute", methods=["GET"])
@login_required
def contribute_page():
    return _render_contribute()


@benchmark_bp.route("/benchmark/contribute/preview", methods=["POST"])
@login_required
def contribute_preview():
    raw = (request.form.get("resume_text") or "").strip()
    if not raw:
        flash("Paste your resume text first — nothing was stored.", "error")
        return redirect(url_for("benchmark_collection.contribute_page"))
    if len(raw) > MAX_RESUME_CHARS:
        flash("That text is too long (max 100,000 characters).", "error")
        return redirect(url_for("benchmark_collection.contribute_page"))
    # Preview only: nothing is stored here, and the raw text is not logged.
    redacted, report = redact_resume_text(raw)
    return _render_contribute(preview_text=redacted, preview_report=report)


@benchmark_bp.route("/benchmark/contribute/confirm", methods=["POST"])
@login_required
def contribute_confirm():
    edited = (request.form.get("resume_text") or "").strip()
    consent = request.form.get("consent") == "on"
    if not consent:
        # No consent, no storage — the preview text is simply discarded.
        flash("Tick the consent box to contribute. Nothing was stored.", "error")
        return redirect(url_for("benchmark_collection.contribute_page"))
    if not edited:
        flash("Nothing to store — paste your resume text first.", "error")
        return redirect(url_for("benchmark_collection.contribute_page"))
    if len(edited) > MAX_RESUME_CHARS:
        flash("That text is too long (max 100,000 characters).", "error")
        return redirect(url_for("benchmark_collection.contribute_page"))
    # Re-redact the confirmed text: anything the contributor re-typed
    # into the preview is still stripped before storage.
    redacted, _report = redact_resume_text(edited)
    try:
        store_contribution(current_user.id, redacted, consent=True)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("benchmark_collection.contribute_page"))
    flash("Thank you — your redacted resume was contributed. You can "
          "delete it any time from this page.", "success")
    return redirect(url_for("benchmark_collection.contribute_page"))


@benchmark_bp.route("/benchmark/contribute/<int:contrib_id>/delete",
                    methods=["POST"])
@login_required
def contribute_delete(contrib_id: int):
    BenchmarkContribution = _models()
    row = BenchmarkContribution.query.filter_by(
        id=contrib_id, user_id=current_user.id).first()
    if row is None:
        # 404 for missing AND for other users' rows (no existence leak).
        return render_template("404.html"), 404
    db.session.delete(row)
    db.session.commit()
    flash("Your contribution was deleted.", "success")
    return redirect(url_for("benchmark_collection.contribute_page"))
