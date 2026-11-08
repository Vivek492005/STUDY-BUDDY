"""Research assistant blueprint — Phase 6 of the monolith split.

See docs/monolith-split-plan.md. The /research/* pages, /api/study/* import
+ collections APIs, and /api/cite/* citation APIs moved here verbatim from
unified_app.register_routes. URLs, methods, rate limits, and response
shapes are unchanged. Endpoints are blueprint-prefixed (research.*) per
Flask semantics; template url_for call sites were updated at move time.

Hard rule: this module MUST NOT import unified_app at top level. Models
(StudyDoc/PaperImport/FlashcardSet/LitReview/GapReport/PaperQuizAttempt/
Collection) and shared helpers (json_safe, call_gemini_ai, _momentum_hook)
still live in unified_app, so they are imported lazily inside the view
functions — the same pattern as studybuddy/learn_routes.py. db/limiter
come from app.extensions, which has no import cycle. All other helpers
live in studybuddy/arxiv_import.py, studybuddy/citation_fmt.py,
studybuddy/flashcards.py, studybuddy/gap_finder.py,
studybuddy/lit_review.py and studybuddy/pdf_qa.py and are safe to import
at top level.
"""

import logging
import os
import re
import uuid
from datetime import datetime

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    jsonify,
    render_template,
    render_template_string,
    request,
)
from flask_login import current_user, login_required

from app.extensions import db, limiter
from studybuddy.arxiv_import import download_pdf as arxiv_download_pdf
from studybuddy.arxiv_import import (
    fetch_arxiv,
    fetch_citation_graph,
    fetch_doi,
    normalize_metadata,
    reading_queue,
    recent_citations,
    search_papers,
    validate_arxiv_id,
    validate_doi,
)
from studybuddy.citation_fmt import STYLES as CITATION_STYLES
from studybuddy.citation_fmt import (
    format_citation,
    from_bibtex,
    style_guide,
    to_bibtex,
)
from studybuddy.flashcards import generate_deck as fc_generate_deck
from studybuddy.flashcards import grade_mcq as fc_grade_mcq
from studybuddy.flashcards import mastery_stats as fc_mastery_stats
from studybuddy.flashcards import (
    card_due_state as fc_card_due_state,
    due_count as fc_due_count,
    srs_update as fc_srs_update,
)
from studybuddy.gap_finder import generate_gap_report as gf_generate_report
from studybuddy.lit_review import generate_review as lr_generate_review
from studybuddy.lit_review import review_to_html as lr_to_html
from studybuddy.lit_review import review_to_markdown as lr_to_markdown
from studybuddy.pdf_qa import chunk_pages as pdfqa_chunk_pages
from studybuddy.pdf_qa import parse_pdf_pages as pdfqa_parse_pages

log = logging.getLogger("acra")

research_bp = Blueprint("research", __name__)


@research_bp.route('/research')
def research_home():
    """Research hub. Signed-in users also get their personal
    research strip (library size, decks due, reviews, reports)."""
    from unified_app import StudyDoc, PaperImport, FlashcardSet, LitReview, GapReport
    stats = None
    if current_user.is_authenticated:
        decks = FlashcardSet.query.filter_by(
            user_id=current_user.id).all()
        stats = {
            "docs": StudyDoc.query.filter_by(
                user_id=current_user.id).count(),
            "imports": PaperImport.query.filter_by(
                user_id=current_user.id).count(),
            "decks": len(decks),
            "due": sum(fc_due_count(s.data or {}) for s in decks),
            "reviews": LitReview.query.filter_by(
                user_id=current_user.id).count(),
            "gap_reports": GapReport.query.filter_by(
                user_id=current_user.id).count(),
        }
    return render_template('research.html', research_stats=stats)

# Feature #25: arXiv/DOI import + citation graph
@research_bp.route('/research/import')
@login_required
def study_import_page():
    """Paper import page (arXiv / DOI)."""
    from unified_app import PaperImport
    papers = PaperImport.query.filter_by(user_id=current_user.id).order_by(
        PaperImport.created_at.desc()).limit(50).all()
    return render_template('study_import.html', papers=[{
        'id': p.id, 'title': p.title, 'authors': p.authors or [],
        'year': p.year, 'venue': p.venue, 'doi': p.doi,
        'arxiv_id': p.arxiv_id, 'openalex_id': p.openalex_id,
        'source': p.source, 'pdf_url': p.pdf_url,
        'is_retracted': bool(p.is_retracted),
        'oa_version': p.oa_version} for p in papers])

