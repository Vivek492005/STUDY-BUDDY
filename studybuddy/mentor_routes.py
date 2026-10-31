"""Mentor & alumni office-hours blueprint — Phase 5 of the monolith split.

See docs/monolith-split-plan.md. The /mentors/* and /api/mentors/* routes
(mentor directory, office-hour booking, Q&A, reviews, admin verify)
moved here verbatim from unified_app.register_routes. URLs, methods, rate
limits, and response shapes are unchanged. Endpoints are
blueprint-prefixed (mentors.*) per Flask semantics; template url_for call
sites were updated at move time.

Hard rule: this module MUST NOT import unified_app at top level. Models
(Mentor/MentorApplication/OfficeHour/OHBooking/OHFeedback/OHNote/OHReport/
MentorQA/MentorQuestion/MentorReview/HealthShare/Portfolio/User) and the
_career_hook helper still live in unified_app, so they are imported lazily
inside the view functions — the same pattern as studybuddy/dsa_routes.py.
db/limiter come from app.extensions, which has no import cycle.
studybuddy.community / mentor_health / momentum are safe top-level imports.
"""

import logging
from datetime import UTC, datetime

from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

import studybuddy.community as community_mod
import studybuddy.mentor_health as mentor_health_mod
import studybuddy.momentum as momentum_mod
from app.extensions import db, limiter

log = logging.getLogger("acra")

mentors_bp = Blueprint("mentors", __name__)

# ================= Wave 6 #27: mentor & alumni office hours =================
# Connect students to lived experience through structured, bounded
# conversations: mentor directory (path-filtered), bookable office-hour
# sessions with capacity + double-book protection, structured question
# templates, post-session action notes, 1-5 feedback, XP hooks, mentor
# applications, and report/admin-verify safety. JSON POSTs take the
# X-CSRFToken header (not exempt); all write routes are rate-limited.

def _mentor_api_error(message, code):
    return jsonify({'error': message}), code

def _mentor_admin_required():
    """Return a 403 response when the caller is not an admin, else None.

    SEC-17: checks the server-side ``User.is_admin`` role first, then
    falls back to the STUDYBUDDY_ADMINS env emails (bootstrap path for
    the very first admin). Every granted admin action is audit-logged
    by the caller.
    """
    if getattr(current_user, 'is_admin', False):
        return None
    if community_mod.mentor_is_admin(current_user.email):
        return None
    return _mentor_api_error(
        'Admin access required.', 403)

def _oh_session_or_404(oh_id):
    from unified_app import OfficeHour
    return db.session.get(OfficeHour, oh_id)

def _oh_active_booking_count(oh_id):
    from unified_app import OHBooking
    return (OHBooking.query
            .filter(OHBooking.office_hour_id == oh_id,
                    OHBooking.status != 'cancelled')
            .count())

def _oh_to_dict(oh):
    scheduled = oh.scheduled_at
    if scheduled is not None and scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=UTC)
    return {
        'id': oh.id,
        'mentor_id': oh.mentor_id,
        'mentor_name': oh.mentor.name if oh.mentor else None,
        'mentor_headline': oh.mentor.headline if oh.mentor else None,
        'topic': oh.topic,
        'path': oh.path,
        'path_label': community_mod.path_label(oh.path),
        'scheduled_at': scheduled.isoformat() if scheduled else None,
        'scheduled_display': scheduled.strftime('%a, %b %d · %I:%M %p')
                             if scheduled else 'TBA',
        'duration_min': oh.duration_min,
        'capacity': oh.capacity,
        'booked': _oh_active_booking_count(oh.id),
        'status': oh.status,
    }

@mentors_bp.route('/mentors')
@login_required
def mentors_hub():
    from unified_app import Mentor, OHBooking, OfficeHour
    """Hub: mentor directory (path filter), upcoming office hours,
    and the student's own bookings."""
    # Defensive seeding: first real visit creates the 14 seed mentors
    # and a starter set of upcoming office hours.
    try:
        if Mentor.query.first() is None:
            community_mod.seed_mentors()
        community_mod.seed_office_hours()
    except Exception:
        log.exception("Mentor seeding failed on hub load")
        db.session.rollback()
    path_filter = (request.args.get('path') or '').strip().lower()
    if path_filter and path_filter not in community_mod.PATH_KEYS:
        path_filter = ''
    try:
        mentors_q = Mentor.query.order_by(Mentor.verified.desc(),
                                          Mentor.name.asc())
        if path_filter:
            mentors_q = mentors_q.filter_by(path=path_filter)
        mentors = mentors_q.limit(60).all()
        upcoming = (OfficeHour.query
                    .filter(OfficeHour.status.in_(('open', 'full')),
                            OfficeHour.scheduled_at >= datetime.now(UTC))
                    .order_by(OfficeHour.scheduled_at.asc())
                    .limit(40).all())
        my_bookings = (OHBooking.query
                       .filter_by(user_id=current_user.id)
                       .filter(OHBooking.status != 'cancelled')
                       .order_by(OHBooking.created_at.desc())
                       .limit(30).all())
    except Exception:
        log.exception("Mentors hub query failed")
        db.session.rollback()
        mentors, upcoming, my_bookings = [], [], []
    return render_template('mentors_hub.html',
                           mentors=mentors,
                           upcoming=upcoming,
                           my_bookings=my_bookings,
                           paths=community_mod.PATHS,
                           path_filter=path_filter,
                           path_label=community_mod.path_label,
                           nav_active='career')

