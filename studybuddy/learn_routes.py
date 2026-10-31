"""Learning hub blueprint — Phase 3 of the monolith split.

See docs/monolith-split-plan.md. The /learn/* pages (learning hub, skill
roadmaps, skill quizzes, certification guides) moved here verbatim from
unified_app.register_routes. URLs, methods, rate limits, and response
shapes are unchanged. Endpoints are blueprint-prefixed (learn.*) per Flask
semantics; template url_for call sites were updated at move time.

Hard rule: this module MUST NOT import unified_app at top level. Models
(RoadmapProgress/QuizAttempt/CertPlan) and shared helpers (json_safe,
_momentum_hook, _ai_ready/call_gemini_ai/GeminiError) still live in
unified_app, so they are imported lazily inside the view functions via
_models() / local imports — the same pattern as studybuddy/dsa_routes.py.
db/limiter come from app.extensions, which has no import cycle. All other
helpers live in studybuddy/learning_roadmaps.py, studybuddy/skill_quizzes.py
and studybuddy/cert_guide.py and are safe to import at top level.
"""

import logging
from datetime import UTC, datetime

from flask import Blueprint, abort, jsonify, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db, limiter
from studybuddy.aptitude_test import SUBMIT_GRACE_SEC
from studybuddy.cert_guide import (
    all_certs,
    build_study_plan,
    filter_certs,
    get_cert,
    validate_plan_payload,
    validate_recommend_payload,
)
from studybuddy.cert_guide import domains as cert_domains
from studybuddy.cert_guide import get_freshness as cert_freshness
from studybuddy.cert_guide import recommend as recommend_certs
from studybuddy.learning_roadmaps import (
    analyze_gap,
    build_personal_roadmap,
    validate_gap_payload,
    validate_progress_payload,
)
from studybuddy.learning_roadmaps import get_skill as get_roadmap_skill
from studybuddy.learning_roadmaps import list_domains as roadmap_domains
from studybuddy.learning_roadmaps import list_roles as roadmap_roles
from studybuddy.learning_roadmaps import list_skills as roadmap_skills
from studybuddy.learning_roadmaps import progress_summary as roadmap_progress_summary
from studybuddy.skill_quizzes import best_scores as quiz_best_scores
from studybuddy.skill_quizzes import (
    build_quiz,
    check_answer,
    grade_quiz,
    public_question,
    quiz_mistakes,
    quiz_skills,
    skill_topics,
    validate_start_payload,
    weakness_drill,
)
from studybuddy.skill_quizzes import get_question as get_quiz_question

log = logging.getLogger("acra")

learn_bp = Blueprint("learn", __name__)


def _models():
    """Lazy import of unified_app models (avoids circular import)."""
    from unified_app import CertPlan, QuizAttempt, RoadmapProgress

    return CertPlan, QuizAttempt, RoadmapProgress

# ---------- LEARNING HUB (Wave 3) ----------
@learn_bp.route('/learn')
def learn_hub():
    from unified_app import CertPlan, QuizAttempt, RoadmapProgress
    """Learning hub: roadmaps, skill quizzes, certification guidance.

    For signed-in users the hub also carries a personal "continue
    learning" strip (roadmap stages, quiz bests, open mistakes,
    saved cert plans) and a combined skill/cert search index.
    """
    search_index = (
        [{"label": s["name"], "kind": "Roadmap",
          "url": url_for('learn.roadmap_detail', skill_id=s["id"])}
         for s in roadmap_skills()]
        + [{"label": sid.replace('-', ' ').title(), "kind": "Quiz",
            "url": url_for('learn.quizzes_home')}
           for sid, _ in quiz_skills()]
        + [{"label": c["name"], "kind": "Certification",
            "url": url_for('learn.cert_detail', cert_id=c["id"])}
           for c in all_certs()])
    cont = None
    if current_user.is_authenticated:
        rows = RoadmapProgress.query.filter_by(
            user_id=current_user.id).all()
        summary = roadmap_progress_summary(rows)
        attempts = (QuizAttempt.query
                    .filter(QuizAttempt.user_id == current_user.id,
                            QuizAttempt.submitted_at.isnot(None))
                    .order_by(QuizAttempt.submitted_at.asc()).all())
        best = quiz_best_scores(attempts)
        best_skill, best_pct = None, -1
        for s, b in best.items():
            if b.get("pct", 0) > best_pct:
                best_skill, best_pct = s, b["pct"]
        book = quiz_mistakes([{
            "skill": a.skill, "results": a.results_json or [],
            "submitted_at": a.submitted_at.isoformat() if a.submitted_at else "",
            "attempt_id": a.id} for a in attempts])
        cont = {
            "skills_started": len(summary),
            "stages_done": sum(s["completed"] for s in summary),
            "stages_total": sum(s["total"] for s in summary),
            "next_skill": next(
                (s for s in summary if s["completed"] < s["total"]), None),
            "quizzes_taken": len(attempts),
            "best_skill": best_skill, "best_pct": best_pct,
            "open_mistakes": book["total"],
            "cert_plans": CertPlan.query.filter_by(
                user_id=current_user.id).count(),
        }
    return render_template('learn.html',
                           num_skills=len(roadmap_skills()),
                           num_quiz_questions=sum(n for _, n in quiz_skills()),
                           num_quiz_skills=len(quiz_skills()),
                           num_certs=len(all_certs()),
                           num_roles=len(roadmap_roles()),
                           search_index=search_index,
                           cont=cont,
                           is_authed=current_user.is_authenticated)

