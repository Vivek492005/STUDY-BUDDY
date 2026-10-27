"""Study library blueprint — Phase 1 of the monolith split.

See docs/monolith-split-plan.md. The /research/library page and the
/study/api/* routes moved here verbatim from unified_app.register_routes.
URLs, methods, rate limits, and response shapes are unchanged. Endpoints
are blueprint-prefixed (study_library.*) per Flask semantics; the four
template url_for('study_library') call sites were updated at move time.

Hard rule: this module MUST NOT import unified_app at top level. The
StudyDoc/StudyQuestion models still live in unified_app, so they are
imported lazily inside the view functions via _models() — the same
pattern as studybuddy/health_review.py::_models. db/limiter come from
app.extensions, which has no import cycle.
"""

import logging
import os
import uuid

from flask import Blueprint, current_app, jsonify, render_template, request
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from app.extensions import db, limiter
from studybuddy.exam_gen import build_exam_from_doc as exam_from_doc
from studybuddy.exam_gen import build_exam_from_docs as exam_from_docs
from studybuddy.exam_gen import grade_exam as exam_grade
from studybuddy.paper_dna import extract_dna as paper_dna_extract
from studybuddy.pdf_qa import answer_across_docs as pdfqa_across
from studybuddy.pdf_qa import answer_question as pdfqa_answer
from studybuddy.pdf_qa import chunk_pages as pdfqa_chunk_pages
from studybuddy.pdf_qa import detect_contrasts as pdfqa_contrasts
from studybuddy.pdf_qa import fluent_answer as pdfqa_fluent
from studybuddy.pdf_qa import grounding_report as pdfqa_grounding
from studybuddy.pdf_qa import is_summary_question as pdfqa_is_summary
from studybuddy.pdf_qa import parse_pdf_pages as pdfqa_parse_pages
from studybuddy.pdf_qa import summarize_across_docs as pdfqa_summarize_across
from studybuddy.pdf_qa import summarize_doc as pdfqa_summarize
from studybuddy.pdf_qa import validate_upload as pdfqa_validate_upload
from studybuddy.pdf_qa import verify_citations as pdfqa_verify

log = logging.getLogger("acra")

study_library_bp = Blueprint("study_library", __name__)


def _models():
    """Lazy import of unified_app models (avoids circular import)."""
    from unified_app import StudyDoc, StudyQuestion

    return StudyDoc, StudyQuestion


# ---------- STUDY INTELLIGENCE (Wave 4 batch 1) ----------
# Feature #21: source-grounded PDF Q&A with citations (NotebookLM-style)
@study_library_bp.route('/research/library')
@login_required
def study_library():
    """Per-user PDF study library page."""
    StudyDoc, _ = _models()
    docs = StudyDoc.query.filter_by(user_id=current_user.id).order_by(
        StudyDoc.created_at.desc()).all()
    documents = [{
        'id': d.id, 'title': d.title, 'pages': d.num_pages,
        'chunks': d.num_chunks, 'source': d.source,
        'created': d.created_at.strftime('%Y-%m-%d'),
    } for d in docs]
    # Template reads `documents` + `is_authed` (this route is
    # login-gated, so is_authed is always True here); `docs` kept
    # for backward compatibility. Passing only `docs` used to render
    # an always-empty list — the 2026-10-24 eval's PDF Library bug.
    return render_template('study_library.html', documents=documents,
                           docs=documents, is_authed=True)

