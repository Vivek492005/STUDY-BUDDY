"""Import-section upgrade tests: dedupe, search merge, one-click full import,
delete, queue, citations+bridges, collections, past-imports render, and the
OpenAlex URL-form resolution for arXiv ids. All hermetic (no network)."""

from unified_app import PaperImport, StudyDoc, User, db


def _authed(client, email="imp@example.com", name="Imp Tester"):
    client.post("/register", data={
        "name": name, "email": email, "password": "Str0ng!Pass1",
        "age_confirm": "1"}, follow_redirects=False)
    client.post("/login", data={"email": email, "password": "Str0ng!Pass1"},
                follow_redirects=False)


def _user_id(app, email="imp@example.com"):
    with app.app_context():
        return User.query.filter_by(email=email).first().id


def _seed_import(app, user_id, **over):
    row = PaperImport(user_id=user_id, title=over.get("title", "Seed Paper"),
                      authors=over.get("authors", ["Ada Lovelace"]),
                      abstract=over.get("abstract", "About neural networks."),
                      year=over.get("year", 2020),
                      venue=over.get("venue", "NeurIPS"),
                      doi=over.get("doi"),
                      arxiv_id=over.get("arxiv_id"),
                      pdf_url=over.get("pdf_url"),
                      openalex_id=over.get("openalex_id"),
                      source=over.get("source", "arxiv"))
    with app.app_context():
        db.session.add(row)
        db.session.commit()
        return row.id


def _pdf_bytes(lines):
    content = b"BT /F1 12 Tf 72 740 Td 14 TL\n"
    for line in lines:
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content += b"(" + safe.encode("latin-1", "ignore") + b") Tj T*\n"
    content += b"ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, xref_pos))
    return out


ARXIV_META = {
    "title": "Attention Is All You Need",
    "authors": ["Ashish Vaswani"], "abstract": "We propose attention.",
    "year": 2017, "venue": "NeurIPS", "doi": None,
    "arxiv_id": "1706.03762", "pdf_url": "https://arxiv.org/pdf/1706.03762",
    "openalex_id": "W2741809807", "url": "https://arxiv.org/abs/1706.03762",
    "source": "arxiv", "fetched_at": "2026-10-14T00:00:00+00:00",
    "is_retracted": False, "oa_version": "publishedVersion",
}


# ---------- past imports render (the bug fix) ----------

def test_import_page_renders_past_imports(client, app):
    _authed(client)
    _seed_import(app, _user_id(app), title="Persistent Paper Title")
    resp = client.get("/research/import")
    assert resp.status_code == 200
    assert b"Persistent Paper Title" in resp.data
    assert b"import-initial" in resp.data
    assert b"in this session" not in resp.data
    assert b"newest first" in resp.data


# ---------- dedupe ----------

def test_arxiv_import_dedupes(client, app, monkeypatch):
    _authed(client)
    monkeypatch.setattr("unified_app.fetch_arxiv",
                        lambda *a, **k: dict(ARXIV_META))
    first = client.post("/api/study/import/arxiv",
                        json={"arxiv_id": "1706.03762"})
    assert first.status_code == 200
    assert first.get_json()["deduped"] is False
    second = client.post("/api/study/import/arxiv",
                         json={"arxiv_id": "1706.03762"})
    assert second.status_code == 200
    assert second.get_json()["deduped"] is True
    assert second.get_json()["import_id"] == first.get_json()["import_id"]
    with app.app_context():
        assert PaperImport.query.filter_by(
            arxiv_id="1706.03762").count() == 1


