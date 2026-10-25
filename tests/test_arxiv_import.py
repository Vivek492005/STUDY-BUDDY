"""Unit tests for arxiv_import.py (Worker B, #25). All network mocked.

Live-network tests are skipped unless STUDY_LIVE_TESTS=1.
No route tests here — logic module only.
"""
import os

import pytest

from studybuddy import arxiv_import
from studybuddy.arxiv_import import (
    cache_get,
    cache_set,
    download_pdf,
    fetch_arxiv,
    fetch_citation_graph,
    fetch_doi,
    normalize_metadata,
    validate_arxiv_id,
    validate_doi,
)

LIVE = os.environ.get("STUDY_LIVE_TESTS") == "1"
needs_live = pytest.mark.skipif(not LIVE, reason="needs real network (set STUDY_LIVE_TESTS=1)")

SAMPLE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v2</id>
    <updated>2017-12-06T00:00:00Z</updated>
    <published>2017-06-12T00:00:00Z</published>
    <title>Attention Is All You Need</title>
    <summary>  The dominant sequence transduction models are based on complex
recurrent or convolutional neural networks. We propose the Transformer.  </summary>
    <author><name>Vaswani, Ashish</name></author>
    <author><name>Shazeer, Noam</name></author>
    <arxiv:doi>10.5555/3295222.3295349</arxiv:doi>
    <arxiv:journal_ref>Advances in Neural Information Processing Systems 30 (2017)</arxiv:journal_ref>
    <link href="http://arxiv.org/abs/1706.03762v2" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/1706.03762v2" rel="related" type="application/pdf"/>
    <arxiv:primary_category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
</feed>"""

EMPTY_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom"></feed>"""

SAMPLE_OPENALEX_WORK = {
    "id": "https://openalex.org/W2741809807",
    "doi": "https://doi.org/10.1038/nature12373",
    "title": "A sample research paper on everything",
    "publication_year": 2021,
    "type": "article",
    "cited_by_count": 42,
    "abstract_inverted_index": {"The": [0], "study": [1], "shows": [2], "results": [3]},
    "authorships": [
        {"author": {"display_name": "Alice Researcher"}},
        {"author": {"display_name": "Bob Scientist"}},
    ],
    "primary_location": {
        "pdf_url": "https://example.org/sample.pdf",
        "source": {"display_name": "Nature"},
    },
    "best_oa_location": {"pdf_url": "https://example.org/sample-oa.pdf"},
    "ids": {
        "doi": "https://doi.org/10.1038/nature12373",
        "openalex": "https://openalex.org/W2741809807",
        "arxiv": "http://arxiv.org/abs/2101.00001",
    },
    "referenced_works": [
        "https://openalex.org/W1",
        "https://openalex.org/W2",
        "https://openalex.org/W3",
    ],
}


class FakeResp:
    def __init__(self, status=200, content=b"", json_data=None, headers=None):
        self.status_code = status
        self.content = content
        self._json = json_data
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _fake_get_factory(responses):
    """responses: list of FakeResp (or callables url->FakeResp), consumed in order."""
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        calls.append({"url": url, "params": params})
        item = responses.pop(0) if responses else FakeResp(404)
        return item(url) if callable(item) else item

    fake_get.calls = calls
    return fake_get


def _work_summary(wid, title="Cited paper", cited_by_count=5, doi=None):
    return {
        "id": f"https://openalex.org/{wid}",
        "title": title,
        "publication_year": 2020,
        "cited_by_count": cited_by_count,
        "doi": doi,
        "authorships": [{"author": {"display_name": "Cite Author"}}],
        "ids": {"doi": f"https://doi.org/{doi}"} if doi else {},
    }


