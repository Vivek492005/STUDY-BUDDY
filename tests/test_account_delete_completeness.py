"""Deletion completeness (audit fix, Stage 2, 2026-10-28).

Before the fix, /account/delete explicitly deleted 19 models while the
codebase has 60+ user-owned tables — "delete my account and all
associated data" silently left orphans behind (and could fail outright
on Postgres FK enforcement). These tests seed one row in EVERY
user-owned table for one user, delete the account through the real
route, and assert nothing of that user's remains — plus the documented
edge cases: mentor profiles are anonymized (not deleted, so other
users' reviews/bookings keep working), guest peer rooms survive
detached, and a second user's data is untouched.

The seeder re-derives the model list from the registry (like the
production code's registry) so a future model cannot slip through
uncovered: any model the seeder cannot seed fails loudly here.
"""

from datetime import date, datetime, timezone

import sqlalchemy as sa

from tests.conftest import login


def _by_table(app):
    from unified_app import db
    return {m.local_table.name: m.class_
            for m in db.Model.registry.mappers}


def _fill_value(col, uid, by_table, app):
    """Dummy value for a required column, or None if it must defer."""
    from unified_app import db
    if col.foreign_keys:
        fk = next(iter(col.foreign_keys))
        parent = by_table.get(fk.column.table.name)
        if parent is None:
            return None
        row = db.session.query(parent).first()
        if row is None:
            return None  # parent not seeded yet: defer this model
        return row.id
    t = col.type
    if isinstance(t, sa.Enum):
        return t.enums[0]
    if isinstance(t, sa.Boolean):
        return False
    if isinstance(t, sa.Integer):
        return 1
    if isinstance(t, sa.Float):
        return 0.0
    if isinstance(t, sa.DateTime):
        return datetime.now(timezone.utc)
    if isinstance(t, sa.Date):
        return date.today()
    if isinstance(t, sa.JSON):
        return {}
    if isinstance(t, (sa.String, sa.Text, sa.Unicode, sa.UnicodeText)):
        return f"seed-{col.name}"
    return None


def _seed(model, uid, by_table, app, **overrides):
    """Try to seed one row of `model` for `uid`. True on success.

    `overrides` supply the user columns for linked (non-user_id)
    models; plain user_id models get user_id=uid automatically.
    Required columns without defaults are dummy-filled by type;
    foreign keys resolve to the first existing parent row, or defer
    (return False) so the caller retries in a later pass.
    """
    from unified_app import db
    kwargs = dict(overrides)
    for col in model.__table__.columns:
        if col.primary_key or col.name in kwargs:
            continue
        if col.name == "user_id":
            kwargs["user_id"] = uid
            continue
        if col.default is not None or col.server_default is not None:
            continue
        if col.nullable:
            continue
        value = _fill_value(col, uid, by_table, app)
        if value is None and col.foreign_keys:
            return False  # defer: parent row missing
        if value is not None:
            kwargs[col.name] = value
    try:
        obj = model(**kwargs)
        db.session.add(obj)
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        return False


