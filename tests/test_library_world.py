"""World-unique library features: faithfulness badge, true cross-doc
synthesis, contrast flags, Paper DNA cards.

Unit tests prove the pure functions; route tests prove the ask path
wires them (single-doc asks gain grounding+verification, library-wide
asks synthesize across docs, DNA endpoint is auth-gated + per-user).
"""
import io

from tests.conftest import login, make_pdf_bytes, register  # noqa: F401

from studybuddy.paper_dna import extract_dna
from studybuddy.pdf_qa import (
    answer_across_docs,
    detect_contrasts,
    fluent_answer,
    grounding_report,
)



def _upload(client, lines, filename="notes.pdf"):
    data = {'file': (io.BytesIO(make_pdf_bytes(list(lines))), filename)}
    return client.post("/study/api/upload", data=data,
                       content_type="multipart/form-data")


def _authed(client):
    register(client)
    login(client)


# ---------- grounding ----------

def test_grounding_abstention_scores_full():
    g = grounding_report({'answer': "I couldn't find this in your documents."})
    assert g['claims'] == 0 and g['score'] == 1.0 and g['abstained'] is True


def test_grounding_all_cited_scores_full():
    g = grounding_report({'answer':
        'Here is what the document says: "Mitochondria produce energy." [doc, p.1]'})
    assert g['claims'] == 1 and g['cited'] == 1 and g['score'] == 1.0


def test_grounding_partial_citation_drops_score():
    g = grounding_report({'answer':
        '"An uncited claim here." and "A cited one here." [doc, p.1]'})
    assert g['claims'] == 2 and g['cited'] == 1 and g['score'] == 0.5


# ---------- contrasts ----------

def _per_doc_opposing():
    return [
        {'doc': 'Paper A', 'claims': [
            {'text': 'Regular exercise significantly increased muscle strength in participants.', 'page': 3}]},
        {'doc': 'Paper B', 'claims': [
            {'text': 'Regular exercise significantly decreased muscle strength in participants.', 'page': 5}]},
    ]


def test_contrasts_fire_on_opposite_directions():
    flags = detect_contrasts(_per_doc_opposing())
    assert len(flags) == 1
    assert flags[0]['docs'] == ['Paper A', 'Paper B']
    assert 'up vs down' in flags[0]['signal']
    assert 'heuristic' in flags[0]['note']


def test_contrasts_silent_when_docs_agree():
    per_doc = [
        {'doc': 'Paper A', 'claims': [
            {'text': 'Regular exercise significantly increased muscle strength in participants.', 'page': 3}]},
        {'doc': 'Paper B', 'claims': [
            {'text': 'Regular exercise also increased muscle strength in older adults.', 'page': 5}]},
    ]
    assert detect_contrasts(per_doc) == []


def test_contrasts_silent_single_doc():
    assert detect_contrasts(_per_doc_opposing()[:1]) == []


# ---------- cross-doc synthesis ----------

def test_across_docs_groups_evidence_per_doc():
    docs = [
        {'title': 'Paper A', 'chunks': [
            {'page': 3, 'index': 0, 'text': 'Regular exercise significantly increased muscle strength in participants.'}]},
        {'title': 'Paper B', 'chunks': [
            {'page': 5, 'index': 0, 'text': 'Regular exercise significantly decreased muscle strength in participants.'}]},
    ]
    result = answer_across_docs('Does regular exercise affect muscle strength?', docs)
    assert result['docs_consulted'] == 2
    assert {e['doc'] for e in result['per_doc']} == {'Paper A', 'Paper B'}
    assert len(result['citations']) >= 2


# ---------- Paper DNA ----------

def _dna_chunks():
    return [
        {'page': 1, 'index': 0, 'heading': 'Abstract',
         'text': 'This paper investigates whether spaced repetition improves vocabulary retention.'},
        {'page': 2, 'index': 0, 'heading': 'Methodology',
         'text': 'We conducted a randomized experiment with n = 120 participants. The dataset used was the WordBank vocabulary dataset.'},
        {'page': 4, 'index': 0, 'heading': 'Results',
         'text': 'Spaced repetition significantly improved retention scores.'},
        {'page': 5, 'index': 0, 'heading': 'Limitations',
         'text': 'A key limitation is that the sample covered only undergraduate students.'},
    ]


def test_dna_extracts_all_fields_with_pages():
    dna = extract_dna(_dna_chunks())
    assert dna['research_question']['page'] == 1
    assert dna['method']['page'] == 2
    assert dna['dataset']['page'] == 2
    assert dna['sample_size']['value'] == '120'
    assert dna['key_result']['page'] == 4
    assert dna['limitation']['page'] == 5
    assert 'method' in dna['sections_found']