# ---------------------------------------------------------------- validate_arxiv_id
@pytest.mark.parametrize(
    "inp,expected",
    [
        ("arXiv:1706.03762v2", "1706.03762"),
        ("1706.03762", "1706.03762"),
        ("1706.037621234", None),  # too long
        ("hep-th/9901001", "hep-th/9901001"),
        ("cond-mat/0101001v3", "cond-mat/0101001"),
        ("https://arxiv.org/abs/1706.03762", "1706.03762"),
        ("https://arxiv.org/pdf/1706.03762v1.pdf", "1706.03762"),
        ("  1706.03762  ", "1706.03762"),
        ("", None),
        (None, None),
        ("not-an-id", None),
        ("abc", None),            # garbage
        ("1234.567", None),       # wrong digit groups
        ("9913.12345", None),     # month 13 invalid
        ("1700.12345", None),     # month 00 invalid
        ("17.03762", None),
        ("1706.0376", "1706.0376"),  # 4-digit suffix is valid arXiv format
    ],
)
def test_validate_arxiv_id(inp, expected):
    assert validate_arxiv_id(inp) == expected


# ---------------------------------------------------------------- validate_doi
@pytest.mark.parametrize(
    "inp,expected",
    [
        ("doi:10.1038/nature12373", "10.1038/nature12373"),
        ("10.1038/nature12373", "10.1038/nature12373"),
        ("https://doi.org/10.1038/nature12373", "10.1038/nature12373"),
        ("http://dx.doi.org/10.1109/5.771073", "10.1109/5.771073"),
        ("DOI:10.1038/NATURE12373", "10.1038/NATURE12373"),
        ("", None),
        (None, None),
        ("not a doi", None),
        ("abc", None),          # garbage
        ("10.123", None),       # no registrant/suffix split
        ("1234.567", None),     # not a DOI prefix
        ("10.1038", None),
        ("11.1038/nature12373", None),
    ],
)
def test_validate_doi(inp, expected):
    assert validate_doi(inp) == expected


# ---------------------------------------------------------------- fetch_arxiv (mocked)
def test_fetch_arxiv_parses_atom(monkeypatch):
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        calls.append({"url": url, "params": params})
        if "export.arxiv.org" in url:
            return FakeResp(200, content=SAMPLE_ATOM.encode(), headers={"Content-Type": "application/atom+xml"})
        return FakeResp(200, json_data={"results": []})

    monkeypatch.setattr(arxiv_import.requests, "get", fake_get)
    out = fetch_arxiv("1706.03762")
    assert "error" not in out
    assert out["title"] == "Attention Is All You Need"
    assert out["authors"] == ["Vaswani, Ashish", "Shazeer, Noam"]
    assert out["year"] == 2017
    assert out["abstract"].startswith("The dominant sequence")
    assert out["doi"] == "10.5555/3295222.3295349"
    assert out["arxiv_id"] == "1706.03762"
    assert out["pdf_url"] == "http://arxiv.org/pdf/1706.03762v2"
    assert out["source"] == "arxiv"
    # first call really went to the arXiv API with our id
    assert "export.arxiv.org" in calls[0]["url"]
    assert calls[0]["params"]["id_list"] == "1706.03762"


def test_fetch_arxiv_invalid_id_no_network(monkeypatch):
    def boom(url, **kw):
        raise AssertionError("no network should happen")

    monkeypatch.setattr(arxiv_import.requests, "get", boom)
    assert "error" in fetch_arxiv("bogus")


def test_fetch_arxiv_empty_feed(monkeypatch):
    monkeypatch.setattr(
        arxiv_import.requests, "get",
        lambda url, **kw: FakeResp(200, content=EMPTY_ATOM.encode()),
    )
    out = fetch_arxiv("1706.03762")
    assert "error" in out


def test_fetch_arxiv_http_error(monkeypatch):
    monkeypatch.setattr(arxiv_import.requests, "get", lambda url, **kw: FakeResp(500))
    out = fetch_arxiv("1706.03762")
    assert out["error"] == "arXiv returned HTTP 500"


def test_fetch_arxiv_request_exception(monkeypatch):
    import requests as rq

    def boom(url, **kw):
        raise rq.ConnectionError("down")

    monkeypatch.setattr(arxiv_import.requests, "get", boom)
    out = fetch_arxiv("1706.03762")
    assert "error" in out and "failed" in out["error"]


