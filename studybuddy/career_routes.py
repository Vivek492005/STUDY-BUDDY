"""Application tracker + career assessment blueprint — Phase 4 of the monolith split.

See docs/monolith-split-plan.md. The /tracker/*, /api/tracker/*,
/api/career/* and /assessment/* routes moved here verbatim from
unified_app.register_routes. URLs, methods, rate limits, and response
shapes are unchanged. Endpoints are blueprint-prefixed (career.*) per Flask
semantics; template url_for call sites were updated at move time.

Hard rule: this module MUST NOT import unified_app at top level. Models
(JobApplication/Referral/CareerAssessment/Retrospective), the
TRACKER_STAGES/TRACKER_STAGE_IDS constants and shared helpers
(_career_hook, json_safe, _assessment_explanations) still live in
unified_app, so they are imported lazily inside the view functions — the
same pattern as studybuddy/dsa_routes.py. db/limiter come from
app.extensions, which has no import cycle. All other helpers live in
studybuddy/deadlines.py, studybuddy/career_assessment.py and sibling
studybuddy modules and are safe to import at top level.
"""

import csv
import io
import logging
import re
from datetime import UTC, date, datetime

from flask import Blueprint, jsonify, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db, limiter
from studybuddy.career_assessment import (
    QUESTIONS,
    public_questions,
    score_assessment,
    validate_assessment_inputs,
)
from studybuddy.career_graph import career_snapshot
from studybuddy.deadlines import DEADLINE_KINDS, upcoming_deadlines
from studybuddy.interview_kit import build_kit
from studybuddy.role_cards import enrich_clusters
from studybuddy.salary_data import query as salary_query

log = logging.getLogger("acra")

career_bp = Blueprint("career", __name__)

# ---------- APPLICATION TRACKER KANBAN (Wave 1 #6) ----------
def _tracker_app_to_dict(a):
    return {
        'id': a.id, 'company': a.company, 'role': a.role, 'stage': a.stage,
        'jd_url': a.jd_url, 'notes': a.notes,
        'applied_date': a.applied_date.isoformat() if a.applied_date else None,
        'deadline': a.deadline.isoformat() if a.deadline else None,
        'deadline_kind': a.deadline_kind,
        'stage_updated_at': a.stage_updated_at.isoformat() if a.stage_updated_at else None,
        'created_at': a.created_at.isoformat() if a.created_at else None,
        'resume_version_id': a.resume_version_id,
    }

def _validate_tracker_payload(payload, require_company_role=True,
                              require_company=None):
    """Validate tracker add/edit fields. Returns (data, error).

    require_company defaults to require_company_role; callers may relax
    the company check alone (wishlist target roles have no company yet)
    while keeping the role required.
    """
    if require_company is None:
        require_company = require_company_role
    data = {}
    company = (payload.get('company') or '').strip()
    role = (payload.get('role') or '').strip()
    if require_company and not company:
        return None, 'Company name is required.'
    if require_company_role and not role:
        return None, 'Role / position is required.'
    if company and len(company) > 200:
        return None, 'Company name is too long (max 200 characters).'
    if role and len(role) > 200:
        return None, 'Role is too long (max 200 characters).'
    data['company'] = company[:200]
    data['role'] = role[:200]
    jd_url = (payload.get('jd_url') or '').strip()
    if jd_url:
        if len(jd_url) > 500:
            return None, 'Link is too long (max 500 characters).'
        if not re.match(r'^https?://', jd_url, re.I):
            return None, 'Link must start with http:// or https://.'
    data['jd_url'] = jd_url[:500] or None
    notes = (payload.get('notes') or '').strip()
    if len(notes) > 5000:
        return None, 'Notes are too long (max 5,000 characters).'
    data['notes'] = notes or None
    raw_version = payload.get('resume_version_id')
    if raw_version not in (None, ''):
        try:
            data['resume_version_id'] = int(raw_version)
        except (TypeError, ValueError):
            return None, 'Invalid resume version.'
    raw_date = (payload.get('applied_date') or '').strip()
    if raw_date:
        try:
            data['applied_date'] = date.fromisoformat(raw_date)
        except ValueError:
            return None, 'Applied date must be YYYY-MM-DD.'
    else:
        data['applied_date'] = None
    return data, None

