"""Audit Stage 4 (PROD-02): the opportunity board shows its freshness.

Verified present (not re-implemented): the board route already passes
``freshness`` + ``freshness_status`` and the template renders
"Last updated: <date>" plus a Fresh/Stale pill. These tests lock that
behaviour so a future template/route change cannot silently drop the
timestamp the audit asked for. The expected date is read from
data/opportunities.json itself (the source of truth), never hardcoded.

PROD-01 (same audit section) needed no code change: community gates
(studybuddy/prod_gating.py, covered by tests/test_ws7_product.py)
already hide booking/rooms until supply exists, seed profiles are
labelled "Sample profile", and the README mentor row states sample
profiles ship with the app. The remaining PROD-01 action — a real
mentor-recruitment drive — needs real people and stays BLOCKED.
"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_board_shows_last_updated_and_freshness_pill(client):
    resp = client.get("/opportunities")
    assert resp.status_code == 200
    assert b"Last updated:" in resp.data
    assert b"Fresh" in resp.data or b"Stale" in resp.data


def test_refresh_status_api_matches_data_file(client):
    with open(REPO_ROOT / "data" / "opportunities.json",
              encoding="utf-8") as f:
        meta = json.load(f)["meta"]
    body = client.get("/api/opportunities/refresh-status").get_json()
    assert body["success"] is True
    assert body["last_updated"] == meta["last_updated"]
    assert body["last_refresh"] == meta["last_refresh"]
    assert body["total"] == body["open"] + body["closed"]
    assert body["open"] > 0