@mentors_bp.route('/mentors/<int:mentor_id>')
@login_required
def mentor_profile(mentor_id):
    from unified_app import Mentor, OHBooking, OfficeHour
    """Mentor profile + their upcoming office hours."""
    mentor = db.session.get(Mentor, mentor_id)
    if mentor is None:
        return render_template('error.html',
                               message='Mentor not found.'), 404
    try:
        upcoming = (OfficeHour.query
                    .filter_by(mentor_id=mentor.id)
                    .filter(OfficeHour.status.in_(('open', 'full')),
                            OfficeHour.scheduled_at >= datetime.now(UTC))
                    .order_by(OfficeHour.scheduled_at.asc())
                    .limit(20).all())
        past = (OfficeHour.query
                .filter_by(mentor_id=mentor.id)
                .filter(OfficeHour.status == 'completed')
                .order_by(OfficeHour.scheduled_at.desc())
                .limit(6).all())
        my_booking_ids = {b.office_hour_id
                          for b in OHBooking.query
                          .filter_by(user_id=current_user.id)
                          .filter(OHBooking.status != 'cancelled')
                          .all()}
    except Exception:
        log.exception("Mentor profile query failed")
        db.session.rollback()
        upcoming, past, my_booking_ids = [], [], set()
    return render_template('mentor_profile.html',
                           mentor=mentor,
                           upcoming=upcoming,
                           past=past,
                           my_booking_ids=my_booking_ids,
                           paths=community_mod.PATHS,
                           path_label=community_mod.path_label,
                           templates=community_mod.QUESTION_TEMPLATES,
                           nav_active='career')

@mentors_bp.route('/mentors/api/office-hours/<int:oh_id>/book', methods=['POST'])
@login_required
@limiter.limit("20 per hour")
def mentor_book_session(oh_id):
    from unified_app import OHBooking, _career_hook
    """Book a seat. JSON: {question_text?, template_id?}. Enforces
    past/double-book/capacity rules. CSRF via X-CSRFToken header."""
    oh = _oh_session_or_404(oh_id)
    if oh is None:
        return _mentor_api_error('Office hour not found.', 404)
    # Phase 5 honesty guard: seed office hours are illustrative only.
    # Booking a seed would fabricate a real interaction (and unlock
    # reviews), so it is rejected with a clear message. Real-mentor
    # bookings are unaffected.
    if oh.mentor is not None and oh.mentor.is_seed:
        return _mentor_api_error(
            'This is an illustrative profile — bookings open when real '
            'mentors join.', 400)
    payload = request.get_json(silent=True) or {}
    question_text = (payload.get('question_text') or '').strip()
    template_id = (payload.get('template_id') or '').strip()
    if template_id and template_id not in community_mod.QUESTION_TEMPLATE_IDS:
        return _mentor_api_error('Unknown question template.', 400)
    if len(question_text) > community_mod.BOOKING_MAX_QUESTION_CHARS:
        return _mentor_api_error(
            'Your question is too long (max %d characters).'
            % community_mod.BOOKING_MAX_QUESTION_CHARS, 400)
    try:
        active = _oh_active_booking_count(oh.id)
        existing = (OHBooking.query
                    .filter_by(user_id=current_user.id, office_hour_id=oh.id)
                    .filter(OHBooking.status != 'cancelled')
                    .first())
        ok, err = community_mod.booking_eligibility(oh, active, existing)
        if not ok:
            return _mentor_api_error(err, 400)
        booking = OHBooking(user_id=current_user.id,
                           office_hour_id=oh.id,
                           question_text=question_text or None,
                           status='booked')
        db.session.add(booking)
        db.session.flush()
        community_mod.refresh_session_status(oh, active + 1)
        db.session.commit()
    except Exception:
        log.exception("Mentor booking failed")
        db.session.rollback()
        return _mentor_api_error('Could not complete the booking. '
                                 'Please try again.', 500)
    _career_hook(current_user.id, 'mentor_book', booking.id)
    return jsonify({'success': True, 'booking_id': booking.id,
                    'session_status': oh.status}), 201