def _store_paper_import(meta):
    """Store a paper import, deduplicated per user.

    If the user already imported this paper (same arXiv ID, else same
    DOI), the existing row is returned and nothing is duplicated.
    Returns (paper, deduped: bool). Existing rows are returned as-is
    (no field refresh) so a re-import never silently rewrites history.
    """
    from unified_app import PaperImport
    arxiv_id = (meta.get('arxiv_id') or '').strip() or None
    doi = (meta.get('doi') or '').strip() or None
    if arxiv_id:
        existing = PaperImport.query.filter_by(
            user_id=current_user.id, arxiv_id=arxiv_id).first()
        if existing is not None:
            return existing, True
    elif doi:
        existing = PaperImport.query.filter_by(
            user_id=current_user.id, doi=doi).first()
        if existing is not None:
            return existing, True
    # ARC-04: fetched_at is now DateTime(timezone=True); parse ISO strings.
    _fetched = meta.get('fetched_at')
    if isinstance(_fetched, str):
        try:
            _fetched = datetime.fromisoformat(_fetched.replace('Z', '+00:00'))
        except ValueError:
            _fetched = None
    paper = PaperImport(
        user_id=current_user.id, title=(meta.get('title') or 'Untitled')[:512],
        authors=meta.get('authors') or [], abstract=meta.get('abstract'),
        year=meta.get('year'), venue=meta.get('venue'), doi=meta.get('doi'),
        arxiv_id=meta.get('arxiv_id'), pdf_url=meta.get('pdf_url'),
        openalex_id=meta.get('openalex_id'), source=meta.get('source'),
        is_retracted=bool(meta.get('is_retracted')),
        oa_version=meta.get('oa_version'),
        fetched_at=_fetched)
    db.session.add(paper)
    db.session.commit()
    return paper, False

@research_bp.route('/api/study/import/arxiv', methods=['POST'])
@limiter.limit("30 per hour")
def study_import_arxiv():
    from unified_app import json_safe
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Import a paper by arXiv ID. JSON body: {"arxiv_id": "..."}."""
    payload = request.get_json(silent=True) or {}
    arxiv_id = validate_arxiv_id(payload.get('arxiv_id') or '')
    if not arxiv_id:
        return jsonify({'error': 'Invalid arXiv ID. Use formats like 1706.03762.'}), 400
    meta = normalize_metadata(fetch_arxiv(arxiv_id))
    if meta.get('error'):
        return jsonify({'error': meta['error']}), 502
    try:
        paper, deduped = _store_paper_import(meta)
    except Exception:
        log.exception("arXiv import store failed")
        db.session.rollback()
        return jsonify({'error': 'Could not save this paper.'}), 500
    meta['import_id'] = paper.id
    meta['deduped'] = deduped
    return jsonify(json_safe(meta))

@research_bp.route('/api/study/import/doi', methods=['POST'])
@limiter.limit("30 per hour")
def study_import_doi():
    from unified_app import json_safe
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Import a paper by DOI. JSON body: {"doi": "..."}."""
    payload = request.get_json(silent=True) or {}
    doi = validate_doi(payload.get('doi') or '')
    if not doi:
        return jsonify({'error': 'Invalid DOI. Use formats like 10.1038/nature12373.'}), 400
    meta = normalize_metadata(fetch_doi(doi))
    if meta.get('error'):
        return jsonify({'error': meta['error']}), 502
    try:
        paper, deduped = _store_paper_import(meta)
    except Exception:
        log.exception("DOI import store failed")
        db.session.rollback()
        return jsonify({'error': 'Could not save this paper.'}), 500
    meta['import_id'] = paper.id
    meta['deduped'] = deduped
    return jsonify(json_safe(meta))

@research_bp.route('/api/study/import/<path:openalex_id>/graph')
@limiter.limit("30 per hour")
def study_import_graph(openalex_id):
    from unified_app import json_safe
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Citation graph around an imported paper (cached 24h)."""
    from urllib.parse import unquote
    graph = fetch_citation_graph(unquote(openalex_id), max_nodes=25)
    if graph.get('error'):
        return jsonify({'error': graph['error']}), 502
    return jsonify(json_safe(graph))

def _fetch_pdf_into_library(pdf_url, title, arxiv_id, doi):
    """Download a PDF and store it as a StudyDoc for the current user.

    Shared by the two-step PDF route and one-click full import so both
    build the identical StudyDoc shape. Returns (doc_dict, err_type)
    where err_type is None on success else "download" | "no-text" |
    "failed". The downloaded file is removed on every failure path;
    on success it stays as the doc's stored_filename.
    """
    from unified_app import StudyDoc
    if not (pdf_url or '').lower().startswith(('http://', 'https://')):
        return None, "download"
    filename = f"{uuid.uuid4().hex}.pdf"
    filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
    try:
        downloaded = arxiv_download_pdf(pdf_url, filepath)
        if not downloaded and arxiv_id:
            # OpenAlex sometimes points at mirrors that block direct
            # download; arXiv's own PDF URL is deterministic — try it.
            downloaded = arxiv_download_pdf(
                f"https://arxiv.org/pdf/{arxiv_id}", filepath)
        if not downloaded:
            return None, "download"
        pages = pdfqa_parse_pages(filepath)
        if not any((p or '').strip() for p in pages):
            try:
                os.remove(filepath)
            except OSError:
                pass
            return None, "no-text"
        chunks = pdfqa_chunk_pages(pages)
        doc = StudyDoc(
            user_id=current_user.id,
            title=(title or 'Imported paper').strip()[:255],
            stored_filename=filename,
            source='arxiv' if arxiv_id else 'doi',
            source_ref=arxiv_id or doi,
            num_pages=len(pages), num_chunks=len(chunks),
            char_count=sum(len(c.get('text', '')) for c in chunks),
            data={'chunks': [dict(c) for c in chunks],
                  'truncated': bool(getattr(chunks, 'truncated', False))})
        db.session.add(doc)
        db.session.commit()
    except Exception:
        log.exception("Study import-PDF failed")
        db.session.rollback()
        try:
            os.remove(filepath)
        except OSError:
            pass
        return None, "failed"
    return {'id': doc.id, 'title': doc.title, 'pages': doc.num_pages,
            'chunks': doc.num_chunks}, None

_PDF_ERRORS = {
    "download": (502, 'Could not download the PDF.'),
    "no-text": (400, 'No extractable text found in this PDF.'),
    "failed": (500, 'Could not import this PDF into your library.'),
}

@research_bp.route('/api/study/import/pdf', methods=['POST'])
@limiter.limit("20 per hour")
def study_import_pdf():
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Fetch an imported paper's PDF into the study library (#25 -> #21).

    JSON body: {"pdf_url": ..., "title": ..., "arxiv_id"/"doi": ...}.
    """
    payload = request.get_json(silent=True) or {}
    pdf_url = (payload.get('pdf_url') or '').strip()
    if not pdf_url.lower().startswith(('http://', 'https://')):
        return jsonify({'error': 'Invalid PDF URL.'}), 400
    arxiv_id = (payload.get('arxiv_id') or '').strip()
    doi = (payload.get('doi') or '').strip()
    doc, err = _fetch_pdf_into_library(
        pdf_url, payload.get('title'), arxiv_id, doi)
    if err:
        status, message = _PDF_ERRORS[err]
        return jsonify({'error': message}), status
    return jsonify({'success': True, 'doc': doc})