# ---------------------------------------------------------------- fetch_doi (mocked)
def test_fetch_doi_parses_openalex(monkeypatch):
    monkeypatch.setattr(
        arxiv_import.requests, "get",
        lambda url, **kw: FakeResp(200, json_data=SAMPLE_OPENALEX_WORK),
    )
    out = fetch_doi("10.1038/nature12373")
    assert "error" not in out
    assert out["title"] == "A sample research paper on everything"
    assert out["authors"] == ["Alice Researcher", "Bob Scientist"]
    assert out["abstract"] == "The study shows results"
    assert out["year"] == 2021
    assert out["venue"] == "Nature"
    assert out["doi"] == "10.1038/nature12373"
    assert out["arxiv_id"] == "2101.00001"
    assert out["pdf_url"] == "https://example.org/sample-oa.pdf"
    assert out["openalex_id"] == "W2741809807"
    assert out["source"] == "openalex"


def test_fetch_doi_404(monkeypatch):
    monkeypatch.setattr(arxiv_import.requests, "get", lambda url, **kw: FakeResp(404))
    out = fetch_doi("10.1038/nature12373")
    assert "error" in out


def test_fetch_doi_invalid_no_network(monkeypatch):
    def boom(url, **kw):
        raise AssertionError("no network should happen")

    monkeypatch.setattr(arxiv_import.requests, "get", boom)
    assert "error" in fetch_doi("garbage")


# ---------------------------------------------------------------- normalize_metadata
def test_normalize_metadata_exact_keys():
    from datetime import datetime

    raw = {"title": "  Some   Title ", "authors": ["Ada Lovelace"], "year": "2020",
           "source": "arxiv", "extra": "dropped"}
    out = normalize_metadata(raw)
    assert set(out.keys()) == {
        "title", "authors", "abstract", "year", "venue", "doi",
        "arxiv_id", "pdf_url", "openalex_id", "url", "source", "fetched_at",
        "is_retracted", "oa_version",
    }
    assert out["title"] == "Some Title"
    assert out["year"] == 2020
    assert out["abstract"] is None
    assert out["authors"] == ["Ada Lovelace"]
    assert out["source"] == "arxiv"
    assert "extra" not in out
    # provenance: fetched_at auto-added as UTC ISO timestamp
    datetime.fromisoformat(out["fetched_at"])


def test_normalize_metadata_preserves_fetched_at():
    out = normalize_metadata({"fetched_at": "2026-01-01T00:00:00+00:00"})
    assert out["fetched_at"] == "2026-01-01T00:00:00+00:00"


def test_normalize_metadata_missing_everything():
    out = normalize_metadata({})
    assert out["authors"] == []
    assert out["year"] is None
    assert out["source"] is None


def test_normalize_metadata_passthrough_error():
    out = normalize_metadata({"error": "boom"})
    assert out == {"error": "boom"}


def test_normalize_metadata_bad_year():
    assert normalize_metadata({"year": "notayear"})["year"] is None
    assert normalize_metadata({"year": None})["year"] is None


# ---------------------------------------------------------------- cache
def test_cache_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(arxiv_import, "_CACHE_DIR", str(tmp_path))
    assert cache_set("k1", {"a": 1}) is True
    assert cache_get("k1") == {"a": 1}
    assert cache_get("missing") is None
    assert cache_get("missing", default="dflt") == "dflt"


def test_cache_ttl_expiry(monkeypatch, tmp_path):
    monkeypatch.setattr(arxiv_import, "_CACHE_DIR", str(tmp_path))
    assert cache_set("k2", "val", ttl=-1) is True  # already expired
    assert cache_get("k2") is None


def test_cache_corrupt_file(monkeypatch, tmp_path):
    monkeypatch.setattr(arxiv_import, "_CACHE_DIR", str(tmp_path))
    (tmp_path / "x.json").write_text("not json {{{")
    # any key hashing to that file would be a fluke; just exercise default path
    assert cache_get("some-key") is None