@mentors_bp.route('/mentors/api/bookings/<int:booking_id>/cancel', methods=['POST'])
@login_required
@limiter.limit("20 per hour")
def mentor_cancel_booking(booking_id):
    from unified_app import OHBooking
    """Cancel a booking until 1 hour before the session. Frees the seat
    and re-opens the session when it was full."""
    booking = (OHBooking.query
               .filter_by(id=booking_id, user_id=current_user.id).first())
    if booking is None:
        return _mentor_api_error('Booking not found.', 404)
    if booking.status == 'cancelled':
        return _mentor_api_error('This booking is already cancelled.', 400)
    oh = booking.session
    ok, err = community_mod.can_cancel(oh)
    if not ok:
        return _mentor_api_error(err, 400)
    try:
        booking.status = 'cancelled'
        db.session.flush()
        active = _oh_active_booking_count(oh.id)
        community_mod.refresh_session_status(oh, active)
        db.session.commit()
    except Exception:
        log.exception("Mentor cancel failed")
        db.session.rollback()
        return _mentor_api_error('Could not cancel the booking.', 500)
    return jsonify({'success': True, 'session_status': oh.status})

@mentors_bp.route('/mentors/api/bookings/<int:booking_id>/attend', methods=['POST'])
@login_required
@limiter.limit("60 per hour")
def mentor_mark_attended(booking_id):
    from unified_app import OHBooking
    """Mark attendance (for demo/testing: the student self-reports
    attendance). Awards XP via momentum."""
    booking = (OHBooking.query
               .filter_by(id=booking_id, user_id=current_user.id).first())
    if booking is None:
        return _mentor_api_error('Booking not found.', 404)
    if booking.status == 'cancelled':
        return _mentor_api_error('A cancelled booking cannot be marked '
                                 'as attended.', 400)
    if booking.status == 'attended':
        return _mentor_api_error('Attendance already recorded.', 400)
    try:
        booking.status = 'attended'
        db.session.commit()
    except Exception:
        log.exception("Mentor attend marking failed")
        db.session.rollback()
        return _mentor_api_error('Could not record attendance.', 500)
    try:
        momentum_mod.record_event(current_user.id, "mentor_session_attended")
    except Exception:
        log.exception("Mentor attendance XP failed")
    return jsonify({'success': True})

@mentors_bp.route('/mentors/api/bookings/<int:booking_id>/note', methods=['POST'])
@login_required
@limiter.limit("60 per hour")
def mentor_save_note(booking_id):
    from unified_app import OHBooking, OHNote
    """Save post-session action notes. JSON: {note_text}."""
    booking = (OHBooking.query
               .filter_by(id=booking_id, user_id=current_user.id).first())
    if booking is None:
        return _mentor_api_error('Booking not found.', 404)
    if booking.status == 'cancelled':
        return _mentor_api_error('A cancelled booking has no notes.', 400)
    payload = request.get_json(silent=True) or {}
    note_text = (payload.get('note_text') or '').strip()
    if not note_text:
        return _mentor_api_error('Write your action notes first.', 400)
    if len(note_text) > community_mod.NOTE_MAX_CHARS:
        return _mentor_api_error('Notes are too long (max %d characters).'
                                 % community_mod.NOTE_MAX_CHARS, 400)
    try:
        note = OHNote(booking_id=booking.id, user_id=current_user.id,
                      note_text=note_text)
        db.session.add(note)
        db.session.commit()
    except Exception:
        log.exception("Mentor note save failed")
        db.session.rollback()
        return _mentor_api_error('Could not save the note.', 500)
    return jsonify({'success': True, 'note_id': note.id}), 201

@mentors_bp.route('/mentors/api/bookings/<int:booking_id>/feedback', methods=['POST'])
@login_required
@limiter.limit("20 per hour")
def mentor_submit_feedback(booking_id):
    from unified_app import OHBooking, OHFeedback
    """Submit 1-5 feedback for a booking. JSON: {rating, what_helped?}.
    Awards XP via momentum."""
    booking = (OHBooking.query
               .filter_by(id=booking_id, user_id=current_user.id).first())
    if booking is None:
        return _mentor_api_error('Booking not found.', 404)
    if booking.status != 'attended':
        return _mentor_api_error('Feedback opens after you attend the '
                                 'session.', 400)
    payload = request.get_json(silent=True) or {}
    try:
        rating = int(payload.get('rating'))
    except (TypeError, ValueError):
        return _mentor_api_error('rating must be an integer 1-5.', 400)
    if not (community_mod.FEEDBACK_MIN_RATING
            <= rating <= community_mod.FEEDBACK_MAX_RATING):
        return _mentor_api_error('rating must be between 1 and 5.', 400)
    what_helped = (payload.get('what_helped') or '').strip()
    if len(what_helped) > community_mod.FEEDBACK_MAX_CHARS:
        return _mentor_api_error('what_helped is too long (max %d '
                                 'characters).'
                                 % community_mod.FEEDBACK_MAX_CHARS, 400)
    try:
        existing = (OHFeedback.query
                    .filter_by(booking_id=booking.id).first())
        if existing:
            return _mentor_api_error('Feedback already submitted for '
                                     'this booking.', 400)
        fb = OHFeedback(booking_id=booking.id, user_id=current_user.id,
                        rating=rating, what_helped=what_helped or None)
        db.session.add(fb)
        db.session.commit()
    except Exception:
        log.exception("Mentor feedback save failed")
        db.session.rollback()
        return _mentor_api_error('Could not save the feedback.', 500)
    try:
        momentum_mod.record_event(current_user.id, "mentor_feedback_given")
    except Exception:
        log.exception("Mentor feedback XP failed")
    return jsonify({'success': True, 'feedback_id': fb.id}), 201