def _learn_ai_tip(prompt, fallback):
    from unified_app import GeminiError, _ai_ready, call_gemini_ai
    """Optional Gemini polish for learning tips. Returns (tip, ai_powered).

    Deterministic fallback always works; the tip never affects scores.
    """
    if not _ai_ready():
        return fallback, False
    try:
        raw = call_gemini_ai(
            "You are a concise study coach. In 2-3 sentences, give one "
            "actionable study tip. No preamble, no bullet list.\n" + prompt[:1500])
        tip = (raw or "").strip()
        if len(tip) >= 20:
            return tip[:600], True
    except GeminiError as e:
        log.warning("Learn AI tip unavailable: %s", e)
    except Exception:
        log.exception("Learn AI tip failed")
    return fallback, False

# ----- Feature #18: skill-gap learning roadmaps -----
@learn_bp.route('/learn/roadmaps')
def roadmaps_home():
    """Browse skills + run the skill-gap analyzer."""
    return render_template('roadmaps.html',
                           skills=roadmap_skills(),
                           domains=roadmap_domains(),
                           roles=roadmap_roles(),
                           is_authed=current_user.is_authenticated)

@learn_bp.route('/learn/roadmaps/<skill_id>')
def roadmap_detail(skill_id):
    from unified_app import RoadmapProgress
    """Full 3-stage roadmap for one skill, with progress toggles."""
    skill = get_roadmap_skill(skill_id)
    if not skill:
        abort(404)
    progress = {}
    if current_user.is_authenticated:
        rows = RoadmapProgress.query.filter_by(
            user_id=current_user.id, skill_id=skill_id).all()
        progress = {r.stage: r.status for r in rows}
    return render_template('roadmap_detail.html', skill=skill,
                           progress=progress,
                           is_authed=current_user.is_authenticated)

@learn_bp.route('/learn/roadmaps/gap', methods=['POST'])
@limiter.limit("30 per hour")
def roadmaps_gap():
    from unified_app import json_safe
    """Skill-gap analysis: current skills + target role -> ranked gaps.

    JSON body: {"target_role": "...", "current_skills": [{"skill_id","stage"}]}.
    CSRF token via X-CSRFToken header (not exempt). Anonymous allowed.
    """
    payload = request.get_json(silent=True) or {}
    cleaned, error = validate_gap_payload(payload)
    if error:
        return jsonify({'error': error}), 400
    gaps, role = analyze_gap(cleaned["current_skills"], cleaned["target_role"])
    if role is None:
        return jsonify({'error': 'Unknown target role.'}), 400
    plan = build_personal_roadmap(gaps)
    gap_desc = "; ".join(
        f"{g['skill_name']} to {g['min_stage']} (priority {g['priority']})"
        for g in gaps[:6])
    fallback_tip = (
        "Start with your highest-priority gap and finish one stage "
        "before moving on. Consistency beats intensity — 1 focused hour "
        "daily compounds fast.")
    # SEC-12: the AI tip burns LLM quota — only for logged-in users;
    # anonymous callers get the deterministic fallback.
    if current_user.is_authenticated:
        tip, ai_powered = _learn_ai_tip(
            f"A student targeting '{role['name']}' has these skill gaps: "
            f"{gap_desc}. Give one actionable study tip.",
            fallback_tip)
    else:
        tip, ai_powered = fallback_tip, False
    return jsonify({'success': True,
                    'role': {'id': role['id'], 'name': role['name']},
                    'gaps': json_safe(gaps),
                    'total_weeks': plan['total_weeks'],
                    'num_skills': plan['num_skills'],
                    'study_tip': tip, 'ai_powered': ai_powered})

