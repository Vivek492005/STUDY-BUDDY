"""Tests for Wave 1 feature #9: opportunity radar."""
import json
from datetime import date, timedelta

import pytest
from markupsafe import escape

from studybuddy.opportunities import (
    CLOSING_SOON_DAYS,
    get_opportunity,
    list_opportunities,
    load_dataset,
    opp_exists,
)
from tests.conftest import login, register
from unified_app import OpportunityBookmark


@pytest.fixture()
def authed_client(client):
    register(client)
    login(client)
    return client


def _bm(client, opp_id="no-such-opp", remove=False):
    url = "/opportunities/bookmark/remove" if remove else "/opportunities/bookmark"
    return client.post(url, data=json.dumps({"opp_id": opp_id}),
                       content_type="application/json")


def _board_picks():
    """One live exam + one live internship from the board's own data source.

    Board tests must never hardcode a specific listing: curated deadlines
    pass (GATE 2027 closed 2026-10-20) and the board correctly hides
    expired entries, so a hardcoded title turns the suite red on a
    calendar date instead of on a code change.
    """
    exam = list_opportunities(opp_type="exam")[0]
    internship = list_opportunities(opp_type="internship")[0]
    return exam, internship


def test_dataset_loads():
    data = load_dataset()
    curated = [o for o in data["opportunities"] if o.get("curated", True)]
    # Counts move with every weekly refresh (49 curated after the
    # 2026-11-16 refresh, down from 68 — expired entries pruned), so
    # assert a substantial set, not an exact snapshot: this is a
    # tripwire against an emptied dataset, not a census.
    assert len(curated) >= 20
    assert set(o["type"] for o in curated) <= {
        "internship", "scholarship", "exam", "hackathon", "competition"}
    # auto-refresh may have added hackathon/competition/internship entries;
    # every type in the file must still be a known type.
    from studybuddy.opportunities import TYPES
    assert set(o["type"] for o in data["opportunities"]) <= set(TYPES)
    import re
    # last_updated changes whenever the auto-refresh runs; just check format.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", data["meta"]["last_updated"])


def test_closing_soon_logic_fixed_today():
    # No hardcoded listings: the weekly refresh prunes expired entries
    # (GATE 2027, Build With AI, Reliance UG, AICTE Pragati all left in
    # the 2026-11-16 refresh), so the cases derive from whatever dated
    # entries the dataset currently holds.
    assert CLOSING_SOON_DAYS == 14  # the pinned spec: 14-day window
    dated = [o for o in load_dataset()["opportunities"] if o.get("deadline")]
    assert len(dated) >= 2
    first = min(dated, key=lambda o: o["deadline"])
    dl = date.fromisoformat(first["deadline"])
    # The 14-day boundary is inclusive.
    by_id = {o["id"]: o for o in
             list_opportunities(today=dl - timedelta(days=14))}
    assert by_id[first["id"]]["closing_soon"] is True
    assert by_id[first["id"]]["days_left"] == 14
    by_id = {o["id"]: o for o in
             list_opportunities(today=dl - timedelta(days=15))}
    assert by_id[first["id"]]["closing_soon"] is False
    assert by_id[first["id"]]["days_left"] == 15
    # The rule holds for every entry at an arbitrary today.
    today = dl - timedelta(days=6)
    listed = list_opportunities(today=today)
    assert any(o["closing_soon"] for o in listed)
    assert any(not o["closing_soon"] for o in listed)
    for o in listed:
        if o["deadline"] is None:
            assert o["days_left"] is None
            assert o["closing_soon"] is False
            continue
        expected = (date.fromisoformat(o["deadline"]) - today).days
        assert o["days_left"] == expected
        assert o["closing_soon"] is (0 <= expected <= CLOSING_SOON_DAYS)


def test_sorted_by_deadline_nulls_not_present():
    # Curated slice keeps the original deterministic fixture for sort checks.
    data = load_dataset()
    curated_ids = {o["id"] for o in data["opportunities"] if o.get("curated", True)}
    opps = [o for o in list_opportunities(today=date(2026, 10, 14))
            if o["id"] in curated_ids]
    # Rolling entries legitimately carry null deadlines; the sort
    # contract covers dated entries only.
    deadlines = [o["deadline"] for o in opps if o["deadline"] is not None]
    assert deadlines == sorted(deadlines)