@mentors_bp.route('/mentors/api/my-bookings')
@login_required
def mentor_my_bookings():
    from unified_app import OHBooking, OHFeedback, OHNote, OfficeHour
    """The student's bookings, optionally filtered to one mentor.
    JSON: {bookings: [{id, topic, scheduled_display, status,
    question_text, notes}]}."""
    mentor_id = request.args.get('mentor_id', type=int)
    try:
        q = OHBooking.query.filter_by(user_id=current_user.id)
        if mentor_id:
            q = q.join(OfficeHour).filter(OfficeHour.mentor_id == mentor_id)
        rows = (q.order_by(OHBooking.created_at.desc()).limit(50).all())
        items = []
        for b in rows:
            sched = b.session.scheduled_at
            if sched is not None and sched.tzinfo is None:
                sched = sched.replace(tzinfo=UTC)
            items.append({
                'id': b.id,
                'topic': b.session.topic,
                'scheduled_display': (sched.strftime('%a, %b %d · %I:%M %p')
                                      if sched else 'TBA'),
                'status': b.status,
                'question_text': b.question_text,
                'notes': [{'id': n.id, 'note_text': n.note_text}
                          for n in b.notes.order_by(OHNote.created_at.desc())],
                'has_feedback': OHFeedback.query.filter_by(booking_id=b.id).first() is not None,
            })
    except Exception:
        log.exception("Mentor my-bookings query failed")
        db.session.rollback()
        return _mentor_api_error('Could not load your bookings.', 500)
    return jsonify({'success': True, 'bookings': items})

@mentors_bp.route('/mentors/apply')
@login_required
def mentor_apply_form():
    from unified_app import MentorApplication
    """Mentor application form (GET renders the form)."""
    existing = (MentorApplication.query
                .filter_by(user_id=current_user.id)
                .order_by(MentorApplication.created_at.desc())
                .first())
    return render_template('mentor_apply.html',
                           existing=existing,
                           paths=community_mod.PATHS,
                           path_label=community_mod.path_label,
                           nav_active='career')

@mentors_bp.route('/mentors/api/apply', methods=['POST'])
@login_required
@limiter.limit("5 per hour")
def mentor_apply_submit():
    from unified_app import MentorApplication
    """Submit a mentor application. JSON or form-encoded fields:
    name, headline, path, company?, college?, bio, linkedin_url?."""
    payload = request.get_json(silent=True)
    if payload is None:
        payload = request.form
    get = payload.get
    name = (get('name') or '').strip() or current_user.name
    headline = (get('headline') or '').strip()
    path = (get('path') or 'other').strip().lower()
    company = (get('company') or '').strip() or None
    college = (get('college') or '').strip() or None
    bio = (get('bio') or '').strip()
    linkedin_url = (get('linkedin_url') or '').strip() or None
    errors = []
    if not name or len(name) > community_mod.APPLY_NAME_MAX_CHARS:
        errors.append('Name is required (max %d characters).'
                      % community_mod.APPLY_NAME_MAX_CHARS)
    if not headline or len(headline) > 200:
        errors.append('Headline is required (max 200 characters).')
    if path not in community_mod.PATH_KEYS:
        errors.append('Pick a valid career path.')
    if not bio or len(bio) < 30:
        errors.append('Bio needs at least 30 characters — tell us your '
                      'story and what you can help with.')
    if len(bio) > community_mod.APPLY_MAX_BIO_CHARS:
        errors.append('Bio is too long (max %d characters).'
                      % community_mod.APPLY_MAX_BIO_CHARS)
    if linkedin_url and not linkedin_url.startswith(('http://', 'https://')):
        errors.append('LinkedIn URL must start with http:// or https://.')
    if errors:
        return _mentor_api_error(' '.join(errors), 400)
    try:
        pending = (MentorApplication.query
                   .filter_by(user_id=current_user.id, status='pending')
                   .first())
        if pending:
            return _mentor_api_error('You already have a pending '
                                     'application.', 400)
        app_row = MentorApplication(user_id=current_user.id, name=name,
                                    headline=headline, path=path,
                                    company=company, college=college,
                                    bio=bio, linkedin_url=linkedin_url,
                                    status='pending')
        db.session.add(app_row)
        db.session.commit()
    except Exception:
        log.exception("Mentor application failed")
        db.session.rollback()
        return _mentor_api_error('Could not submit the application.', 500)
    return jsonify({'success': True, 'application_id': app_row.id}), 201