@learn_bp.route('/learn/roadmaps/progress', methods=['POST'])
@limiter.limit("60 per hour")
def roadmaps_progress():
    from unified_app import RoadmapProgress, _momentum_hook
    """Save per-stage progress. Login required.

    JSON body: {"skill_id": "...", "stage": "beginner", "status": "completed"}.
    CSRF token via X-CSRFToken header (not exempt).
    """
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to track progress.'}), 401
    payload = request.get_json(silent=True) or {}
    cleaned, error = validate_progress_payload(payload)
    if error:
        return jsonify({'error': error}), 400
    try:
        row = RoadmapProgress.query.filter_by(
            user_id=current_user.id, skill_id=cleaned["skill_id"],
            stage=cleaned["stage"]).first()
        if row:
            row.status = cleaned["status"]
        else:
            row = RoadmapProgress(
                user_id=current_user.id, skill_id=cleaned["skill_id"],
                stage=cleaned["stage"], status=cleaned["status"])
            db.session.add(row)
        db.session.commit()
    except Exception:
        log.exception("Roadmap progress save failed")
        db.session.rollback()
        return jsonify({'error': 'Could not save progress. Please try again.'}), 500
    # Wave 5 #27: a completed roadmap stage earns XP — starting one doesn't.
    if cleaned["status"] == "completed":
        _momentum_hook(current_user.id, 'roadmap_step')
    return jsonify({'success': True, 'status': cleaned["status"]})

@learn_bp.route('/learn/roadmaps/dashboard')
@login_required
def roadmaps_dashboard():
    from unified_app import RoadmapProgress
    """The user's roadmap progress across skills."""
    rows = RoadmapProgress.query.filter_by(user_id=current_user.id).all()
    return render_template('roadmaps_dashboard.html',
                           summary=roadmap_progress_summary(rows),
                           skills={s["id"]: s for s in roadmap_skills()})

# ----- Feature #19: skill quizzes -----
@learn_bp.route('/learn/quizzes')
def quizzes_home():
    from unified_app import QuizAttempt
    """Pick a skill and quiz configuration."""
    skills = [{"id": s, "count": n, "topics": skill_topics(s)[:8]}
              for s, n in quiz_skills()]
    best = {}
    if current_user.is_authenticated:
        attempts = QuizAttempt.query.filter(
            QuizAttempt.user_id == current_user.id,
            QuizAttempt.submitted_at.isnot(None)).all()
        best = quiz_best_scores(attempts)
    return render_template('quizzes.html', skills=skills, best=best,
                           is_authed=current_user.is_authenticated)

@learn_bp.route('/learn/quizzes/start', methods=['POST'])
@limiter.limit("20 per hour")
def quizzes_start():
    from unified_app import QuizAttempt
    """Start a quiz attempt. Login required (history + best scores).

    JSON body: {"skill": "...", "num_questions": 10,
                "difficulties": ["easy","medium"], "timed": false,
                "time_limit_sec": 600}.
    CSRF token via X-CSRFToken header (not exempt).
    """
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to take a quiz.'}), 401
    payload = request.get_json(silent=True) or {}
    cleaned, error = validate_start_payload(payload)
    if error:
        return jsonify({'error': error}), 400
    questions = build_quiz(cleaned["skill"], n=cleaned["num_questions"],
                           difficulties=cleaned["difficulties"] or None)
    if not questions:
        return jsonify({'error': 'No questions available for that skill.'}), 400
    try:
        row = QuizAttempt(
            user_id=current_user.id, skill=cleaned["skill"],
            num_questions=len(questions),
            difficulty_mix=cleaned["difficulties"],
            timed=cleaned["timed"], time_limit_sec=cleaned["time_limit_sec"],
            question_ids=[q["id"] for q in questions],
            checks_json=[],
            started_at=datetime.now(UTC))
        db.session.add(row)
        db.session.commit()
    except Exception:
        log.exception("Quiz attempt creation failed")
        db.session.rollback()
        return jsonify({'error': 'Could not start the quiz. Please try again.'}), 500
    return jsonify({'success': True, 'attempt_id': row.id,
                    'skill': cleaned["skill"],
                    'timed': cleaned["timed"],
                    'time_limit_sec': cleaned["time_limit_sec"],
                    'questions': [public_question(q) for q in questions]})