def test_dna_missing_fields_stay_null_not_invented():
    dna = extract_dna([{'page': 1, 'index': 0, 'heading': None,
                        'text': 'The mitochondria is the powerhouse of the cell.'}])
    assert dna['research_question'] is None
    assert dna['dataset'] is None
    assert dna['sample_size'] is None
    assert dna['limitation'] is None


# ---------- routes ----------

def test_ask_single_doc_carries_grounding_and_verification(client):
    _authed(client)
    doc_id = _upload(client, ["The mitochondria is the powerhouse of the cell."]).get_json()["doc"]["id"]
    resp = client.post("/study/api/ask", json={
        "doc_id": doc_id, "question": "What is the powerhouse of the cell?"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["grounding"]["claims"] >= 1
    assert body["grounding"]["score"] == 1.0
    assert body["verification"]["ok"] is True


def test_ask_library_wide_synthesizes_and_flags_contrast(client):
    _authed(client)
    _upload(client, ["Regular exercise significantly increased muscle strength in participants."], "a.pdf")
    _upload(client, ["Regular exercise significantly decreased muscle strength in participants."], "b.pdf")
    resp = client.post("/study/api/ask", json={
        "doc_id": None, "question": "Does regular exercise affect muscle strength?"})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    body = resp.get_json()
    assert body["docs_consulted"] == 2
    assert len(body["per_doc"]) == 2
    assert len(body["contrasts"]) == 1
    assert body["contrasts"][0]["docs"] and 'heuristic' in body["contrasts"][0]["note"]
    assert body["grounding"]["claims"] >= 1


def test_ask_library_wide_abstains_honestly(client):
    _authed(client)
    _upload(client, ["The mitochondria is the powerhouse of the cell."])
    resp = client.post("/study/api/ask", json={
        "doc_id": None, "question": "What is the capital of Assyria in 700 BC?"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["citations"] == []
    assert "couldn't find" in body["answer"]
    assert body["grounding"]["abstained"] is True
    assert body["grounding"]["score"] == 1.0


def test_dna_endpoint_requires_login_and_ownership(client):
    _authed(client)
    doc_id = _upload(client, ["The mitochondria is the powerhouse of the cell."]).get_json()["doc"]["id"]
    resp = client.get(f"/study/api/documents/{doc_id}/dna")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] and "research_question" in body["dna"]
    assert "sample_size" in body["dna"] and "sections_found" in body["dna"]
    client.post("/logout")
    resp = client.get(f"/study/api/documents/{doc_id}/dna")
    assert resp.status_code == 401
    register(client, email="second@example.com")
    login(client, email="second@example.com")
    resp = client.get(f"/study/api/documents/{doc_id}/dna")
    assert resp.status_code == 404


# ---------- fluent answers ----------

_EXTRACTIVE = ('Here is what the document says: "The mitochondria is the '
               'powerhouse of the cell." [doc, p.1]')


def test_fluent_keeps_quotes_and_markers():
    out = fluent_answer(_EXTRACTIVE, lambda prompt: (
        'In short, "The mitochondria is the powerhouse of the cell." '
        '[doc, p.1] — that is where the cell gets its energy.'))
    assert out['fluent'] is True
    assert '[doc, p.1]' in out['answer']


def test_fluent_falls_back_when_marker_dropped():
    out = fluent_answer(_EXTRACTIVE, lambda prompt:
                        'The mitochondria is the powerhouse of the cell.')
    assert out['fluent'] is False
    assert out['answer'] == _EXTRACTIVE


def test_fluent_falls_back_on_new_uncited_claim():
    out = fluent_answer(_EXTRACTIVE, lambda prompt: (
        '"The mitochondria is the powerhouse of the cell." [doc, p.1] '
        '"And ribosomes make proteins."'))
    assert out['fluent'] is False
    assert out['answer'] == _EXTRACTIVE


def test_fluent_falls_back_when_provider_errors():
    def boom(prompt):
        raise RuntimeError("provider down")
    out = fluent_answer(_EXTRACTIVE, boom)
    assert out['fluent'] is False and out['answer'] == _EXTRACTIVE


def test_fluent_never_rewrites_abstentions():
    called = []
    out = fluent_answer("I couldn't find this in your documents.",
                        lambda prompt: called.append(prompt) or "x")
    assert out['fluent'] is False and called == []


def test_ask_fluent_success_via_mocked_gateway(client, monkeypatch):
    import studybuddy.ai_gateway as gw
    monkeypatch.setattr(gw, "ai_ready", lambda: True)
    monkeypatch.setattr(gw, "generate", lambda prompt, **kw: (
        'In short, "The mitochondria is the powerhouse of the cell." '
        '[doc, p.1] — that is where the cell gets its energy.'))
    _authed(client)
    doc_id = _upload(client, ["The mitochondria is the powerhouse of the cell."]).get_json()["doc"]["id"]
    resp = client.post("/study/api/ask", json={
        "doc_id": doc_id, "question": "What is the powerhouse of the cell?",
        "fluent": True})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["fluent"] is True
    assert body["answer"].startswith("In short,")
    assert body["grounding"]["score"] == 1.0


def test_ask_fluent_without_provider_keeps_extractive(client):
    _authed(client)
    doc_id = _upload(client, ["The mitochondria is the powerhouse of the cell."]).get_json()["doc"]["id"]
    resp = client.post("/study/api/ask", json={
        "doc_id": doc_id, "question": "What is the powerhouse of the cell?",
        "fluent": True})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["fluent"] is False
    assert body["fluent_reason"] == "no provider configured"
    assert "powerhouse" in body["answer"]


# ---------- mock exams ----------

from studybuddy.exam_gen import build_exam_from_chunks, grade_exam  # noqa: E402

_RICH_LINES = [
    "Photosynthesis is the process by which green plants convert light energy into chemical energy.",
    "Photosynthesis takes place in the chloroplast of plant cells, where chlorophyll captures sunlight.",
    "Chlorophyll is the green pigment that absorbs light for photosynthesis.",
    "The mitochondria is the powerhouse of the cell, producing energy for the cell.",
    "Mitochondria generate most of the chemical energy needed to power the cell.",
    "During photosynthesis, plants produce glucose and release oxygen into the air.",
    "Glucose is a simple sugar that stores chemical energy for the plant.",
    "Chlorophyll molecules in the chloroplast absorb red and blue light most strongly.",
    "Mitochondria are found in nearly every eukaryotic cell and produce energy.",
    "Photosynthesis sustains nearly all life on Earth by producing oxygen and glucose.",
]


def _rich_chunks():
    from studybuddy.pdf_qa import chunk_pages
    text = "\n".join(_RICH_LINES)
    return chunk_pages([text])


def test_exam_builds_auto_gradeable_first():
    exam = build_exam_from_chunks(_rich_chunks(), "Bio notes", n=10)
    assert exam["total"] >= 1
    kinds = [q["kind"] for q in exam["questions"]]
    assert "mcq" in kinds or "cloze" in kinds
    for q in exam["questions"]:
        if q["kind"] == "mcq":
            assert len(q["options"]) == 4 and q["correct_index"] in range(4)


def test_exam_grading_mcq_cloze_and_qa_rules():
    questions = [
        {"id": "mcq-0", "kind": "mcq", "question": "Q?", "options": ["a", "b"],
         "correct_index": 1, "answer": "b", "page": 1, "doc_title": "Doc"},
        {"id": "cloze-0", "kind": "cloze", "question": "Fill ___.",
         "options": [], "answer": "Photosynthesis", "page": 2, "doc_title": "Doc"},
        {"id": "qa-0", "kind": "qa", "question": "What?",
         "options": [], "answer": "A sentence.", "page": 3, "doc_title": "Doc"},
    ]
    grade = grade_exam(questions, {"mcq-0": 1, "cloze-0": "photosynthesis"})
    assert grade["auto_total"] == 2 and grade["auto_correct"] == 2
    assert grade["score_pct"] == 100.0
    qa_result = [r for r in grade["results"] if r["id"] == "qa-0"][0]
    assert qa_result["correct"] is None  # never auto-scored
    grade_wrong = grade_exam(questions, {"mcq-0": 0, "cloze-0": "nope"})
    assert grade_wrong["auto_correct"] == 0
    assert {"doc_title": "Doc", "page": 1} in grade_wrong["reread"]


def test_exam_generate_and_grade_roundtrip(client):
    _authed(client)
    doc_id = _upload(client, _RICH_LINES, "bio.pdf").get_json()["doc"]["id"]
    resp = client.post("/study/api/exam/generate",
                       json={"doc_id": doc_id, "n": 10})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    exam = resp.get_json()["exam"]
    assert exam["total"] >= 1
    answers = {}
    for q in exam["questions"]:
        if q["kind"] == "mcq":
            answers[q["id"]] = q["correct_index"]
        elif q["kind"] == "cloze":
            answers[q["id"]] = q["answer"]
    resp = client.post("/study/api/exam/grade",
                       json={"questions": exam["questions"], "answers": answers})
    assert resp.status_code == 200
    grade = resp.get_json()["grade"]
    assert grade["auto_total"] >= 1 and grade["auto_correct"] == grade["auto_total"]
    assert grade["score_pct"] == 100.0


def test_exam_generate_thin_doc_400_and_auth(client):
    resp = client.post("/study/api/exam/generate", json={"doc_id": 1})
    assert resp.status_code == 401
    _authed(client)
    doc_id = _upload(client, ["Hello world."], "thin.pdf").get_json()["doc"]["id"]
    resp = client.post("/study/api/exam/generate", json={"doc_id": doc_id})
    assert resp.status_code == 400
    resp = client.post("/study/api/exam/grade", json={"questions": []})
    assert resp.status_code == 400


# ---------- summary mode ----------

import re as _re

from studybuddy.pdf_qa import (  # noqa: E402
    is_summary_question,
    summarize_across_docs,
    summarize_doc,
)


def test_summary_detection_covers_typo_and_variants():
    assert is_summary_question("what is summary of this dcoument") is True
    assert is_summary_question("Give me an overview of this paper") is True
    assert is_summary_question("What are the key points?") is True
    assert is_summary_question("What grade did it receive?") is False
    assert is_summary_question("What is the powerhouse of the cell?") is False


def test_summarize_doc_quotes_only_real_sentences_in_order():
    chunks = _rich_chunks()
    result = summarize_doc("Bio notes", chunks, top_k=5)
    assert result["answer"].startswith("Summary of the document:")
    assert len(result["citations"]) >= 1
    source = " ".join(c["text"] for c in chunks)
    quotes = _re.findall(r'"([^"]{10,})"', result["answer"])
    assert len(quotes) >= 1
    for q in quotes:
        assert q in source, "summary invented a sentence"
    pages = [c["page"] for c in result["citations"]]
    assert pages == sorted(pages), "summary not in reading order"


def test_summarize_doc_empty_abstains():
    result = summarize_doc("Empty", [])
    assert result["answer"].startswith("I couldn't find")
    assert result["citations"] == []


def test_summarize_across_docs_groups_per_doc():
    docs = [
        {"title": "A", "chunks": _rich_chunks()},
        {"title": "B", "chunks": _rich_chunks()},
    ]
    result = summarize_across_docs(docs)
    assert result["docs_consulted"] == 2
    assert len(result["per_doc"]) == 2
    assert result["answer"].startswith("Summary across 2 documents:")


def test_ask_summary_question_returns_cited_summary_not_abstention(client):
    _authed(client)
    doc_id = _upload(client, _RICH_LINES, "bio.pdf").get_json()["doc"]["id"]
    resp = client.post("/study/api/ask", json={
        "doc_id": doc_id, "question": "what is summary of this dcoument"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["summary"] is True
    assert body["citations"], "summary must carry citations"
    assert "Summary of the document" in body["answer"]
    assert body["grounding"]["score"] == 1.0


def test_ask_library_wide_summary_and_history_roundtrip(client):
    _authed(client)
    doc_id = _upload(client, _RICH_LINES, "bio.pdf").get_json()["doc"]["id"]
    resp = client.post("/study/api/ask", json={
        "doc_id": None, "question": "summarize my library"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["summary"] is True
    assert body["per_doc"], "library summary must group per doc"
    # Single-doc asks attach to the doc, so per-doc history shows them.
    client.post("/study/api/ask", json={
        "doc_id": doc_id, "question": "summarize this document"})
    hist = client.get(f"/study/api/documents/{doc_id}/history")
    assert hist.status_code == 200
    assert len(hist.get_json()["questions"]) == 1
    # Regression guard for the browser bug: the JS must read the key the
    # endpoint actually returns ("questions"), not "history".
    import pathlib
    js = pathlib.Path("static/js/pdf_qa.js").read_text()
    assert "data.questions" in js


def test_library_page_renders_lib_ask_and_dna_buttons(client):
    _authed(client)
    _upload(client, ["The mitochondria is the powerhouse of the cell."])
    resp = client.get("/research/library")
    assert resp.status_code == 200
    assert b"qa-lib-ask-btn" in resp.data
    assert b"qa-dna-doc" in resp.data