@research_bp.route('/api/study/import/search', methods=['POST'])
@limiter.limit("30 per hour")
def study_import_search():
    from unified_app import json_safe
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Keyword search across OpenAlex + arXiv + Semantic Scholar.

    JSON body: {"query": "...", "max_results"?: 10} (capped at 25).
    Results are merged + deduped (DOI, else arXiv ID, else title).
    """
    payload = request.get_json(silent=True) or {}
    query = (payload.get('query') or '').strip()
    if not query:
        return jsonify({'error': 'Please provide a search query.'}), 400
    try:
        max_results = int(payload.get('max_results') or 10)
    except (TypeError, ValueError):
        max_results = 10
    max_results = max(1, min(25, max_results))
    results = search_papers(query, max_results)
    return jsonify(json_safe({'success': True, 'results': results}))

@research_bp.route('/api/study/import/full', methods=['POST'])
@limiter.limit("30 per hour")
def study_import_full():
    from unified_app import json_safe
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """One-click import: metadata + PDF into the library in one call.

    JSON body: exactly one of {"arxiv_id": ...} or {"doi": ...}.
    The PDF step is best-effort: a PDF failure never fails the
    metadata import (doc is null + doc_error explains why).
    """
    payload = request.get_json(silent=True) or {}
    arxiv_raw = (payload.get('arxiv_id') or '').strip()
    doi_raw = (payload.get('doi') or '').strip()
    if bool(arxiv_raw) == bool(doi_raw):
        return jsonify({'error': 'Provide exactly one of arxiv_id or doi.'}), 400
    if arxiv_raw:
        arxiv_id = validate_arxiv_id(arxiv_raw)
        if not arxiv_id:
            return jsonify({'error': 'Invalid arXiv ID. Use formats like 1706.03762.'}), 400
        meta = normalize_metadata(fetch_arxiv(arxiv_id))
    else:
        doi = validate_doi(doi_raw)
        if not doi:
            return jsonify({'error': 'Invalid DOI. Use formats like 10.1038/nature12373.'}), 400
        meta = normalize_metadata(fetch_doi(doi))
    if meta.get('error'):
        return jsonify({'error': meta['error']}), 502
    try:
        paper, deduped = _store_paper_import(meta)
    except Exception:
        log.exception("Full import store failed")
        db.session.rollback()
        return jsonify({'error': 'Could not save this paper.'}), 500
    paper_dict = {
        'id': paper.id, 'title': paper.title,
        'authors': paper.authors or [], 'year': paper.year,
        'venue': paper.venue, 'doi': paper.doi,
        'arxiv_id': paper.arxiv_id, 'pdf_url': paper.pdf_url,
        'openalex_id': paper.openalex_id, 'source': paper.source,
        'is_retracted': bool(paper.is_retracted),
        'oa_version': paper.oa_version}
    response = {'success': True, 'paper': paper_dict, 'doc': None,
                'deduped': deduped}
    pdf_url = meta.get('pdf_url')
    if not pdf_url:
        response['doc_error'] = 'No PDF URL available for this paper.'
    else:
        doc, err = _fetch_pdf_into_library(
            pdf_url, meta.get('title'), meta.get('arxiv_id'),
            meta.get('doi'))
        if err:
            response['doc_error'] = _PDF_ERRORS[err][1]
        else:
            response['doc'] = doc
    return jsonify(json_safe(response))

@research_bp.route('/api/study/import/<int:paper_id>', methods=['DELETE'])
def study_import_delete(paper_id):
    from unified_app import PaperImport
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Delete one of the user's imports (owner only; others get 404)."""
    paper = PaperImport.query.filter_by(
        id=paper_id, user_id=current_user.id).first()
    if paper is None:
        return jsonify({'error': 'Paper not found.'}), 404
    db.session.delete(paper)
    db.session.commit()
    return jsonify({'success': True})

