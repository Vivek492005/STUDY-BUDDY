"""Tests for the 2026-10-24 CAREER polish pass.

- Tracker: stale-application follow-up nudges (14+ days in an active
  stage) computed in _tracker_stats and rendered on the board.
- Opportunities: "recently closed" opt-in view + a closing-soon strip
  for bookmarked items.
- Mentors: seed (example) profiles must be honestly labeled — the
  'Verified' badge is reserved for real, team-verified mentors.
"""
import json
from datetime import UTC, datetime, timedelta

import pytest

from studybuddy.opportunities import list_opportunities
from tests.conftest import login, register


@pytest.fixture
def authed_client(client):
    register(client)
    login(client)
    return client


# ---------------------------------------------------------------------------
# Tracker stale nudges
# ---------------------------------------------------------------------------

def _add_app(app, user_id, company, stage, days_ago):
    from unified_app import JobApplication, db
    with app.app_context():
        a = JobApplication(user_id=user_id, company=company,
                           role="SDE Intern", stage=stage)
        a.stage_updated_at = datetime.now(UTC) - timedelta(days=days_ago)
        db.session.add(a)
        db.session.commit()
        return a.id


def test_tracker_stats_flags_stale_applications(authed_client, app):
    from unified_app import User
    with app.app_context():
        uid = User.query.filter_by(email="aarav@example.com").first().id
    _add_app(app, uid, "OldCorp", "applied", 20)
    _add_app(app, uid, "FreshCorp", "applied", 2)
    _add_app(app, uid, "WishCorp", "wishlist", 40)   # wishlist never nudged
    _add_app(app, uid, "DoneCorp", "rejected", 40)   # closed never nudged
    r = authed_client.get("/tracker/stats")
    assert r.status_code == 200
    stale = r.get_json()["stats"]["stale"]
    assert [s["company"] for s in stale] == ["OldCorp"]
    assert stale[0]["days"] >= 14
    board = authed_client.get("/tracker").get_data(as_text=True)
    assert "Follow up" in board and "OldCorp" in board


# ---------------------------------------------------------------------------
# Opportunities: closing-soon strip + recently closed
# ---------------------------------------------------------------------------

def _soon_pick():
    for o in list_opportunities():
        if o["closing_soon"]:
            return o
    return None


def _closed_pick():
    for o in list_opportunities(include_past=True):
        if o["is_past"] and o["days_left"] is not None and o["days_left"] >= -30:
            return o
    return None


def test_closing_soon_strip_for_bookmarked(authed_client):
    pick = _soon_pick()
    if pick is None:
        pytest.skip("no closing-soon opportunity in the current dataset")
    r = authed_client.post("/opportunities/bookmark",
                           data=json.dumps({"opp_id": pick["id"]}),
                           content_type="application/json")
    assert r.status_code == 201
    html = authed_client.get("/opportunities").get_data(as_text=True)
    assert "closing soon" in html
    assert pick["title"] in html


def test_recently_closed_view(authed_client):
    pick = _closed_pick()
    if pick is None:
        pytest.skip("nothing closed in the last 30 days in the dataset")
    html = authed_client.get("/opportunities?closed=1").get_data(as_text=True)
    assert "Recently closed" in html
    assert pick["title"] in html
    # Default board still hides it from the open grid.
    html_open = authed_client.get("/opportunities").get_data(as_text=True)
    assert "Recently closed" not in html_open


# ---------------------------------------------------------------------------
# Mentors: honest example-profile labeling
# ---------------------------------------------------------------------------

def test_seed_mentors_labeled_example_not_verified(authed_client, app):
    html = authed_client.get("/mentors").get_data(as_text=True)
    assert "Illustrative profile" in html
    # The hub explainer distinguishes illustrative seeds from real mentors.
    assert "cannot be booked" in html
    from unified_app import Mentor
    with app.app_context():
        seed = Mentor.query.filter_by(is_seed=True).first()
        assert seed is not None
        profile = authed_client.get(f"/mentors/{seed.id}").get_data(as_text=True)
    assert "Illustrative profile" in profile
    assert "cannot be booked" in profile