@learn_bp.route('/learn/quizzes/<int:attempt_id>/check', methods=['POST'])
@limiter.limit("120 per hour")
def quizzes_check(attempt_id):
    from unified_app import QuizAttempt, json_safe
    """Instant per-question feedback. Login + owner only.

    JSON body: {"question_id": "...", "answer": 2}.
    """
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in.'}), 401
    row = QuizAttempt.query.filter_by(id=attempt_id,
                                      user_id=current_user.id).first()
    if not row or row.submitted_at is not None:
        return jsonify({'error': 'Quiz attempt not found.'}), 404
    payload = request.get_json(silent=True) or {}
    qid = str(payload.get("question_id") or "")
    if qid not in (row.question_ids or []):
        return jsonify({'error': 'Question is not part of this quiz.'}), 400
    question = get_quiz_question(qid)
    if not question:
        return jsonify({'error': 'Unknown question.'}), 400
    if row.timed and row.time_limit_sec:
        elapsed = (datetime.now(UTC) -
                   row.started_at.replace(tzinfo=UTC)).total_seconds()
        if elapsed > row.time_limit_sec + SUBMIT_GRACE_SEC:
            return jsonify({'error': 'Time is up for this quiz.'}), 400
    raw_answer = payload.get("answer")
    try:
        chosen = int(raw_answer)
    except (TypeError, ValueError):
        chosen = None
    result = check_answer(question, chosen if chosen is not None else -1)
    try:
        checks = list(row.checks_json or [])
        checks = [c for c in checks if c.get("question_id") != qid]
        checks.append({"question_id": qid, "chosen_index": chosen,
                       "correct": result["correct"]})
        row.checks_json = json_safe(checks)
        db.session.commit()
    except Exception:
        log.exception("Quiz check save failed")
        db.session.rollback()
    return jsonify({'success': True, 'correct': result["correct"],
                    'correct_index': result["correct_index"],
                    'explanation': result["explanation"]})

@learn_bp.route('/learn/quizzes/<int:attempt_id>/submit', methods=['POST'])
@limiter.limit("20 per hour")
def quizzes_submit(attempt_id):
    from unified_app import QuizAttempt, _momentum_hook, json_safe
    """Grade the attempt server-side and persist the full review.

    JSON body: {"answers": {"<qid>": <index>}}. Timing validated
    server-side for timed quizzes.
    """
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in.'}), 401
    row = QuizAttempt.query.filter_by(id=attempt_id,
                                      user_id=current_user.id).first()
    if not row or row.submitted_at is not None:
        return jsonify({'error': 'Quiz attempt not found.'}), 404
    payload = request.get_json(silent=True) or {}
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        return jsonify({'error': 'Provide an answers object.'}), 400
    questions = [get_quiz_question(qid) for qid in (row.question_ids or [])]
    questions = [q for q in questions if q]
    if not questions:
        return jsonify({'error': 'This quiz has no questions.'}), 400
    expired = False
    if row.timed and row.time_limit_sec:
        elapsed = (datetime.now(UTC) -
                   row.started_at.replace(tzinfo=UTC)).total_seconds()
        if elapsed > row.time_limit_sec + SUBMIT_GRACE_SEC:
            expired = True
    results, summary = grade_quiz(questions, answers)
    summary["expired"] = expired
    # Weak-topic study links: point at the roadmap skill page.
    summary["roadmap_url"] = url_for('learn.roadmap_detail', skill_id=row.skill)
    try:
        row.answers_json = json_safe({str(k): v for k, v in answers.items()})
        row.results_json = json_safe(results)
        row.summary_json = json_safe(summary)
        row.submitted_at = datetime.now(UTC)
        db.session.commit()
    except Exception:
        log.exception("Quiz submit save failed")
        db.session.rollback()
        return jsonify({'error': 'Could not save your results. Please try again.'}), 500
    # Wave 5 #27: a submitted skill quiz is a meaningful career action.
    _momentum_hook(current_user.id, 'quiz_completed')
    return jsonify({'success': True, 'summary': json_safe(summary),
                    'report_url': url_for('learn.view_quiz_result', attempt_id=row.id)})