@study_library_bp.route('/study/api/upload', methods=['POST'])
@limiter.limit("20 per hour")
def study_upload():
    StudyDoc, _ = _models()
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Upload a PDF into the study library. Multipart form with 'file'.

    CSRF token via X-CSRFToken header (not exempt). Returns the doc summary.
    """
    file = request.files.get('file')
    ok, err = pdfqa_validate_upload(file)
    if not ok:
        return jsonify({'error': err}), 400
    filename = f"{uuid.uuid4().hex[:8]}_{secure_filename(file.filename)}"
    filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
    try:
        file.save(filepath)
    except Exception:
        log.exception("Study upload save failed")
        return jsonify({'error': 'Could not save the uploaded file.'}), 500
    try:
        pages = pdfqa_parse_pages(filepath)
        if not any((p or '').strip() for p in pages):
            raise ValueError("no-text")
        chunks = pdfqa_chunk_pages(pages)
        title = (request.form.get('title') or '').strip() or file.filename
        doc = StudyDoc(
            user_id=current_user.id, title=title[:255],
            stored_filename=filename, source='upload',
            num_pages=len(pages), num_chunks=len(chunks),
            char_count=sum(len(c.get('text', '')) for c in chunks),
            data={'chunks': [dict(c) for c in chunks],
                  'truncated': bool(getattr(chunks, 'truncated', False))})
        db.session.add(doc)
        db.session.commit()
    except ValueError as e:
        if str(e) == "no-text":
            try:
                os.remove(filepath)
            except OSError:
                pass
            return jsonify({'error': 'No extractable text found in this PDF '
                                     '(it may be a scanned image).'}), 400
        raise
    except Exception:
        log.exception("Study upload processing failed")
        db.session.rollback()
        try:
            os.remove(filepath)
        except OSError:
            pass
        return jsonify({'error': 'Could not process this PDF. Please try another file.'}), 500
    return jsonify({'success': True, 'doc': {
        'id': doc.id, 'title': doc.title, 'pages': doc.num_pages,
        'chunks': doc.num_chunks}})

@study_library_bp.route('/study/api/ask', methods=['POST'])
@limiter.limit("60 per hour")
def study_ask():
    StudyDoc, StudyQuestion = _models()
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Ask a cited question over one doc (doc_id) or the whole library.

    JSON body: {"doc_id": int|null, "question": str}.
    CSRF token via X-CSRFToken header (not exempt).

    Single-doc asks answer extractively from that doc. Library-wide
    asks (doc_id null) run true cross-document synthesis: evidence is
    retrieved from the merged pool and grouped per document, with
    heuristic contrast flags when docs point in opposite directions.
    Summary/overview questions bypass keyword retrieval (which has
    nothing to match) and get an extractive summary instead: the
    doc's top TF-IDF sentences, quoted with page citations.
    Every response carries a faithfulness badge: verification (every
    quoted claim's citation marker) + grounding (share of claims
    cited; abstentions score 1.0 — claiming nothing is grounded).
    """
    payload = request.get_json(silent=True) or {}
    question = (payload.get('question') or '').strip()
    if not question:
        return jsonify({'error': 'Please type a question first.'}), 400
    if len(question) > 2000:
        return jsonify({'error': 'Question is too long (max 2000 characters).'}), 400
    doc_id = payload.get('doc_id')
    try:
        if doc_id:
            doc = StudyDoc.query.filter_by(id=int(doc_id),
                                           user_id=current_user.id).first()
            if not doc:
                return jsonify({'error': 'Document not found.'}), 404
            chunks = (doc.data or {}).get('chunks') or []
            if not chunks:
                return jsonify({'error': 'No indexed passages found in your library.'}), 400
            is_summary = pdfqa_is_summary(question)
            if is_summary:
                result = pdfqa_summarize(doc.title, chunks, top_k=5)
            else:
                result = pdfqa_answer(question, doc.title, chunks, top_k=5)
            response = {'success': True, 'doc_id': doc.id,
                        'doc_title': doc.title,
                        'answer': result.get('answer'),
                        'citations': result.get('citations') or [],
                        'summary': is_summary}
            stored_doc_id = doc.id
        else:
            candidates = StudyDoc.query.filter_by(
                user_id=current_user.id).order_by(
                StudyDoc.created_at.desc()).all()
            if not candidates:
                return jsonify({'error': 'Your library is empty — upload a PDF first.'}), 400
            docs_with_chunks = [
                {'title': d.title, 'chunks': (d.data or {}).get('chunks') or []}
                for d in candidates]
            docs_with_chunks = [d for d in docs_with_chunks if d['chunks']]
            if not docs_with_chunks:
                return jsonify({'error': 'No indexed passages found in your library.'}), 400
            is_summary = pdfqa_is_summary(question)
            if is_summary:
                result = pdfqa_summarize_across(docs_with_chunks)
            else:
                result = pdfqa_across(question, docs_with_chunks, top_k=3)
            per_doc = result.get('per_doc') or []
            response = {'success': True, 'doc_id': None,
                        'doc_title': None,
                        'answer': result.get('answer'),
                        'citations': result.get('citations') or [],
                        'per_doc': per_doc,
                        'docs_consulted': result.get('docs_consulted', 0),
                        'contrasts': [] if is_summary else pdfqa_contrasts(per_doc),
                        'summary': is_summary}
            stored_doc_id = None
        response['verification'] = pdfqa_verify(
            {'answer': response['answer']})
        response['grounding'] = pdfqa_grounding(
            {'answer': response['answer']})
        # Optional fluent rewrite: same claims, same markers, readable
        # prose — re-verified, otherwise the extractive answer stands.
        response['fluent'] = False
        response['fluent_reason'] = None
        if payload.get('fluent') and not response['grounding']['abstained']:
            try:
                from studybuddy import ai_gateway
                if ai_gateway.ai_ready():
                    labels = None
                    if not doc_id:
                        labels = [e.get('doc') for e in
                                  (response.get('per_doc') or [])]
                    fluent = pdfqa_fluent(
                        response['answer'],
                        lambda prompt: ai_gateway.generate(
                            prompt, _llm_user_id=current_user.id,
                            _llm_feature='study_fluent'),
                        required_labels=labels)
                    if fluent['fluent']:
                        response['answer'] = fluent['answer']
                        response['verification'] = pdfqa_verify(
                            {'answer': response['answer']})
                        response['grounding'] = pdfqa_grounding(
                            {'answer': response['answer']})
                        response['fluent'] = True
                    response['fluent_reason'] = fluent['reason']
                else:
                    response['fluent_reason'] = 'no provider configured'
            except Exception:
                log.exception("Study fluent rewrite failed; keeping extractive answer")
                response['fluent_reason'] = 'provider unavailable'
        q = StudyQuestion(user_id=current_user.id, doc_id=stored_doc_id,
                          question=question[:2000],
                          answer=response['answer'],
                          citations=response['citations'])
        db.session.add(q)
        db.session.commit()
    except Exception:
        log.exception("Study ask failed")
        db.session.rollback()
        return jsonify({'error': 'Could not answer right now. Please try again.'}), 500
    return jsonify(response)

