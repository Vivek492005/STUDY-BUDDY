"""Tests for Wave 5 #27: humane momentum system (momentum.py + routes + hooks).

Covers: XP rules & level curve, streak math (pure), idempotency, day-one
bonus, shield forgiveness & graceful reset, quest progress/claims, leagues,
reflections, opt-out, routes (auth + CSRF), and hook integration through
real route handlers.
"""
from datetime import UTC, datetime, timedelta

import pytest

from studybuddy import momentum
from studybuddy.momentum import (
    ALL_EVENT_TYPES,
    CAREER_XP_ACTIONS,
    DAY_ONE_BONUS_XP,
    DIRECT_EVENTS,
    QUEST_TEMPLATES,
    SHIELDS_PER_MONTH,
    XP_RULES,
    apply_streak,
    dedupe_key_for,
    empty_summary,
    iso_week_key,
    level_for_xp,
    level_progress,
    month_key,
    week_start_date,
    xp_for_level,
)
from tests.conftest import login, register
from unified_app import (
    LeagueMember,
    LeagueResult,
    MomentumSettings,
    QuestProgress,
    StreakState,
    User,
    WeeklyReflection,
    XPLedger,
    create_app,
    db,
)

UTC = UTC


def _dt(y, m, d, hh=12):
    return datetime(y, m, d, hh, 0, 0, tzinfo=UTC)


@pytest.fixture()
def authed_client(client):
    register(client)
    login(client)
    return client


def _uid(app, email="aarav@example.com"):
    with app.app_context():
        return User.query.filter_by(email=email).first().id


def _register_extra(client, name, email):
    """Register an additional user: log out first (the register route skips
    creation while a session is active)."""
    client.post("/logout", follow_redirects=False)
    register(client, name=name, email=email)


def _ledger(app, user_id, event_type=None):
    with app.app_context():
        q = XPLedger.query.filter_by(user_id=user_id)
        if event_type:
            q = q.filter_by(event_type=event_type)
        return q.all()


# ---------------------------------------------------------------------------
# XP rules & level curve (pure)
# ---------------------------------------------------------------------------

def test_xp_rules_exact_values():
    assert XP_RULES == {
        "quiz_completed": 20, "dsa_solved": 25, "mock_completed": 30,
        "resume_analyzed": 15, "roadmap_step": 10,
        "flashcards_reviewed": 15, "aptitude_completed": 20,
        "coach_analyzed": 15, "star_saved": 10,
        "paper_quiz_completed": 20, "peer_session_completed": 40,
        "retrospective_logged": 15, "mentor_session_attended": 30,
        "mentor_feedback_given": 10, "health_review_completed": 20,
        "quest_completed": 50,
    }


def test_xp_rules_all_positive_ints():
    assert len(XP_RULES) == 16
    for ev, xp in XP_RULES.items():
        assert isinstance(xp, int) and xp > 0, ev


def test_direct_events_exclude_quest_bonus():
    assert "quest_completed" not in DIRECT_EVENTS
    assert DIRECT_EVENTS == set(XP_RULES) - {"quest_completed"}


def test_all_event_types_include_day_one():
    assert "day_one_win" in ALL_EVENT_TYPES
    assert set(XP_RULES) <= ALL_EVENT_TYPES


def test_level_curve():
    assert level_for_xp(0) == 1
    assert level_for_xp(99) == 1
    assert level_for_xp(100) == 2
    assert level_for_xp(399) == 2
    assert level_for_xp(400) == 3
    assert level_for_xp(900) == 4
    assert level_for_xp(-50) == 1
    assert level_for_xp(None) == 1
    assert level_for_xp("nope") == 1


def test_xp_for_level():
    assert xp_for_level(1) == 0
    assert xp_for_level(2) == 100
    assert xp_for_level(3) == 400
    assert xp_for_level(0) == 0
    assert xp_for_level("bad") == 0


def test_level_progress():
    level, into, span, pct = level_progress(0)
    assert (level, into, span, pct) == (1, 0, 100, 0.0)
    level, into, span, pct = level_progress(50)
    assert level == 1 and into == 50 and span == 100 and pct == 50.0
    level, into, span, pct = level_progress(250)
    assert level == 2 and into == 150 and span == 300 and pct == 50.0