@research_bp.route('/api/study/import/queue', methods=['POST'])
@limiter.limit("30 per hour")
def study_import_queue():
    from unified_app import PaperImport, json_safe
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Prioritized reading queue over the user's imports.

    JSON body: {"minutes_per_paper"?: 45 (clamped 5..240),
    "interests"?: "keyword string"}. Interests are split on
    commas/whitespace into keywords for reading_queue.
    """
    payload = request.get_json(silent=True) or {}
    try:
        minutes = int(payload.get('minutes_per_paper') or 45)
    except (TypeError, ValueError):
        minutes = 45
    minutes = max(5, min(240, minutes))
    interests_raw = payload.get('interests') or ''
    if isinstance(interests_raw, list):
        interests = [str(i) for i in interests_raw]
    else:
        interests = [w for w in re.split(r"[,;\s]+", str(interests_raw)) if w]
    imports = PaperImport.query.filter_by(
        user_id=current_user.id).order_by(
            PaperImport.created_at.desc()).all()
    if not imports:
        return jsonify({'error': 'No imported papers yet — import a paper first to build a reading queue.'}), 400
    papers = [{'id': p.id, 'title': p.title, 'year': p.year,
               'abstract': p.abstract or ''} for p in imports]
    result = reading_queue(papers, interests=interests,
                           minutes_per_paper=minutes)
    return jsonify(json_safe({'success': True, 'queue': result}))

@research_bp.route('/api/study/import/citations')
@limiter.limit("30 per hour")
def study_import_citations():
    from unified_app import PaperImport, json_safe
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """New citations to the user's imports + bridge papers.

    Uses the user's up to 8 most recent imports that have an
    OpenAlex record. A bridge is a paper citing >=2 of my imports.
    """
    imports = PaperImport.query.filter_by(
        user_id=current_user.id).filter(
            PaperImport.openalex_id.isnot(None)).order_by(
                PaperImport.created_at.desc()).limit(8).all()
    if not imports:
        return jsonify({'success': True, 'recent': [], 'bridges': [],
                        'note': 'No imported papers with OpenAlex records yet — import a paper first to track new citations.'})
    result = recent_citations(
        [p.openalex_id for p in imports], per_paper=5, max_papers=8)
    return jsonify(json_safe({'success': True,
                              'recent': result.get('recent', []),
                              'bridges': result.get('bridges', [])}))

def _collection_dict(collection):
    return {
        'id': collection.id, 'name': collection.name,
        'token': collection.token,
        'url': f"/research/collections/{collection.token}",
        'paper_count': len(collection.paper_ids or [])}

def _validate_collection_paper_ids(raw_ids):
    """Clean + ownership-check a paper_ids list. Returns (ids, error)."""
    from unified_app import PaperImport
    if not isinstance(raw_ids, list):
        return None, 'paper_ids must be a list.'
    clean = []
    for raw in raw_ids:
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            return None, 'paper_ids must be integers.'
        if pid not in clean:
            clean.append(pid)
    if len(clean) > 100:
        return None, 'A collection holds at most 100 papers.'
    if clean:
        owned = {p.id for p in PaperImport.query.filter_by(
            user_id=current_user.id).filter(
                PaperImport.id.in_(clean)).all()}
        if set(clean) - owned:
            return None, 'Some papers do not belong to you.'
    return clean, None

@research_bp.route('/api/study/collections', methods=['POST'])
def study_collection_create():
    from unified_app import Collection
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Create a token-shared collection. JSON: {"name", "paper_ids"[]}.

    Returns the public URL; anyone with the token can read it.
    """
    payload = request.get_json(silent=True) or {}
    name = (payload.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Please provide a collection name.'}), 400
    paper_ids, error = _validate_collection_paper_ids(
        payload.get('paper_ids') or [])
    if error:
        return jsonify({'error': error}), 400
    collection = Collection(
        user_id=current_user.id, name=name[:120],
        token=uuid.uuid4().hex, paper_ids=paper_ids)
    db.session.add(collection)
    db.session.commit()
    return jsonify({'success': True,
                    'collection': _collection_dict(collection)})

@research_bp.route('/api/study/collections')
def study_collection_list():
    from unified_app import Collection
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """List the user's collections."""
    collections = Collection.query.filter_by(
        user_id=current_user.id).order_by(
            Collection.created_at.desc()).all()
    return jsonify({'collections': [_collection_dict(c) for c in collections]})

@research_bp.route('/api/study/collections/<token>/papers', methods=['POST'])
def study_collection_set_papers(token):
    from unified_app import Collection
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Replace a collection's paper list (owner only; others get 404)."""
    collection = Collection.query.filter_by(
        token=token, user_id=current_user.id).first()
    if collection is None:
        return jsonify({'error': 'Collection not found.'}), 404
    payload = request.get_json(silent=True) or {}
    paper_ids, error = _validate_collection_paper_ids(
        payload.get('paper_ids') or [])
    if error:
        return jsonify({'error': error}), 400
    collection.paper_ids = paper_ids
    db.session.commit()
    return jsonify({'success': True,
                    'collection': _collection_dict(collection)})

@research_bp.route('/research/collections/<token>')
def study_collection_public(token):
    """Public read-only view of a token-shared collection.

    Rendered inline (no template file) so the frontend owner can
    replace it with a styled template later without a backend change.
    """
    from unified_app import PaperImport, Collection
    collection = Collection.query.filter_by(token=token).first()
    if collection is None:
        abort(404)
    wanted = collection.paper_ids or []
    rows = PaperImport.query.filter(
        PaperImport.id.in_(wanted)).all() if wanted else []
    by_id = {p.id: p for p in rows}
    papers = [by_id[pid] for pid in wanted if pid in by_id]
    return render_template_string(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>{{ name|e }} — STUDY-BUDDY collection</title></head>"
        "<body><h1>{{ name|e }}</h1>"
        "{% if not papers %}<p>No papers in this collection yet.</p>{% endif %}"
        "<ul>{% for p in papers %}<li><strong>{{ p.title|e }}</strong>"
        "{% if p.authors %} — {{ p.authors|join(', ')|e }}{% endif %}"
        "{% if p.year %} ({{ p.year }}){% endif %}"
        "{% if p.venue %} — {{ p.venue|e }}{% endif %}"
        "{% if p.doi %} — <a href='https://doi.org/{{ p.doi|e }}'>DOI</a>{% endif %}"
        "{% if p.arxiv_id %} — <a href='https://arxiv.org/abs/{{ p.arxiv_id|e }}'>arXiv</a>{% endif %}"
        "</li>{% endfor %}</ul></body></html>",
        name=collection.name, papers=papers)

# Feature #24: citation formatter (IEEE/APA/MLA/Vancouver/Chicago + BibTeX)
@research_bp.route('/research/cite')
def cite_page():
    """Citation formatter page (public utility)."""
    return render_template('study_cite.html')

# ================= Live Data Engine: trending research =================
@research_bp.route('/research/trending')
def trending_page():
    """Live trending papers from arXiv + OpenAlex (public)."""
    from studybuddy import trending as trending_mod
    papers = trending_mod.get_trending(40)
    freshness = trending_mod.get_freshness()
    return render_template('study_trending.html', papers=papers,
                           freshness=freshness)

# ================= Wave 4 #22: literature reviews =================
def _review_paper_metas(user_id, docs):
    """Best-effort PaperImport metadata per doc (for BibTeX enrichment)."""
    from unified_app import PaperImport
    metas = {}
    try:
        imports = PaperImport.query.filter_by(user_id=user_id).all()
    except Exception:
        return metas
    for d in docs:
        ref = (d.source_ref or '').strip().lower()
        if not ref:
            continue
        for pi in imports:
            if (pi.arxiv_id or '').strip().lower() == ref or \
               (pi.doi or '').strip().lower() == ref:
                metas[d.id] = {'title': pi.title, 'authors': pi.authors or [],
                               'year': pi.year, 'venue': pi.venue, 'doi': pi.doi}
                break
    return metas

@research_bp.route('/research/review')
@login_required
def litreview_home():
    """Literature review generator page."""
    from unified_app import StudyDoc, LitReview
    docs = StudyDoc.query.filter_by(user_id=current_user.id).order_by(
        StudyDoc.created_at.desc()).all()
    reviews = LitReview.query.filter_by(user_id=current_user.id).order_by(
        LitReview.created_at.desc()).all()
    return render_template('litreview_home.html',
                           documents=[{'id': d.id, 'title': d.title,
                                       'chunks': d.num_chunks} for d in docs],
                           reviews=[{'id': r.id, 'title': r.title,
                                     'docs': len(r.doc_ids or []),
                                     'created': r.created_at.strftime('%Y-%m-%d')}
                                    for r in reviews])

@research_bp.route('/research/api/review/generate', methods=['POST'])
@limiter.limit("10 per hour")
def litreview_generate():
    from unified_app import StudyDoc, LitReview, call_gemini_ai
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Generate a cited literature review over 2+ library documents.

    JSON body: {"doc_ids": [int], "title": str}.
    CSRF via X-CSRFToken header (not exempt)."""
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get('doc_ids') or []
    title = (payload.get('title') or '').strip()[:120]
    if not isinstance(raw_ids, list) or len(raw_ids) < 2:
        return jsonify({'error': 'Select at least 2 documents.'}), 400
    try:
        doc_ids = [int(d) for d in raw_ids[:10]]
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid document selection.'}), 400
    try:
        docs = StudyDoc.query.filter(
            StudyDoc.id.in_(doc_ids),
            StudyDoc.user_id == current_user.id).all()
        if len(docs) < 2:
            return jsonify({'error': 'Select at least 2 of your documents.'}), 400

        def _polish(text):
            return call_gemini_ai(
                "Polish this academic synthesis paragraph for clarity and flow. "
                "Do not add new claims or citations; keep every [Title, p.X] "
                "marker exactly as-is:\n\n" + text)

        review = lr_generate_review(
            docs, paper_metas=_review_paper_metas(current_user.id, docs),
            gemini_polish=_polish)
        if not isinstance(review, dict) or review.get('error'):
            return jsonify({'error': (review or {}).get('error')
                            or 'Could not build a review from these documents.'}), 400
        lr = LitReview(user_id=current_user.id,
                       title=title or 'Literature review',
                       doc_ids=[d.id for d in docs], data=review)
        db.session.add(lr)
        db.session.commit()
    except Exception:
        log.exception("Review generation failed")
        db.session.rollback()
        return jsonify({'error': 'Could not generate the review. Please try again.'}), 500
    return jsonify({'success': True, 'id': lr.id})

@research_bp.route('/research/review/<int:rid>')
@login_required
def litreview_view(rid):
    """View a saved literature review."""
    from unified_app import LitReview
    lr = LitReview.query.filter_by(id=rid, user_id=current_user.id).first_or_404()
    return render_template('litreview_view.html', review=lr)

@research_bp.route('/research/review/<int:rid>/export')
@login_required
def litreview_export(rid):
    """Download a review as markdown or HTML."""
    from unified_app import LitReview
    lr = LitReview.query.filter_by(id=rid, user_id=current_user.id).first_or_404()
    fmt = (request.args.get('format') or 'md').lower()
    data = lr.data or {}
    if fmt == 'html':
        content, mimetype, ext = lr_to_html(data), 'text/html', 'html'
    else:
        content, mimetype, ext = lr_to_markdown(data), 'text/markdown', 'md'
    safe = re.sub(r'[^a-zA-Z0-9_-]+', '-', (lr.title or 'review')).strip('-') or 'review'
    return Response(content, mimetype=mimetype,
                    headers={'Content-Disposition':
                             'attachment; filename=%s.%s' % (safe, ext)})

@research_bp.route('/research/api/review/<int:rid>', methods=['DELETE'])
@limiter.limit("30 per hour")
def litreview_delete(rid):
    from unified_app import LitReview
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Delete a saved literature review. CSRF via X-CSRFToken header."""
    try:
        lr = LitReview.query.filter_by(id=rid, user_id=current_user.id).first()
        if not lr:
            return jsonify({'error': 'Review not found.'}), 404
        db.session.delete(lr)
        db.session.commit()
    except Exception:
        log.exception("Review delete failed")
        db.session.rollback()
        return jsonify({'error': 'Could not delete the review.'}), 500
    return jsonify({'success': True})

# ================= Wave 4 #23: research-gap finder =================
@research_bp.route('/research/gaps')
@login_required
def gapfinder_home():
    """Research-gap finder page (replaces stub)."""
    from unified_app import StudyDoc, GapReport
    docs = StudyDoc.query.filter_by(user_id=current_user.id).order_by(
        StudyDoc.created_at.desc()).all()
    reports = GapReport.query.filter_by(user_id=current_user.id).order_by(
        GapReport.created_at.desc()).limit(10).all()
    return render_template('gapfinder_home.html',
                           documents=[{'id': d.id, 'title': d.title,
                                       'chunks': d.num_chunks} for d in docs],
                           reports=[{'id': r.id,
                                     'docs': len(r.doc_ids or []),
                                     'gaps': len((r.data or {}).get('gaps') or []),
                                     'created': r.created_at.strftime('%Y-%m-%d')}
                                    for r in reports])

@research_bp.route('/research/api/gaps/analyze', methods=['POST'])
@limiter.limit("10 per hour")
def gaps_analyze():
    from unified_app import StudyDoc, PaperImport, GapReport
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Analyze a paper collection for research gaps.

    JSON body: {"doc_ids": [int]} (empty = whole library).
    CSRF via X-CSRFToken header (not exempt)."""
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get('doc_ids') or []
    try:
        wanted = [int(d) for d in raw_ids[:20]] if isinstance(raw_ids, list) else []
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid document selection.'}), 400
    try:
        q = StudyDoc.query.filter_by(user_id=current_user.id)
        docs = q.filter(StudyDoc.id.in_(wanted)).all() if wanted else \
            q.order_by(StudyDoc.created_at.desc()).all()
        if not docs:
            return jsonify({'error': 'Your library is empty — upload a PDF first.'}), 400
        imports = PaperImport.query.filter_by(user_id=current_user.id).all()
        report = gf_generate_report(docs, imports)
        if not isinstance(report, dict):
            return jsonify({'error': 'Could not analyze these documents.'}), 500
        gr = GapReport(user_id=current_user.id,
                       doc_ids=[d.id for d in docs], data=report)
        db.session.add(gr)
        db.session.commit()
    except Exception:
        log.exception("Gap analysis failed")
        db.session.rollback()
        return jsonify({'error': 'Could not analyze right now. Please try again.'}), 500
    return jsonify({'success': True, 'id': gr.id, 'report': report})

@research_bp.route('/research/api/gaps/<int:gid>')
@login_required
def gaps_get(gid):
    """Fetch a saved gap report as JSON."""
    from unified_app import GapReport
    gr = GapReport.query.filter_by(id=gid, user_id=current_user.id).first_or_404()
    return jsonify({'success': True, 'report': gr.data or {},
                    'doc_ids': gr.doc_ids or []})

# ================= Wave 4 #26: flashcards & paper quizzes =================
@research_bp.route('/research/flashcards')
@login_required
def flashcards_home():
    """Flashcards & quizzes hub (replaces stub)."""
    from unified_app import StudyDoc, FlashcardSet
    docs = StudyDoc.query.filter_by(user_id=current_user.id).order_by(
        StudyDoc.created_at.desc()).all()
    sets = FlashcardSet.query.filter_by(user_id=current_user.id).order_by(
        FlashcardSet.created_at.desc()).all()
    return render_template('flashcards_home.html',
                           documents=[{'id': d.id, 'title': d.title,
                                       'chunks': d.num_chunks} for d in docs],
                           sets=[{'id': s.id, 'doc_id': s.doc_id,
                                  'cards': s.card_count,
                                  'due': fc_due_count(s.data or {}),
                                  'created': s.created_at.strftime('%Y-%m-%d')}
                                 for s in sets],
                           total_due=sum(fc_due_count(s.data or {})
                                         for s in sets))

@research_bp.route('/research/api/flashcards/generate', methods=['POST'])
@limiter.limit("10 per hour")
def flashcards_generate():
    from unified_app import StudyDoc, FlashcardSet
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Generate a flashcard/MCQ deck from one document.

    JSON body: {"doc_id": int}. CSRF via X-CSRFToken header (not exempt)."""
    payload = request.get_json(silent=True) or {}
    try:
        doc_id = int(payload.get('doc_id') or 0)
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid document.'}), 400
    try:
        doc = StudyDoc.query.filter_by(id=doc_id,
                                       user_id=current_user.id).first()
        if not doc:
            return jsonify({'error': 'Document not found.'}), 404
        deck = fc_generate_deck(doc)
        cards = (deck or {}).get('cards') or []
        if not cards:
            return jsonify({'error': 'No extractable study content found in this document.'}), 400
        fs = FlashcardSet(user_id=current_user.id, doc_id=doc.id,
                          card_count=len(cards), data=deck)
        db.session.add(fs)
        db.session.commit()
    except Exception:
        log.exception("Flashcard generation failed")
        db.session.rollback()
        return jsonify({'error': 'Could not generate flashcards. Please try again.'}), 500
    return jsonify({'success': True, 'id': fs.id, 'cards': len(cards)})

@research_bp.route('/research/flashcards/<int:sid>')
@login_required
def flashcards_review(sid):
    """Spaced-repetition review + quiz UI for one deck."""
    from unified_app import StudyDoc, FlashcardSet
    fs = FlashcardSet.query.filter_by(id=sid,
                                      user_id=current_user.id).first_or_404()
    doc = StudyDoc.query.filter_by(id=fs.doc_id).first()
    cards = (fs.data or {}).get('cards') or []
    progress = (fs.data or {}).get('progress') or {}
    # Enrich each card with its SRS state so the UI can offer a
    # due-only review queue and show the next due date per card.
    enriched = []
    for c in cards:
        c = dict(c)
        entry = progress.get(c.get('id')) or {}
        c['srs_state'] = fc_card_due_state(entry)
        c['srs_due'] = entry.get('due')
        enriched.append(c)
    # Strip nothing: the UI needs questions/options; answers stay in the
    # payload but quiz grading is server-side, so no leak concern here.
    return render_template('flashcards_review.html', set_id=fs.id,
                           doc_title=doc.title if doc else 'Document',
                           stats=fc_mastery_stats(fs.data or {}),
                           cards=enriched)

@research_bp.route('/research/api/flashcards/<int:sid>/progress', methods=['POST'])
@limiter.limit("60 per hour")
def flashcards_progress(sid):
    from unified_app import FlashcardSet, _momentum_hook
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Mark a card known/unknown. JSON: {"card_id": str, "known": bool}."""
    payload = request.get_json(silent=True) or {}
    card_id = str(payload.get('card_id') or '')
    known = bool(payload.get('known'))
    if not card_id:
        return jsonify({'error': 'Missing card_id.'}), 400
    try:
        fs = FlashcardSet.query.filter_by(id=sid,
                                          user_id=current_user.id).first()
        if not fs:
            return jsonify({'error': 'Deck not found.'}), 404
        data = fs.data or {}
        cards = {c.get('id') for c in data.get('cards') or []}
        if card_id not in cards:
            return jsonify({'error': 'Unknown card.'}), 400
        progress = data.setdefault('progress', {})
        entry = progress.setdefault(card_id, {'known': False, 'reviews': 0})
        entry['known'] = known
        entry['reviews'] = int(entry.get('reviews') or 0) + 1
        # SM-2-lite: schedule the next review (ease/interval/due).
        fc_srs_update(entry, known)
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(fs, 'data')
        db.session.commit()
    except Exception:
        log.exception("Flashcard progress failed")
        db.session.rollback()
        return jsonify({'error': 'Could not save progress.'}), 500
    # Wave 5 #27: a finished review session (5+ card reviews on the deck)
    # earns XP. Idempotent per day, so marking more cards never double-pays.
    try:
        _progress = (fs.data or {}).get('progress') or {}
        _total_reviews = sum(int((e or {}).get('reviews') or 0)
                             for e in _progress.values()
                             if isinstance(e, dict))
        if _total_reviews >= 5:
            _momentum_hook(current_user.id, 'flashcards_reviewed')
    except Exception:
        pass
    return jsonify({'success': True,
                    'stats': fc_mastery_stats(fs.data or {})})

@research_bp.route('/research/api/flashcards/<int:sid>/quiz', methods=['POST'])
@limiter.limit("30 per hour")
def flashcards_quiz(sid):
    from unified_app import FlashcardSet, PaperQuizAttempt, _momentum_hook
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Submit MCQ answers. JSON: {"answers": {card_id: chosen_index}}."""
    payload = request.get_json(silent=True) or {}
    answers = payload.get('answers') or {}
    if not isinstance(answers, dict):
        return jsonify({'error': 'Invalid answers.'}), 400
    try:
        fs = FlashcardSet.query.filter_by(id=sid,
                                          user_id=current_user.id).first()
        if not fs:
            return jsonify({'error': 'Deck not found.'}), 404
        cards = [c for c in (fs.data or {}).get('cards') or []
                 if c.get('kind') == 'mcq']
        if not cards:
            return jsonify({'error': 'This deck has no quiz questions.'}), 400
        result = fc_grade_mcq(cards, answers)
        attempt = PaperQuizAttempt(
            user_id=current_user.id, set_id=fs.id,
            score=int(result.get('score') or 0),
            total=int(result.get('total') or 0), data=result)
        db.session.add(attempt)
        db.session.commit()
    except Exception:
        log.exception("Paper quiz grading failed")
        db.session.rollback()
        return jsonify({'error': 'Could not grade the quiz.'}), 500
    # Wave 5 #27: a completed paper quiz earns XP.
    _momentum_hook(current_user.id, 'paper_quiz_completed')
    return jsonify({'success': True, 'score': attempt.score,
                    'total': attempt.total, 'details': result.get('details') or []})

@research_bp.route('/research/api/flashcards/<int:sid>/stats')
@login_required
def flashcards_stats(sid):
    """Mastery stats for a deck."""
    from unified_app import FlashcardSet
    fs = FlashcardSet.query.filter_by(id=sid,
                                      user_id=current_user.id).first_or_404()
    return jsonify({'success': True,
                    'stats': fc_mastery_stats(fs.data or {})})

@research_bp.route('/api/cite/format', methods=['POST'])
@limiter.limit("60 per hour")
def cite_format():
    """Format one citation. JSON body: {"meta": {...}, "style": "apa"}."""
    payload = request.get_json(silent=True) or {}
    style = (payload.get('style') or '').strip().lower()
    if style not in CITATION_STYLES:
        return jsonify({'error': 'Unknown style. Choose from: {}'.format(
            ', '.join(CITATION_STYLES))}), 400
    meta = payload.get('meta') or {}
    if not isinstance(meta, dict):
        return jsonify({'error': 'Invalid metadata.'}), 400
    try:
        citation = format_citation(meta, style)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception:
        log.exception("Citation format failed")
        return jsonify({'error': 'Could not format this citation.'}), 500
    return jsonify({'citation': citation, 'style': style})

@research_bp.route('/api/cite/bibtex/export', methods=['POST'])
@limiter.limit("60 per hour")
def cite_bibtex_export():
    """Export metadata as BibTeX. JSON body: {"meta": {...}}."""
    payload = request.get_json(silent=True) or {}
    meta = payload.get('meta') or {}
    if not isinstance(meta, dict):
        return jsonify({'error': 'Invalid metadata.'}), 400
    try:
        bibtex = to_bibtex(meta)
    except Exception:
        log.exception("BibTeX export failed")
        return jsonify({'error': 'Could not export BibTeX.'}), 500
    return jsonify({'bibtex': bibtex})

@research_bp.route('/api/cite/bibtex/import', methods=['POST'])
@limiter.limit("60 per hour")
def cite_bibtex_import():
    """Parse pasted BibTeX. JSON body: {"text": "..."}."""
    from unified_app import json_safe
    payload = request.get_json(silent=True) or {}
    text = payload.get('text') or ''
    if not isinstance(text, str) or not text.strip():
        return jsonify({'error': 'Paste some BibTeX first.'}), 400
    try:
        metas = from_bibtex(text[:50000])
    except Exception:
        log.exception("BibTeX import failed")
        return jsonify({'error': 'Could not parse this BibTeX.'}), 400
    return jsonify({'metas': json_safe(metas)})

@research_bp.route('/api/cite/guide/<style>')
def cite_guide(style):
    """In-app style-guide reference for one citation style."""
    from unified_app import json_safe
    style = (style or '').strip().lower()
    if style not in CITATION_STYLES:
        return jsonify({'error': 'Unknown style.'}), 400
    try:
        guide = style_guide(style)
    except Exception:
        log.exception("Cite guide failed")
        return jsonify({'error': 'Guide unavailable.'}), 500
    return jsonify(json_safe(guide))
