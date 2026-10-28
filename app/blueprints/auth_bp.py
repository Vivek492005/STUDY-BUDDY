"""Auth blueprint — ARC-01 strangler extraction (phase 1).

The ``/register``, ``/login`` and ``/logout`` routes moved here verbatim from
``unified_app.create_app``. URLs are identical (``/login`` etc.); endpoints are
now blueprint-prefixed (``auth.login`` …) per Flask semantics, so every
``url_for('login')`` call site (templates + ``unified_app.py``) and
``login_manager.login_view`` were updated to the ``auth.`` names at move time.

The view bodies still depend on helpers/models defined in ``unified_app``
(imported below at module level). That import is safe because the blueprint
is registered from *inside* ``create_app`` — i.e. only after ``unified_app``
has finished importing.
"""

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user

from app.extensions import limiter

# Helpers/models live in the monolith for now; they move out in later phases.
# Imported here (not at unified_app import time) so there is no import cycle.
from unified_app import (  # noqa: E402
    User,
    _google_configured,
    _rotate_session_on_login,
    check_password_policy,
    db,
    is_safe_next,
    log_security_event,
)

auth_bp = Blueprint("auth", __name__)


@auth_bp.route('/register', methods=['GET', 'POST'], endpoint='register')
@limiter.limit("20 per hour")  # SEC-04: stricter for auth endpoints
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    error = None
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        age_confirm = request.form.get('age_confirm')
        if not name or not email or not password:
            error = 'Please fill in name, email and password.'
        elif not age_confirm:
            # PRIV-06: age gate
            error = ('Please confirm you are at least 13 years old. '
                     'If under 18, have a parent/guardian review our '
                     'Privacy Policy and Terms with you.')
        else:
            pw_ok, pw_msg = check_password_policy(password)
            if not pw_ok:
                error = pw_msg
            elif '@' not in email or '.' not in email:
                error = 'Please enter a valid email address.'
            else:
                # SEC-06 (audit fix, 2026-10-28): the old code flashed
                # "An account with this email already exists" — an
                # account-existence oracle. The response must now be
                # identical whether the email exists or not: same
                # redirect, same flash, no auto-login either way (an
                # auto-logged-in new account vs a login redirect for
                # an existing one would still distinguish them).
                # There is no mailer yet, so no "check your inbox"
                # promise is made — the message is true for both cases
                # (a new account was just created; an existing one was
                # already ready). Existence is recorded only in the
                # internal security log, never in the response.
                existing = User.query.filter_by(email=email).first()
                if existing is None:
                    user = User(name=name, email=email)
                    user.set_password(password)
                    db.session.add(user)
                    db.session.commit()
                else:
                    user = existing
                log_security_event("register_attempt", user_id=user.id,
                                   details={"account_exists": existing is not None})
                flash('Your account is ready. Please log in to continue.',
                      'success')
                return redirect(url_for('auth.login'))
    return render_template('register.html', error=error,
                           google_configured=_google_configured())

@auth_bp.route('/login', methods=['GET', 'POST'], endpoint='login')
@limiter.limit("10 per hour")  # SEC-04: brute-force protection
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    error = None
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            _rotate_session_on_login()
            login_user(user, remember=bool(request.form.get('remember')))
            # SEC-14: audit log (hashed id, no email/password)
            log_security_event("login_success", user_id=user.id)
            flash(f'Welcome back, {user.name}!', 'success')
            nxt = request.args.get('next') or request.form.get('next')
            return redirect(nxt if is_safe_next(nxt) else url_for('dashboard'))
        # SEC-14: failed login — no email in the log (PII); only whether
        # the account exists, which aids abuse detection.
        log_security_event("login_failure", user_id=user.id if user else None,
                           details={"account_exists": bool(user)})
        error = 'Invalid email or password.'
    return render_template('login.html', error=error,
                           next=request.args.get('next', ''),
                           google_configured=_google_configured())

@auth_bp.route('/logout', methods=['GET', 'POST'], endpoint='logout')
def logout():
    # Sign-out must be a POST (CSRF-protected form) so a stray GET —
    # e.g. a prefetched link — can never log the user out (P2#11).
    if request.method == 'GET':
        abort(405, description="Please use the Log out button to sign out.")
    logout_user()
    # Audit fix (2026-10-28): logout_user() alone leaves the session
    # cookie data behind; clear it so nothing session-scoped survives.
    from flask import session as _session
    _session.clear()
    # Stage 4 fix: session.clear() above ALSO wipes the "_remember" =
    # "clear" marker logout_user() just set, so Flask-Login's
    # after_request handler would never delete the remember cookie —
    # the browser would keep re-presenting it and re-authenticating
    # after every logout (and, via the session-version hook, loop on
    # /login forever). Restore the marker when a remember cookie was
    # actually presented.
    _remember_name = current_app.config.get(
        "REMEMBER_COOKIE_NAME", "remember_token")
    if _remember_name in request.cookies:
        _session["_remember"] = "clear"
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))