def test_quest_templates_sane():
    assert len(QUEST_TEMPLATES) >= 5
    ids = [q["id"] for q in QUEST_TEMPLATES]
    assert len(ids) == len(set(ids))
    # Phase 4: career quests count source rows (per-ref, not per-day),
    # so their event types are career ledger events, not XP_RULES.
    allowed = set(XP_RULES) | {v[0] for v in CAREER_XP_ACTIONS.values()}
    for q in QUEST_TEMPLATES:
        assert q["event_type"] in allowed
        assert 1 <= int(q["target"]) <= 7  # weekly-counted => max 7/week
        assert q["title"] and q["description"]


def test_week_helpers():
    d = _dt(2026, 10, 14)  # a Wednesday
    ws = week_start_date(d)
    assert ws.weekday() == 0  # Monday
    assert ws == _dt(2026, 10, 12).date()
    assert iso_week_key(d) == "2026-W42"
    assert month_key(d) == "2026-10"
    # never raises on garbage
    assert week_start_date(None).weekday() == 0
    assert week_start_date("garbage").weekday() == 0


def test_dedupe_key_format():
    day = _dt(2026, 10, 14).date()
    assert dedupe_key_for(7, "quiz_completed", day) == "7:quiz_completed:2026-10-14"


# ---------------------------------------------------------------------------
# Streak math (pure apply_streak)
# ---------------------------------------------------------------------------

def test_streak_first_ever():
    t = apply_streak(0, 0, None, 0, None, _dt(2026, 10, 14).date())
    assert t["current"] == 1 and t["longest"] == 1
    assert not t["shield_used"] and not t["reset"]


def test_streak_same_day_no_double():
    today = _dt(2026, 10, 14).date()
    t = apply_streak(3, 5, today, 0, "2026-10", today)
    assert t["current"] == 3 and t["longest"] == 5
    assert not t["shield_used"]


def test_streak_consecutive_day():
    today = _dt(2026, 10, 14).date()
    t = apply_streak(3, 5, today - timedelta(days=1), 0, "2026-10", today)
    assert t["current"] == 4 and t["longest"] == 5
    assert not t["shield_used"] and not t["reset"]


def test_streak_longest_updates():
    today = _dt(2026, 10, 14).date()
    t = apply_streak(5, 5, today - timedelta(days=1), 0, "2026-10", today)
    assert t["current"] == 6 and t["longest"] == 6


def test_streak_shield_forgiveness():
    today = _dt(2026, 10, 14).date()
    t = apply_streak(4, 9, today - timedelta(days=2), 0, "2026-10", today)
    assert t["shield_used"] and not t["reset"]
    assert t["current"] == 5 and t["longest"] == 9
    assert t["shields_used_month"] == 1


def test_streak_shield_month_rolls_over():
    today = _dt(2026, 11, 2).date()
    t = apply_streak(4, 9, today - timedelta(days=2), 2, "2026-10", today)
    assert t["shield_used"]  # October's usage doesn't carry into November
    assert t["shields_used_month"] == 1
    assert t["shields_month"] == "2026-11"


def test_streak_exhaustion_graceful_reset_to_one():
    today = _dt(2026, 10, 14).date()
    t = apply_streak(12, 20, today - timedelta(days=3), 2, "2026-10", today)
    assert t["reset"] and not t["shield_used"]
    assert t["current"] == 1  # kindness: restart at 1, never 0
    assert t["longest"] == 20  # longest is never punished


def test_streak_never_raises_on_garbage():
    t = apply_streak("x", None, "not-a-date", "y", None, None)
    assert isinstance(t, dict) and t["current"] >= 0


# ---------------------------------------------------------------------------
# record_event: ledger, idempotency, day-one bonus, streaks
# ---------------------------------------------------------------------------

