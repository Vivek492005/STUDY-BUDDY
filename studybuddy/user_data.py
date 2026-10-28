"""Canonical registry of user-owned data (audit fix, 2026-10-28).

Before this module, `/account/export` covered 14 models and
`/account/delete` covered 19, while the codebase has 60+ user-owned
tables — so "delete my account and all associated data" silently left
orphans behind (and, on Postgres with enforced foreign keys, could fail
outright). Both handlers now use this one registry, and
``tests/test_account_delete_completeness.py`` re-derives the model list
from the SQLAlchemy registry so a future model cannot slip through
uncovered.

Scope rules (documented, not accidental):
* Plain ``user_id`` models: deleted on account delete, exported on
  export. One exception: ``LLMConsent`` is exported under its existing
  special ``llm_consent`` key (first row), not the generic list.
* ``Mentor`` profiles are ANONYMIZED, not deleted: other users'
  ``MentorReview`` rows and ``OHBooking`` rows reference the profile
  via ``mentor_id``/``office_hour_id``. Deleting the profile would
  destroy other users' data or break foreign keys; anonymizing (name
  -> "Deleted user", PII cleared, ``user_id`` -> NULL) removes the
  deleted user's personal data while keeping referential integrity.
* Peer rooms the user hosts (or interviews in) are deleted together
  with their answers/feedback — the room is the host's entity. Rooms
  the user merely guests in survive with ``guest_id`` cleared.
* Team leagues the user created are deleted with their memberships
  (``created_by`` is NOT NULL, so they cannot be detached).
* ``LLMUsage`` rows with ``user_id`` NULL are anonymous ledger rows
  and are never attributed to anyone; only ``user_id == uid`` rows are
  deleted.

All imports from ``unified_app`` are lazy (inside functions): this
module must never create an import cycle at app-boot time.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

#: (attribute name on unified_app, export key) for plain user_id models.
#: Export keys for the original 14 are unchanged (tests + consumers
#: rely on them); new keys follow the same plural-snake convention.
USER_ID_MODELS = [
    ("PasswordResetToken", "password_reset_tokens"),
    ("ResumeAnalysis", "resume_analyses"),
    ("ResumeVersion", "resume_versions"),
    ("JDMatchReport", "jd_matches"),
    ("BulletRewrite", "bullet_rewrites"),
    ("CoverDoc", "cover_docs"),
    ("LinkedInReport", "linkedin_reports"),
    ("JobApplication", "job_applications"),
    ("Referral", "referrals"),
    ("Retrospective", "retrospectives"),
    ("Experiment", "experiments"),
    ("CareerAssessment", "career_assessments"),
    ("InterviewSession", "interview_sessions"),
    ("CommunicationAnalysis", "communication_analyses"),
    ("DSAProgress", "dsa_progress"),
    ("AptitudeAttempt", "aptitude_attempts"),
    ("STARDraft", "star_drafts"),
    ("RoadmapProgress", "roadmap_progress"),
    ("QuizAttempt", "quiz_attempts"),
    ("CertPlan", "cert_plans"),
    ("StudyDoc", "study_docs"),
    ("StudyQuestion", "study_questions"),
    ("PaperImport", "paper_imports"),
    ("Collection", "collections"),
    ("LitReview", "lit_reviews"),
    ("GapReport", "gap_reports"),
    ("FlashcardSet", "flashcard_sets"),
    ("PaperQuizAttempt", "paper_quiz_attempts"),
    ("ResearchSession", "research_sessions"),
    ("Goal", "goals"),
    ("OpportunityBookmark", "opportunity_bookmarks"),
    ("Portfolio", "portfolios"),
    ("PeerAnswer", "peer_answers"),
    # Mentor is exported here but ANONYMIZED on delete (see module doc).
    ("Mentor", "mentor_profiles"),
    ("MentorApplication", "mentor_applications"),
    ("OHBooking", "oh_bookings"),
    ("OHNote", "oh_notes"),
    ("OHFeedback", "oh_feedback"),
    ("MentorQuestion", "mentor_questions"),
    ("MentorReview", "mentor_reviews"),
    ("ProfileSnapshot", "profile_snapshots"),
    ("UserAPIKey", "user_api_keys"),
    ("XPLedger", "xp_ledger"),
    ("LabRun", "lab_runs"),
    ("LabSave", "lab_saves"),
    ("LabToolFavorite", "lab_tool_favorites"),
    ("LabPathProgress", "lab_path_progress"),
    ("BenchmarkContribution", "benchmark_contributions"),
    ("StreakState", "streak_states"),
    ("LeagueMember", "league_members"),
    ("LeagueResult", "league_results"),
    ("QuestProgress", "quest_progress"),
    ("WeeklyReflection", "weekly_reflections"),
    ("MomentumSettings", "momentum_settings"),
    ("TeamLeagueMember", "team_league_memberships"),
    ("SalaryEvaluation", "salary_evaluations"),
    ("HealthReview", "health_reviews"),
    ("HealthPulse", "health_pulses"),
    ("HealthGoal", "health_goals"),
    ("HealthShare", "health_shares"),
]


def _models():
    """Return (unified_app module, LLMUsage class).

    Lazy: unified_app must be fully loaded before its models are read.
    """
    import unified_app as ua

    from studybuddy.llm_ledger import LLMUsage

    return ua, LLMUsage


def user_id_models():
    """All plain user_id models as (model, export_key) pairs."""
    ua, LLMUsage = _models()
    out = []
    for attr, key in USER_ID_MODELS:
        out.append((getattr(ua, attr), key))
    # LLMConsent is exported specially (llm_consent key) but deleted
    # like any other user_id model.
    out.append((ua.LLMConsent, "llm_consent_records"))
    out.append((LLMUsage, "llm_usage"))
    return out


def _topo_delete_order(models):
    """Order models so children are deleted before their parents.

    Uses SQLAlchemy foreign-key metadata: if model A's table has a
    foreign key to model B's table (and B is also in the delete set),
    A must be deleted before B, otherwise the FK blocks B's deletion
    on Postgres. Kahn's algorithm: at each step, delete a model that
    no remaining model still references. Edges to tables outside the
    delete set (Mentor, which is anonymized, not deleted; OfficeHour,
    which survives) are ignored. A reference cycle falls back to
    name order for the remainder (the caller's loud leftover check
    still catches any real failure).
    """
    tables = {m.__table__.name: m for m in models}
    dependents = {m: set() for m in models}
    for m in models:
        for fk in m.__table__.foreign_keys:
            target = fk.column.table.name
            if target in tables and tables[target] is not m:
                dependents[tables[target]].add(m)
    ordered = []
    remaining = set(models)
    while remaining:
        ready = sorted((m for m in remaining
                        if not (dependents[m] & remaining)),
                       key=lambda x: x.__name__)
        if not ready:
            ordered.extend(sorted(remaining, key=lambda x: x.__name__))
            break
        ordered.extend(ready)
        remaining -= set(ready)
    return ordered


def delete_all_user_data(uid, db):
    """Delete every user-owned row for ``uid`` (no commit).

    Returns a {model_name: deleted_count} dict for logging/tests.
    Raises RuntimeError if any user_id model still has rows afterwards
    (fail loudly — silent orphans were the old bug). The caller owns
    the transaction: commit on success, rollback on failure.
    """
    ua, _ = _models()
    counts = {}

    def _del(model, clause):
        n = model.query.filter(clause).delete(
            synchronize_session=False)
        counts[model.__name__] = counts.get(model.__name__, 0) + n
        return n

    # 1. Rooms the user hosts/interviews in: their answers + feedback
    #    go with the room (the room is the host's entity).
    my_room_ids = [r.id for r in ua.PeerRoom.query.filter(
        (ua.PeerRoom.host_id == uid)
        | (ua.PeerRoom.interviewer_id == uid)).all()]
    if my_room_ids:
        _del(ua.PeerAnswer, ua.PeerAnswer.room_id.in_(my_room_ids))
        _del(ua.PeerFeedback, ua.PeerFeedback.room_id.in_(my_room_ids))

    # 2. Linked (non-user_id) rows.
    _del(ua.PeerFeedback,
         (ua.PeerFeedback.reviewer_id == uid)
         | (ua.PeerFeedback.reviewee_id == uid))
    _del(ua.PeerBlock,
         (ua.PeerBlock.blocker_id == uid)
         | (ua.PeerBlock.blocked_id == uid))
    _del(ua.OHReport, ua.OHReport.reporter_user_id == uid)
    my_league_ids = [t.id for t in ua.TeamLeague.query.filter_by(
        created_by=uid).all()]
    if my_league_ids:
        _del(ua.TeamLeagueMember,
             ua.TeamLeagueMember.league_id.in_(my_league_ids))
    _del(ua.TeamLeague, ua.TeamLeague.created_by == uid)
    _del(ua.PeerRoom,
         (ua.PeerRoom.host_id == uid)
         | (ua.PeerRoom.interviewer_id == uid))
    # Rooms the user merely guests in survive, detached.
    n_guests = ua.PeerRoom.query.filter_by(guest_id=uid).update(
        {"guest_id": None}, synchronize_session=False)
    counts["PeerRoom_guest_detached"] = n_guests

    # 3. Plain user_id models, children first (Mentor excepted).
    models = [m for m, _ in user_id_models() if m is not ua.Mentor]
    for model in _topo_delete_order(models):
        _del(model, model.user_id == uid)

    # 4. Mentor profiles: anonymize, never delete (see module doc).
    mentors = ua.Mentor.query.filter_by(user_id=uid).all()
    for mentor in mentors:
        mentor.name = "Deleted user"
        mentor.headline = "Former mentor"
        mentor.company = None
        mentor.college = None
        mentor.bio = None
        mentor.linkedin_url = None
        mentor.avatar_url = None
        mentor.verified = False
        mentor.is_seed = False
        mentor.user_id = None
    counts["Mentor_anonymized"] = len(mentors)

    # 5. Verify: fail loudly on leftovers (the old silent-orphan bug).
    leftovers = []
    for model, _ in user_id_models():
        if model is ua.Mentor:
            if model.query.filter_by(user_id=uid).count():
                leftovers.append(model.__name__)
        elif model.query.filter_by(user_id=uid).count():
            leftovers.append(model.__name__)
    if leftovers:
        raise RuntimeError(
            f"account delete incomplete, rows remain for uid={uid}: "
            f"{sorted(leftovers)}")
    return counts


def _serialize_row(row):
    return {c.name: str(getattr(row, c.name))
            for c in row.__table__.columns}


def export_user_data(uid):
    """All user-owned rows for ``uid`` as {export_key: [rows]}.

    Per-model failures degrade to an empty list (the previous export
    behaviour) and are logged; LLMConsent is intentionally excluded
    here because the export route already emits it as ``llm_consent``.
    """
    ua, _ = _models()
    data = {}
    for model, key in user_id_models():
        if model is ua.LLMConsent:
            continue
        try:
            rows = model.query.filter_by(user_id=uid).all()
            data[key] = [_serialize_row(r) for r in rows]
        except Exception:
            log.exception("account export failed for %s", model.__name__)
            data[key] = []
    # Linked rows (no user_id column).
    try:
        hosted = ua.PeerRoom.query.filter_by(host_id=uid).all()
        guested = ua.PeerRoom.query.filter_by(guest_id=uid).all()
        seen = set()
        rooms = []
        for r in hosted + guested:
            if r.id not in seen:
                seen.add(r.id)
                rooms.append(_serialize_row(r))
        data["peer_rooms"] = rooms
    except Exception:
        log.exception("account export failed for PeerRoom")
        data["peer_rooms"] = []
    for model, key, clause in [
        (ua.PeerFeedback, "peer_feedback",
         (ua.PeerFeedback.reviewer_id == uid)
         | (ua.PeerFeedback.reviewee_id == uid)),
        (ua.PeerBlock, "peer_blocks",
         (ua.PeerBlock.blocker_id == uid)
         | (ua.PeerBlock.blocked_id == uid)),
        (ua.OHReport, "oh_reports",
         ua.OHReport.reporter_user_id == uid),
        (ua.TeamLeague, "team_leagues_created",
         ua.TeamLeague.created_by == uid),
    ]:
        try:
            rows = model.query.filter(clause).all()
            data[key] = [_serialize_row(r) for r in rows]
        except Exception:
            log.exception("account export failed for %s", model.__name__)
            data[key] = []
    return data