@mentors_bp.route('/mentors/api/report', methods=['POST'])
@login_required
@limiter.limit("10 per hour")
def mentor_report():
    from unified_app import Mentor, OHReport, OfficeHour
    """Report a mentor or session. JSON: {mentor_id?, office_hour_id?,
    reason, details?}."""
    payload = request.get_json(silent=True) or {}
    reason = (payload.get('reason') or '').strip().lower()
    if reason not in community_mod.REPORT_REASONS:
        return _mentor_api_error('Unknown reason. Pick one of: %s.'
                                 % ', '.join(community_mod.REPORT_REASONS),
                                 400)
    details = (payload.get('details') or '').strip() or None
    if details and len(details) > community_mod.REPORT_MAX_CHARS:
        return _mentor_api_error('Details are too long (max %d characters).'
                                 % community_mod.REPORT_MAX_CHARS, 400)
    mentor_id = payload.get('mentor_id')
    oh_id = payload.get('office_hour_id')
    if mentor_id is not None:
        try:
            mentor_id = int(mentor_id)
        except (TypeError, ValueError):
            return _mentor_api_error('mentor_id must be an integer.', 400)
        if db.session.get(Mentor, mentor_id) is None:
            return _mentor_api_error('Mentor not found.', 404)
    if oh_id is not None:
        try:
            oh_id = int(oh_id)
        except (TypeError, ValueError):
            return _mentor_api_error('office_hour_id must be an integer.',
                                     400)
        if db.session.get(OfficeHour, oh_id) is None:
            return _mentor_api_error('Office hour not found.', 404)
    if mentor_id is None and oh_id is None:
        return _mentor_api_error('Report a mentor, a session, or both.',
                                 400)
    try:
        report = OHReport(reporter_user_id=current_user.id,
                          mentor_id=mentor_id, office_hour_id=oh_id,
                          reason=reason, details=details, status='open')
        db.session.add(report)
        db.session.commit()
    except Exception:
        log.exception("Mentor report failed")
        db.session.rollback()
        return _mentor_api_error('Could not file the report.', 500)
    return jsonify({'success': True, 'report_id': report.id}), 201

@mentors_bp.route('/mentors/api/admin/verify/<int:mentor_id>', methods=['POST'])
@login_required
@limiter.limit("30 per hour")
def mentor_admin_verify(mentor_id):
    from unified_app import Mentor
    """Verify a mentor (set verified=True). SEC-17: requires the
    server-side admin role (User.is_admin) or a STUDYBUDDY_ADMINS
    bootstrap email; the action is audit-logged."""
    forbidden = _mentor_admin_required()
    if forbidden is not None:
        return forbidden
    mentor = db.session.get(Mentor, mentor_id)
    if mentor is None:
        return _mentor_api_error('Mentor not found.', 404)
    if mentor.is_seed:
        return _mentor_api_error(
            'Illustrative profiles cannot be verified.', 400)
    try:
        mentor.verified = True
        db.session.commit()
        # SEC-17 audit log (no PII beyond hashed admin id).
        log.warning("ADMIN_ACTION action=mentor_verify admin_id=%s mentor_id=%s",
                    current_user.id, mentor_id)
    except Exception:
        log.exception("Mentor verify failed")
        db.session.rollback()
        return _mentor_api_error('Could not verify the mentor.', 500)
    return jsonify({'success': True})

