"""DSA practice blueprint — Phase 2 of the monolith split.

See docs/monolith-split-plan.md. The /dsa/* pages and /api/dsa/*
routes moved here verbatim from unified_app.register_routes.
URLs, methods, rate limits, and response shapes are unchanged.
Endpoints are blueprint-prefixed (dsa.*) per Flask semantics; the
template url_for('dsa_*') call sites were updated at move time.

Hard rule: this module MUST NOT import unified_app at top level. The
DSAProgress model still lives in unified_app, so it is imported lazily
inside the view functions via _models() — the same pattern as
studybuddy/study_library.py::_models. _momentum_hook (also in
unified_app) is imported lazily inside dsa_status only. db/limiter come
from app.extensions, which has no import cycle. All other helpers live
in studybuddy/dsa_practice.py and studybuddy/a2z_lessons.py and are
safe to import at top level.
"""

import logging

from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import current_user, login_required

from app.extensions import db, limiter
from studybuddy.a2z_lessons import get_lesson as a2z_get_lesson
from studybuddy.a2z_lessons import lesson_ids as a2z_lesson_ids
from studybuddy.a2z_lessons import resolve_suggested as a2z_resolve_suggested
from studybuddy.dsa_practice import (
    all_problems,
    a2z_problems,
    expand_statuses,
    filter_problems,
    get_problem,
    progress_summary,
    public_problem,
    reveal_approach,
    reveal_hint,
    sheet_structure,
    validate_status_payload,
    weak_topic_diagnosis,
)
from studybuddy.dsa_practice import difficulties as dsa_difficulties
from studybuddy.dsa_practice import topics as dsa_topics

log = logging.getLogger("acra")

dsa_bp = Blueprint("dsa", __name__)


def _models():
    """Lazy import of unified_app models (avoids circular import)."""
    from unified_app import DSAProgress

    return DSAProgress


@dsa_bp.route('/dsa')
def dsa_home():
    """DSA practice: filter problems by topic/difficulty.

    The progress tracker is built into this page (user request,
    2026-10-23): the same summary the standalone tracker page uses,
    rendered right under the intro instead of behind a link.
    """
    DSAProgress = _models()
    progress_rows = []
    if current_user.is_authenticated:
        rows = DSAProgress.query.filter_by(user_id=current_user.id).all()
        progress_rows = [(r.problem_id, r.status) for r in rows]
    progress = progress_summary(progress_rows)
    return render_template('dsa.html', topics=dsa_topics(),
                           difficulties=dsa_difficulties(),
                           total=len(all_problems()),
                           is_authed=current_user.is_authenticated,
                           a2z=progress['a2z'],
                           classic=progress['classic'])


@dsa_bp.route('/api/dsa/problems')
def dsa_problem_list():
    """JSON problem list with optional topic/difficulty filters."""
    DSAProgress = _models()
    topic = (request.args.get('topic') or '').strip()[:60]
    difficulty = (request.args.get('difficulty') or '').strip()[:10]
    problems = filter_problems(topic or None, difficulty or None)
    out = []
    if current_user.is_authenticated:
        rows = DSAProgress.query.filter_by(user_id=current_user.id).all()
        progress = expand_statuses([(r.problem_id, r.status) for r in rows])
        for p in problems:
            p = dict(p)
            p['my_status'] = progress.get(p['id'])
            out.append(p)
    else:
        out = problems
    return jsonify({'problems': out, 'count': len(out)})


@dsa_bp.route('/api/dsa/progress')
def dsa_progress_api():
    """Progress summary JSON for the tracker embedded on /dsa.

    The embedded tracker refreshes from this endpoint when the tab
    becomes visible again, so marks made in another tab (or on a
    problem page) show up without a manual reload. Anonymous
    visitors get the zeroed summary.
    """
    DSAProgress = _models()
    progress_rows = []
    if current_user.is_authenticated:
        rows = DSAProgress.query.filter_by(user_id=current_user.id).all()
        progress_rows = [(r.problem_id, r.status) for r in rows]
    summary = progress_summary(progress_rows)
    return jsonify({'is_authed': current_user.is_authenticated,
                    'a2z': summary['a2z'],
                    'classic': summary['classic']})


@dsa_bp.route('/dsa/diagnosis')
@login_required
def dsa_diagnosis():
    """Weak-topic diagnosis dashboard (owner only)."""
    DSAProgress = _models()
    rows = DSAProgress.query.filter_by(user_id=current_user.id).all()
    progress = [{"problem_id": r.problem_id, "status": r.status,
                 "hints_used": r.hints_used} for r in rows]
    report = weak_topic_diagnosis(progress)
    return render_template('dsa_diagnosis.html', report=report)


@dsa_bp.route('/dsa/a2z')
def dsa_a2z_page():
    """Striver A2Z sheet — simple rows: solve on the original
    platform, mark progress here."""
    DSAProgress = _models()
    statuses = {}
    if current_user.is_authenticated:
        rows = DSAProgress.query.filter_by(user_id=current_user.id).all()
        statuses = expand_statuses([(r.problem_id, r.status) for r in rows])
    steps = sheet_structure(statuses)
    return render_template('dsa_a2z.html', steps=steps,
                           total=sum(s['total'] for s in steps),
                           solved=sum(s['solved'] for s in steps),
                           lesson_ids=a2z_lesson_ids())