@career_bp.route('/tracker')
@login_required
def tracker_board():
    from unified_app import JobApplication, TRACKER_STAGES, _tracker_stats
    """Kanban board of the user's job applications."""
    apps = (JobApplication.query.filter_by(user_id=current_user.id)
            .order_by(JobApplication.created_at.desc()).all())
    by_stage = {sid: [] for sid, _ in TRACKER_STAGES}
    for a in apps:
        by_stage.setdefault(a.stage, []).append(a)
    try:
        deadlines = upcoming_deadlines(current_user.id)
    except Exception:
        deadlines = []
    return render_template('tracker.html', stages=TRACKER_STAGES,
                           by_stage=by_stage, stats=_tracker_stats(current_user.id),
                           deadlines=deadlines)

@career_bp.route('/tracker/add', methods=['POST'])
@limiter.limit("60 per hour")
def tracker_add():
    from unified_app import JobApplication, TRACKER_STAGE_IDS, _career_hook
    """Add a job application. CSRF via X-CSRFToken header (not exempt)."""
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to track applications.'}), 401
    payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    payload = payload or {}
    stage = (payload.get('stage') or 'wishlist').strip()
    if stage not in TRACKER_STAGE_IDS:
        return jsonify({'error': 'Invalid stage.'}), 400
    # Phase 2: a wishlist target role may not have a company yet, so the
    # "Add to tracker" handoff from discovery can pass company blank —
    # but ONLY when stage is explicitly 'wishlist' in the payload. A bare
    # add (stage omitted, defaulting to wishlist) still requires a
    # company, preserving the existing validation contract.
    # Role stays required in all cases.
    explicit_wishlist = (payload.get('stage') or '').strip() == 'wishlist'
    data, error = _validate_tracker_payload(
        payload, require_company=not explicit_wishlist)
    if error:
        return jsonify({'error': error}), 400
    app_row = JobApplication(user_id=current_user.id, stage=stage, **data)
    db.session.add(app_row)
    db.session.commit()
    _career_hook(current_user.id, 'tracker_add', app_row.id)
    return jsonify({'success': True, 'application': _tracker_app_to_dict(app_row)}), 201

@career_bp.route('/tracker/<int:app_id>/move', methods=['POST'])
def tracker_move(app_id):
    from unified_app import JobApplication, TRACKER_STAGE_IDS, _career_hook
    """Move an application to another stage. CSRF via X-CSRFToken header."""
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in.'}), 401
    app_row = JobApplication.query.filter_by(id=app_id,
                                             user_id=current_user.id).first()
    if not app_row:
        return jsonify({'error': 'Application not found.'}), 404
    payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    stage = ((payload or {}).get('stage') or '').strip()
    if stage not in TRACKER_STAGE_IDS:
        return jsonify({'error': 'Invalid stage.'}), 400
    changed = app_row.stage != stage
    if changed:
        app_row.stage = stage
        app_row.stage_updated_at = datetime.now(UTC)
        db.session.commit()
        _career_hook(current_user.id, 'tracker_move', app_row.id)
    return jsonify({'success': True, 'application': _tracker_app_to_dict(app_row)})

@career_bp.route('/tracker/<int:app_id>/edit', methods=['POST'])
def tracker_edit(app_id):
    from unified_app import JobApplication
    """Edit an application's fields. CSRF via X-CSRFToken header."""
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in.'}), 401
    app_row = JobApplication.query.filter_by(id=app_id,
                                             user_id=current_user.id).first()
    if not app_row:
        return jsonify({'error': 'Application not found.'}), 404
    payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    data, error = _validate_tracker_payload(payload or {}, require_company_role=False)
    if error:
        return jsonify({'error': error}), 400
    if data['company']:
        app_row.company = data['company']
    if data['role']:
        app_row.role = data['role']
    app_row.jd_url = data['jd_url']
    app_row.notes = data['notes']
    app_row.applied_date = data['applied_date']
    db.session.commit()
    return jsonify({'success': True, 'application': _tracker_app_to_dict(app_row)})