@learn_bp.route('/learn/quizzes/<int:attempt_id>')
@login_required
def view_quiz_result(attempt_id):
    from unified_app import QuizAttempt
    """Full review of a submitted quiz attempt (owner only)."""
    row = QuizAttempt.query.filter_by(id=attempt_id,
                                      user_id=current_user.id).first()
    if not row or row.submitted_at is None:
        abort(404)
    return render_template('quiz_result.html', attempt=row,
                           summary=row.summary_json or {},
                           results=row.results_json or [])

@learn_bp.route('/learn/quizzes/history')
@login_required
def quizzes_history():
    from unified_app import QuizAttempt
    """The user's quiz attempts + per-skill best scores."""
    attempts = (QuizAttempt.query
                .filter(QuizAttempt.user_id == current_user.id,
                        QuizAttempt.submitted_at.isnot(None))
                .order_by(QuizAttempt.submitted_at.desc()).all())
    return render_template('quiz_history.html', attempts=attempts,
                           best=quiz_best_scores(attempts))

@learn_bp.route('/learn/quizzes/mistakes')
@login_required
def quizzes_mistakes():
    from unified_app import QuizAttempt
    """Mistake Book for skill quizzes: latest-state wrong questions
    across all submitted attempts, grouped by skill, with a
    weakness-drill shortcut per skill."""
    attempts = (QuizAttempt.query
                .filter(QuizAttempt.user_id == current_user.id,
                        QuizAttempt.submitted_at.isnot(None))
                .order_by(QuizAttempt.submitted_at.asc()).all())
    book = quiz_mistakes([{
        "skill": a.skill, "results": a.results_json or [],
        "submitted_at": a.submitted_at.isoformat() if a.submitted_at else "",
        "attempt_id": a.id} for a in attempts])
    return render_template('quiz_mistakes.html', book=book,
                           is_authed=True)

@learn_bp.route('/learn/quizzes/drill', methods=['POST'])
@limiter.limit("20 per hour")
def quizzes_drill():
    from unified_app import QuizAttempt
    """Start a weakness-targeted drill attempt for one skill.

    Builds the question set from the user's own graded history via
    weakness_drill() (weak topics first; general fill when the user
    has no history yet) and returns the standard quiz-start payload.
    """
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to start a drill.'}), 401
    payload = request.get_json(silent=True) or {}
    skill = str(payload.get("skill") or "").strip()
    if skill not in dict(quiz_skills()):
        return jsonify({'error': 'Unknown skill.'}), 400
    attempts = (QuizAttempt.query
                .filter(QuizAttempt.user_id == current_user.id,
                        QuizAttempt.skill == skill,
                        QuizAttempt.submitted_at.isnot(None))
                .order_by(QuizAttempt.submitted_at.desc()).limit(10).all())
    past = []
    for a in attempts:
        past.extend(a.results_json or [])
    try:
        drill = weakness_drill(skill, past, n=10)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    questions = drill["questions"]
    try:
        row = QuizAttempt(
            user_id=current_user.id, skill=skill,
            num_questions=len(questions),
            difficulty_mix=[], timed=False, time_limit_sec=None,
            question_ids=[q["id"] for q in questions],
            checks_json=[], started_at=datetime.now(UTC))
        db.session.add(row)
        db.session.commit()
    except Exception:
        log.exception("Quiz drill creation failed")
        db.session.rollback()
        return jsonify({'error': 'Could not start the drill. Please try again.'}), 500
    return jsonify({'success': True, 'attempt_id': row.id,
                    'skill': skill, 'timed': False, 'time_limit_sec': None,
                    'questions': questions,
                    'target_topics': drill["target_topics"],
                    'guidance': drill["guidance"]})