@mentors_bp.route('/mentors/api/admin/office-hours', methods=['POST'])
@login_required
@limiter.limit("30 per hour")
def mentor_admin_create_session():
    from unified_app import Mentor, OfficeHour
    """Admin-lite: schedule an office hour for a mentor. JSON:
    {mentor_id, topic, scheduled_at ("YYYY-MM-DD HH:MM", UTC),
    duration_min?, capacity?}. 403 unless the user's email is in
    STUDYBUDDY_ADMINS."""
    forbidden = _mentor_admin_required()
    if forbidden is not None:
        return forbidden
    payload = request.get_json(silent=True) or {}
    try:
        mentor_id = int(payload.get('mentor_id'))
    except (TypeError, ValueError):
        return _mentor_api_error('mentor_id must be an integer.', 400)
    mentor = db.session.get(Mentor, mentor_id)
    if mentor is None:
        return _mentor_api_error('Mentor not found.', 404)
    topic = (payload.get('topic') or '').strip()
    if not topic or len(topic) > 200:
        return _mentor_api_error('topic is required (max 200 characters).',
                                 400)
    raw_when = (payload.get('scheduled_at') or '').strip()
    try:
        scheduled = datetime.strptime(raw_when, "%Y-%m-%d %H:%M")
        scheduled = scheduled.replace(tzinfo=UTC)
    except ValueError:
        return _mentor_api_error('scheduled_at must look like '
                                 '"YYYY-MM-DD HH:MM" (UTC).', 400)
    if scheduled <= datetime.now(UTC):
        return _mentor_api_error('scheduled_at must be in the future.',
                                 400)
    try:
        duration_min = int(payload.get('duration_min', 45))
        capacity = int(payload.get('capacity', 12))
    except (TypeError, ValueError):
        return _mentor_api_error('duration_min and capacity must be '
                                 'integers.', 400)
    if not 15 <= duration_min <= 180:
        return _mentor_api_error('duration_min must be 15-180.', 400)
    if not 1 <= capacity <= 100:
        return _mentor_api_error('capacity must be 1-100.', 400)
    try:
        oh = OfficeHour(mentor_id=mentor.id, topic=topic, path=mentor.path,
                        scheduled_at=scheduled, duration_min=duration_min,
                        capacity=capacity, status='open')
        db.session.add(oh)
        db.session.commit()
        # SEC-17 audit log.
        log.warning("ADMIN_ACTION action=office_hour_create admin_id=%s mentor_id=%s oh_id=%s",
                    current_user.id, mentor.id, oh.id)
    except Exception:
        log.exception("Mentor session creation failed")
        db.session.rollback()
        return _mentor_api_error('Could not schedule the session.', 500)
    return jsonify({'success': True, 'office_hour': _oh_to_dict(oh)}), 201

# ================= Phase 5: mentor matching, Q&A, reviews, dashboard ====
# Honesty first: seed mentors are ILLUSTRATIVE profiles (never verified,
# never bookable, never reviewed). Every serialization carries the
# illustrative/verified labels from mentor_health_mod.mentor_to_dict.

@mentors_bp.route('/mentors/api/admin/applications/<int:application_id>/approve',
            methods=['POST'])
@login_required
@limiter.limit("30 per hour")
def mentor_admin_approve(application_id):
    from unified_app import Mentor, MentorApplication
    """Approve a mentor application (admin only): status -> approved
    and create the REAL Mentor row (verified=True, is_seed=False,
    user_id linked). Idempotent: re-approving returns the existing row.
    This is the real-supply pipeline — no mentor is ever invented."""
    forbidden = _mentor_admin_required()
    if forbidden is not None:
        return forbidden
    app_row = db.session.get(MentorApplication, application_id)
    if app_row is None:
        return _mentor_api_error('Application not found.', 404)
    try:
        mentor = (Mentor.query.filter_by(user_id=app_row.user_id,
                                         is_seed=False).first())
        if mentor is None:
            mentor = Mentor(name=app_row.name, headline=app_row.headline,
                            path=app_row.path, company=app_row.company,
                            college=app_row.college, bio=app_row.bio,
                            linkedin_url=app_row.linkedin_url,
                            verified=True, is_seed=False,
                            user_id=app_row.user_id)
            db.session.add(mentor)
        app_row.status = 'approved'
        db.session.commit()
        log.warning("ADMIN_ACTION action=mentor_approve admin_id=%s "
                    "application_id=%s mentor_id=%s",
                    current_user.id, application_id, mentor.id)
    except Exception:
        log.exception("Mentor approve failed")
        db.session.rollback()
        return _mentor_api_error('Could not approve the application.',
                                 500)
    return jsonify({'success': True,
                    'mentor': mentor_health_mod.mentor_to_dict(mentor)})

@mentors_bp.route('/api/mentors/matches')
@login_required
@limiter.limit("30 per hour")
def mentors_matches_api():
    from unified_app import Mentor, Portfolio
    """Transparent mentor matches (never raises).

    Scoring (fixed weights, see mentor_health.mentor_match_scores):
    path/cluster overlap +40, skill overlap +10 each (cap +40),
    verified real mentor +20, cap 100. Seeds are included but always
    flagged illustrative. No cluster and no portfolio skills ->
    empty matches + a note (generic verified rows are not matches).
    """
    try:
        from studybuddy.career_graph import career_snapshot
        snapshot, _warnings = career_snapshot(current_user.id)
        cluster_id = (snapshot.get("cluster") or {}).get("id")
        portfolio = Portfolio.query.filter_by(
            user_id=current_user.id).first()
        skills = []
        if portfolio is not None and isinstance(portfolio.skills, list):
            skills = [str(s) for s in portfolio.skills]
        if not cluster_id and not skills:
            return jsonify({
                'success': True, 'matches': [],
                'note': 'Complete the career discovery assessment or '
                        'add portfolio skills for personalized matches.'})
        mentors = Mentor.query.order_by(Mentor.name.asc()).all()
        matches = mentor_health_mod.mentor_match_scores(
            cluster_id, skills, mentors)
        return jsonify({'success': True, 'matches': matches})
    except Exception:
        log.exception("Mentor matches failed")
        return jsonify({'success': True, 'matches': [],
                        'note': 'Matches are unavailable right now.'})