@career_bp.route('/tracker/<int:app_id>/delete', methods=['POST'])
def tracker_delete(app_id):
    from unified_app import JobApplication, Referral
    """Delete an application. CSRF via X-CSRFToken header."""
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in.'}), 401
    app_row = JobApplication.query.filter_by(id=app_id,
                                             user_id=current_user.id).first()
    if not app_row:
        return jsonify({'error': 'Application not found.'}), 404
    # Phase 1: keep referral history; only unlink it from this application.
    for ref in Referral.query.filter_by(application_id=app_id).all():
        ref.application_id = None
    db.session.delete(app_row)
    db.session.commit()
    return jsonify({'success': True})

@career_bp.route('/tracker/stats')
@login_required
def tracker_stats():
    from unified_app import _tracker_stats
    """JSON stats for the user's tracker."""
    return jsonify({'success': True, 'stats': _tracker_stats(current_user.id)})

# ---------- CAREER PLAN PHASE 1: tracker spine ----------
_REFERRAL_CHANNELS = ('linkedin', 'email', 'other')
_REFERRAL_STATUSES = ('asked', 'replied', 'referred', 'declined')

def _referral_to_dict(r, app_map=None):
    from unified_app import JobApplication
    app_row = None
    if r.application_id:
        if app_map is not None:
            app_row = app_map.get(r.application_id)
        else:
            app_row = db.session.get(JobApplication, r.application_id)
    return {'id': r.id, 'application_id': r.application_id,
            'company': app_row.company if app_row else None,
            'role': app_row.role if app_row else None,
            'contact_name': r.contact_name, 'channel': r.channel,
            'status': r.status, 'notes': r.notes,
            'created_at': r.created_at.isoformat() if r.created_at else None,
            'updated_at': r.updated_at.isoformat() if r.updated_at else None}

@career_bp.route('/api/tracker/<int:app_id>/kit')
@limiter.limit("30 per hour")
def tracker_kit(app_id):
    from unified_app import JobApplication, json_safe
    """Interview kit for one application (matched bank questions +
    roadmap link + checklist). CSRF via X-CSRFToken header."""
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in.'}), 401
    app_row = JobApplication.query.filter_by(id=app_id,
                                             user_id=current_user.id).first()
    if not app_row:
        return jsonify({'error': 'Application not found.'}), 404
    return jsonify(json_safe({'success': True,
                              'kit': build_kit(app_row.company, app_row.role)}))