# ---------------------------------------------------------------- citation graph (mocked)
def test_fetch_citation_graph(monkeypatch):
    work = dict(SAMPLE_OPENALEX_WORK)
    out_details = [_work_summary(f"W{i}", title=f"Ref {i}", cited_by_count=i) for i in range(1, 4)]
    in_details = [_work_summary("W100", title="Citing paper", cited_by_count=99)]

    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        if url.endswith("/works/W2741809807"):
            return FakeResp(200, json_data=work)
        filt = (params or {}).get("filter", "")
        if filt.startswith("openalex:"):
            return FakeResp(200, json_data={"results": out_details})
        if filt.startswith("cites:"):
            return FakeResp(200, json_data={"results": in_details})
        return FakeResp(404)

    monkeypatch.setattr(arxiv_import.requests, "get", fake_get)
    graph = fetch_citation_graph("W2741809807", max_nodes=25)
    assert "error" not in graph
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert nodes["W2741809807"]["is_center"] is True
    assert "W1" in nodes and "W100" in nodes
    edge_set = {(e["from"], e["to"]) for e in graph["edges"]}
    assert ("W2741809807", "W1") in edge_set      # center -> referenced
    assert ("W100", "W2741809807") in edge_set    # citer -> center
    for n in graph["nodes"]:
        assert set(n.keys()) >= {"id", "title", "authors", "year", "cited_by_count", "is_center"}


def test_fetch_citation_graph_node_cap(monkeypatch):
    work = dict(SAMPLE_OPENALEX_WORK)
    work["referenced_works"] = [f"https://openalex.org/W{i}" for i in range(100)]
    out_details = [_work_summary(f"W{i}") for i in range(100)]
    in_details = [_work_summary(f"C{i}") for i in range(100)]

    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        if url.endswith("/works/W2741809807"):
            return FakeResp(200, json_data=work)
        filt = (params or {}).get("filter", "")
        if filt.startswith("openalex:"):
            return FakeResp(200, json_data={"results": out_details})
        if filt.startswith("cites:"):
            return FakeResp(200, json_data={"results": in_details})
        return FakeResp(404)

    monkeypatch.setattr(arxiv_import.requests, "get", fake_get)
    graph = fetch_citation_graph("W2741809807", max_nodes=10)
    assert len(graph["nodes"]) <= 10


def test_fetch_citation_graph_bad_id_no_network(monkeypatch):
    def boom(url, **kw):
        raise AssertionError("no network should happen")

    monkeypatch.setattr(arxiv_import.requests, "get", boom)
    assert "error" in fetch_citation_graph("")


# ---------------------------------------------------------------- download_pdf (mocked)
def _stub_fetch(monkeypatch, resp):
    """Stub the SEC-02 fetch seam: no DNS, no network."""
    monkeypatch.setattr(arxiv_import, "validate_url", lambda url: True)
    monkeypatch.setattr(arxiv_import, "safe_fetch_stream", lambda url, **kw: resp)

class _StreamResp:
    def __init__(self, chunks, ctype="application/pdf", status=200):
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, chunk_size=65536):
        return iter(self._chunks)


def test_download_pdf_ok(monkeypatch, tmp_path):
    pdf = b"%PDF-1.4 fake content"
    _stub_fetch(monkeypatch, _StreamResp([pdf]))
    dest = str(tmp_path / "paper.pdf")
    assert download_pdf("https://example.org/x.pdf", dest) is True
    assert open(dest, "rb").read() == pdf


def test_download_pdf_wrong_content_type(monkeypatch, tmp_path):
    _stub_fetch(monkeypatch, _StreamResp([b"<html>"], ctype="text/html"))
    assert download_pdf("https://example.org/x", str(tmp_path / "x.pdf")) is False