# ----- Feature #20: certification guidance -----
@learn_bp.route('/learn/certs')
def certs_home():
    """Browse certifications + run the recommender."""
    certs = all_certs()
    return render_template('certs.html',
                           domains=cert_domains(),
                           roles=roadmap_roles(),
                           freshness=cert_freshness(),
                           num_certs=len(certs),
                           num_providers=len({c.get("provider", "") for c in certs}),
                           num_free=sum(1 for c in certs if not c.get("cost_inr")),
                           is_authed=current_user.is_authenticated)

@learn_bp.route('/learn/certs/browse')
@limiter.limit("60 per hour")
def certs_browse():
    """JSON list of certifications, filterable by domain/difficulty/cost."""
    try:
        max_cost = request.args.get('max_cost')
        max_cost = float(max_cost) if max_cost not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({'error': 'max_cost must be a number.'}), 400
    certs = filter_certs(domain=request.args.get('domain') or None,
                         max_cost_inr=max_cost,
                         difficulty=request.args.get('difficulty') or None)
    summaries = [{
        "id": c["id"], "name": c["name"], "provider": c["provider"],
        "domain": c.get("domain"), "cost_inr": c.get("cost_inr"),
        "cost_note": c.get("cost_note"), "validity_years": c.get("validity_years"),
        "difficulty": c.get("difficulty"),
        "url": url_for('learn.cert_detail', cert_id=c["id"]),
    } for c in certs]
    return jsonify({'success': True, 'certifications': summaries})

@learn_bp.route('/learn/certs/<cert_id>')
def cert_detail(cert_id):
    """Certification detail + study-plan generator."""
    cert = get_cert(cert_id)
    if not cert:
        abort(404)
    return render_template('cert_detail.html', cert=cert,
                           is_authed=current_user.is_authenticated)

@learn_bp.route('/learn/certs/recommend', methods=['POST'])
@limiter.limit("30 per hour")
def certs_recommend():
    from unified_app import json_safe
    """Rank certifications by role match, skill overlap, budget, difficulty.

    JSON body: {"skills": [...], "target_role": "...",
                "budget_inr": 20000, "top_n": 8}.
    CSRF token via X-CSRFToken header (not exempt). Anonymous allowed.
    """
    payload = request.get_json(silent=True) or {}
    cleaned, error = validate_recommend_payload(payload)
    if error:
        return jsonify({'error': error}), 400
    ranked = recommend_certs(skills=cleaned["skills"],
                             target_role=cleaned["target_role"],
                             budget_inr=cleaned["budget_inr"],
                             top_n=cleaned["top_n"])
    return jsonify({'success': True,
                    'recommendations': json_safe(ranked)})

@learn_bp.route('/learn/certs/plan', methods=['POST'])
@limiter.limit("20 per hour")
def certs_plan():
    from unified_app import CertPlan, json_safe
    """Generate (and save) a week-by-week study plan. Login required.

    JSON body: {"cert_id": "...", "weeks": 8}.
    CSRF token via X-CSRFToken header (not exempt).
    """
    if not current_user.is_authenticated:
        return jsonify({'error': 'Please log in to save a study plan.'}), 401
    payload = request.get_json(silent=True) or {}
    cleaned, error = validate_plan_payload(payload)
    if error:
        return jsonify({'error': error}), 400
    plan, perr = build_study_plan(cleaned["cert_id"], cleaned["weeks"])
    if perr:
        return jsonify({'error': perr}), 400
    try:
        row = CertPlan(user_id=current_user.id, cert_id=cleaned["cert_id"],
                       weeks=cleaned["weeks"], plan_json=json_safe(plan))
        db.session.add(row)
        db.session.commit()
    except Exception:
        log.exception("Cert plan save failed")
        db.session.rollback()
        return jsonify({'error': 'Could not save the study plan. Please try again.'}), 500
    return jsonify({'success': True, 'plan_id': row.id,
                    'plan': json_safe(plan)})

@learn_bp.route('/learn/certs/plans')
@login_required
def certs_plans():
    from unified_app import CertPlan
    """The user's saved certification study plans."""
    plans = (CertPlan.query
             .filter_by(user_id=current_user.id)
             .order_by(CertPlan.created_at.desc()).all())
    cert_names = {c["id"]: c["name"] for c in all_certs()}
    return render_template('cert_plans.html', plans=plans,
                           cert_names=cert_names)