def test_record_event_creates_row(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        res = momentum.record_event(uid, "quiz_completed", now=_dt(2026, 10, 14))
    assert res["ok"] and res["xp"] == 20
    rows = _ledger(app, uid, "quiz_completed")
    assert len(rows) == 1 and rows[0].xp == 20


def test_record_event_unknown_event_graceful(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        res = momentum.record_event(uid, "watch_netflix", now=_dt(2026, 10, 14))
    assert res["ok"] is False
    assert _ledger(app, uid) == []


def test_record_event_quest_bonus_not_direct(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        res = momentum.record_event(uid, "quest_completed")
    assert res["ok"] is False
    assert _ledger(app, uid, "quest_completed") == []


def test_record_event_bad_user_graceful(app):
    with app.app_context():
        assert momentum.record_event("nope", "quiz_completed")["ok"] is False
        assert momentum.record_event(-3, "quiz_completed")["ok"] is False


def test_record_event_idempotent_same_day(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        r1 = momentum.record_event(uid, "quiz_completed", now=_dt(2026, 10, 14, 9))
        r2 = momentum.record_event(uid, "quiz_completed", now=_dt(2026, 10, 14, 18))
    assert r1["ok"] and r2.get("duplicate")
    rows = _ledger(app, uid, "quiz_completed")
    assert len(rows) == 1


def test_record_event_different_events_same_day(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        momentum.record_event(uid, "quiz_completed", now=_dt(2026, 10, 14))
        momentum.record_event(uid, "dsa_solved", now=_dt(2026, 10, 14))
    assert len(_ledger(app, uid, "quiz_completed")) == 1
    assert len(_ledger(app, uid, "dsa_solved")) == 1


def test_record_event_next_day_counts_again(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        momentum.record_event(uid, "quiz_completed", now=_dt(2026, 10, 14))
        momentum.record_event(uid, "quiz_completed", now=_dt(2026, 10, 15))
    assert len(_ledger(app, uid, "quiz_completed")) == 2


def test_day_one_bonus_once(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        r1 = momentum.record_event(uid, "quiz_completed", now=_dt(2026, 10, 14))
        r2 = momentum.record_event(uid, "dsa_solved", now=_dt(2026, 10, 15))
    assert r1["day_one"] is True and r1["bonus"] == DAY_ONE_BONUS_XP == 25
    assert r2["day_one"] is False and r2["bonus"] == 0
    bonus_rows = _ledger(app, uid, "day_one_win")
    assert len(bonus_rows) == 1 and bonus_rows[0].xp == 25
    with app.app_context():
        total = sum(r.xp for r in XPLedger.query.filter_by(user_id=uid).all())
    assert total == 20 + 25 + 25  # quiz + day-one + dsa


def test_streak_increments_across_days(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        momentum.record_event(uid, "quiz_completed", now=_dt(2026, 10, 12))
        momentum.record_event(uid, "dsa_solved", now=_dt(2026, 10, 13))
        st = StreakState.query.filter_by(user_id=uid).first()
    assert st.current == 2 and st.longest == 2
    assert st.last_active_date == _dt(2026, 10, 13).date()


def test_streak_not_double_counted_same_day(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        momentum.record_event(uid, "quiz_completed", now=_dt(2026, 10, 14, 9))
        momentum.record_event(uid, "dsa_solved", now=_dt(2026, 10, 14, 20))
        st = StreakState.query.filter_by(user_id=uid).first()
    assert st.current == 1


def test_shield_forgiveness_end_to_end(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        momentum.record_event(uid, "quiz_completed", now=_dt(2026, 10, 12))
        # skip Oct 13 entirely; act on Oct 14 -> shield repairs the gap
        res = momentum.record_event(uid, "dsa_solved", now=_dt(2026, 10, 14))
        st = StreakState.query.filter_by(user_id=uid).first()
    assert res["shield_used"] is True
    assert st.current == 2 and st.shields_used_month == 1


def test_shield_exhaustion_graceful_reset_end_to_end(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        momentum.record_event(uid, "quiz_completed", now=_dt(2026, 10, 1))
        momentum.record_event(uid, "dsa_solved", now=_dt(2026, 10, 3))   # shield 1
        momentum.record_event(uid, "mock_completed", now=_dt(2026, 10, 5))  # shield 2
        res = momentum.record_event(uid, "star_saved", now=_dt(2026, 10, 7))  # exhausted
        st = StreakState.query.filter_by(user_id=uid).first()
    assert res["streak_reset"] is True
    assert st.current == 1  # graceful: 1, not 0


# ---------------------------------------------------------------------------
# Quests
# ---------------------------------------------------------------------------

def test_quest_progress_counts_weekly(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        for d in (12, 13, 14):  # Mon/Tue/Wed of the same ISO week
            momentum.record_event(uid, "quiz_completed", now=_dt(2026, 10, d))
        qp = QuestProgress.query.filter_by(
            user_id=uid, week_start=_dt(2026, 10, 12).date(),
            quest_id="quiz-3").first()
    assert qp is not None
    assert qp.count == 3 and qp.completed is True and qp.claimed is False


def test_quest_below_target_not_completed(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        for d in (12, 13):
            momentum.record_event(uid, "quiz_completed", now=_dt(2026, 10, d))
        qp = QuestProgress.query.filter_by(
            user_id=uid, week_start=_dt(2026, 10, 12).date(),
            quest_id="quiz-3").first()
    assert qp.count == 2 and qp.completed is False


def test_quest_progress_resets_weekly(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        momentum.record_event(uid, "quiz_completed", now=_dt(2026, 10, 14))
        summary = momentum.get_momentum_summary(uid, now=_dt(2026, 10, 19))
    q = next(x for x in summary["quests"] if x["id"] == "quiz-3")
    assert q["count"] == 0 and q["completed"] is False  # new ISO week


def test_claim_quest_awards_bonus(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        for d in (12, 13, 14):
            momentum.record_event(uid, "quiz_completed", now=_dt(2026, 10, d))
        res = momentum.claim_quest(uid, "quiz-3", now=_dt(2026, 10, 14))
    assert res["ok"] and res["xp"] == 50
    assert len(_ledger(app, uid, "quest_completed")) == 1
    with app.app_context():
        qp = QuestProgress.query.filter_by(
            user_id=uid, week_start=_dt(2026, 10, 12).date(),
            quest_id="quiz-3").first()
        assert qp.claimed is True


def test_claim_quest_idempotent(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        for d in (12, 13, 14):
            momentum.record_event(uid, "quiz_completed", now=_dt(2026, 10, d))
        r1 = momentum.claim_quest(uid, "quiz-3", now=_dt(2026, 10, 14))
        r2 = momentum.claim_quest(uid, "quiz-3", now=_dt(2026, 10, 14))
    assert r1["ok"] and r2.get("duplicate")
    assert len(_ledger(app, uid, "quest_completed")) == 1


def test_claim_quest_not_completed_fails(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        momentum.record_event(uid, "quiz_completed", now=_dt(2026, 10, 14))
        res = momentum.claim_quest(uid, "quiz-3", now=_dt(2026, 10, 14))
    assert res["ok"] is False and res["reason"] == "not completed"
    assert _ledger(app, uid, "quest_completed") == []


def test_claim_unknown_quest_fails(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        res = momentum.claim_quest(uid, "nope-quest")
    assert res["ok"] is False


# ---------------------------------------------------------------------------
# Leagues
# ---------------------------------------------------------------------------

def test_join_league(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        res = momentum.join_league(uid, now=_dt(2026, 10, 14))
        row = LeagueMember.query.filter_by(user_id=uid).first()
    assert res["ok"] and res["league_key"] == "2026-W42"
    assert row.league_key == "2026-W42" and row.opted_out is False


def test_join_league_idempotent(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        momentum.join_league(uid, now=_dt(2026, 10, 14))
        r2 = momentum.join_league(uid, now=_dt(2026, 10, 15))
        n = LeagueMember.query.filter_by(user_id=uid).count()
    assert r2.get("duplicate") and n == 1


def test_leave_league_removes_from_board(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        momentum.join_league(uid, now=_dt(2026, 10, 14))
        momentum.leave_league(uid)
        row = LeagueMember.query.filter_by(user_id=uid).first()
        summary = momentum.get_momentum_summary(uid, now=_dt(2026, 10, 14))
    assert row.opted_out is True
    assert summary["league"]["member"] is False
    assert summary["league"]["board"] == []


def test_leave_league_without_join_ok(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        assert momentum.leave_league(uid)["ok"] is True


def test_league_board_ranks_consistency_over_xp(app, client):
    # two users in the same cohort: one high-XP single day, one steady
    _register_extra(client, "Steady Sam", "sam@example.com")
    _register_extra(client, "Flash Fay", "fay@example.com")
    with app.app_context():
        sam = User.query.filter_by(email="sam@example.com").first().id
        fay = User.query.filter_by(email="fay@example.com").first().id
        momentum.join_league(sam, now=_dt(2026, 10, 14))
        momentum.join_league(fay, now=_dt(2026, 10, 14))
        # Fay: one big day (mock 30 + day-one 25 = 55 xp, 1 active day)
        momentum.record_event(fay, "mock_completed", now=_dt(2026, 10, 14))
        # Sam: three steady days of quizzes (20*3 + 25 day-one = 85 xp, 3 days)
        for d in (12, 13, 14):
            momentum.record_event(sam, "quiz_completed", now=_dt(2026, 10, d))
        board = momentum.get_momentum_summary(sam, now=_dt(2026, 10, 14))["league"]["board"]
    assert len(board) == 2
    assert board[0]["name"] == "Steady Sam"  # 3 active days beats 1
    assert board[0]["streak_days"] == 3
    assert board[1]["name"] == "Flash Fay"


def test_finalize_league_week_records_winner(app, client):
    _register_extra(client, "Winnie W", "win@example.com")
    _register_extra(client, "Lola L", "lola@example.com")
    with app.app_context():
        w = User.query.filter_by(email="win@example.com").first().id
        l = User.query.filter_by(email="lola@example.com").first().id
        momentum.join_league(w, now=_dt(2026, 10, 7))
        momentum.join_league(l, now=_dt(2026, 10, 7))
        for d in (5, 6, 7):  # last week (Mon Oct 5 - Sun Oct 11)
            momentum.record_event(w, "quiz_completed", now=_dt(2026, 10, d))
        momentum.record_event(l, "quiz_completed", now=_dt(2026, 10, 6))
        res = momentum.finalize_league_week(now=_dt(2026, 10, 14))
        rows = LeagueResult.query.filter_by(
            league_key="2026-W41",
            week_start=_dt(2026, 10, 5).date()).all()
    assert res["ok"] and res["leagues"] >= 1
    assert len(rows) == 2
    by_user = {r.user_id: r for r in rows}
    assert by_user[w].won is True and by_user[w].streak_days == 3
    assert by_user[l].won is False


def test_finalize_league_week_idempotent(app, client):
    _register_extra(client, "Ida I", "ida@example.com")
    with app.app_context():
        u = User.query.filter_by(email="ida@example.com").first().id
        momentum.join_league(u, now=_dt(2026, 10, 7))
        momentum.record_event(u, "quiz_completed", now=_dt(2026, 10, 6))
        momentum.finalize_league_week(now=_dt(2026, 10, 14))
        n1 = LeagueResult.query.count()
        momentum.finalize_league_week(now=_dt(2026, 10, 14))
        n2 = LeagueResult.query.count()
    assert n1 == n2 == 1


def test_last_week_winner_in_summary(app, client):
    _register_extra(client, "Champ C", "champ@example.com")
    with app.app_context():
        u = User.query.filter_by(email="champ@example.com").first().id
        momentum.join_league(u, now=_dt(2026, 10, 7))
        momentum.record_event(u, "quiz_completed", now=_dt(2026, 10, 6))
        summary = momentum.get_momentum_summary(u, now=_dt(2026, 10, 14))
    lw = summary["league"]["last_week"]
    assert lw is not None and lw["winner"] == "Champ C"


# ---------------------------------------------------------------------------
# Reflections & opt-out
# ---------------------------------------------------------------------------

def test_save_reflection(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        res = momentum.save_reflection(uid, "Shipped my portfolio",
                                       "Two mocks next week",
                                       now=_dt(2026, 10, 14))
        row = WeeklyReflection.query.filter_by(user_id=uid).first()
    assert res["ok"]
    assert row.wins == "Shipped my portfolio"
    assert row.focus_next == "Two mocks next week"
    assert row.week_start == _dt(2026, 10, 12).date()


def test_save_reflection_upsert(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        momentum.save_reflection(uid, "v1", "f1", now=_dt(2026, 10, 14))
        momentum.save_reflection(uid, "v2", "f2", now=_dt(2026, 10, 15))
        rows = WeeklyReflection.query.filter_by(user_id=uid).all()
    assert len(rows) == 1 and rows[0].wins == "v2"


def test_save_reflection_empty_fails(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        res = momentum.save_reflection(uid, "   ", "", now=_dt(2026, 10, 14))
    assert res["ok"] is False
    with app.app_context():
        assert WeeklyReflection.query.filter_by(user_id=uid).count() == 0


def test_save_reflection_logs_xp_and_feeds_quest(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        momentum.save_reflection(uid, "Did good work", "More DSA",
                                 now=_dt(2026, 10, 14))
        rows = _ledger(app, uid, "retrospective_logged")
        summary = momentum.get_momentum_summary(uid, now=_dt(2026, 10, 14))
    assert len(rows) == 1 and rows[0].xp == 15
    q = next(x for x in summary["quests"] if x["id"] == "reflect-1")
    assert q["count"] == 1 and q["completed"] is True


def test_opt_out_makes_record_event_noop(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        assert momentum.set_opt_out(uid, True)["ok"]
        res = momentum.record_event(uid, "quiz_completed", now=_dt(2026, 10, 14))
        summary = momentum.get_momentum_summary(uid, now=_dt(2026, 10, 14))
    assert res.get("skipped") is True
    assert _ledger(app, uid) == []
    assert summary["opted_out"] is True
    with app.app_context():
        assert StreakState.query.filter_by(user_id=uid).first() is None


def test_opt_back_in_resumes(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        momentum.set_opt_out(uid, True)
        momentum.set_opt_out(uid, False)
        res = momentum.record_event(uid, "quiz_completed", now=_dt(2026, 10, 14))
    assert res["ok"] and len(_ledger(app, uid, "quiz_completed")) == 1


def test_join_league_while_opted_out_refused(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        momentum.set_opt_out(uid, True)
        res = momentum.join_league(uid, now=_dt(2026, 10, 14))
    assert res["ok"] is False


# ---------------------------------------------------------------------------
# Summary shape
# ---------------------------------------------------------------------------

def test_summary_shape_fresh_user(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        s = momentum.get_momentum_summary(uid, now=_dt(2026, 10, 14))
    assert s["total_xp"] == 0 and s["level"] == 1
    assert s["streak"]["current"] == 0
    assert s["streak"]["shields_left"] == SHIELDS_PER_MONTH
    assert s["day_one"]["earned"] is False
    assert len(s["quests"]) == len(QUEST_TEMPLATES)
    assert s["league"]["member"] is False
    assert s["reflection"] is None
    assert s["opted_out"] is False


def test_summary_level_and_streak(app, authed_client):
    uid = _uid(app)
    with app.app_context():
        momentum.record_event(uid, "quiz_completed", now=_dt(2026, 10, 14))
        s = momentum.get_momentum_summary(uid, now=_dt(2026, 10, 14))
    assert s["total_xp"] == 45 and s["level"] == 1
    assert s["streak"]["current"] == 1
    assert s["day_one"]["earned"] is True


def test_empty_summary_safe():
    s = empty_summary()
    assert s["total_xp"] == 0 and s["quests"] == []


def test_summary_never_raises_on_garbage(app):
    with app.app_context():
        s = momentum.get_momentum_summary(999999, now=_dt(2026, 10, 14))
    assert s["total_xp"] == 0  # unknown user -> zeros, no raise


# ---------------------------------------------------------------------------
# Routes: auth, hub, APIs
# ---------------------------------------------------------------------------

def test_hub_requires_login(client):
    r = client.get("/momentum", follow_redirects=False)
    assert r.status_code == 302


def test_hub_renders(authed_client):
    r = authed_client.get("/momentum")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "momentum" in html.lower()
    assert "csrf-token" in html


def test_summary_api_requires_login(client):
    r = client.get("/momentum/api/summary", follow_redirects=False)
    assert r.status_code == 302


def test_summary_api(authed_client):
    r = authed_client.get("/momentum/api/summary")
    assert r.status_code == 200
    d = r.get_json()
    assert d["success"] and d["summary"]["level"] == 1


def test_quest_claim_route(authed_client, app):
    uid = _uid(app)
    # Quests are weekly: record the events inside the CURRENT week, or the
    # claim (which counts this week's events) sees 0 and returns 400.
    from datetime import datetime as _datetime
    monday = momentum.week_start_date(_datetime.now(UTC))
    with app.app_context():
        for offset in (0, 1, 2):
            day = monday + timedelta(days=offset)
            momentum.record_event(
                uid, "quiz_completed",
                now=datetime(day.year, day.month, day.day, 12,
                             tzinfo=UTC))
    r = authed_client.post("/momentum/api/quest/quiz-3/claim")
    assert r.status_code == 200
    assert r.get_json()["success"] is True
    # second claim is a graceful duplicate, not an error
    r2 = authed_client.post("/momentum/api/quest/quiz-3/claim")
    assert r2.status_code == 200 and r2.get_json()["duplicate"] is True


def test_quest_claim_route_unknown_404(authed_client):
    r = authed_client.post("/momentum/api/quest/nope/claim")
    assert r.status_code == 404


def test_quest_claim_route_incomplete_400(authed_client):
    r = authed_client.post("/momentum/api/quest/quiz-3/claim")
    assert r.status_code == 400


def test_league_join_leave_routes(authed_client):
    r = authed_client.post("/momentum/api/league/join")
    assert r.status_code == 200 and r.get_json()["success"]
    r = authed_client.post("/momentum/api/league/leave")
    assert r.status_code == 200 and r.get_json()["success"]


def test_reflect_route(authed_client):
    r = authed_client.post("/momentum/api/reflect",
                           json={"wins": "Finished 3 quizzes",
                                 "focus_next": "Mock interview"})
    assert r.status_code == 200 and r.get_json()["success"]
    r = authed_client.post("/momentum/api/reflect",
                           json={"wins": "  ", "focus_next": ""})
    assert r.status_code == 400


def test_opt_out_route(authed_client, app):
    uid = _uid(app)
    r = authed_client.post("/momentum/api/opt-out", json={"opt_out": True})
    assert r.status_code == 200 and r.get_json()["opted_out"] is True
    with app.app_context():
        row = MomentumSettings.query.filter_by(user_id=uid).first()
        assert row.momentum_opt_out is True
    r = authed_client.get("/momentum")
    assert r.status_code == 200
    assert "paused" in r.get_data(as_text=True).lower()
    r = authed_client.post("/momentum/api/opt-out", json={"opt_out": False})
    assert r.get_json()["opted_out"] is False


def test_post_routes_require_login(client):
    for url in ("/momentum/api/quest/quiz-3/claim",
                "/momentum/api/league/join", "/momentum/api/league/leave",
                "/momentum/api/reflect", "/momentum/api/opt-out"):
        r = client.post(url, json={})
        assert r.status_code == 401, url


# ---------------------------------------------------------------------------
# CSRF: POSTs without a token must be rejected (400)
# ---------------------------------------------------------------------------

@pytest.fixture()
def csrf_app():
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SECRET_KEY": "momentum-csrf-test",
        "WTF_CSRF_ENABLED": True,
    })
    with app.app_context():
        db.create_all()
    yield app


def test_csrf_rejected_on_momentum_posts(csrf_app):
    client = csrf_app.test_client()
    register(client)
    login(client)
    for url, body in (
            ("/momentum/api/quest/quiz-3/claim", {}),
            ("/momentum/api/league/join", {}),
            ("/momentum/api/league/leave", {}),
            ("/momentum/api/reflect", {"wins": "x", "focus_next": "y"}),
            ("/momentum/api/opt-out", {"opt_out": True})):
        r = client.post(url, json=body)
        assert r.status_code == 400, url


# ---------------------------------------------------------------------------
# Hook integration: real route handlers must mint XP rows
# ---------------------------------------------------------------------------

def _draft_payload():
    return {"competency": "leadership",
            "situation": "Our fest app had no working build three weeks out.",
            "task": "As tech lead, I had to ship it for 2000 attendees.",
            "action": "I split work into daily deliverables and unblocked the payment integration myself.",
            "result": "We shipped early and handled 1800 registrations with zero downtime."}


def test_hook_star_save_mints_xp(authed_client, app):
    uid = _uid(app)
    r = authed_client.post("/star/save", json=_draft_payload())
    assert r.status_code == 200
    assert len(_ledger(app, uid, "star_saved")) == 1


def test_hook_star_save_still_works_if_momentum_broken(authed_client, app,
                                                       monkeypatch):
    # Even if record_event explodes, the main flow must succeed.
    import unified_app
    monkeypatch.setattr(unified_app.momentum_mod, "record_event",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = authed_client.post("/star/save", json=_draft_payload())
    assert r.status_code == 200 and r.get_json()["success"]


def test_hook_dsa_solved_only_on_solved(authed_client, app):
    from studybuddy.dsa_practice import all_problems
    uid = _uid(app)
    pid = all_problems()[0]["id"]
    r = authed_client.post(f"/dsa/{pid}/status",
                           json={"status": "attempted", "code": "x"})
    assert r.status_code == 200
    assert _ledger(app, uid, "dsa_solved") == []
    r = authed_client.post(f"/dsa/{pid}/status",
                           json={"status": "solved", "code": "x"})
    assert r.status_code == 200
    rows = _ledger(app, uid, "dsa_solved")
    assert len(rows) == 1 and rows[0].xp == 25


def test_hook_skill_quiz_submit_mints_xp(authed_client, app):
    from studybuddy.skill_quizzes import get_question, quiz_skills
    uid = _uid(app)
    skill = quiz_skills()[0][0]
    r = authed_client.post("/learn/quizzes/start",
                           json={"skill": skill, "num_questions": 3,
                                 "difficulties": [], "timed": False})
    assert r.status_code == 200
    data = r.get_json()
    answers = {q["id"]: get_question(q["id"])["answer"]
               for q in data["questions"]}
    r = authed_client.post(f"/learn/quizzes/{data['attempt_id']}/submit",
                           json={"answers": answers})
    assert r.status_code == 200
    rows = _ledger(app, uid, "quiz_completed")
    assert len(rows) == 1 and rows[0].xp == 20


def test_hook_roadmap_step_only_on_completed(authed_client, app):
    from studybuddy.learning_roadmaps import list_skills
    uid = _uid(app)
    skill = list_skills()[0]["id"]
    r = authed_client.post("/learn/roadmaps/progress",
                           json={"skill_id": skill, "stage": "beginner",
                                 "status": "in_progress"})
    assert r.status_code == 200
    assert _ledger(app, uid, "roadmap_step") == []
    r = authed_client.post("/learn/roadmaps/progress",
                           json={"skill_id": skill, "stage": "beginner",
                                 "status": "completed"})
    assert r.status_code == 200
    assert len(_ledger(app, uid, "roadmap_step")) == 1


def test_hook_coach_analyze_mints_xp(authed_client, app):
    uid = _uid(app)
    text = ("In my previous role I led a team of four engineers. "
            "We rebuilt the reporting pipeline over six weeks. "
            "I coordinated daily standups and unblocked dependencies early. "
            "The new pipeline cut report generation time by half.")
    r = authed_client.post("/coach/analyze", json={"text": text})
    assert r.status_code == 200
    assert len(_ledger(app, uid, "coach_analyzed")) == 1


def test_hook_aptitude_submit_mints_xp(authed_client, app):
    uid = _uid(app)
    r = authed_client.post("/aptitude/start",
                           json={"category": "mixed",
                                 "num_questions": 5, "time_limit": 300})
    assert r.status_code == 200, r.get_data(as_text=True)
    aid = r.get_json()["attempt_id"]
    r = authed_client.post(f"/aptitude/{aid}/submit", json={"answers": {}})
    assert r.status_code == 200
    assert len(_ledger(app, uid, "aptitude_completed")) == 1


def test_hook_interview_submit_mints_xp(authed_client, app):
    uid = _uid(app)
    r = authed_client.post("/interview/start",
                           json={"company": "Google", "role": "sde",
                                 "category": "behavioral", "num_questions": 2})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    data = r.get_json()
    sid = data["session_id"]
    answers = [{"question_id": q["id"],
                "answer": "Situation: the service was failing. Action: I triaged logs and rolled back. "
                          "Result: recovered in 20 minutes, errors down 90%. I weighed the trade-off "
                          "between speed and safety because haste risks data loss. Edge cases validated."}
               for q in data["questions"]]
    r = authed_client.post(f"/interview/{sid}/submit",
                           json={"answers": answers})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert len(_ledger(app, uid, "mock_completed")) == 1


# ---------------------------------------------------------------------------
# Dashboard widget
# ---------------------------------------------------------------------------

def test_dashboard_widget_renders(authed_client, app):
    uid = _uid(app)
    with app.app_context():
        momentum.record_event(uid, "quiz_completed", now=_dt(2026, 10, 14))
    r = authed_client.get("/dashboard")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "momentum" in html.lower()
    assert "Open hub" in html