def test_download_pdf_too_big(monkeypatch, tmp_path):
    _stub_fetch(monkeypatch, _StreamResp([b"x" * (31 * 1024 * 1024)]))
    dest = str(tmp_path / "big.pdf")
    assert download_pdf("https://example.org/x.pdf", dest) is False


# ---------------------------------------------------------------- live smoke tests
@needs_live
def test_live_arxiv_attention():
    out = normalize_metadata(fetch_arxiv("1706.03762"))
    assert "error" not in out
    assert "Attention Is All You Need" in out["title"]


@needs_live
def test_live_openalex_doi():
    out = normalize_metadata(fetch_doi("10.1038/nature12373"))
    assert "error" not in out
    assert out["title"]


# ---------------------------------------------------------------- zero-error protocol: negative paths
import requests as _rq  # noqa: E402


def test_fetch_arxiv_timeout_returns_error(monkeypatch):
    def boom(url, **kw):
        raise _rq.Timeout("timed out")

    monkeypatch.setattr(arxiv_import.requests, "get", boom)
    out = fetch_arxiv("1706.03762")
    assert "error" in out and "failed" in out["error"]


def test_fetch_arxiv_429_returns_error_not_retried(monkeypatch):
    calls = []

    def fake(url, **kw):
        calls.append(url)
        return FakeResp(429)

    monkeypatch.setattr(arxiv_import.requests, "get", fake)
    out = fetch_arxiv("1706.03762")
    assert out["error"] == "arXiv returned HTTP 429"
    assert len(calls) == 1  # HTTP errors are not retried


def test_fetch_arxiv_malformed_xml(monkeypatch):
    monkeypatch.setattr(
        arxiv_import.requests, "get",
        lambda url, **kw: FakeResp(200, content=b"<not xml at all <"),
    )
    out = fetch_arxiv("1706.03762")
    assert "error" in out and "parse" in out["error"]


def test_retry_then_success(monkeypatch):
    """Two network failures then success: max-2-retries recovers cleanly."""
    calls = []

    def flaky(url, **kw):
        calls.append(url)
        if len(calls) < 3 and "export.arxiv.org" in url:
            raise _rq.ConnectionError("flaky")
        if "export.arxiv.org" in url:
            return FakeResp(200, content=SAMPLE_ATOM.encode())
        return FakeResp(200, json_data={"results": []})

    monkeypatch.setattr(arxiv_import.requests, "get", flaky)
    out = fetch_arxiv("1706.03762")
    assert "error" not in out
    assert out["title"] == "Attention Is All You Need"
    assert sum(1 for c in calls if "export.arxiv.org" in c) == 3


def test_no_retry_on_http_500(monkeypatch):
    calls = []

    def fake(url, **kw):
        calls.append(url)
        return FakeResp(500)

    monkeypatch.setattr(arxiv_import.requests, "get", fake)
    assert fetch_arxiv("1706.03762")["error"] == "arXiv returned HTTP 500"
    assert len(calls) == 1


def test_fetch_doi_timeout(monkeypatch):
    monkeypatch.setattr(
        arxiv_import.requests, "get",
        lambda url, **kw: (_ for _ in ()).throw(_rq.Timeout("t")),
    )
    out = fetch_doi("10.1038/nature12373")
    assert "error" in out


def test_fetch_doi_500(monkeypatch):
    monkeypatch.setattr(arxiv_import.requests, "get", lambda url, **kw: FakeResp(500))
    out = fetch_doi("10.1038/nature12373")
    assert out["error"] == "OpenAlex returned HTTP 500"


def test_fetch_doi_429(monkeypatch):
    monkeypatch.setattr(arxiv_import.requests, "get", lambda url, **kw: FakeResp(429))
    out = fetch_doi("10.1038/nature12373")
    assert out["error"] == "OpenAlex returned HTTP 429"


def test_fetch_doi_connection_error(monkeypatch):
    def boom(url, **kw):
        raise _rq.ConnectionError("down")

    monkeypatch.setattr(arxiv_import.requests, "get", boom)
    out = fetch_doi("10.1038/nature12373")
    assert "error" in out