@career_bp.route('/api/tracker/offers/compare', methods=['POST'])
@limiter.limit("30 per hour")
def tracker_offers_compare():
    from unified_app import json_safe
    """Stateless offer comparison. No DB writes.

    Body: {role?, tier?, offers: [{title, base_lpa, bonus_lpa?,
    esop_lpa?, location?}]} — exactly 2 or 3 offers. The band, when
    role+tier match salary data, aggregates across experience bands
    (min of mins, median of medians, max of maxs) and is indicative.
    """
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in.'}), 401
    payload = request.get_json(silent=True) or {}
    offers_raw = payload.get('offers')
    if not isinstance(offers_raw, list) or not (2 <= len(offers_raw) <= 3):
        return jsonify({'error': 'Provide 2 or 3 offers to compare.'}), 400

    def _num(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        f = float(v)
        if f != f or f in (float('inf'), float('-inf')):
            return None
        return f

    offers = []
    for i, o in enumerate(offers_raw):
        if not isinstance(o, dict):
            return jsonify({'error': f'Offer {i + 1} is invalid.'}), 400
        base = _num(o.get('base_lpa'))
        if base is None or base < 0:
            return jsonify({'error': f'Offer {i + 1} needs a base_lpa >= 0.'}), 400
        bonus = _num(o.get('bonus_lpa')) if o.get('bonus_lpa') is not None else 0.0
        esop = _num(o.get('esop_lpa')) if o.get('esop_lpa') is not None else 0.0
        if bonus is None or bonus < 0 or esop is None or esop < 0:
            return jsonify({'error': f'Offer {i + 1} has an invalid bonus/esop value.'}), 400
        title = (str(o.get('title') or f'Offer {i + 1}')).strip()[:200] or f'Offer {i + 1}'
        offers.append({'title': title, 'base_lpa': round(base, 2),
                       'bonus_lpa': round(bonus, 2), 'esop_lpa': round(esop, 2),
                       'total_lpa': round(base + bonus + esop, 2),
                       'location': (str(o.get('location') or '').strip()[:200] or None)})
    ranked = sorted(offers, key=lambda x: -x['total_lpa'])
    for rank, o in enumerate(ranked, start=1):
        o['rank'] = rank
    band = None
    role = (payload.get('role') or '').strip()
    tier = (payload.get('tier') or '').strip()
    if role and tier:
        try:
            entries = salary_query(role=role, tier=tier)
        except Exception:
            entries = []
        if entries:
            meds = sorted(e['median_lpa'] for e in entries)
            mid = (meds[len(meds) // 2] if len(meds) % 2
                   else (meds[len(meds) // 2 - 1] + meds[len(meds) // 2]) / 2)
            band = {'min_lpa': min(e['min_lpa'] for e in entries),
                    'median_lpa': round(mid, 2),
                    'max_lpa': max(e['max_lpa'] for e in entries)}
    top = ranked[0]
    verdict = f"{top['title']} has the highest total at {top['total_lpa']} LPA."
    if band:
        if top['total_lpa'] < band['min_lpa']:
            verdict += " That sits below the indicative band for this role and tier."
        elif top['total_lpa'] > band['max_lpa']:
            verdict += " That sits above the indicative band for this role and tier."
        else:
            verdict += " That sits within the indicative band for this role and tier."
    return jsonify(json_safe({'success': True, 'offers': ranked,
                              'band': band, 'verdict': verdict}))

@career_bp.route('/api/tracker/referrals')
def tracker_referrals_list():
    from unified_app import JobApplication, Referral, json_safe
    """List the user's referrals, with company/role joined when linked."""
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in.'}), 401
    rows = (Referral.query.filter_by(user_id=current_user.id)
            .order_by(Referral.created_at.desc()).all())
    ids = [r.application_id for r in rows if r.application_id]
    apps = JobApplication.query.filter(JobApplication.id.in_(ids)).all() if ids else []
    app_map = {a.id: a for a in apps}
    return jsonify(json_safe({'success': True,
                              'referrals': [_referral_to_dict(r, app_map) for r in rows]}))

@career_bp.route('/api/tracker/referrals', methods=['POST'])
@limiter.limit("60 per hour")
def tracker_referrals_add():
    from unified_app import JobApplication, Referral, _career_hook, json_safe
    """Add a referral ask. CSRF via X-CSRFToken header."""
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in.'}), 401
    payload = request.get_json(silent=True) or {}
    name = (payload.get('contact_name') or '').strip()
    if not name:
        return jsonify({'error': 'Contact name is required.'}), 400
    channel = (payload.get('channel') or 'other').strip()
    if channel not in _REFERRAL_CHANNELS:
        return jsonify({'error': 'Invalid channel.'}), 400
    notes = (payload.get('notes') or '').strip()
    if len(notes) > 5000:
        return jsonify({'error': 'Notes are too long (max 5,000 characters).'}), 400
    app_id = payload.get('application_id')
    app_row = None
    if app_id not in (None, ''):
        try:
            app_id = int(app_id)
        except (TypeError, ValueError):
            return jsonify({'error': 'Invalid application_id.'}), 400
        app_row = JobApplication.query.filter_by(id=app_id,
                                                 user_id=current_user.id).first()
        if not app_row:
            return jsonify({'error': 'Application not found.'}), 400
    row = Referral(user_id=current_user.id,
                   application_id=app_row.id if app_row else None,
                   contact_name=name[:120], channel=channel, status='asked',
                   notes=notes or None)
    db.session.add(row)
    db.session.commit()
    _career_hook(current_user.id, 'referral_ask', row.id)
    return jsonify(json_safe({'success': True, 'referral': _referral_to_dict(row)})), 201

@career_bp.route('/api/tracker/referrals/<int:ref_id>', methods=['PATCH'])
def tracker_referrals_patch(ref_id):
    from unified_app import Referral, json_safe
    """Update a referral's status and/or notes. CSRF via X-CSRFToken header."""
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in.'}), 401
    row = Referral.query.filter_by(id=ref_id, user_id=current_user.id).first()
    if not row:
        return jsonify({'error': 'Referral not found.'}), 404
    payload = request.get_json(silent=True) or {}
    if 'status' in payload:
        status = (payload.get('status') or '').strip()
        if status not in _REFERRAL_STATUSES:
            return jsonify({'error': 'Invalid status.'}), 400
        row.status = status
    if 'notes' in payload:
        notes = (payload.get('notes') or '').strip()
        if len(notes) > 5000:
            return jsonify({'error': 'Notes are too long (max 5,000 characters).'}), 400
        row.notes = notes or None
    db.session.commit()
    return jsonify(json_safe({'success': True, 'referral': _referral_to_dict(row)}))

@career_bp.route('/api/tracker/referrals/<int:ref_id>', methods=['DELETE'])
def tracker_referrals_delete(ref_id):
    from unified_app import Referral
    """Delete a referral. CSRF via X-CSRFToken header."""
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in.'}), 401
    row = Referral.query.filter_by(id=ref_id, user_id=current_user.id).first()
    if not row:
        return jsonify({'error': 'Referral not found.'}), 404
    db.session.delete(row)
    db.session.commit()
    return jsonify({'success': True})

@career_bp.route('/api/tracker/<int:app_id>/timeline')
def tracker_timeline(app_id):
    from unified_app import JobApplication, Retrospective, TRACKER_STAGES, json_safe
    """Timeline for one application, derived honestly from current fields.

    No history table exists yet, so events come from what the row already
    stores: creation, the current stage (only when stage_updated_at is
    more than 60s after created_at — creation sets both within the same
    second, so a larger gap marks a real move), a deadline if set, and
    one event per linked retrospective. Sorted by date ascending.
    """
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in.'}), 401
    app_row = JobApplication.query.filter_by(id=app_id,
                                             user_id=current_user.id).first()
    if not app_row:
        return jsonify({'error': 'Application not found.'}), 404

    def _aware(v):
        if v is None:
            return None
        return v if getattr(v, 'tzinfo', None) is not None else v.replace(tzinfo=UTC)

    events = []
    created = _aware(app_row.created_at)
    if created:
        events.append({'date': created.isoformat(), 'event': 'Added to tracker',
                       'detail': f"{app_row.company} — {app_row.role}"})
    updated = _aware(app_row.stage_updated_at)
    if created and updated and (updated - created).total_seconds() > 60:
        label = dict(TRACKER_STAGES).get(app_row.stage, app_row.stage)
        events.append({'date': updated.isoformat(), 'event': f'Moved to {label}',
                       'detail': f"Stage is now {label}."})
    if app_row.deadline:
        events.append({'date': app_row.deadline.isoformat(),
                       'event': f"Deadline: {app_row.deadline_kind or 'other'}",
                       'detail': f"{app_row.company} — {app_row.role}"})
    retro = Retrospective.query.filter_by(application_id=app_id,
                                          user_id=current_user.id).first()
    if retro:
        when = _aware(retro.completed_at) or _aware(retro.created_at)
        if when:
            events.append({'date': when.isoformat(), 'event': 'Retrospective written',
                           'detail': 'Completed.' if retro.completed else 'In progress.'})
    events.sort(key=lambda e: e['date'])
    return jsonify(json_safe({'success': True, 'timeline': events}))

@career_bp.route('/api/tracker/digest')
def tracker_digest():
    from unified_app import _tracker_stats, json_safe
    """Weekly digest: stale items, 7-day deadlines, stats, suggested actions.

    API-only for now — no mail infrastructure exists, so nothing is emailed.
    """
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in.'}), 401
    stats = _tracker_stats(current_user.id)
    try:
        upcoming = upcoming_deadlines(current_user.id, days=7)
    except Exception:
        upcoming = []
    actions = []
    if upcoming:
        actions.append(f"{len(upcoming)} deadline(s) in the next 7 days — check dates and prep.")
    stale = stats.get('stale') or []
    if stale:
        actions.append(f"{len(stale)} application(s) stale 14+ days — consider a follow-up.")
    counts = stats.get('counts') or {}
    if stats.get('total') and not counts.get('interview'):
        actions.append("No applications in interview stage — add target companies.")
    if not stats.get('total'):
        actions.append("No applications yet — add your first target company.")
    digest = {'stale': stale, 'upcoming_7d': upcoming, 'counts': counts,
              'total': stats.get('total', 0),
              'response_rate': stats.get('response_rate', 0.0),
              'suggested_actions': actions[:4]}
    return jsonify(json_safe({'success': True, 'digest': digest}))

@career_bp.route('/api/tracker/import', methods=['POST'])
@limiter.limit("30 per hour")
def tracker_import():
    from unified_app import JobApplication, TRACKER_STAGE_IDS, json_safe
    """Import applications from CSV text. One bad row never fails the import.

    Body: {csv: str}. Header row (case-insensitive, extra columns ignored)
    must include company, role, stage, applied_date. Per row: company+role
    required; unknown stage imports as wishlist (recorded in notes); bad
    applied_date skips the row (recorded in errors). Row numbers in
    errors/notes are file line numbers (header is line 1). Cap: 200 rows.
    """
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in.'}), 401
    payload = request.get_json(silent=True) or {}
    raw = payload.get('csv') or ''
    if not isinstance(raw, str) or not raw.strip():
        return jsonify({'error': 'Provide CSV text in the "csv" field.'}), 400
    try:
        reader = csv.DictReader(io.StringIO(raw.lstrip('\ufeff')))
        fieldnames = [(f or '').strip().lower() for f in (reader.fieldnames or [])]
    except Exception:
        return jsonify({'error': 'Could not parse the CSV.'}), 400
    required = ('company', 'role', 'stage', 'applied_date')
    missing = [c for c in required if c not in fieldnames]
    if missing:
        return jsonify({'error': f"CSV header must include: {', '.join(missing)}."}), 400
    key_map = {}
    for orig in (reader.fieldnames or []):
        key_map[(orig or '').strip().lower()] = orig
    rows = []
    for row in reader:
        if row is None:
            continue
        vals = {}
        for k, v in row.items():
            if k is None:
                continue
            vals[k] = v.strip() if isinstance(v, str) else v
        if not any(vals.values()):
            continue
        rows.append(vals)
    if len(rows) > 200:
        return jsonify({'error': 'CSV import is capped at 200 rows.'}), 400
    imported = 0
    skipped = 0
    errors = []
    notes = []
    for idx, vals in enumerate(rows, start=2):
        company = (vals.get(key_map['company']) or '').strip()
        role = (vals.get(key_map['role']) or '').strip()
        if not company or not role:
            skipped += 1
            errors.append({'row': idx, 'error': 'Company and role are required.'})
            continue
        stage = (vals.get(key_map['stage']) or 'wishlist').strip().lower()
        if stage not in TRACKER_STAGE_IDS:
            notes.append({'row': idx,
                          'note': f"Unknown stage '{stage}' — imported as wishlist."})
            stage = 'wishlist'
        applied = None
        raw_date = (vals.get(key_map['applied_date']) or '').strip()
        if raw_date:
            try:
                applied = datetime.strptime(raw_date, '%Y-%m-%d').date()
            except ValueError:
                skipped += 1
                errors.append({'row': idx,
                               'error': f"Bad applied_date '{raw_date}' (use YYYY-MM-DD)."})
                continue
        jd_url = ''
        if 'jd_url' in key_map:
            jd_url = (vals.get(key_map['jd_url']) or '').strip()
            if jd_url and not re.match(r'^https?://', jd_url, re.I):
                jd_url = ''
        note_text = ''
        if 'notes' in key_map:
            note_text = (vals.get(key_map['notes']) or '').strip()
        db.session.add(JobApplication(
            user_id=current_user.id, company=company[:200], role=role[:200],
            stage=stage, jd_url=(jd_url[:500] or None) if jd_url else None,
            notes=(note_text[:5000] or None) if note_text else None,
            applied_date=applied))
        imported += 1
    db.session.commit()
    return jsonify(json_safe({'success': True, 'imported': imported,
                              'skipped': skipped, 'errors': errors, 'notes': notes}))

# ---------- CAREER FOUNDATIONS (Phase 0): snapshot, freshness, deadlines ----------
@career_bp.route('/api/career/snapshot')
@limiter.limit("30 per hour")
def career_snapshot_api():
    from unified_app import json_safe
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Read-only career graph snapshot for the current user."""
    snapshot, warnings = career_snapshot(current_user.id)
    return jsonify(json_safe({'success': True, 'snapshot': snapshot,
                              'warnings': warnings}))

@career_bp.route('/api/career/freshness')
@limiter.limit("60 per hour")
def career_freshness_api():
    from unified_app import json_safe
    """Public: freshness of the curated career datasets (no user data)."""
    from studybuddy.freshness import all_freshness
    return jsonify(json_safe({'success': True,
                              'datasets': all_freshness()}))

@career_bp.route('/api/career/deadlines')
@limiter.limit("30 per hour")
def career_deadlines_api():
    from unified_app import json_safe
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to continue.'}), 401
    """Upcoming tracker + bookmarked-opportunity deadlines (30 days)."""
    return jsonify(json_safe({'success': True, 'deadlines':
                              upcoming_deadlines(current_user.id)}))

@career_bp.route('/tracker/<int:app_id>/deadline', methods=['POST'])
@limiter.limit("30 per hour")
def tracker_set_deadline(app_id):
    from unified_app import JobApplication, _career_hook
    """Set or clear a deadline on one application. CSRF via X-CSRFToken header."""
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in.'}), 401
    app_row = JobApplication.query.filter_by(id=app_id,
                                             user_id=current_user.id).first()
    if not app_row:
        return jsonify({'error': 'Application not found.'}), 404
    payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    raw = ((payload or {}).get('deadline') or '').strip()
    kind = ((payload or {}).get('deadline_kind') or '').strip()
    if kind and kind not in DEADLINE_KINDS:
        return jsonify({'error': 'Invalid deadline_kind. Use one of: '
                                 + ', '.join(DEADLINE_KINDS) + '.'}), 400
    if not raw:
        app_row.deadline = None
        app_row.deadline_kind = None
        app_row.deadline_set_at = None
    else:
        try:
            parsed = date.fromisoformat(raw[:10])
        except ValueError:
            return jsonify({'error': 'Invalid deadline date. Use YYYY-MM-DD.'}), 400
        app_row.deadline = parsed
        app_row.deadline_kind = kind or 'other'
        app_row.deadline_set_at = datetime.now(UTC)
    db.session.commit()
    if raw:
        _career_hook(current_user.id, 'deadline_set', app_row.id)
    return jsonify({'success': True,
                    'application': _tracker_app_to_dict(app_row)})

# ---------- CAREER-DISCOVERY ASSESSMENT (Wave 1 #7) ----------
@career_bp.route('/assessment')
@login_required
def assessment_quiz():
    """Guided career-discovery quiz (16 questions, stepper UI)."""
    return render_template('assessment.html', questions=public_questions())

@career_bp.route('/assessment/submit', methods=['POST'])
@limiter.limit("10 per hour")
def assessment_submit():
    from unified_app import CareerAssessment, _assessment_explanations, _career_hook, json_safe
    """Score the assessment answers and persist the result.

    Login required. Accepts JSON {answers: {question_id: option_index}}.
    CSRF via X-CSRFToken header (not exempt).
    """
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to take the assessment.'}), 401
    payload = request.get_json(silent=True) if request.is_json else {}
    answers, error = validate_assessment_inputs(payload or {})
    if error:
        return jsonify({'error': error}), 400

    # --- Deterministic server-side scoring ---
    clusters = score_assessment(answers)
    clusters, ai_powered = _assessment_explanations(clusters)
    # Phase 2 handoffs: roadmap/quiz links + role cards per top cluster.
    clusters = enrich_clusters(clusters)
    for c in clusters:
        c["ai_powered"] = ai_powered
    result = {
        'clusters': clusters,
        'ai_powered': ai_powered,
        'total_questions': len(QUESTIONS),
    }

    # --- Persist per user (retakes create new rows) ---
    try:
        row = CareerAssessment(
            user_id=current_user.id,
            top_clusters=json_safe(clusters),
            answers_json=json_safe(answers),
        )
        db.session.add(row)
        db.session.commit()
    except Exception:
        log.exception("Assessment persistence failed")
        db.session.rollback()
        return jsonify({'error': 'Could not save your result. Please try again.'}), 500
    _career_hook(current_user.id, 'assessment_complete', row.id)

    return jsonify({
        'success': True,
        'assessment_id': row.id,
        'result': json_safe(result),
        'saved_url': url_for('career.view_assessment', assessment_id=row.id),
    })

@career_bp.route('/assessment/<int:assessment_id>')
@login_required
def view_assessment(assessment_id):
    from unified_app import CareerAssessment
    """Render a saved assessment result (owner only)."""
    row = CareerAssessment.query.filter_by(id=assessment_id,
                                           user_id=current_user.id).first_or_404()
    # Phase 2: enrich at render time so pre-Phase-2 rows get handoffs too.
    clusters = enrich_clusters(row.top_clusters)
    return render_template('assessment_result.html',
                           clusters=clusters,
                           assessment_id=row.id, created_at=row.created_at)

@career_bp.route('/api/career/assessment/history')
@limiter.limit("60 per hour")
def assessment_history():
    from unified_app import CareerAssessment, json_safe
    """Re-take tracking: the user's past assessment results, newest first.

    Read-only over existing CareerAssessment rows. Each attempt carries
    the completed timestamp, top cluster ids, and top cluster names.
    """
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in.'}), 401
    from studybuddy.career_assessment import CLUSTERS as _CA_CLUSTERS
    rows = (CareerAssessment.query
            .filter_by(user_id=current_user.id)
            .order_by(CareerAssessment.created_at.desc(),
                      CareerAssessment.id.desc()).all())
    attempts = []
    for row in rows:
        top = row.top_clusters or []
        ids, names = [], []
        for item in top:
            if isinstance(item, dict):
                cid = str(item.get("id") or "")
                ids.append(cid)
                names.append(str(item.get("name")
                                 or _CA_CLUSTERS.get(cid, {}).get("name", cid)))
            elif isinstance(item, str):
                ids.append(item)
                names.append(_CA_CLUSTERS.get(item, {}).get("name", item))
        created = row.created_at
        attempts.append({
            "completed_at": (created.isoformat() if hasattr(created, "isoformat")
                             else str(created)),
            "top_clusters": ids,
            "top_cluster_names": names,
        })
    return jsonify(json_safe({"success": True, "attempts": attempts,
                              "count": len(attempts)}))