def test_delete_removes_every_user_owned_row(client, app):
    from unified_app import User, db
    from studybuddy.user_data import user_id_models
    import unified_app as ua

    with app.app_context():
        u = User(name="Del All", email="delall@example.com")
        u.set_password("Str0ng!Pass1")
        db.session.add(u)
        db.session.commit()
        uid = u.id
        # Control: a second user's row must survive.
        u2 = User(name="Control", email="control@example.com")
        u2.set_password("Str0ng!Pass1")
        db.session.add(u2)
        db.session.commit()
        u2_id = u2.id
        ctrl = ua.ResumeAnalysis(user_id=u2_id, resume_text="control")
        db.session.add(ctrl)
        db.session.commit()

        by_table = _by_table(app)
        # One combined dependency-pass loop: user_id models + linked
        # models + OfficeHour (the non-user-owned parent that bookings
        # and notes reference). Children defer until parents exist.
        pending = [(m, {}) for m, _ in user_id_models()] + [
            (ua.PeerRoom, {"host_id": uid}),
            (ua.PeerFeedback, {"reviewer_id": uid, "reviewee_id": uid}),
            (ua.PeerBlock, {"blocker_id": uid, "blocked_id": uid}),
            (ua.TeamLeague, {"created_by": uid}),
            (ua.OHReport, {"reporter_user_id": uid}),
            (ua.OfficeHour, {}),
        ]
        for _ in range(12):
            if not pending:
                break
            nxt = []
            for model, over in pending:
                if not _seed(model, uid, by_table, app, **over):
                    nxt.append((model, over))
            pending = nxt
        assert not pending, \
            f"could not seed: {[m.__name__ for m, _ in pending]}"
        # A room hosted by the control user, guested by uid: must
        # survive detached (guest_id -> NULL). Token is required +
        # unique, so it is set explicitly.
        guest_room = ua.PeerRoom(host_id=u2_id, guest_id=uid,
                                 token="guestroom12")
        db.session.add(guest_room)
        db.session.commit()
        guest_room_id = guest_room.id
        # A league created by uid with the control user as a member:
        # league + memberships go with the creator.
        league = ua.TeamLeague.query.filter_by(created_by=uid).first()
        assert league is not None
        db.session.add(ua.TeamLeagueMember(league_id=league.id,
                                           user_id=u2_id))
        db.session.commit()

    login(client, email="delall@example.com", password="Str0ng!Pass1")
    r = client.post("/account/delete",
                    data={"password": "Str0ng!Pass1", "confirm": "DELETE"},
                    follow_redirects=False)
    assert r.status_code == 302, f"delete failed: {r.status_code}"

    with app.app_context():
        assert db.session.get(User, uid) is None
        # Every user_id model: zero rows for uid.
        for model, _ in user_id_models():
            if model is ua.Mentor:
                continue  # anonymized, asserted below
            n = model.query.filter_by(user_id=uid).count()
            assert n == 0, f"{model.__name__} still has {n} rows"
        # Mentor profile anonymized, not deleted.
        mentors = ua.Mentor.query.filter_by(user_id=uid).all()
        assert not mentors, "mentor profile still linked to deleted user"
        anon = ua.Mentor.query.filter_by(name="Deleted user").all()
        assert len(anon) == 1
        assert anon[0].user_id is None
        # Linked rows gone.
        assert ua.PeerRoom.query.filter_by(host_id=uid).count() == 0
        assert ua.PeerFeedback.query.filter(
            (ua.PeerFeedback.reviewer_id == uid)
            | (ua.PeerFeedback.reviewee_id == uid)).count() == 0
        assert ua.PeerBlock.query.filter(
            (ua.PeerBlock.blocker_id == uid)
            | (ua.PeerBlock.blocked_id == uid)).count() == 0
        assert ua.TeamLeague.query.filter_by(created_by=uid).count() == 0
        assert ua.OHReport.query.filter_by(
            reporter_user_id=uid).count() == 0
        # Guest room survived, detached.
        room = db.session.get(ua.PeerRoom, guest_room_id)
        assert room is not None and room.guest_id is None
        # Control user's data untouched.
        assert db.session.get(User, u2_id) is not None
        assert ua.ResumeAnalysis.query.filter_by(
            user_id=u2_id).count() == 1


def test_delete_covers_every_registered_user_id_model(client, app):
    """Guard against registry drift: every model class with a user_id
    column must appear in studybuddy.user_data's registry."""
    from unified_app import db
    from studybuddy.user_data import user_id_models

    with app.app_context():
        registered = {m for m, _ in user_id_models()}
        for mapper in db.Model.registry.mappers:
            cls = mapper.class_
            cols = {c.name for c in cls.__table__.columns}
            if "user_id" in cols:
                assert cls in registered, \
                    f"{cls.__name__} has user_id but is not in user_data"