def test_doi_import_stores_retraction_flags(client, app, monkeypatch):
    _authed(client)
    meta = dict(ARXIV_META, arxiv_id=None, doi="10.1234/retracted",
                is_retracted=True, oa_version="submittedVersion")
    monkeypatch.setattr("unified_app.fetch_doi", lambda *a, **k: dict(meta))
    resp = client.post("/api/study/import/doi",
                       json={"doi": "10.1234/retracted"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["is_retracted"] is True
    assert body["oa_version"] == "submittedVersion"
    with app.app_context():
        p = PaperImport.query.filter_by(doi="10.1234/retracted").first()
        assert p is not None and p.is_retracted is True
        assert p.oa_version == "submittedVersion"


# ---------- delete ----------

def test_delete_owner_and_stranger(client, app):
    _authed(client)
    pid = _seed_import(app, _user_id(app))
    resp = client.delete(f"/api/study/import/{pid}")
    assert resp.status_code == 200 and resp.get_json()["success"] is True
    with app.app_context():
        assert db.session.get(PaperImport, pid) is None
    pid2 = _seed_import(app, _user_id(app))
    # A fresh client: /register while logged in does not switch accounts.
    stranger = app.test_client()
    _authed(stranger, email="other@example.com", name="Other User")
    resp = stranger.delete(f"/api/study/import/{pid2}")
    assert resp.status_code == 404
    with app.app_context():
        assert db.session.get(PaperImport, pid2) is not None


# ---------- search merge (pure) + route ----------

def test_search_papers_merges_sources(monkeypatch):
    from studybuddy import arxiv_import
    oa = [{"title": "OpenAlex Title", "authors": ["A U Thor"],
           "abstract": "OA abstract.", "year": 2021, "venue": "Nature",
           "doi": "10.1/aaa", "arxiv_id": "2101.00001",
           "pdf_url": None, "openalex_id": "W1", "source": "openalex",
           "cited_by_count": 42}]
    ax = [{"title": "arXiv Title", "authors": ["A U Thor"],
           "abstract": "arXiv abstract.", "year": 2021, "venue": None,
           "doi": "10.1/aaa", "arxiv_id": "2101.00001",
           "pdf_url": "https://arxiv.org/pdf/2101.00001",
           "openalex_id": None, "source": "arxiv"}]
    s2 = [{"title": "Unrelated S2 Paper", "authors": ["B U Thor"],
           "abstract": "Other.", "year": 2022, "venue": "Science",
           "doi": "10.2/bbb", "arxiv_id": None, "pdf_url": None,
           "openalex_id": None, "source": "semanticscholar"}]
    monkeypatch.setattr(arxiv_import, "search_openalex", lambda *a, **k: oa)
    monkeypatch.setattr(arxiv_import, "search_arxiv", lambda *a, **k: ax)
    monkeypatch.setattr(arxiv_import, "search_semanticscholar",
                        lambda *a, **k: s2)
    out = arxiv_import.search_papers("attention", max_results=10)
    assert len(out) == 2
    merged = [r for r in out if r.get("doi") == "10.1/aaa"][0]
    assert merged["source"] == "merged"
    assert set(merged["sources"]) >= {"arxiv", "openalex"}
    assert merged["title"] == "arXiv Title"  # arXiv title preferred
    assert merged["venue"] == "Nature"  # OpenAlex venue kept
    assert merged["cited_by_count"] == 42


def test_search_route_shapes(client, monkeypatch):
    _authed(client)
    canned = [{"title": "Found Paper", "authors": ["A"], "year": 2023,
               "venue": "ICML", "doi": "10.3/ccc", "arxiv_id": None,
               "pdf_url": None, "openalex_id": "W9", "abstract": "Abs.",
               "sources": ["openalex"], "source": "openalex"}]
    monkeypatch.setattr("unified_app.search_papers",
                        lambda *a, **k: canned)
    resp = client.post("/api/study/import/search", json={"query": "found"})
    assert resp.status_code == 200
    assert resp.get_json()["results"][0]["title"] == "Found Paper"
    assert client.post("/api/study/import/search",
                       json={"query": "  "}).status_code == 400


def test_search_route_requires_login(app):
    anon_client = app.test_client()
    resp = anon_client.post("/api/study/import/search", json={"query": "x"})
    assert resp.status_code == 401


# ---------- one-click full import ----------

def test_full_import_with_pdf(client, app, monkeypatch):
    _authed(client)
    monkeypatch.setattr("unified_app.fetch_arxiv",
                        lambda *a, **k: dict(ARXIV_META))

    def fake_download(url, dest):
        with open(dest, "wb") as fh:
            fh.write(_pdf_bytes(["Attention mechanism transforms models."]))
        return True

    monkeypatch.setattr("unified_app.arxiv_download_pdf", fake_download)
    resp = client.post("/api/study/import/full",
                       json={"arxiv_id": "1706.03762"})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:400]
    body = resp.get_json()
    assert body["success"] is True and body["doc"] is not None
    assert body["doc"]["pages"] >= 1 and body["doc"]["chunks"] >= 1
    with app.app_context():
        doc = StudyDoc.query.filter_by(
            source_ref="1706.03762").first()
        assert doc is not None and doc.source == "arxiv"


def test_full_import_doc_null_on_pdf_failure(client, app, monkeypatch):
    _authed(client)
    monkeypatch.setattr("unified_app.fetch_arxiv",
                        lambda *a, **k: dict(ARXIV_META))
    monkeypatch.setattr("unified_app.arxiv_download_pdf",
                        lambda *a, **k: False)
    resp = client.post("/api/study/import/full",
                       json={"arxiv_id": "1706.03762"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["doc"] is None and body.get("doc_error")
    with app.app_context():
        assert PaperImport.query.filter_by(
            arxiv_id="1706.03762").first() is not None


def test_full_import_validation(client):
    _authed(client)
    assert client.post("/api/study/import/full", json={}).status_code == 400
    assert client.post("/api/study/import/full", json={
        "arxiv_id": "1706.03762",
        "doi": "10.1/aaa"}).status_code == 400
    assert client.post("/api/study/import/full",
                       json={"arxiv_id": "bogus"}).status_code == 400


def test_two_step_pdf_route_still_works(client, app, monkeypatch):
    _authed(client)

    def fake_download(url, dest):
        with open(dest, "wb") as fh:
            fh.write(_pdf_bytes(["Imported paper body text here."]))
        return True

    monkeypatch.setattr("unified_app.arxiv_download_pdf", fake_download)
    resp = client.post("/api/study/import/pdf", json={
        "pdf_url": "https://arxiv.org/pdf/1706.03762",
        "title": "Attention", "arxiv_id": "1706.03762"})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:400]
    assert resp.get_json()["success"] is True


# ---------- reading queue ----------

def test_queue_over_imports(client, app):
    _authed(client)
    uid = _user_id(app)
    _seed_import(app, uid, title="Neural Nets",
                 abstract="Deep neural networks learn representations.")
    _seed_import(app, uid, title="Old Survey",
                 abstract="A survey of classical methods.", year=2010)
    resp = client.post("/api/study/import/queue",
                       json={"minutes_per_paper": 30,
                             "interests": "neural"})
    assert resp.status_code == 200
    q = resp.get_json()["queue"]
    assert q["total_papers"] == 2 and q["total_minutes"] == 60
    assert q["queue"][0]["title"] == "Neural Nets"  # interest match first
    # Contract the import page JS relies on: body.queue is the
    # reading_queue dict whose nested "queue" list holds the items
    # (a past browser-QA failure came from the JS reading it as a list).
    assert isinstance(q["queue"], list) and len(q["queue"]) == 2
    assert q["queue"][0]["est_minutes"] == 30


def test_queue_empty_400(client):
    _authed(client)
    resp = client.post("/api/study/import/queue", json={})
    assert resp.status_code == 400


# ---------- citations + bridges ----------

def test_recent_citations_bridges(monkeypatch):
    from studybuddy import arxiv_import

    class FakeResp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload

        def json(self):
            return self._payload

    bridge = {"id": "https://openalex.org/W999", "title": "Bridge Study",
              "authorships": [{"author": {"display_name": "Citer One"}}],
              "publication_year": 2025, "ids": {"doi": "https://doi.org/10.9/z"},
              "doi": "https://doi.org/10.9/z"}
    solo = {"id": "https://openalex.org/W888", "title": "Solo Citer",
            "authorships": [], "publication_year": 2024,
            "ids": {}, "doi": None}

    def fake_get(url, params=None):
        if params and str(params.get("filter", "")).startswith("cites:"):
            sid = params["filter"].split(":", 1)[1]
            if sid == "W111":
                return FakeResp(200, {"results": [bridge]})
            return FakeResp(200, {"results": [bridge, solo]})
        if url.endswith("/works/W111"):
            return FakeResp(200, {"id": "https://openalex.org/W111",
                                  "title": "First Paper"})
        if url.endswith("/works/W222"):
            return FakeResp(200, {"id": "https://openalex.org/W222",
                                  "title": "Second Paper"})
        return FakeResp(404, {})

    monkeypatch.setattr(arxiv_import, "_get", fake_get)
    monkeypatch.setattr(arxiv_import, "cache_get", lambda *a, **k: None)
    monkeypatch.setattr(arxiv_import, "cache_set", lambda *a, **k: None)
    out = arxiv_import.recent_citations(["W111", "W222"])
    assert len(out["recent"]) == 3  # bridge twice (once per import) + solo
    assert len(out["bridges"]) == 1
    assert out["bridges"][0]["title"] == "Bridge Study"
    assert set(out["bridges"][0]["cites"]) == {"First Paper", "Second Paper"}


def test_citations_route_shapes(client, app, monkeypatch):
    _authed(client)
    _seed_import(app, _user_id(app), openalex_id="W111")
    canned = {"recent": [{"title": "Citing Work", "authors": ["C"],
                          "year": 2025, "doi": "10.9/z",
                          "openalex_id": "W999",
                          "cited_paper_title": "Seed Paper"}],
              "bridges": []}
    monkeypatch.setattr("unified_app.recent_citations",
                        lambda *a, **k: canned)
    resp = client.get("/api/study/import/citations")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["recent"][0]["title"] == "Citing Work"


def test_citations_route_no_openalex_note(client):
    _authed(client)
    resp = client.get("/api/study/import/citations")
    assert resp.status_code == 200
    assert resp.get_json()["recent"] == [] and "note" in resp.get_json()


# ---------- collections ----------

def test_collections_crud_and_public_page(client, app):
    _authed(client)
    uid = _user_id(app)
    pid = _seed_import(app, uid)
    resp = client.post("/api/study/collections",
                       json={"name": "My List", "paper_ids": [pid]})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    coll = resp.get_json()["collection"]
    assert coll["paper_count"] == 1 and coll["token"] and coll["url"]
    lst = client.get("/api/study/collections")
    assert lst.get_json()["collections"][0]["name"] == "My List"
    pid2 = _seed_import(app, uid, title="Second Paper")
    upd = client.post(f"/api/study/collections/{coll['token']}/papers",
                      json={"paper_ids": [pid, pid2]})
    assert upd.status_code == 200
    assert upd.get_json()["collection"]["paper_count"] == 2
    anon = app.test_client()
    page = anon.get(coll["url"])
    assert page.status_code == 200
    assert b"Seed Paper" in page.data and b"Second Paper" in page.data
    assert anon.get("/research/collections/nope").status_code == 404
    assert client.post("/api/study/collections",
                       json={"name": " ", "paper_ids": []}).status_code == 400
    assert client.post("/api/study/collections",
                       json={"name": "Big",
                             "paper_ids": list(range(1, 102))}).status_code == 400


def test_collections_stranger_cannot_edit(client, app):
    _authed(client)
    pid = _seed_import(app, _user_id(app))
    token = client.post("/api/study/collections",
                        json={"name": "Mine",
                              "paper_ids": [pid]}).get_json()["collection"]["token"]
    stranger = app.test_client()
    _authed(stranger, email="other@example.com", name="Other User")
    resp = stranger.post(f"/api/study/collections/{token}/papers",
                         json={"paper_ids": [pid]})
    assert resp.status_code == 404


# ---------- OpenAlex URL-form resolution ----------

def test_resolve_openalex_url_form(monkeypatch):
    from studybuddy import arxiv_import

    seen = {}

    class FakeResp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(url, params=None):
        seen["url"] = url
        return FakeResp(200, {"id": "https://openalex.org/W2741809807"})

    monkeypatch.setattr(arxiv_import, "_get", fake_get)
    assert arxiv_import._resolve_openalex_for_arxiv("1706.03762") == "W2741809807"
    assert seen["url"] == ("https://api.openalex.org/works/"
                           "https://arxiv.org/abs/1706.03762")
    monkeypatch.setattr(arxiv_import, "_get",
                        lambda *a, **k: FakeResp(404, {}))
    assert arxiv_import._resolve_openalex_for_arxiv("1706.03762") is None
    import requests as _rq

    def boom(*a, **k):
        raise _rq.RequestException("down")

    monkeypatch.setattr(arxiv_import, "_get", boom)
    assert arxiv_import._resolve_openalex_for_arxiv("1706.03762") is None