def test_fetch_doi_malformed_json(monkeypatch):
    monkeypatch.setattr(
        arxiv_import.requests, "get",
        lambda url, **kw: FakeResp(200, json_data=None),  # .json() raises ValueError
    )
    out = fetch_doi("10.1038/nature12373")
    assert "error" in out and "parse" in out["error"]


def test_fetch_arxiv_title_mismatch_logs_warning(monkeypatch, caplog):
    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        if "export.arxiv.org" in url:
            return FakeResp(200, content=SAMPLE_ATOM.encode())
        # OpenAlex URL-form resolution: GET /works/https://arxiv.org/abs/<id>
        if url.endswith("/works/https://arxiv.org/abs/1706.03762"):
            return FakeResp(200, json_data={"id": "https://openalex.org/W9"})
        if url.endswith("/works/W9"):
            return FakeResp(200, json_data={
                "id": "https://openalex.org/W9",
                "title": "A Completely Unrelated Different Paper On Something Else",
            })
        return FakeResp(404)

    monkeypatch.setattr(arxiv_import.requests, "get", fake_get)
    with caplog.at_level("WARNING", logger="arxiv_import"):
        out = fetch_arxiv("1706.03762")
    assert "error" not in out
    assert out["openalex_id"] == "W9"
    assert "mismatch" in caplog.text.lower()
    assert "1706.03762" in caplog.text


def test_fetch_arxiv_title_match_no_warning(monkeypatch, caplog):
    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        if "export.arxiv.org" in url:
            return FakeResp(200, content=SAMPLE_ATOM.encode())
        # OpenAlex URL-form resolution: GET /works/https://arxiv.org/abs/<id>
        if url.endswith("/works/https://arxiv.org/abs/1706.03762"):
            return FakeResp(200, json_data={"id": "https://openalex.org/W9"})
        if url.endswith("/works/W9"):
            return FakeResp(200, json_data={
                "id": "https://openalex.org/W9",
                "title": "Attention Is All You Need",
            })
        return FakeResp(404)

    monkeypatch.setattr(arxiv_import.requests, "get", fake_get)
    with caplog.at_level("WARNING", logger="arxiv_import"):
        out = fetch_arxiv("1706.03762")
    assert "error" not in out
    assert "mismatch" not in caplog.text.lower()


def test_fetch_citation_graph_empty(monkeypatch, tmp_path):
    """Paper with no references and no citers -> center node only."""
    monkeypatch.setattr(arxiv_import, "_CACHE_DIR", str(tmp_path))
    work = dict(SAMPLE_OPENALEX_WORK, referenced_works=[])

    def fake_get(url, params=None, **kw):
        if url.endswith("/works/W2741809807"):
            return FakeResp(200, json_data=work)
        return FakeResp(200, json_data={"results": []})

    monkeypatch.setattr(arxiv_import.requests, "get", fake_get)
    graph = fetch_citation_graph("W2741809807")
    assert "error" not in graph
    assert len(graph["nodes"]) == 1
    assert graph["nodes"][0]["is_center"] is True
    assert graph["edges"] == []


def test_fetch_citation_graph_429_uses_cached_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(arxiv_import, "_CACHE_DIR", str(tmp_path))
    cached = {"nodes": [{"id": "W9", "title": "Cached", "authors": [],
                         "year": 2020, "cited_by_count": 3, "is_center": True}],
              "edges": []}
    assert cache_set("citation_graph:W9", cached) is True
    monkeypatch.setattr(arxiv_import.requests, "get", lambda url, **kw: FakeResp(429))
    graph = fetch_citation_graph("W9")
    assert "error" not in graph
    assert graph.get("from_cache") is True
    assert graph["nodes"] == cached["nodes"]