@study_library_bp.route('/study/api/documents/<int:doc_id>/dna')
def study_dna(doc_id):
    StudyDoc, _ = _models()
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Paper DNA card for one document, computed from its chunks.

    Each field is a {text, page} citation or null when the document
    does not state it. No LLM, no invention.
    """
    doc = StudyDoc.query.filter_by(id=doc_id,
                                   user_id=current_user.id).first()
    if not doc:
        return jsonify({'error': 'Document not found.'}), 404
    chunks = (doc.data or {}).get('chunks') or []
    return jsonify({'success': True, 'doc_id': doc.id,
                    'doc_title': doc.title,
                    'dna': paper_dna_extract(chunks)})

@study_library_bp.route('/study/api/exam/generate', methods=['POST'])
@limiter.limit("30 per hour")
def study_exam_generate():
    StudyDoc, _ = _models()
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Mock exam from one doc (doc_id) or the whole library (null).

    Questions come from the flashcard generators; the response
    carries the key (self-test, like flashcards). An empty exam is
    a plain 400, never invented questions.
    """
    payload = request.get_json(silent=True) or {}
    doc_id = payload.get('doc_id')
    try:
        if doc_id:
            doc = StudyDoc.query.filter_by(
                id=int(doc_id), user_id=current_user.id).first()
            if not doc:
                return jsonify({'error': 'Document not found.'}), 404
            exam = exam_from_doc(doc, payload.get('n', 10))
        else:
            docs = StudyDoc.query.filter_by(
                user_id=current_user.id).order_by(
                StudyDoc.created_at.desc()).all()
            if not docs:
                return jsonify({'error': 'Your library is empty — upload a PDF first.'}), 400
            exam = exam_from_docs(docs, payload.get('n', 10))
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid document id.'}), 400
    if exam['total'] == 0:
        return jsonify({'error': 'Not enough extractable material to build an exam — try a longer document with repeated key terms.'}), 400
    return jsonify({'success': True, 'exam': exam})

@study_library_bp.route('/study/api/exam/grade', methods=['POST'])
@limiter.limit("60 per hour")
def study_exam_grade():
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Grade a mock exam. MCQ + cloze auto-scored; QA prompts return
    with their model answer for self-check and are never auto-scored."""
    payload = request.get_json(silent=True) or {}
    questions = payload.get('questions')
    if (not isinstance(questions, list) or not questions
            or len(questions) > 50):
        return jsonify({'error': 'Send 1–50 exam questions to grade.'}), 400
    return jsonify({'success': True,
                    'grade': exam_grade(questions,
                                        payload.get('answers') or {})})

@study_library_bp.route('/study/api/documents/<int:doc_id>/history')
def study_history(doc_id):
    StudyDoc, StudyQuestion = _models()
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Per-doc question history for the library page."""
    doc = StudyDoc.query.filter_by(id=doc_id, user_id=current_user.id).first()
    if not doc:
        return jsonify({'error': 'Document not found.'}), 404
    qs = StudyQuestion.query.filter_by(
        user_id=current_user.id, doc_id=doc_id).order_by(
        StudyQuestion.created_at.desc()).limit(50).all()
    return jsonify({'questions': [{
        'id': q.id, 'question': q.question, 'answer': q.answer,
        'citations': q.citations or [],
        'created': q.created_at.strftime('%Y-%m-%d %H:%M')} for q in qs]})

@study_library_bp.route('/study/api/documents/<int:doc_id>', methods=['DELETE'])
def study_delete(doc_id):
    StudyDoc, _ = _models()
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Delete a doc, its stored file, and its question history."""
    doc = StudyDoc.query.filter_by(id=doc_id, user_id=current_user.id).first()
    if not doc:
        return jsonify({'error': 'Document not found.'}), 404
    try:
        if doc.stored_filename:
            try:
                os.remove(os.path.join(current_app.config['UPLOAD_FOLDER'],
                                       doc.stored_filename))
            except OSError:
                pass
        db.session.delete(doc)
        db.session.commit()
    except Exception:
        log.exception("Study doc delete failed")
        db.session.rollback()
        return jsonify({'error': 'Could not delete this document.'}), 500
    return jsonify({'success': True})
