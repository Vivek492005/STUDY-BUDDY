"""Route-level tests for Wave 4 batch 1: study library (#21), paper import (#25), cite (#24).

Includes negative paths: auth, invalid input, bad uploads, mocked external
failures. CSRF-enabled app fixture proves missing-token rejection.
"""
import io

import pytest
from tests.conftest import login, make_pdf_bytes, register  # noqa: F401

from unified_app import PaperImport, StudyDoc, StudyQuestion, db


def _pdf_file(lines=("The mitochondria is the powerhouse of the cell.",
                     "Photosynthesis occurs in the chloroplast of plants.")):
    return (io.BytesIO(make_pdf_bytes(list(lines))), "notes.pdf")


def _upload(client, lines=None, filename="notes.pdf"):
    data = {'file': (io.BytesIO(make_pdf_bytes(list(lines or (
        "The mitochondria is the powerhouse of the cell.",
    )))), filename)}
    return client.post("/study/api/upload", data=data,
                       content_type="multipart/form-data")


def _authed(client):
    register(client)
    login(client)


# ---------- #21 page + upload ----------

def test_library_requires_login(client):
    resp = client.get("/research/library")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


def test_library_renders_bento(client):
    _authed(client)
    resp = client.get("/research/library")
    assert resp.status_code == 200
    assert b"b-card" in resp.data and b"data-theme" in resp.data


def test_upload_requires_login(client):
    resp = client.post("/study/api/upload", data={},
                       content_type="multipart/form-data")
    assert resp.status_code == 401
    assert resp.get_json()["error"]


def test_upload_no_file_400(client):
    _authed(client)
    resp = client.post("/study/api/upload", data={},
                       content_type="multipart/form-data")
    assert resp.status_code == 400


def test_upload_rejects_txt(client):
    _authed(client)
    data = {'file': (io.BytesIO(b"hello"), "notes.txt")}
    resp = client.post("/study/api/upload", data=data,
                       content_type="multipart/form-data")
    assert resp.status_code == 400
    assert resp.get_json()["error"]


def test_upload_rejects_fake_pdf(client):
    _authed(client)
    data = {'file': (io.BytesIO(b"not a pdf at all"), "fake.pdf")}
    resp = client.post("/study/api/upload", data=data,
                       content_type="multipart/form-data")
    assert resp.status_code == 400


def test_upload_happy_path(client, app, tmp_path):
    _authed(client)
    resp = _upload(client)
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    body = resp.get_json()
    assert body["success"] and body["doc"]["chunks"] > 0
    with app.app_context():
        doc = db.session.get(StudyDoc, body["doc"]["id"])
        assert doc is not None and doc.num_pages >= 1
        assert (doc.data or {}).get("chunks")


def test_ask_requires_login(client):
    resp = client.post("/study/api/ask", json={"question": "hi"})
    assert resp.status_code == 401


def test_ask_empty_question_400(client):
    _authed(client)
    resp = client.post("/study/api/ask", json={"question": "   "})
    assert resp.status_code == 400


def test_ask_empty_library_400(client):
    _authed(client)
    resp = client.post("/study/api/ask", json={"question": "anything?"})
    assert resp.status_code == 400
    assert "empty" in resp.get_json()["error"].lower()


def test_ask_cited_answer(client, app):
    _authed(client)
    up = _upload(client).get_json()
    doc_id = up["doc"]["id"]
    resp = client.post("/study/api/ask", json={
        "doc_id": doc_id, "question": "What is the powerhouse of the cell?"})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    body = resp.get_json()
    assert body["success"]
    assert body["citations"], "expected at least one citation"
    for c in body["citations"]:
        assert c["page"] >= 1
        assert c["excerpt"]
    with app.app_context():
        assert StudyQuestion.query.filter_by(doc_id=doc_id).count() == 1