def test_type_filter_and_invalid():
    data = load_dataset()
    curated = [o for o in data["opportunities"] if o.get("curated", True)]
    counts = {}
    for o in curated:
        counts[o["type"]] = counts.get(o["type"], 0) + 1
    # Exact per-type counts move with every weekly refresh (exam 25 -> 22
    # in the 2026-11-16 refresh); assert every known type is represented.
    for t in ("exam", "scholarship", "internship", "hackathon", "competition"):
        assert counts.get(t, 0) >= 1, f"no curated {t} entries"
    # filtered listing returns only that type (auto entries included)
    for o in list_opportunities(opp_type="hackathon"):
        assert o["type"] == "hackathon"
    assert list_opportunities(opp_type="lottery") == []


def test_opp_exists():
    live_id = list_opportunities()[0]["id"]
    assert opp_exists(live_id) is True
    assert get_opportunity(live_id) is not None
    assert get_opportunity(live_id)["id"] == live_id
    assert opp_exists("nope") is False
    assert get_opportunity("nope") is None


def test_board_renders_public(client):
    exam, _internship = _board_picks()
    r = client.get("/opportunities")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Opportunity" in html and "Radar" in html
    assert "Curated starter set" in html
    assert str(escape(exam["title"])) in html
    assert "opportunities.py" in html  # schema pointer


def test_board_type_filter(client):
    exam, internship = _board_picks()
    html = client.get("/opportunities?type=exam").get_data(as_text=True)
    assert str(escape(exam["title"])) in html
    assert str(escape(internship["title"])) not in html


def test_board_invalid_type_defaults_to_all(client):
    exam, internship = _board_picks()
    html = client.get("/opportunities?type=hack").get_data(as_text=True)
    assert str(escape(exam["title"])) in html
    assert str(escape(internship["title"])) in html


def test_board_anon_no_bookmark_buttons(client):
    html = client.get("/opportunities").get_data(as_text=True)
    assert '<button class="opp-bm' not in html
    assert "Log in</a> to bookmark" in html


def test_unauth_bookmark_401(client):
    assert _bm(client).status_code == 401
    assert _bm(client, remove=True).status_code == 401


def test_bookmark_add_remove(authed_client, app):
    exam, internship = _board_picks()
    r = _bm(authed_client, opp_id=exam["id"])
    assert r.status_code == 201
    assert r.get_json()["opp_id"] == exam["id"]
    # idempotent re-add
    assert _bm(authed_client, opp_id=exam["id"]).status_code == 201
    with app.app_context():
        assert OpportunityBookmark.query.count() == 1
    # shows as saved on the board
    html = authed_client.get("/opportunities").get_data(as_text=True)
    assert "★ Saved" in html
    # saved-only filter
    html = authed_client.get("/opportunities?saved=1").get_data(as_text=True)
    assert str(escape(exam["title"])) in html
    assert str(escape(internship["title"])) not in html
    # remove
    assert _bm(authed_client, opp_id=exam["id"], remove=True).status_code == 200
    with app.app_context():
        assert OpportunityBookmark.query.count() == 0
    assert _bm(authed_client, remove=True).status_code == 404


def test_bookmark_invalid_opp_404(authed_client):
    assert _bm(authed_client, opp_id="no-such-opp").status_code == 404


def test_bookmark_isolation(client, app):
    exam, _internship = _board_picks()
    register(client, email="a@a.com")
    login(client, email="a@a.com")
    _bm(client, opp_id=exam["id"])
    # second user (logout is POST-only)
    client.post("/logout", follow_redirects=False)
    register(client, name="Bee", email="b@b.com")
    login(client, email="b@b.com")
    html = client.get("/opportunities").get_data(as_text=True)
    assert "★ Saved" not in html  # B sees nothing saved
    assert _bm(client, opp_id=exam["id"], remove=True).status_code == 404  # can't remove A's
    with app.app_context():
        assert OpportunityBookmark.query.count() == 1