@dsa_bp.route('/dsa/a2z/<problem_id>/learn')
def dsa_a2z_learn(problem_id):
    """In-app lesson for an A2Z theory topic or pattern problem:
    full content, code, and suggested follow-up practice."""
    DSAProgress = _models()
    problem_id = (problem_id or '').strip()[:50]
    entries = {e['id']: e for e in a2z_problems()}
    entry = entries.get(problem_id)
    lesson = a2z_get_lesson(problem_id)
    if entry is None or lesson is None:
        abort(404)
    status = None
    if current_user.is_authenticated:
        rows = DSAProgress.query.filter_by(user_id=current_user.id).all()
        status = expand_statuses(
            [(r.problem_id, r.status) for r in rows]).get(problem_id)
    suggested = a2z_resolve_suggested(
        lesson, {p['id']: p for p in all_problems()})
    return render_template('dsa_a2z_learn.html', entry=entry,
                           lesson=lesson, status=status,
                           suggested=suggested,
                           is_authed=current_user.is_authenticated)


@dsa_bp.route('/dsa/tracker')
@login_required
def dsa_tracker_page():
    """Progress tracker: overall, per-module and per-difficulty."""
    DSAProgress = _models()
    rows = DSAProgress.query.filter_by(user_id=current_user.id).all()
    progress = progress_summary([(r.problem_id, r.status) for r in rows])
    return render_template('dsa_tracker.html', a2z=progress['a2z'],
                           classic=progress['classic'])


@dsa_bp.route('/dsa/<problem_id>/untrack', methods=['POST'])
@limiter.limit("30 per hour")
def dsa_untrack(problem_id):
    """Remove a progress mark (undo a 'Mark as solved')."""
    DSAProgress = _models()
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to save progress.'}), 401
    problem_id = (problem_id or '').strip()[:50]
    if not get_problem(problem_id):
        return jsonify({'error': 'Unknown problem.'}), 404
    row = DSAProgress.query.filter_by(user_id=current_user.id,
                                      problem_id=problem_id).first()
    if row:
        db.session.delete(row)
        db.session.commit()
    return jsonify({'success': True, 'status': None})


@dsa_bp.route('/dsa/<problem_id>')
def dsa_problem_page(problem_id):
    """Problem workspace: statement, progressive hints, code area."""
    DSAProgress = _models()
    problem_id = (problem_id or '').strip()[:50]
    p = get_problem(problem_id)
    if not p:
        abort(404)
    hints_revealed = 0
    my_status = None
    saved_code = ""
    if current_user.is_authenticated:
        row = DSAProgress.query.filter_by(user_id=current_user.id,
                                          problem_id=problem_id).first()
        if row:
            hints_revealed = row.hints_used
            my_status = row.status
            saved_code = row.code or ""
        else:
            # The same problem may have been marked in the other
            # set (classic <-> A2Z share problems by LeetCode slug).
            all_rows = DSAProgress.query.filter_by(
                user_id=current_user.id).all()
            my_status = expand_statuses(
                [(r.problem_id, r.status) for r in all_rows]).get(problem_id)
    payload = public_problem(problem_id, hints_revealed)
    return render_template('dsa_problem.html', problem=payload,
                           my_status=my_status, saved_code=saved_code,
                           is_authed=current_user.is_authenticated)


def _dsa_progress_row(problem_id):
    DSAProgress = _models()
    row = DSAProgress.query.filter_by(user_id=current_user.id,
                                      problem_id=problem_id).first()
    if not row:
        row = DSAProgress(user_id=current_user.id, problem_id=problem_id)
        db.session.add(row)
    return row


@dsa_bp.route('/dsa/<problem_id>/hint', methods=['POST'])
@limiter.limit("30 per hour")
def dsa_hint(problem_id):
    """Reveal the next progressive hint. Login required; persisted."""
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to use hints.'}), 401
    problem_id = (problem_id or '').strip()[:50]
    if not get_problem(problem_id):
        return jsonify({'error': 'Unknown problem.'}), 404
    try:
        row = _dsa_progress_row(problem_id)
        hint, new_count = reveal_hint(problem_id, row.hints_used or 0)
        row.hints_used = new_count
        if row.status == 'attempted':
            pass
        db.session.commit()
    except Exception:
        log.exception("DSA hint persistence failed")
        db.session.rollback()
        return jsonify({'error': 'Could not reveal hint. Please try again.'}), 500
    return jsonify({'success': True, 'hint': hint, 'hints_used': new_count,
                    'exhausted': hint is None})


@dsa_bp.route('/dsa/<problem_id>/approach', methods=['POST'])
@limiter.limit("30 per hour")
def dsa_approach(problem_id):
    """Reveal the approach outline — only on explicit request."""
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to view the approach.'}), 401
    problem_id = (problem_id or '').strip()[:50]
    approach = reveal_approach(problem_id)
    if approach is None:
        return jsonify({'error': 'Unknown problem.'}), 404
    return jsonify({'success': True, 'approach': approach})


@dsa_bp.route('/dsa/<problem_id>/status', methods=['POST'])
@limiter.limit("30 per hour")
def dsa_status(problem_id):
    """Save code + mark solved/stuck/attempted. Login required."""
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to save progress.'}), 401
    problem_id = (problem_id or '').strip()[:50]
    if not get_problem(problem_id):
        return jsonify({'error': 'Unknown problem.'}), 404
    payload = request.get_json(silent=True) or {}
    data, error = validate_status_payload(payload)
    if error:
        return jsonify({'error': error}), 400
    try:
        row = _dsa_progress_row(problem_id)
        row.status = data['status']
        row.code = data['code']
        db.session.commit()
    except Exception:
        log.exception("DSA status persistence failed")
        db.session.rollback()
        return jsonify({'error': 'Could not save progress. Please try again.'}), 500
    # Wave 5 #27: only a *solved* problem earns XP — attempts don't.
    if row.status == 'solved':
        from unified_app import _momentum_hook  # lazy: avoids cycle
        _momentum_hook(current_user.id, 'dsa_solved')
    return jsonify({'success': True, 'status': row.status})
