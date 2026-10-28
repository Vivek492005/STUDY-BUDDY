"""Retention purge (audit fix, Stage 2, 2026-10-28).

The privacy page promises extracted text/analyses are kept 90 days of
inactivity unless saved. These tests seed one OLD (>90d) and one RECENT
row in every purged table and assert: dry-run deletes nothing, apply
deletes exactly the old rows, and a saved artifact (ResumeVersion) is
never purged.
"""

from datetime import datetime, timedelta, timezone

OLD = datetime.now(timezone.utc) - timedelta(days=100)
RECENT = datetime.now(timezone.utc) - timedelta(days=10)


def _seed(app, uid):
    import unified_app as ua
    from unified_app import db
    with app.app_context():
        rows = [
            ua.ResumeAnalysis(user_id=uid, created_at=OLD),
            ua.ResumeAnalysis(user_id=uid, created_at=RECENT),
            ua.JDMatchReport(user_id=uid, created_at=OLD),
            ua.JDMatchReport(user_id=uid, created_at=RECENT),
            ua.BulletRewrite(user_id=uid, original="old",
                            created_at=OLD),
            ua.BulletRewrite(user_id=uid, original="new",
                            created_at=RECENT),
            ua.LinkedInReport(user_id=uid, created_at=OLD),
            ua.LinkedInReport(user_id=uid, created_at=RECENT),
            ua.InterviewSession(user_id=uid, company="Acme",
                                role="SDE", category="behavioral",
                                created_at=OLD),
            ua.InterviewSession(user_id=uid, company="Acme",
                                role="SDE", category="behavioral",
                                created_at=RECENT),
            ua.CommunicationAnalysis(user_id=uid, created_at=OLD),
            ua.CommunicationAnalysis(user_id=uid, created_at=RECENT),
            ua.AptitudeAttempt(user_id=uid, category="quant",
                               num_questions=5,
                               time_limit_sec=60, started_at=OLD,
                               created_at=OLD),
            ua.AptitudeAttempt(user_id=uid, category="quant",
                               num_questions=5,
                               time_limit_sec=60, started_at=RECENT,
                               created_at=RECENT),
            ua.QuizAttempt(user_id=uid, skill="python", num_questions=5,
                           started_at=OLD, created_at=OLD),
            ua.QuizAttempt(user_id=uid, skill="python", num_questions=5,
                           started_at=RECENT, created_at=RECENT),
            ua.CareerAssessment(user_id=uid, created_at=OLD),
            ua.CareerAssessment(user_id=uid, created_at=RECENT),
            ua.Experiment(user_id=uid, hypothesis="h", change="c",
                          target="t", created_at=OLD),
            ua.Experiment(user_id=uid, hypothesis="h", change="c",
                          target="t", created_at=RECENT),
            # Saved artifacts: must NEVER be purged.
            ua.ResumeVersion(user_id=uid, name="keep-me",
                             resume_text="kept resume text",
                             created_at=OLD),
        ]
        db.session.add_all(rows)
        db.session.commit()


def _counts(app, uid):
    import unified_app as ua
    from studybuddy.retention import PURGE_TABLES
    out = {}
    with app.app_context():
        for attr, _ in PURGE_TABLES:
            model = getattr(ua, attr)
            out[model.__name__] = model.query.filter_by(
                user_id=uid).count()
        out["ResumeVersion"] = ua.ResumeVersion.query.filter_by(
            user_id=uid).count()
    return out


def test_purge_dry_run_deletes_nothing(app):
    import unified_app as ua
    from unified_app import db
    from studybuddy.retention import purge_stale_analyses
    with app.app_context():
        u = ua.User(name="Ret", email="ret@example.com")
        u.set_password("Str0ng!Pass1")
        db.session.add(u)
        db.session.commit()
        uid = u.id
    _seed(app, uid)
    with app.app_context():
        counts = purge_stale_analyses(db, days=90, dry_run=True)
    assert all(v == 1 for v in counts.values()), counts
    after = _counts(app, uid)
    assert all(v == 2 for k, v in after.items()
               if k != "ResumeVersion"), after
    assert after["ResumeVersion"] == 1


def test_purge_apply_deletes_only_old_rows(app):
    import unified_app as ua
    from unified_app import db
    from studybuddy.retention import purge_stale_analyses
    with app.app_context():
        u = ua.User(name="Ret2", email="ret2@example.com")
        u.set_password("Str0ng!Pass1")
        db.session.add(u)
        db.session.commit()
        uid = u.id
    _seed(app, uid)
    with app.app_context():
        counts = purge_stale_analyses(db, days=90, dry_run=False)
    assert all(v == 1 for v in counts.values()), counts
    after = _counts(app, uid)
    assert all(v == 1 for k, v in after.items()), after
    # The surviving row in each table is the recent one. (SQLite
    # returns naive datetimes, so compare naive against naive.)
    with app.app_context():
        row = ua.ResumeAnalysis.query.filter_by(user_id=uid).first()
        assert row.created_at.replace(tzinfo=None) >= \
            RECENT.replace(tzinfo=None) - timedelta(seconds=5)