def _ensure_mentor_qa_seeded():
    from unified_app import MentorQA
    """Backfill the MentorQA table from data/mentor_qa.json once.

    Library entries are community knowledge (mentor_id NULL,
    attribution 'Community knowledge base') — never attributed to a
    named mentor who did not write them.
    """
    try:
        if MentorQA.query.first() is None:
            for row in mentor_health_mod.load_mentor_qa():
                db.session.add(MentorQA(
                    question=row["question"], answer=row["answer"],
                    topic=row["topic"], mentor_id=None,
                    attribution=mentor_health_mod.QA_ATTRIBUTION))
            db.session.commit()
    except Exception:
        log.exception("Mentor QA seeding failed")
        db.session.rollback()

@mentors_bp.route('/api/mentors/qa')
@limiter.limit("60 per hour")
def mentors_qa_api():
    from unified_app import MentorQA
    """Public Q&A library. ?topic= filters (unknown topic -> [] + 200).
    Every entry carries its attribution; library answers are community
    knowledge, not any named mentor's words."""
    _ensure_mentor_qa_seeded()
    topic = (request.args.get('topic') or '').strip()
    try:
        q = MentorQA.query.order_by(MentorQA.id.asc())
        if topic:
            q = q.filter_by(topic=topic)
        rows = q.limit(200).all()
    except Exception:
        log.exception("Mentor QA list failed")
        db.session.rollback()
        rows = []
    return jsonify({'success': True, 'qa': [{
        'id': r.id, 'question': r.question, 'answer': r.answer,
        'topic': r.topic, 'attribution': r.attribution} for r in rows]})

@mentors_bp.route('/api/mentors/qa/ask', methods=['POST'])
@login_required
@limiter.limit("30 per hour")
def mentors_qa_ask_api():
    from unified_app import MentorQuestion
    """Ask a question into the real-mentor inbox. This stores the
    question as 'open' — it never auto-generates an answer. topic,
    when given, must be a mentor path key (else it is stored as
    general/NULL so every mentor's dashboard can see it)."""
    payload = request.get_json(silent=True) or {}
    question = (payload.get('question') or '').strip()
    if len(question) < 20:
        return jsonify({'error': 'Question needs at least 20 characters '
                                 '— give context.'}), 400
    if len(question) > 2000:
        return jsonify({'error': 'Question is too long (max 2000 '
                                 'characters).'}), 400
    topic = (payload.get('topic') or '').strip().lower() or None
    if topic is not None and topic not in community_mod.PATH_KEYS:
        topic = None
    try:
        row = MentorQuestion(user_id=current_user.id, question=question,
                             topic=topic, status='open')
        db.session.add(row)
        db.session.commit()
    except Exception:
        log.exception("Mentor QA ask failed")
        db.session.rollback()
        return jsonify({'error': 'Could not save your question. '
                                 'Please try again.'}), 500
    return jsonify({'success': True, 'question_id': row.id}), 201

@mentors_bp.route('/api/mentors/qa/mine')
@login_required
def mentors_qa_mine_api():
    from unified_app import MentorQuestion
    """The caller's own inbox questions only (never anyone else's)."""
    try:
        rows = (MentorQuestion.query
                .filter_by(user_id=current_user.id)
                .order_by(MentorQuestion.created_at.desc()).all())
    except Exception:
        log.exception("Mentor QA mine failed")
        db.session.rollback()
        rows = []
    return jsonify({'success': True, 'questions': [{
        'id': r.id, 'question': r.question, 'topic': r.topic,
        'status': r.status,
        'created_at': r.created_at.isoformat()
        if r.created_at else None} for r in rows]})

@mentors_bp.route('/api/mentors/<int:mentor_id>/reviews', methods=['POST'])
@login_required
@limiter.limit("30 per hour")
def mentors_review_create_api(mentor_id):
    from unified_app import Mentor, MentorReview, OHBooking, OfficeHour
    """Review a mentor (201). Requires a real interaction: a booking
    with this mentor in any non-cancelled state. Rating must be an
    int 1-5; one review per (user, mentor). Seeds can never be
    reviewed because seed bookings are rejected (see book guard)."""
    mentor = db.session.get(Mentor, mentor_id)
    if mentor is None:
        return jsonify({'error': 'Mentor not found.'}), 404
    payload = request.get_json(silent=True) or {}
    rating = payload.get('rating')
    if isinstance(rating, bool) or not isinstance(rating, int) \
            or not 1 <= rating <= 5:
        return jsonify({'error': 'Rating must be an integer 1-5.'}), 400
    text = (payload.get('text') or '').strip() or None
    if text is not None and len(text) > 2000:
        return jsonify({'error': 'Review is too long (max 2000 '
                                 'characters).'}), 400
    try:
        interacted = (db.session.query(OHBooking.id)
                      .join(OfficeHour,
                            OHBooking.office_hour_id == OfficeHour.id)
                      .filter(OHBooking.user_id == current_user.id,
                              OfficeHour.mentor_id == mentor.id,
                              OHBooking.status != 'cancelled')
                      .first() is not None)
        if not interacted:
            return jsonify({'error': 'Reviews require a booking with '
                                     'this mentor first.'}), 400
        existing = MentorReview.query.filter_by(
            user_id=current_user.id, mentor_id=mentor.id).first()
        if existing is not None:
            return jsonify({'error': 'You have already reviewed this '
                                     'mentor.'}), 400
        review = MentorReview(mentor_id=mentor.id,
                              user_id=current_user.id, rating=rating,
                              text=text)
        db.session.add(review)
        db.session.commit()
    except Exception:
        log.exception("Mentor review create failed")
        db.session.rollback()
        return jsonify({'error': 'Could not save your review. '
                                 'Please try again.'}), 500
    return jsonify({'success': True, 'review': {
        'id': review.id, 'rating': review.rating, 'text': review.text,
        'created_at': review.created_at.isoformat()
        if review.created_at else None}}), 201