def test_ask_honest_abstention(client):
    _authed(client)
    up = _upload(client).get_json()
    resp = client.post("/study/api/ask", json={
        "doc_id": up["doc"]["id"],
        "question": "What is the capital of Assyria in 700 BC?"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["citations"] == []
    assert "couldn't find" in body["answer"]


def test_ask_other_users_doc_404(client):
    _authed(client)
    up = _upload(client).get_json()
    doc_id = up["doc"]["id"]
    client.post("/logout")
    register(client, email="second@example.com")
    login(client, email="second@example.com")
    resp = client.post("/study/api/ask", json={"doc_id": doc_id,
                                               "question": "hi?"})
    assert resp.status_code == 404


def test_history_requires_login(client):
    assert client.get("/study/api/documents/1/history").status_code == 401


def test_history_roundtrip(client):
    _authed(client)
    doc_id = _upload(client).get_json()["doc"]["id"]
    client.post("/study/api/ask", json={"doc_id": doc_id, "question": "Q?"})
    resp = client.get(f"/study/api/documents/{doc_id}/history")
    assert resp.status_code == 200
    assert len(resp.get_json()["questions"]) == 1


def test_delete_flow(client, app):
    _authed(client)
    doc_id = _upload(client).get_json()["doc"]["id"]
    with app.app_context():
        fname = db.session.get(StudyDoc, doc_id).stored_filename
        upload_dir = app.config["UPLOAD_FOLDER"]
    assert client.delete("/study/api/documents/999999").status_code == 404
    resp = client.delete(f"/study/api/documents/{doc_id}")
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(StudyDoc, doc_id) is None
    import os
    assert not os.path.exists(os.path.join(upload_dir, fname))


# ---------- #25 import routes ----------

def test_import_page_requires_login(client):
    assert client.get("/research/import").status_code == 302


def test_import_page_renders(client):
    _authed(client)
    resp = client.get("/research/import")
    assert resp.status_code == 200 and b"b-card" in resp.data


def test_import_arxiv_requires_login(client):
    resp = client.post("/api/study/import/arxiv", json={"arxiv_id": "1706.03762"})
    assert resp.status_code == 401


def test_import_arxiv_invalid_id_400(client):
    _authed(client)
    for bad in ["abc", "1234.567", "", "http://evil.com/x"]:
        resp = client.post("/api/study/import/arxiv", json={"arxiv_id": bad})
        assert resp.status_code == 400, bad


def test_import_arxiv_api_failure_502(client, monkeypatch):
    _authed(client)
    monkeypatch.setattr("studybuddy.research_routes.fetch_arxiv",
                        lambda *a, **k: {"error": "arXiv is unreachable"})
    resp = client.post("/api/study/import/arxiv", json={"arxiv_id": "1706.03762"})
    assert resp.status_code == 502
    assert "unreachable" in resp.get_json()["error"]


def test_import_arxiv_happy_path(client, app, monkeypatch):
    _authed(client)
    meta = {"title": "Attention Is All You Need",
            "authors": ["Ashish Vaswani"], "abstract": "We propose...",
            "year": 2017, "venue": "NeurIPS", "doi": None,
            "arxiv_id": "1706.03762", "pdf_url": "https://arxiv.org/pdf/1706.03762",
            "openalex_id": "W2741809807", "url": "https://arxiv.org/abs/1706.03762",
            "source": "arxiv", "fetched_at": "2026-10-14T00:00:00+00:00"}
    monkeypatch.setattr("studybuddy.research_routes.fetch_arxiv", lambda *a, **k: dict(meta))
    resp = client.post("/api/study/import/arxiv", json={"arxiv_id": "1706.03762"})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    body = resp.get_json()
    assert body["title"] == "Attention Is All You Need"
    assert body["source"] == "arxiv" and body["fetched_at"]
    with app.app_context():
        p = PaperImport.query.filter_by(arxiv_id="1706.03762").first()
        assert p is not None and p.fetched_at


def test_import_doi_invalid_400(client):
    _authed(client)
    resp = client.post("/api/study/import/doi", json={"doi": "not-a-doi"})
    assert resp.status_code == 400


def test_import_doi_api_failure_502(client, monkeypatch):
    _authed(client)
    monkeypatch.setattr("studybuddy.research_routes.fetch_doi",
                        lambda *a, **k: {"error": "OpenAlex timed out"})
    resp = client.post("/api/study/import/doi",
                       json={"doi": "10.1038/nature12373"})
    assert resp.status_code == 502


def test_graph_requires_login(client):
    assert client.get("/api/study/import/W1/graph").status_code == 401


def test_graph_happy_and_error(client, monkeypatch):
    _authed(client)
    monkeypatch.setattr(
        "studybuddy.research_routes.fetch_citation_graph",
        lambda *a, **k: {"nodes": [{"id": "W1", "title": "T",
                                   "is_center": True}],
                         "edges": []})
    resp = client.get("/api/study/import/W1/graph")
    assert resp.status_code == 200
    assert resp.get_json()["nodes"]
    monkeypatch.setattr("studybuddy.research_routes.fetch_citation_graph",
                        lambda *a, **k: {"error": "rate limited"})
    assert client.get("/api/study/import/W1/graph").status_code == 502


def test_import_pdf_bad_url_400(client):
    _authed(client)
    resp = client.post("/api/study/import/pdf", json={"pdf_url": "notaurl"})
    assert resp.status_code == 400


def test_import_pdf_download_failure_502(client, monkeypatch):
    _authed(client)
    monkeypatch.setattr("studybuddy.research_routes.arxiv_download_pdf",
                        lambda *a, **k: False)
    resp = client.post("/api/study/import/pdf",
                       json={"pdf_url": "https://arxiv.org/pdf/1706.03762",
                             "title": "Attention"})
    assert resp.status_code == 502


# ---------- #24 cite routes ----------

def test_cite_page_public_bento(client):
    resp = client.get("/research/cite")
    assert resp.status_code == 200
    assert b"b-card" in resp.data and b"data-theme" in resp.data


def test_cite_format_styles(client):
    meta = {"title": "Attention Is All You Need",
            "authors": ["Ashish Vaswani", "Noam Shazeer"],
            "year": 2017, "venue": "Advances in Neural Information Processing Systems",
            "pages": "5998-6008", "arxiv_id": "1706.03762"}
    for style in ["ieee", "apa", "mla", "vancouver", "chicago"]:
        resp = client.post("/api/cite/format",
                           json={"meta": meta, "style": style})
        assert resp.status_code == 200, style
        assert "Vaswani" in resp.get_json()["citation"], style


def test_cite_format_unknown_style_400(client):
    resp = client.post("/api/cite/format",
                       json={"meta": {"title": "T"}, "style": "bogus"})
    assert resp.status_code == 400


def test_cite_format_empty_meta_no_none(client):
    resp = client.post("/api/cite/format", json={"meta": {}, "style": "apa"})
    assert resp.status_code == 200
    assert "None" not in resp.get_json()["citation"]


def test_bibtex_roundtrip(client):
    meta = {"title": "Attention Is All You Need",
            "authors": ["Ashish Vaswani"], "year": 2017,
            "venue": "NeurIPS", "arxiv_id": "1706.03762"}
    exp = client.post("/api/cite/bibtex/export", json={"meta": meta})
    assert exp.status_code == 200
    assert "@" in exp.get_json()["bibtex"]
    imp = client.post("/api/cite/bibtex/import",
                      json={"text": exp.get_json()["bibtex"]})
    assert imp.status_code == 200
    metas = imp.get_json()["metas"]
    assert metas and metas[0]["title"] == "Attention Is All You Need"


def test_bibtex_import_garbage(client):
    resp = client.post("/api/cite/bibtex/import", json={"text": "hello world"})
    assert resp.status_code == 200
    assert resp.get_json()["metas"] == []


def test_bibtex_import_empty_400(client):
    resp = client.post("/api/cite/bibtex/import", json={"text": "   "})
    assert resp.status_code == 400


def test_cite_guide(client):
    resp = client.get("/api/cite/guide/apa")
    assert resp.status_code == 200 and resp.get_json()
    assert client.get("/api/cite/guide/bogus").status_code == 400


# ---------- CSRF: missing token must be rejected ----------

@pytest.fixture()
def csrf_app():
    from unified_app import create_app
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SECRET_KEY": "test-secret-key",
        "WTF_CSRF_ENABLED": True,
    })
    with app.app_context():
        db.create_all()
    yield app


def test_json_api_rejects_missing_csrf(csrf_app):
    client = csrf_app.test_client()
    resp = client.post("/api/cite/format",
                       json={"meta": {"title": "T"}, "style": "apa"})
    assert resp.status_code == 400
