"""Data-retention enforcement (audit fix, Stage 2, 2026-10-28).

The privacy page promises: "Extracted text and analyses are kept for
90 days of inactivity unless you save them", and
``docs/privacy/data-inventory.md`` lists the tables that promise
covers. Until now no job enforced it — old analyses accumulated
forever. This module is the enforcement; scheduling it is a dashboard
step (see ``scripts/purge_stale_data.py`` and the BLOCKED list in the
audit-fixes progress note — a Render Cron Job, not code).

Scope decisions (documented, not accidental):
* Purged: exactly the 10 inventory tables with a 90-day line —
  resume/jd/bullet/linkedin analyses, interview sessions,
  communication analyses, aptitude + quiz attempts, career
  assessments, lab experiments. All have ``created_at``.
* NOT purged: ResumeVersion, CoverDoc, Portfolio and other saved
  artifacts. Those ARE the "unless you save them" half of the promise:
  a user who saves an analysis as a version/doc keeps it.
* "Inactivity" is approximated by row age (``created_at`` older than
  ``days``). Per-user last-activity tracking does not exist, so a
  per-user inactivity clock cannot be computed honestly from current
  schema; row age is the conservative, auditable proxy, and it is the
  same proxy the inventory wording implies for transient analyses.
* The purge is age-only: it does not filter by user, so anonymous
  rows (if any ever exist — /try is store-nothing by design) age out
  the same way.
* Dry-run is the default everywhere: the script prints counts and
  deletes nothing unless ``--apply`` is passed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

#: (attribute name on unified_app, inventory table name).
PURGE_TABLES = [
    ("ResumeAnalysis", "resume_analysis"),
    ("JDMatchReport", "jd_match_report"),
    ("BulletRewrite", "bullet_rewrite"),
    ("LinkedInReport", "linkedin_report"),
    ("InterviewSession", "interview_session"),
    ("CommunicationAnalysis", "communication_analysis"),
    ("AptitudeAttempt", "aptitude_attempt"),
    ("QuizAttempt", "quiz_attempt"),
    ("CareerAssessment", "career_assessment"),
    ("Experiment", "experiment"),
]

DEFAULT_RETENTION_DAYS = 90


def purge_stale_analyses(db, days=DEFAULT_RETENTION_DAYS,
                         dry_run=True, now=None):
    """Delete inventory rows older than ``days`` (or count them).

    Returns {model_name: count}. With ``dry_run=True`` (default)
    nothing is deleted. With ``dry_run=False`` the deletions commit
    in the caller's session (this function commits once at the end).
    All model imports are lazy: unified_app must be fully loaded.
    """
    import unified_app as ua

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    counts = {}
    for attr, _table in PURGE_TABLES:
        model = getattr(ua, attr)
        query = model.query.filter(model.created_at < cutoff)
        counts[model.__name__] = query.count()
        if not dry_run:
            query.delete(synchronize_session=False)
    if not dry_run:
        db.session.commit()
    return counts