@mentors_bp.route('/api/mentors/<int:mentor_id>/reviews')
@limiter.limit("60 per hour")
def mentors_review_list_api(mentor_id):
    from unified_app import Mentor, MentorReview
    """Public review list + average. Average is null and count 0 when
    there are no reviews — never a default 5.0 or fake value."""
    mentor = db.session.get(Mentor, mentor_id)
    if mentor is None:
        return jsonify({'error': 'Mentor not found.'}), 404
    try:
        rows = (MentorReview.query.filter_by(mentor_id=mentor.id)
                .order_by(MentorReview.created_at.desc()).all())
    except Exception:
        log.exception("Mentor review list failed")
        db.session.rollback()
        rows = []
    ratings = [r.rating for r in rows]
    average = round(sum(ratings) / len(ratings), 1) if ratings else None
    return jsonify({'success': True, 'reviews': [{
        'rating': r.rating, 'text': r.text,
        'created_at': r.created_at.isoformat()
        if r.created_at else None} for r in rows],
        'average': average, 'count': len(rows)})

@mentors_bp.route('/api/mentors/dashboard')
@login_required
@limiter.limit("30 per hour")
def mentors_dashboard_api():
    from unified_app import HealthShare, Mentor, MentorQuestion, MentorReview, OHBooking, OfficeHour, User
    """Dashboard for approved REAL mentors only (linked via
    Mentor.user_id, is_seed=False). Anyone else gets a 404 with a
    clear message — never an empty dashboard pretending otherwise.
    qa_questions = open inbox questions in the mentor's path, plus
    general (topic NULL) questions, capped at 20. Health summaries
    are counts only, never content."""
    try:
        mentor = (Mentor.query.filter_by(user_id=current_user.id,
                                         is_seed=False).first())
        if mentor is None:
            return jsonify({'error': 'You are not an approved mentor '
                                     'yet. Apply via the mentor '
                                     'application form.'}), 404
        bookings = (db.session.query(OHBooking, OfficeHour, User)
                    .join(OfficeHour,
                          OHBooking.office_hour_id == OfficeHour.id)
                    .join(User, OHBooking.user_id == User.id)
                    .filter(OfficeHour.mentor_id == mentor.id)
                    .order_by(OHBooking.created_at.desc())
                    .limit(50).all())
        reviews = (MentorReview.query.filter_by(mentor_id=mentor.id)
                   .order_by(MentorReview.created_at.desc()).all())
        ratings = [r.rating for r in reviews]
        questions = (MentorQuestion.query
                     .filter(MentorQuestion.status == 'open',
                             db.or_(MentorQuestion.topic.is_(None),
                                    MentorQuestion.topic == mentor.path))
                     .order_by(MentorQuestion.created_at.asc())
                     .limit(20).all())
        shares = HealthShare.query.filter_by(
            mentor_id=mentor.id).count()
    except Exception:
        log.exception("Mentor dashboard failed")
        db.session.rollback()
        return jsonify({'error': 'Could not load the dashboard. '
                                 'Please try again.'}), 500
    return jsonify({'success': True, 'dashboard': {
        'mentor': mentor_health_mod.mentor_to_dict(mentor),
        'bookings': [{
            'id': b.id, 'user_display': (u.name or 'Student'),
            'status': b.status,
            'scheduled_at': oh.scheduled_at.isoformat()
            if oh.scheduled_at else None} for b, oh, u in bookings],
        'bookings_count': len(bookings),
        'reviews': [{'rating': r.rating, 'text': r.text}
                    for r in reviews],
        'average_rating': (round(sum(ratings) / len(ratings), 1)
                           if ratings else None),
        'qa_questions': [{'id': q.id, 'question': q.question,
                          'status': q.status} for q in questions],
        'shared_health_summaries': shares}})

