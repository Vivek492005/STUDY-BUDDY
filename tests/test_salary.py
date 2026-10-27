"""Tests for Wave 1 feature #8: India salary explorer."""


from studybuddy.salary_data import load_dataset, lookup, query
from tests.conftest import login, register


def test_dataset_loads_and_valid():
    data = load_dataset()
    assert len(data["entries"]) == 408
    assert len(data["roles"]) == 34
    assert data["meta"]["last_updated"] == "2026-10-26"


def test_query_filters():
    rows = query(role="Software Development Engineer", tier="tier1", band="0-1")
    assert len(rows) == 1
    r = rows[0]
    assert r["min_lpa"] == 4.0 and r["median_lpa"] == 8.0 and r["max_lpa"] == 14.0
    assert "Bengaluru" in r["cities"]


def test_query_invalid_filter_returns_empty():
    assert query(role="Astronaut") == []
    assert query(tier="tier9") == []
    assert query(band="99-100") == []


def test_lookup_miss_returns_none():
    assert lookup("Nope", "tier1", "0-1") is None


def test_page_renders_public(client):
    r = client.get("/salary")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Salary Explorer" in html
    assert "Indicative market ranges" in html
    assert "last updated" in html
    # anonymous visitors get the login CTA, not the compare box
    assert "Log in</a> to compare" in html
    assert 'id="cmpVal"' not in html


def test_page_compare_visible_when_authed(client):
    register(client)
    login(client)
    html = client.get("/salary").get_data(as_text=True)
    assert 'id="cmpVal"' in html
    assert "your expected salary" in html


def test_data_api_all(client):
    r = client.get("/salary/data")
    assert r.status_code == 200
    body = r.get_json()
    assert body["success"] is True
    assert len(body["entries"]) == 408


def test_data_api_filtered(client):
    r = client.get("/salary/data", query_string={
        "role": "Data Analyst", "tier": "tier2", "band": "1-3"})
    body = r.get_json()
    assert body["success"] is True
    assert len(body["entries"]) == 1
    e = body["entries"][0]
    assert e["role"] == "Data Analyst"
    assert e["tier_label"] == "Tier 2"
    assert e["min_lpa"] <= e["median_lpa"] <= e["max_lpa"]


def test_data_api_invalid_filter_empty(client):
    r = client.get("/salary/data", query_string={"role": "'; DROP TABLE"})
    assert r.status_code == 200
    assert r.get_json()["entries"] == []


def test_options_embedded_in_page(client):
    html = client.get("/salary").get_data(as_text=True)
    assert "Software Development Engineer" in html
    assert "Tier 1" in html


# ---------- #11: city-level entry-level SDE ranges ----------

def test_city_entries_five_metros_with_provenance():
    from studybuddy.salary_data import city_query
    rows = city_query(role="Software Development Engineer", band="0-1")
    # Phase 2: 20 cities (the original 5 metros must still be present).
    cities = {r["city"] for r in rows}
    assert {"Mumbai", "Delhi NCR", "Bengaluru", "Hyderabad",
            "Chennai"} <= cities
    for r in rows:
        if r["source_url"]:
            # DATA-01 provenance fields on sourced city records
            for field in ("id", "source_url", "retrieved_at", "license",
                          "last_verified", "confidence"):
                assert r[field], (r["city"], field)
        else:
            # Derived rows honestly carry no source: low confidence +
            # an indicative license, never a fabricated provenance.
            assert r["confidence"] == "low"
            assert "Indicative" in r["license"]
        assert r["confidence"] in ("high", "medium", "low")
        assert 0 < r["min_lpa"] <= r["median_lpa"] <= r["max_lpa"]


def test_city_lookup():
    from studybuddy.salary_data import lookup_city
    e = lookup_city("Software Development Engineer", "Bengaluru", "0-1")
    assert e is not None
    assert e["median_lpa"] == 8.5
    assert lookup_city("Software Development Engineer", "Gotham", "0-1") is None
    assert lookup_city("Astronaut", "Mumbai", "0-1") is None
