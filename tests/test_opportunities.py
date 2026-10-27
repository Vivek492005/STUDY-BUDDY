"""Tests for Wave 1 feature #9: opportunity radar."""
import json
from datetime import date

import pytest
from markupsafe import escape

from studybuddy.opportunities import (
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


def _bm(client, opp_id="gate-2027", remove=False):
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
    # 68 after the Phase 3 refresh (27 + 41 verified additions,
    # including the first curated competitions).
    assert len(curated) == 68
    assert set(o["type"] for o in curated) == {
        "internship", "scholarship", "exam", "hackathon", "competition"}
    # auto-refresh may have added hackathon/competition/internship entries;
    # every type in the file must still be a known type.
    from studybuddy.opportunities import TYPES
    assert set(o["type"] for o in data["opportunities"]) <= set(TYPES)
    import re
    # last_updated changes whenever the auto-refresh runs; just check format.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", data["meta"]["last_updated"])


def test_closing_soon_logic_fixed_today():
    opps = list_opportunities(today=date(2026, 10, 14))
    by_id = {o["id"]: o for o in opps}
    assert by_id["gate-2027"]["closing_soon"] is True       # 6 days
    assert by_id["gate-2027"]["days_left"] == 6
    # (ms-engage-2026 was removed in the 2026-10-24 refresh as
    # unverifiable; Build With AI: Basics is the 12-day case now.)
    assert by_id["devpost-30447-build-with-ai-basics"]["closing_soon"] is True  # 12 days
    assert by_id["reliance-foundation-ug-2026"]["closing_soon"] is True  # 14 days, inclusive
    assert by_id["aicte-pragati-2026"]["closing_soon"] is False          # 17 days
    assert by_id["upsc-cse-2027"]["closing_soon"] is False


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
    # Curated counts after the Phase 3 refresh (verified from the file):
    # 41 real additions, including the first curated competitions.
    assert counts.get("exam") == 25
    assert counts.get("scholarship") == 20
    assert counts.get("internship") == 13
    assert counts.get("hackathon") == 4
    assert counts.get("competition") == 6
    # filtered listing returns only that type (auto entries included)
    for o in list_opportunities(opp_type="hackathon"):
        assert o["type"] == "hackathon"
    assert list_opportunities(opp_type="lottery") == []


def test_opp_exists():
    assert opp_exists("gate-2027") is True
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
    register(client, email="a@a.com")
    login(client, email="a@a.com")
    _bm(client, opp_id="gate-2027")
    # second user (logout is POST-only)
    client.post("/logout", follow_redirects=False)
    register(client, name="Bee", email="b@b.com")
    login(client, email="b@b.com")
    html = client.get("/opportunities").get_data(as_text=True)
    assert "★ Saved" not in html  # B sees nothing saved
    assert _bm(client, opp_id="gate-2027", remove=True).status_code == 404  # can't remove A's
    with app.app_context():
        assert OpportunityBookmark.query.count() == 1