def test_fetch_citation_graph_500_no_cache_is_error(monkeypatch, tmp_path):
    monkeypatch.setattr(arxiv_import, "_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(arxiv_import.requests, "get", lambda url, **kw: FakeResp(500))
    out = fetch_citation_graph("W9")
    assert out["error"] == "OpenAlex returned HTTP 500"


def test_fetch_citation_graph_timeout_no_cache_is_error(monkeypatch, tmp_path):
    monkeypatch.setattr(arxiv_import, "_CACHE_DIR", str(tmp_path))

    def boom(url, **kw):
        raise _rq.Timeout("t")

    monkeypatch.setattr(arxiv_import.requests, "get", boom)
    out = fetch_citation_graph("W9")
    assert "error" in out and "failed" in out["error"]


def test_fetch_citation_graph_caches_success(monkeypatch, tmp_path):
    monkeypatch.setattr(arxiv_import, "_CACHE_DIR", str(tmp_path))
    work = dict(SAMPLE_OPENALEX_WORK, referenced_works=[])

    def fake_get(url, params=None, **kw):
        if url.endswith("/works/W2741809807"):
            return FakeResp(200, json_data=work)
        return FakeResp(200, json_data={"results": []})

    monkeypatch.setattr(arxiv_import.requests, "get", fake_get)
    graph = fetch_citation_graph("W2741809807")
    assert "error" not in graph
    cached = cache_get("citation_graph:W2741809807")
    assert cached is not None
    assert cached["nodes"][0]["id"] == "W2741809807"


def test_fetch_citation_graph_dedupes_nodes(monkeypatch, tmp_path):
    """Same OpenAlex id appearing as both a reference and a citer -> one node."""
    monkeypatch.setattr(arxiv_import, "_CACHE_DIR", str(tmp_path))
    work = dict(SAMPLE_OPENALEX_WORK,
                referenced_works=["https://openalex.org/W1"])
    shared = _work_summary("W1", title="Shared paper")

    def fake_get(url, params=None, **kw):
        if url.endswith("/works/W2741809807"):
            return FakeResp(200, json_data=work)
        filt = (params or {}).get("filter", "")
        if filt.startswith("openalex:"):
            return FakeResp(200, json_data={"results": [shared]})
        if filt.startswith("cites:"):
            return FakeResp(200, json_data={"results": [shared]})
        return FakeResp(404)

    monkeypatch.setattr(arxiv_import.requests, "get", fake_get)
    graph = fetch_citation_graph("W2741809807")
    ids = [n["id"] for n in graph["nodes"]]
    assert ids.count("W1") == 1


def test_download_pdf_http_error(monkeypatch, tmp_path):
    _stub_fetch(monkeypatch, _StreamResp([], status=404))
    dest = str(tmp_path / "x.pdf")
    assert download_pdf("https://example.org/x.pdf", dest) is False
    assert not os.path.exists(dest)


def test_download_pdf_midstream_error_cleans_up(monkeypatch, tmp_path):
    class Flaky(_StreamResp):
        def iter_content(self, chunk_size=65536):
            yield b"%PDF-1.4 partial"
            raise _rq.ConnectionError("dropped")

    _stub_fetch(monkeypatch, Flaky([b"unused"], ctype="application/pdf"))
    dest = str(tmp_path / "partial.pdf")
    assert download_pdf("https://example.org/x.pdf", dest) is False
    assert not os.path.exists(dest)  # partial file cleaned up


def test_download_pdf_empty_url():
    assert download_pdf("", "/tmp/x.pdf") is False
    assert download_pdf("https://example.org/x.pdf", "") is False


def test_cache_set_unserializable():
    assert cache_set("bad", object()) is False


def test_download_pdf_rejects_ssrf_url(tmp_path):
    """SEC-02: download_pdf must refuse private/metadata URLs (no mock)."""
    dest = str(tmp_path / "evil.pdf")
    assert download_pdf("http://169.254.169.254/latest/meta-data/", dest) is False
    assert download_pdf("http://127.0.0.1:8080/x.pdf", dest) is False
    assert not os.path.exists(dest)
