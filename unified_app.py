"""
Study-Buddy-Powered Excellence Platform
Main Entry Point

Phase 1 (Foundation): Flask-SQLAlchemy + Flask-Login persistence.
- Users can register / log in (email + password, Werkzeug hashing).
- Resume analyses are saved per-user when logged in; /analyze still works anonymously.
- /dashboard (login required) shows analysis history + career goals.
- Research chat sessions are saved per-user when logged in.
"""
import hashlib
import json
import logging
import os
import random
import re
import secrets
import sys
import time
import uuid

# When run as `python unified_app.py`, make `import unified_app` resolve to
# this already-running module instead of loading a second copy. Without this,
# studybuddy/* lazy imports get a *different* module object with its own
# SQLAlchemy() instance, and their queries fail with "current Flask app is not
# registered with this 'SQLAlchemy' instance" (dev-only; gunicorn imports the
# module once as `unified_app`, so this branch never runs there).
if __name__ == "__main__" and "unified_app" not in sys.modules:
    sys.modules["unified_app"] = sys.modules["__main__"]

from datetime import UTC, date, datetime, timedelta

import requests
from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import (
    UserMixin,
    current_user,
    login_required,
    login_user,
    # NOTE (ARC-01): logout_user moved with the auth blueprint; the one
    # remaining module-level use (account_delete) imports it locally.
)
from flask_wtf.csrf import CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

# SEC-14: structured security event logging (no PII payloads).
from studybuddy.security_events import log_security_event

# ARC-09: custom exceptions with a central error handler.
from studybuddy.exceptions import (
    ExternalServiceError,
    StudyBuddyError,
    handle_studybuddy_error,
)

from studybuddy.github_integration import (
    GithubAPIError,
    analyze_github_profile,
    validate_github_username,
)
from studybuddy.auth_hardening import (
    check_password_policy,
    generate_email_token,
    generate_reset_token,
    verify_email_token,
)
from studybuddy.constructor_guardrails import (
    build_grounded_prompt,
    post_process_paper,
    verify_citations,
)
from studybuddy.leetcode_integration import (
    LeetCodeAPIError,
    fetch_leetcode_stats,
    get_card_stats,
    render_error_svg,
    render_leetcode_card_svg,
    validate_leetcode_username,
)
from studybuddy import eng_jd_v2, eng_jobs, eng_interview_v2  # ENG-07/11/12 v2 engines

log = logging.getLogger("acra")


def _safe_user_ref_id(user_id):
    """Hash a user id for logging (no PII in logs)."""
    if user_id is None:
        return "anon"
    return hashlib.sha256(f"uid:{user_id}".encode()).hexdigest()[:16]

# Load environment variables (.env for local dev, platform env vars in prod)
load_dotenv()

# google-genai (new SDK) is optional: the app must boot and serve everything
# except live Gemini calls even if the package / API key is missing.
try:
    from google import genai as genai_client
    from google.genai import types as genai_types
    _GENAI_IMPORTED = True
except Exception:  # pragma: no cover - defensive
    genai_client = None
    genai_types = None
    _GENAI_IMPORTED = False

# Sentinel API-key values that mean "not configured" (must stay in sync with
# .env.example so a fresh setup always shows the friendly missing-key message).
_GEMINI_KEY_SENTINELS = frozenset({
    "", "your_actual_api_key_here", "<redacted>", "change-me",
    "YOUR_GEMINI_API_KEY", "paste-your-key-here",
})


def _gemini_api_key():
    """Return the configured Gemini key, or None when missing/placeholder."""
    key = (os.getenv("GEMINI_API_KEY") or "").strip().strip('"').strip("'")
    return key if key and key not in _GEMINI_KEY_SENTINELS else None


def _ai_ready():
    from studybuddy import ai_gateway
    return ai_gateway.ai_ready()


class GeminiError(ExternalServiceError):
    """A Gemini failure with a user-safe message (safe to return to clients).

    ARC-09: subclasses ExternalServiceError so AI failures get the central
    handler (502 + user-safe message) while existing ``except GeminiError``
    handlers keep working unchanged.
    """
    status_code = 502
    user_message = "The AI service is temporarily unavailable. Please try again shortly."


class GithubRepoError(ExternalServiceError):
    """A GitHub repository fetch failure with a user-safe message.

    ARC-09: subclasses ExternalServiceError for central handling.
    """
    status_code = 502
    user_message = "Could not fetch the GitHub repository. Please try again later."


# --- Gemini model discovery cache (module level, refreshed hourly) ---
_MODEL_CACHE = {"names": [], "fetched_at": 0.0}
_MODEL_CACHE_TTL = 3600


def _make_gemini_client():
    """Build a genai client with a 30s HTTP timeout, or None when unusable.

    The HTTP client is constructed explicitly (proxy + CA bundle from env)
    because httpx's env parsing chokes on bracketed IPv6 entries that some
    sandboxes put in no_proxy (e.g. "[::1]" -> "Invalid port" crash).
    """
    key = _gemini_api_key()
    if not _GENAI_IMPORTED or not key:
        return None
    import httpx
    proxy = (os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
             or os.getenv("HTTP_PROXY") or os.getenv("http_proxy"))
    verify = os.getenv("SSL_CERT_FILE") or True
    # NB: both sync AND async clients must be supplied - the SDK builds an
    # async client unconditionally, which would hit the same env bug.
    http_client = httpx.Client(proxy=proxy, timeout=30.0,
                               trust_env=False, verify=verify)
    async_client = httpx.AsyncClient(proxy=proxy, timeout=30.0,
                                     trust_env=False, verify=verify)
    return genai_client.Client(
        api_key=key,
        http_options=genai_types.HttpOptions(
            timeout=30_000,  # ms
            httpx_client=http_client,
            httpx_async_client=async_client),
    )


def _discover_model_names(client):
    """Return cached generateContent-capable model names (hourly refresh)."""
    now = time.monotonic()
    if _MODEL_CACHE["names"] and now - _MODEL_CACHE["fetched_at"] < _MODEL_CACHE_TTL:
        return list(_MODEL_CACHE["names"])
    names = []
    for m in client.models.list():
        actions = [str(a).lower() for a in (getattr(m, "supported_actions", None) or [])]
        if "generatecontent" in actions or "generate_content" in actions:
            names.append(m.name)
    # Prefer flash/pro variants first, keep the rest as fallbacks.
    priority = [n for kw in ("flash", "pro") for n in names if kw in n.lower()]
    ordered = priority + [n for n in names if n not in priority]
    _MODEL_CACHE.update({"names": ordered, "fetched_at": now})
    return list(ordered)


def _github_get(path):
    """GET a GitHub REST API path with timeout; raises GithubRepoError."""
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "acra-student-platform"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"
    try:
        resp = requests.get("https://api.github.com" + path, headers=headers, timeout=10)
    except requests.RequestException as e:
        raise GithubRepoError(
            "Could not reach GitHub right now. Check your connection and try again."
        ) from e
    if resp.status_code == 404:
        raise GithubRepoError("Repository not found on GitHub. Check the URL and try again.")
    if resp.status_code == 403 and "rate limit" in resp.text.lower():
        raise GithubRepoError("GitHub's API rate limit was hit. Please try again in a few minutes.")
    if not resp.ok:
        raise GithubRepoError(f"GitHub returned an unexpected response ({resp.status_code}).")
    return resp.json()


def _fetch_repo_data(owner, repo):
    """Fetch real repository facts from the GitHub API for the constructor."""
    meta = _github_get(f"/repos/{owner}/{repo}")
    languages = _github_get(f"/repos/{owner}/{repo}/languages") or {}
    readme_text = ""
    try:
        readme = _github_get(f"/repos/{owner}/{repo}/readme")
        import base64
        readme_text = base64.b64decode(readme.get("content", "") or "").decode(
            "utf-8", "ignore")[:4000]
    except GithubRepoError:
        readme_text = ""
    files, dirs = [], set()
    try:
        tree = _github_get(f"/repos/{owner}/{repo}/git/trees/HEAD?recursive=1")
        for entry in (tree.get("tree") or [])[:3000]:
            if entry.get("type") == "blob":
                files.append(entry.get("path", ""))
                if "/" in entry.get("path", ""):
                    dirs.add(entry["path"].split("/")[0])
    except GithubRepoError:
        pass
    total_lang_bytes = sum(languages.values()) or 1
    top_languages = sorted(languages.items(), key=lambda kv: kv[1], reverse=True)[:5]
    file_count = len(files)
    complexity = ("High" if file_count > 200 or len(languages) > 6
                  else "Moderate" if file_count > 50 else "Focused")
    return {
        "full_name": meta.get("full_name", f"{owner}/{repo}"),
        "description": (meta.get("description") or "No description provided.")[:500],
        "stars": meta.get("stargazers_count", 0),
        "forks": meta.get("forks_count", 0),
        "primary_language": meta.get("language") or "Unknown",
        "topics": (meta.get("topics") or [])[:10],
        "top_languages": [(lang, round(100.0 * n / total_lang_bytes, 1))
                          for lang, n in top_languages],
        "file_count": file_count,
        "top_dirs": sorted(dirs)[:12],
        "sample_files": files[:40],
        "has_tests": any("test" in f.lower() for f in files),
        "has_docker": any(f.lower().endswith("dockerfile") or "docker-compose" in f.lower()
                          for f in files),
        "has_ci": any(".github/workflows" in f for f in files),
        "readme": readme_text,
        "complexity": complexity,
    }


def _parse_json_safely(text):
    """Strip code fences and parse JSON; raises ValueError on failure."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*```$", "", t)
    return json.loads(t.strip())

# Add current directory to path
sys.path.insert(0, os.path.dirname(__file__))

# Import logic from Resume Analyzer components
import studybuddy.community as community_mod
import studybuddy.momentum as momentum_mod
import studybuddy.retrospective_data as retro_data
from models.ats_scorer import calculate_ats_score
from models.resume_feedback import generate_feedback, get_overall_summary
from models.skill_extractor import extract_skills, get_skill_names, get_skills_by_category
# ENG-05 + SEC-08: hardened parsing pipeline (content-validated, sandboxed)
from studybuddy.parsing import ParseError as _ParseError
from studybuddy.parsing import parse_upload_file as _parse_upload_file
from parser.section_extractor import (
    extract_education_details,
    extract_experience_years,
    extract_sections,
)
from parser.text_cleaning import clean_text, extract_email, extract_links, extract_phone
from preparation.coding_practice import get_available_companies as get_coding_companies
from preparation.coding_practice import (
    get_available_topics,
    get_coding_questions,
    get_company_coding_stats,
)
from preparation.interview_questions import generate_interview_questions
from studybuddy.mock_interview import hub_company_questions, interview_hub_data
from studybuddy.resume_insights import (
    analyze_bullets, build_fix_checklist, dimension_notes)
from recommendation.career_guidance import suggest_career_paths
from recommendation.job_recommender import get_recommendations
from recommendation.learning_roadmap import estimate_total_time, generate_roadmap
from recommendation.skill_gap_analysis import analyze_skill_gaps
from studybuddy.aptitude_test import SUBMIT_GRACE_SEC, score_attempt, validate_test_setup
from studybuddy.aptitude_test import all_questions as aptitude_all
from studybuddy.aptitude_test import categories as aptitude_categories
from studybuddy.aptitude_test import check_answer as aptitude_check
from studybuddy.aptitude_test import concepts as aptitude_concepts
from studybuddy.aptitude_test import get_concept as aptitude_concept
from studybuddy.aptitude_test import mastery_from_attempts as aptitude_mastery
from studybuddy.aptitude_test import mistakes_from_attempts as aptitude_mistakes_calc
from studybuddy.aptitude_test import pick_for_setup as aptitude_pick_for_setup
from studybuddy.aptitude_test import pick_questions as aptitude_pick
from studybuddy.aptitude_test import selection_counts as aptitude_selection_counts
from studybuddy.aptitude_test import topics as aptitude_topics
from studybuddy.aptitude_test import track_counts as aptitude_track_counts
from studybuddy.aptitude_test import COMPANY_TESTS as APTITUDE_COMPANY_TESTS
from studybuddy.aptitude_test import TRACKS as APTITUDE_TRACKS
from studybuddy.arxiv_import import download_pdf as arxiv_download_pdf
from studybuddy.arxiv_import import (
    fetch_arxiv,
    fetch_citation_graph,
    fetch_doi,
    normalize_metadata,
    validate_arxiv_id,
    validate_doi,
)
from studybuddy.bullet_rewriter import BULLET_MAX_CHARS, BULLET_MIN_CHARS, improve_bullet
from studybuddy.career_assessment import (
    QUESTIONS,
    public_questions,
    rule_based_why,
    score_assessment,
    validate_assessment_inputs,
)
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
from studybuddy.citation_fmt import STYLES as CITATION_STYLES
from studybuddy.citation_fmt import (
    format_citation,
    from_bibtex,
    style_guide,
    to_bibtex,
)
from studybuddy.communication_coach import (
    QUESTION_TYPES as COACH_QUESTION_TYPES,
)
from studybuddy.communication_coach import (
    analyze_transcript,
    coach_questions,
    validate_coach_extras,
    validate_coach_inputs,
    video_presence_checklist,
)
from studybuddy.cover_generator import (
    rule_based_cover_letter,
    rule_based_outreach,
    validate_cover_inputs,
)
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
from studybuddy.flashcards import generate_deck as fc_generate_deck
from studybuddy.flashcards import grade_mcq as fc_grade_mcq
from studybuddy.flashcards import mastery_stats as fc_mastery_stats
from studybuddy.flashcards import (
    card_due_state as fc_card_due_state,
    due_count as fc_due_count,
    srs_update as fc_srs_update,
)
from studybuddy.gap_finder import generate_gap_report as gf_generate_report
from studybuddy.health_review import DIMENSIONS as HR_DIMENSIONS
from studybuddy.health_review import current_month_key as hr_current_month_key
from studybuddy.health_review import generate_review as hr_generate_review
from studybuddy.health_review import valid_month_key as hr_valid_month_key
from studybuddy.jd_matcher import (
    JD_MAX_CHARS,
    JD_MIN_CHARS,
    generate_rule_based_suggestions,
    score_jd_match,
)
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
from studybuddy.linkedin_grader import (
    generate_rule_based_summary,
    grade_linkedin_profile,
    validate_linkedin_inputs,
)
from studybuddy.lit_review import generate_review as lr_generate_review
from studybuddy.lit_review import review_to_html as lr_to_html
from studybuddy.lit_review import review_to_markdown as lr_to_markdown
from studybuddy.mock_interview import (
    generate_rule_based_feedback,
    interview_options,
    pick_questions,
    score_answer,
    validate_interview_setup,
)
from studybuddy.opportunities import TYPES as OPP_TYPES
from studybuddy.opportunities import OpportunityDataError, list_opportunities, opp_exists
from studybuddy.opportunities import get_meta as opp_meta
from studybuddy.pdf_qa import answer_question as pdfqa_answer
from studybuddy.pdf_qa import chunk_pages as pdfqa_chunk_pages
from studybuddy.pdf_qa import parse_pdf_pages as pdfqa_parse_pages
from studybuddy.pdf_qa import retrieve as pdfqa_retrieve
from studybuddy.pdf_qa import validate_upload as pdfqa_validate_upload
from studybuddy.portfolio import unique_slug, validate_portfolio_payload
from studybuddy.salary_data import SalaryDataError
from studybuddy.salary_data import get_freshness as salary_freshness
from studybuddy.salary_data import get_options as salary_options
from studybuddy.salary_data import query as salary_query
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
from studybuddy.star_builder import (
    COMPETENCIES,
    COMPETENCY_IDS,
    SECTION_GUIDE,
    SECTIONS,
    all_examples,
    examples_for,
    generate_rule_based_rewrite_tip,
    score_draft,
    validate_draft,
)

# ARC-01: extension objects live in app/extensions.py now; imported back
# here so the monolith keeps working during the strangler migration.
from app.extensions import csrf, db, limiter, login_manager, migrate, oauth

# LLM-03: import for side effect — registers LLMUsage model with db so
# db.create_all() covers llm_usage in dev/test (migration owns prod).
import studybuddy.llm_ledger  # noqa: F401,E402


# ========================================================
# --- DATABASE MODELS ---
# ========================================================

class User(UserMixin, db.Model):
    """Registered student user."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)
    # --- Social login + profile integrations (added post-Phase-1) ---
    # google_sub is unique via ix_user_google_sub (see the Alembic baseline migration).
    google_sub = db.Column(db.String(255), nullable=True)
    avatar_url = db.Column(db.String(512), nullable=True)
    github_username = db.Column(db.String(39), nullable=True)
    leetcode_username = db.Column(db.String(39), nullable=True)
    # --- SEC-09: email verification ---
    email_verified = db.Column(db.Boolean, default=False, nullable=False)
    email_verified_at = db.Column(db.DateTime, nullable=True)
    # --- SEC-17: server-side admin role (not just email in env) ---
    is_admin = db.Column(db.Boolean, default=False, nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class PasswordResetToken(db.Model):
    """Single-use password reset token (SEC-09). Stores hash, not the token."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    token_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", backref=db.backref("reset_tokens", lazy="dynamic"))


class ResumeAnalysis(db.Model):
    """A saved resume analysis belonging to a user."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    filename = db.Column(db.String(255))
    ats_score = db.Column(db.Float)
    result_json = db.Column(db.JSON)  # full analysis payload (dashboard rendering)
    # Cleaned resume text (truncated) so Wave-1 JD matching can reuse the
    # latest saved analysis without a re-upload.
    resume_text = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("analyses", lazy="dynamic",
                                                     cascade="all, delete-orphan"))


class JDMatchReport(db.Model):
    """A JD-matched ATS scoring report belonging to a user (Wave 1 #1)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    job_title = db.Column(db.String(255))
    score = db.Column(db.Float)
    report_json = db.Column(db.JSON)  # full match report payload
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("jd_match_reports", lazy="dynamic",
                                                     cascade="all, delete-orphan"))


class BulletRewrite(db.Model):
    """A saved bullet-rewrite result belonging to a user (Wave 1 #2)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    original = db.Column(db.Text, nullable=False)
    rewrites = db.Column(db.JSON)  # [{"label","text","tip"}...] + optional AI variant
    ai_powered = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("bullet_rewrites", lazy="dynamic",
                                                     cascade="all, delete-orphan"))


class ResumeVersion(db.Model):
    """A named, saved resume version belonging to a user (Wave 1 #3)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)  # e.g. "Backend v1"
    resume_text = db.Column(db.Text, nullable=False)
    is_default = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           onupdate=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("resume_versions", lazy="dynamic",
                                                     cascade="all, delete-orphan"))


class CoverDoc(db.Model):
    """A generated cover letter / outreach message belonging to a user (Wave 1 #4)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    doc_type = db.Column(db.String(20), nullable=False)  # cover_letter | outreach
    title = db.Column(db.String(255))
    tone = db.Column(db.String(20), default="professional", nullable=False)
    content = db.Column(db.Text, nullable=False)
    ai_powered = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("cover_docs", lazy="dynamic",
                                                     cascade="all, delete-orphan"))


class LinkedInReport(db.Model):
    """A LinkedIn profile grading report belonging to a user (Wave 1 #5)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    score = db.Column(db.Float)
    report_json = db.Column(db.JSON)  # full grading report payload
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("linkedin_reports", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


# Kanban stages for the application tracker (Wave 1 #6).
TRACKER_STAGES = (
    ("wishlist", "Wishlist"),
    ("applied", "Applied"),
    ("oa", "OA / Assignment"),
    ("interview", "Interview"),
    ("offer", "Offer"),
    ("rejected", "Rejected"),
)
TRACKER_STAGE_IDS = {s[0] for s in TRACKER_STAGES}


class JobApplication(db.Model):
    """A tracked job application belonging to a user (Wave 1 #6)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    company = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(200), nullable=False)
    stage = db.Column(db.String(20), nullable=False, default="wishlist", index=True)
    jd_url = db.Column(db.String(500))
    notes = db.Column(db.Text)
    applied_date = db.Column(db.Date)
    resume_version_id = db.Column(db.Integer, db.ForeignKey("resume_version.id"),
                                 nullable=True, index=True)  # which version was sent
    stage_updated_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                                 nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),  # ARC-04
                           onupdate=lambda: datetime.now(UTC), nullable=True)

    user = db.relationship("User", backref=db.backref("job_applications", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class Retrospective(db.Model):
    """A guided per-rejection retrospective (Wave 5 #29).

    One row per JobApplication (unique application_id); the wizard edits it
    step by step (facts -> observed patterns -> hypotheses -> experiment).
    All free-text fields are the user's own words; any auto-computed stats
    are shown alongside but stored only if the user writes them down.
    """
    __tablename__ = "retrospective"

    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("job_application.id"),
                               nullable=False, unique=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    facts = db.Column(db.Text)               # step 1: what happened, user-written
    observed_patterns = db.Column(db.Text)   # step 2: patterns the user noticed
    hypotheses = db.Column(db.Text)          # step 3: hypothesis starters + free text
    stage_reached = db.Column(db.String(20))  # step 1: pipeline stage reached
    completed = db.Column(db.Boolean, nullable=False, default=False)
    completed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           onupdate=lambda: datetime.now(UTC), nullable=False)

    application = db.relationship("JobApplication",
                                  backref=db.backref("retrospective", uselist=False,
                                                     cascade="all, delete-orphan"))
    user = db.relationship("User", backref=db.backref("retrospectives", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class Experiment(db.Model):
    """One controlled change derived from a retrospective hypothesis (Wave 5 #29).

    The hypothesis is always labeled as a hypothesis, never a fact; the
    target must be measurable so the experiment can be judged.
    """
    __tablename__ = "experiment"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    hypothesis = db.Column(db.Text, nullable=False)   # labeled as hypothesis
    change = db.Column(db.Text, nullable=False)      # the one controlled change
    target = db.Column(db.Text, nullable=False)      # what to measure
    duration = db.Column(db.String(50))               # e.g. "3 weeks"
    result = db.Column(db.Text)                       # logged when done
    status = db.Column(db.String(20), nullable=False, default="planned", index=True)
    linked_application_id = db.Column(db.Integer, db.ForeignKey("job_application.id"))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           onupdate=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("experiments", lazy="dynamic",
                                                      cascade="all, delete-orphan"))
    linked_application = db.relationship("JobApplication",
                                         backref=db.backref("experiments",
                                                            cascade="all, delete-orphan"))


class CareerAssessment(db.Model):
    """A career-discovery assessment result belonging to a user (Wave 1 #7)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    top_clusters = db.Column(db.JSON)  # ranked cluster results
    answers_json = db.Column(db.JSON)  # {question_id: option_index}
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("career_assessments", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class InterviewSession(db.Model):
    """A company-specific mock interview session belonging to a user (Wave 2 #13)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    company = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(100), nullable=False)
    category = db.Column(db.String(50), nullable=False)
    questions_json = db.Column(db.JSON)  # selected question dicts
    answers_json = db.Column(db.JSON)    # [{question_id, answer, scoring, feedback}]
    total_score = db.Column(db.Float)
    ai_powered = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("interview_sessions", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class CommunicationAnalysis(db.Model):
    """A communication-coach analysis belonging to a user (Wave 2 #14)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    clarity_score = db.Column(db.Float)
    analysis_json = db.Column(db.JSON)  # full analysis payload
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("communication_analyses", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class DSAProgress(db.Model):
    """Per-user DSA practice progress (Wave 2 #15). One row per user+problem."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    problem_id = db.Column(db.String(50), nullable=False, index=True)
    status = db.Column(db.String(20), default="attempted", nullable=False)  # attempted|solved|stuck
    hints_used = db.Column(db.Integer, default=0, nullable=False)
    code = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           onupdate=lambda: datetime.now(UTC), nullable=False)

    __table_args__ = (db.UniqueConstraint("user_id", "problem_id", name="uq_dsa_user_problem"),)

    user = db.relationship("User", backref=db.backref("dsa_progress", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class AptitudeAttempt(db.Model):
    """A timed aptitude test attempt belonging to a user (Wave 2 #16)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    category = db.Column(db.String(30), nullable=False)  # quantitative|logical|verbal|mixed
    num_questions = db.Column(db.Integer, nullable=False)
    time_limit_sec = db.Column(db.Integer, nullable=False)
    question_ids = db.Column(db.JSON)   # ordered question ids (server-picked)
    answers_json = db.Column(db.JSON)   # {qid: chosen_index}
    results_json = db.Column(db.JSON)   # per-question review payload
    summary_json = db.Column(db.JSON)   # score + analytics
    expired = db.Column(db.Boolean, default=False, nullable=False)
    started_at = db.Column(db.DateTime, nullable=False)  # server timestamp
    submitted_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("aptitude_attempts", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class STARDraft(db.Model):
    """A STAR answer draft belonging to a user (Wave 2 #17)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    competency = db.Column(db.String(50), nullable=False)
    situation = db.Column(db.Text, default="")
    task = db.Column(db.Text, default="")
    action = db.Column(db.Text, default="")
    result = db.Column(db.Text, default="")
    scores_json = db.Column(db.JSON)       # per-section + overall scores
    suggestions_json = db.Column(db.JSON)  # rewrite suggestions
    ai_powered = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           onupdate=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("star_drafts", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class RoadmapProgress(db.Model):
    """Per-user learning-roadmap progress (Wave 3 #18). One row per user+skill+stage."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    skill_id = db.Column(db.String(50), nullable=False, index=True)
    stage = db.Column(db.String(20), nullable=False)  # beginner|intermediate|advanced
    status = db.Column(db.String(20), default="not_started", nullable=False)  # not_started|in_progress|completed
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           onupdate=lambda: datetime.now(UTC), nullable=False)

    __table_args__ = (db.UniqueConstraint("user_id", "skill_id", "stage",
                                          name="uq_roadmap_user_skill_stage"),)

    user = db.relationship("User", backref=db.backref("roadmap_progress", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class QuizAttempt(db.Model):
    """A skill-quiz attempt belonging to a user (Wave 3 #19)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    skill = db.Column(db.String(50), nullable=False, index=True)
    num_questions = db.Column(db.Integer, nullable=False)
    difficulty_mix = db.Column(db.JSON)   # list of difficulties or []
    timed = db.Column(db.Boolean, default=False, nullable=False)
    time_limit_sec = db.Column(db.Integer, default=0, nullable=False)
    question_ids = db.Column(db.JSON)     # ordered question ids (server-picked)
    checks_json = db.Column(db.JSON)      # per-question instant-check records
    answers_json = db.Column(db.JSON)     # {qid: chosen_index}
    results_json = db.Column(db.JSON)     # per-question review payload
    summary_json = db.Column(db.JSON)     # score + topic/difficulty analytics
    started_at = db.Column(db.DateTime, nullable=False)  # server timestamp
    submitted_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("quiz_attempts", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class CertPlan(db.Model):
    """A saved certification study plan belonging to a user (Wave 3 #20)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    cert_id = db.Column(db.String(50), nullable=False, index=True)
    weeks = db.Column(db.Integer, nullable=False)
    plan_json = db.Column(db.JSON)  # week-by-week plan
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("cert_plans", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class StudyDoc(db.Model):
    """A PDF in a user's study library (Wave 4 #21). Chunks live in data."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    stored_filename = db.Column(db.String(255))  # uuid-prefixed name under UPLOAD_FOLDER
    source = db.Column(db.String(20), default="upload", nullable=False)  # upload | arxiv | doi
    source_ref = db.Column(db.String(255))  # arXiv ID or DOI when imported
    num_pages = db.Column(db.Integer, default=0)
    num_chunks = db.Column(db.Integer, default=0)
    char_count = db.Column(db.Integer, default=0)
    data = db.Column(db.JSON)  # {"chunks": [{"page":1,"index":0,"text":"...","heading":...}], "truncated": bool}
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("study_docs", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class StudyQuestion(db.Model):
    """A cited Q&A exchange against a user's study library (Wave 4 #21)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    doc_id = db.Column(db.Integer, db.ForeignKey("study_doc.id"), nullable=True, index=True)
    question = db.Column(db.Text, nullable=False)
    answer = db.Column(db.Text)
    citations = db.Column(db.JSON)  # [{"doc": title, "page": n, "chunk": i, "excerpt": str}]
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("study_questions", lazy="dynamic",
                                                      cascade="all, delete-orphan"))
    doc = db.relationship("StudyDoc", backref=db.backref("questions", lazy="dynamic",
                                                          cascade="all, delete-orphan"))


class PaperImport(db.Model):
    """An arXiv/DOI-imported paper (Wave 4 #25) with provenance."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title = db.Column(db.String(512), nullable=False)
    authors = db.Column(db.JSON)  # ["First Last", ...]
    abstract = db.Column(db.Text)
    year = db.Column(db.Integer)
    venue = db.Column(db.String(255))
    doi = db.Column(db.String(255))
    arxiv_id = db.Column(db.String(64), index=True)
    pdf_url = db.Column(db.String(512))
    openalex_id = db.Column(db.String(64))
    source = db.Column(db.String(20))  # arxiv | openalex
    fetched_at = db.Column(db.DateTime(timezone=True))  # UTC provenance timestamp (ARC-04)
    data = db.Column(db.JSON)  # {"graph": {...}} cached citation graph
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("paper_imports", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class LitReview(db.Model):
    """A generated multi-paper literature review (Wave 4 #22)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    doc_ids = db.Column(db.JSON)  # [study_doc ids]
    data = db.Column(db.JSON)  # {"per_paper": [...], "themes": [...],
                               #  "claims_table": [...], "coverage_gaps": str,
                               #  "bibtex": str}
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("lit_reviews", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class GapReport(db.Model):
    """A cached research-gap analysis over a user's paper collection (Wave 4 #23)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    doc_ids = db.Column(db.JSON)  # [study_doc ids] analyzed
    data = db.Column(db.JSON)  # {"gaps": [{"title","type","evidence":[...],
                               #  "suggested_questions":[]}], "next_papers": [...]}
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("gap_reports", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class FlashcardSet(db.Model):
    """Flashcards + MCQs generated from a StudyDoc (Wave 4 #26)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    doc_id = db.Column(db.Integer, db.ForeignKey("study_doc.id"), nullable=False, index=True)
    card_count = db.Column(db.Integer, default=0)
    data = db.Column(db.JSON)  # {"cards": [{"id","kind","question","answer",
                               #  "options":[],"chunk":i,"page":n,"doc_title":""}],
                               #  "progress": {card_id: {"known": bool, "reviews": int}}}
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("flashcard_sets", lazy="dynamic",
                                                      cascade="all, delete-orphan"))
    doc = db.relationship("StudyDoc", backref=db.backref("flashcard_sets", lazy="dynamic",
                                                         cascade="all, delete-orphan"))


class PaperQuizAttempt(db.Model):
    """A scored MCQ quiz attempt from a paper's generated questions (Wave 4 #26)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    set_id = db.Column(db.Integer, db.ForeignKey("flashcard_set.id"), nullable=False, index=True)
    score = db.Column(db.Integer, default=0)
    total = db.Column(db.Integer, default=0)
    data = db.Column(db.JSON)  # {"answers": {card_id: chosen_idx}, "details": [...]}
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("paper_quiz_attempts", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class ResearchSession(db.Model):
    """A saved research-chat conversation belonging to a user."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title = db.Column(db.String(255))
    data = db.Column(db.JSON)  # {"messages": [{"role":..., "content":...}], "doc_hash": ...}
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           onupdate=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("research_sessions", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class Goal(db.Model):
    """A career goal tracked by a user on their dashboard."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    target_date = db.Column(db.Date, nullable=True)
    status = db.Column(db.String(20), default="active", nullable=False)  # active | done
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),  # ARC-04
                           onupdate=lambda: datetime.now(UTC), nullable=True)

    user = db.relationship("User", backref=db.backref("goals", lazy="dynamic",
                                                     cascade="all, delete-orphan"))


class OpportunityBookmark(db.Model):
    """A user's bookmarked opportunity from the radar board (Wave 1 #9)."""
    __tablename__ = "opportunity_bookmark"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    opp_id = db.Column(db.String(64), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    __table_args__ = (db.UniqueConstraint("user_id", "opp_id",
                                         name="uq_bookmark_user_opp"),)

    user = db.relationship("User", backref=db.backref("opportunity_bookmarks", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class Portfolio(db.Model):
    """A user's one-page portfolio (Wave 1 #12). One row per user."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False,
                       unique=True, index=True)
    slug = db.Column(db.String(60), nullable=False, unique=True, index=True)
    display_name = db.Column(db.String(80), nullable=False)
    headline = db.Column(db.String(140))
    bio = db.Column(db.Text)
    skills = db.Column(db.JSON)
    projects = db.Column(db.JSON)
    links = db.Column(db.JSON)
    show_github = db.Column(db.Boolean, default=False, nullable=False)
    show_leetcode = db.Column(db.Boolean, default=False, nullable=False)
    is_published = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           onupdate=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("portfolio", uselist=False,
                                                      cascade="all, delete-orphan"))


class PeerRoom(db.Model):
    """A peer mock-interview room (Wave 5 #28).

    Two students take turns interviewing each other. The host creates the
    room (role + category + optional scheduling), a peer joins via the
    12-char share token, and the platform structures question cards,
    timing, role swaps and reciprocal rubric feedback. question_ids holds
    the current round's interview-bank question ids (in display order);
    drawn_ids accumulates every id drawn so role swaps draw fresh cards.
    """
    __tablename__ = "peer_room"

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(12), unique=True, nullable=False, index=True)
    host_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False,
                       index=True)
    role = db.Column(db.String(30), nullable=False, default="general")
    category = db.Column(db.String(30), nullable=False, default="behavioral")
    company = db.Column(db.String(60), nullable=True)
    difficulty = db.Column(db.String(10), nullable=True)
    num_questions = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="open", index=True)
    scheduled_at = db.Column(db.DateTime, nullable=True)
    guest_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True,
                        index=True)
    invite_only = db.Column(db.Boolean, nullable=False, default=False)
    interviewer_id = db.Column(db.Integer, db.ForeignKey("user.id"),
                              nullable=True)
    question_ids = db.Column(db.JSON)
    drawn_ids = db.Column(db.JSON)
    round_no = db.Column(db.Integer, nullable=False, default=0)
    proposed_slots = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           nullable=False)

    host = db.relationship("User", foreign_keys=[host_id])
    guest = db.relationship("User", foreign_keys=[guest_id])
    interviewer = db.relationship("User", foreign_keys=[interviewer_id])


class PeerAnswer(db.Model):
    """A typed text answer by the current interviewee in a peer room."""
    __tablename__ = "peer_answer"

    __table_args__ = (db.UniqueConstraint("room_id", "user_id", "question_id",
                                         name="uq_peer_answer"),)

    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey("peer_room.id"),
                       nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False,
                       index=True)
    question_id = db.Column(db.String(20), nullable=False)
    question_text = db.Column(db.Text)
    answer_text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           nullable=False)

    room = db.relationship("PeerRoom", backref=db.backref(
        "answers", lazy="dynamic", cascade="all, delete-orphan"))


class PeerFeedback(db.Model):
    """Reciprocal rubric feedback: each participant reviews the other.

    Five 1-5 rubric dimensions plus free-text comments. One row per
    (room, reviewer, reviewee) — resubmission updates the existing row.
    """
    __tablename__ = "peer_feedback"

    __table_args__ = (db.UniqueConstraint("room_id", "reviewer_id", "reviewee_id",
                                         name="uq_peer_feedback_pair"),)

    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey("peer_room.id"),
                       nullable=False, index=True)
    reviewer_id = db.Column(db.Integer, db.ForeignKey("user.id"),
                           nullable=False, index=True)
    reviewee_id = db.Column(db.Integer, db.ForeignKey("user.id"),
                           nullable=False, index=True)
    clarity = db.Column(db.Integer, nullable=False)
    structure = db.Column(db.Integer, nullable=False)
    depth = db.Column(db.Integer, nullable=False)
    relevance = db.Column(db.Integer, nullable=False)
    confidence = db.Column(db.Integer, nullable=False)
    comments = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           nullable=False)

    room = db.relationship("PeerRoom", backref=db.backref(
        "feedbacks", lazy="dynamic", cascade="all, delete-orphan"))


class PeerBlock(db.Model):
    """Safety: a user reporting + blocking another user.

    Blocked users can neither join each other's rooms nor see each other's
    rooms on the open board (both directions).
    """
    __tablename__ = "peer_block"

    __table_args__ = (db.UniqueConstraint("blocker_id", "blocked_id",
                                         name="uq_peer_block"),)

    id = db.Column(db.Integer, primary_key=True)
    blocker_id = db.Column(db.Integer, db.ForeignKey("user.id"),
                          nullable=False, index=True)
    blocked_id = db.Column(db.Integer, db.ForeignKey("user.id"),
                          nullable=False, index=True)
    reason = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           nullable=False)


class Mentor(db.Model):
    """A mentor profile for Wave 6 #27 office hours.

    Seed profiles (is_seed=True) ship with the app and are marked verified;
    real mentors join via MentorApplication and get verified=True only after
    an admin approves them. user_id links the profile to the mentor's own
    STUDY-BUDDY account when one exists (null for seed rows).
    """
    __tablename__ = "mentor"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    headline = db.Column(db.String(200), nullable=False)
    path = db.Column(db.String(30), nullable=False, default="other", index=True)
    company = db.Column(db.String(120))
    college = db.Column(db.String(160))
    bio = db.Column(db.Text)
    linkedin_url = db.Column(db.String(300))
    avatar_url = db.Column(db.String(300), nullable=True)
    verified = db.Column(db.Boolean, nullable=False, default=False)
    is_seed = db.Column(db.Boolean, nullable=False, default=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True,
                        index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           nullable=False)

    user = db.relationship("User", foreign_keys=[user_id])


class MentorApplication(db.Model):
    """A student's application to become a mentor. Status moves
    pending -> approved | rejected; approval creates the Mentor row."""
    __tablename__ = "mentor_application"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False,
                        index=True)
    name = db.Column(db.String(120), nullable=False)
    headline = db.Column(db.String(200), nullable=False)
    path = db.Column(db.String(30), nullable=False, default="other")
    company = db.Column(db.String(120))
    college = db.Column(db.String(160))
    bio = db.Column(db.Text)
    linkedin_url = db.Column(db.String(300))
    status = db.Column(db.String(20), nullable=False, default="pending",
                       index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           nullable=False)

    user = db.relationship("User", foreign_keys=[user_id])


class OfficeHour(db.Model):
    """One mentor office-hour session: a scheduled, capacity-bounded slot
    students book seats in."""
    __tablename__ = "office_hour"

    id = db.Column(db.Integer, primary_key=True)
    mentor_id = db.Column(db.Integer, db.ForeignKey("mentor.id"), nullable=False,
                          index=True)
    topic = db.Column(db.String(200), nullable=False)
    path = db.Column(db.String(30), nullable=False, default="other", index=True)
    scheduled_at = db.Column(db.DateTime, nullable=False, index=True)
    duration_min = db.Column(db.Integer, nullable=False, default=45)
    capacity = db.Column(db.Integer, nullable=False, default=12)
    status = db.Column(db.String(20), nullable=False, default="open", index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           nullable=False)

    mentor = db.relationship("Mentor",
                             backref=db.backref("office_hours", lazy="dynamic",
                                                cascade="all, delete-orphan"))


class OHBooking(db.Model):
    """A student's seat in an office hour. The unique constraint prevents
    double-booking; cancelled seats don't count against capacity."""
    __tablename__ = "oh_booking"

    __table_args__ = (db.UniqueConstraint("user_id", "office_hour_id",
                                         name="uq_oh_booking_user_session"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False,
                        index=True)
    office_hour_id = db.Column(db.Integer, db.ForeignKey("office_hour.id"),
                               nullable=False, index=True)
    question_text = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, default="booked", index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           nullable=False)

    user = db.relationship("User", foreign_keys=[user_id])
    session = db.relationship("OfficeHour",
                              backref=db.backref("bookings", lazy="dynamic",
                                                 cascade="all, delete-orphan"))


class OHNote(db.Model):
    """Post-session action notes a student writes for themselves after
    attending an office hour."""
    __tablename__ = "oh_note"

    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("oh_booking.id"),
                           nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False,
                        index=True)
    note_text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           nullable=False)

    booking = db.relationship("OHBooking",
                              backref=db.backref("notes", lazy="dynamic",
                                                 cascade="all, delete-orphan"))


class OHFeedback(db.Model):
    """One rating per booking: 1-5 stars plus what helped most."""
    __tablename__ = "oh_feedback"

    __table_args__ = (db.UniqueConstraint("booking_id",
                                         name="uq_oh_feedback_booking"),)

    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("oh_booking.id"),
                           nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False,
                        index=True)
    rating = db.Column(db.Integer, nullable=False)
    what_helped = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           nullable=False)

    booking = db.relationship("OHBooking",
                              backref=db.backref("feedback", uselist=False,
                                                 cascade="all, delete-orphan"))


class OHReport(db.Model):
    """A safety report against a mentor or a session: spam, harassment,
    fake profiles, no-shows. Reviewed by admins."""
    __tablename__ = "oh_report"

    id = db.Column(db.Integer, primary_key=True)
    reporter_user_id = db.Column(db.Integer, db.ForeignKey("user.id"),
                                 nullable=False, index=True)
    mentor_id = db.Column(db.Integer, db.ForeignKey("mentor.id"), nullable=True,
                          index=True)
    office_hour_id = db.Column(db.Integer, db.ForeignKey("office_hour.id"),
                               nullable=True, index=True)
    reason = db.Column(db.String(40), nullable=False)
    details = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, default="open", index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           nullable=False)


class AnonymousAnalysis(db.Model):
    """A resume analysis for an anonymous visitor.

    Replaces the old per-process RESULTS_CACHE dict so results survive
    restarts and work across multiple gunicorn workers. Rows expire after
    24h and are cleaned up opportunistically on each new analysis.
    """
    id = db.Column(db.String(36), primary_key=True)  # uuid4 hex
    filename = db.Column(db.String(255))
    result_json = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)


_ANON_TTL_SECONDS = 24 * 3600


class ProfileSnapshot(db.Model):
    """Cached third-party profile snapshot (GitHub / LeetCode).

    One row per (user, provider); refreshed on demand with 24h freshness so
    we stay kind to the upstream APIs. The payload in `data` is whatever the
    integration module returned (analyzer result or stats dict)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    provider = db.Column(db.String(20), nullable=False)  # "github" | "leetcode"
    username = db.Column(db.String(80), nullable=False)
    data = db.Column(db.JSON)
    fetched_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("profile_snapshots", lazy="dynamic",
                                                      cascade="all, delete-orphan"))

    __table_args__ = (db.UniqueConstraint("user_id", "provider",
                                          name="uq_snapshot_user_provider"),)


class LLMConsent(db.Model):
    """PRIV-03: Records when a user consented to LLM processing.

    Shown once before the first LLM-backed action: what text is sent,
    to which provider, retention. Stored with timestamp + policy version.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False,
                        unique=True, index=True)
    consented_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                             nullable=False)
    policy_version = db.Column(db.String(20), nullable=False, default="v1")
    # What the user agreed to (snapshot of the notice text version)
    notice_text = db.Column(db.Text, nullable=True)

    user = db.relationship("User", backref=db.backref("llm_consent", uselist=False,
                                                      cascade="all, delete-orphan"))

class UserAPIKey(db.Model):
    """PRIV-05 BYOK: user's own LLM API key, stored encrypted.

    The key is encrypted with Fernet (key from ENCRYPTION_KEY env, or
    SECRET_KEY-derived fallback). Only used per-request, never logged.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False,
                        unique=True, index=True)
    provider = db.Column(db.String(30), nullable=False, default="openrouter")
    encrypted_key = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           nullable=False)

    user = db.relationship("User", backref=db.backref("api_key", uselist=False,
                                                      cascade="all, delete-orphan"))

    @staticmethod
    def _fernet():
        from cryptography.fernet import Fernet
        import hashlib
        import base64
        raw = os.environ.get("ENCRYPTION_KEY") or os.environ.get("SECRET_KEY", "")
        # Derive a 32-byte Fernet key from the secret
        digest = hashlib.sha256(raw.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(digest))

    def set_key(self, plaintext):
        self.encrypted_key = self._fernet().encrypt(
            plaintext.encode()).decode()

    def get_key(self):
        return self._fernet().decrypt(
            self.encrypted_key.encode()).decode()


CONSENT_POLICY_VERSION = "v1"
CONSENT_NOTICE = (
    "AI features send your text (e.g. resume, questions) to our AI provider "
    "(Google Gemini, with Groq/OpenRouter fallback) to generate responses. "
    "Your prompts are not stored. You can withdraw consent anytime from your profile."
)


_SNAPSHOT_TTL_SECONDS = 24 * 3600


class XPLedger(db.Model):
    """One XP-earning career action (Wave 5 #27: humane momentum system).

    ``dedupe_key`` ("<user_id>:<event_type>:<YYYY-MM-DD>") is unique so each
    daily event is recorded at most once — duplicates are skipped, never an
    error. Weekly quest/league counting is derived from these keys.
    """
    __tablename__ = "xp_ledger"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    event_type = db.Column(db.String(40), nullable=False, index=True)
    xp = db.Column(db.Integer, nullable=False, default=0)
    dedupe_key = db.Column(db.String(120), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("xp_ledger", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class StreakState(db.Model):
    """Per-user streak with monthly forgiveness shields (Wave 5 #27).

    Two shields per calendar month auto-repair a missed day; when shields run
    out the streak resets gracefully to 1, never 0.
    """
    __tablename__ = "streak_state"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False,
                        unique=True, index=True)
    current = db.Column(db.Integer, default=0, nullable=False)
    longest = db.Column(db.Integer, default=0, nullable=False)
    last_active_date = db.Column(db.Date, nullable=True)
    shields_used_month = db.Column(db.Integer, default=0, nullable=False)
    shields_month = db.Column(db.String(7), nullable=True)  # "YYYY-MM"

    user = db.relationship("User", backref=db.backref("streak_state", uselist=False,
                                                      cascade="all, delete-orphan"))


class LeagueMember(db.Model):
    """Opt-in weekly league cohort membership (Wave 5 #27).

    ``league_key`` is the ISO week of joining ("2026-W42"); cohorts stay
    small (~8-12) by construction. ``opted_out`` hides the user from the
    board without deleting history.
    """
    __tablename__ = "league_member"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False,
                        unique=True, index=True)
    league_key = db.Column(db.String(16), nullable=False, index=True)
    joined_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)
    opted_out = db.Column(db.Boolean, default=False, nullable=False)

    user = db.relationship("User", backref=db.backref("league_member", uselist=False,
                                                      cascade="all, delete-orphan"))


class LeagueResult(db.Model):
    """Finalized weekly standings for one league cohort (Wave 5 #27).

    Written once per (league, week) by momentum.finalize_league_week().
    ``won`` marks the week's consistency winner (streak days + quests, not
    raw XP prestige).
    """
    __tablename__ = "league_result"
    id = db.Column(db.Integer, primary_key=True)
    league_key = db.Column(db.String(16), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    week_start = db.Column(db.Date, nullable=False, index=True)
    xp = db.Column(db.Integer, default=0, nullable=False)
    streak_days = db.Column(db.Integer, default=0, nullable=False)
    quests_completed = db.Column(db.Integer, default=0, nullable=False)
    won = db.Column(db.Boolean, default=False, nullable=False)

    __table_args__ = (db.UniqueConstraint("league_key", "user_id", "week_start",
                                          name="uq_league_result"),)

    user = db.relationship("User", backref=db.backref("league_results", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class QuestProgress(db.Model):
    """Per-user weekly quest progress (Wave 5 #27). Counts are derived from
    XPLedger rows; ``claimed`` guards the one-time 50 XP completion bonus."""
    __tablename__ = "quest_progress"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    week_start = db.Column(db.Date, nullable=False, index=True)
    quest_id = db.Column(db.String(40), nullable=False)
    count = db.Column(db.Integer, default=0, nullable=False)
    completed = db.Column(db.Boolean, default=False, nullable=False)
    claimed = db.Column(db.Boolean, default=False, nullable=False)

    __table_args__ = (db.UniqueConstraint("user_id", "week_start", "quest_id",
                                          name="uq_quest_progress"),)

    user = db.relationship("User", backref=db.backref("quest_progress", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class WeeklyReflection(db.Model):
    """A user's 2-minute weekly reflection (Wave 5 #27). One per ISO week."""
    __tablename__ = "weekly_reflection"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    week_start = db.Column(db.Date, nullable=False)
    wins = db.Column(db.Text)
    focus_next = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           onupdate=lambda: datetime.now(UTC), nullable=False)

    __table_args__ = (db.UniqueConstraint("user_id", "week_start",
                                          name="uq_weekly_reflection"),)

    user = db.relationship("User", backref=db.backref("weekly_reflections", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class MomentumSettings(db.Model):
    """Momentum preferences (Wave 5 #27). A separate table so the User model
    stays untouched. When ``momentum_opt_out`` is true, record_event() is a
    no-op and the hub shows a rejoin CTA."""
    __tablename__ = "momentum_settings"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False,
                        unique=True, index=True)
    momentum_opt_out = db.Column(db.Boolean, default=False, nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           onupdate=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("momentum_settings", uselist=False,
                                                      cascade="all, delete-orphan"))


class HealthReview(db.Model):
    """A monthly, explainable career-health review (Wave 6 #30).

    One row per (user, month_key). dimensions_json holds the five transparent
    dimensions (direction, proof, opportunity, preparation, wellbeing), each
    {score 0-100 or None, trend vs last month or None, confidence 0-100,
    evidence: [human strings]}. No opaque employability score — everything
    the user sees is backed by evidence strings. Idempotent: regenerating a
    month updates the row in place.
    """
    __tablename__ = "health_review"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    month_key = db.Column(db.String(7), nullable=False, index=True)  # "2026-10"
    dimensions_json = db.Column(db.JSON)  # per-dimension payloads (see docstring)
    narrative = db.Column(db.Text)         # 2-3 plain sentences, no jargon
    next_action = db.Column(db.Text)      # single smallest high-leverage action
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),  # ARC-04
                           onupdate=lambda: datetime.now(UTC), nullable=True)

    __table_args__ = (db.UniqueConstraint("user_id", "month_key",
                                          name="uq_health_review_user_month"),)

    user = db.relationship("User", backref=db.backref("health_reviews", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


def _utcnow_naive():
    """UTC now without tzinfo (SQLite returns naive datetimes)."""
    return datetime.now(UTC).replace(tzinfo=None)


def _purge_expired_anonymous():
    """Delete expired anonymous analyses (best-effort, never raises)."""
    try:
        AnonymousAnalysis.query.filter(
            AnonymousAnalysis.expires_at < _utcnow_naive()).delete()
        db.session.commit()
    except Exception:
        db.session.rollback()
        log.exception("Failed to purge expired anonymous analyses")


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ========================================================
# --- HELPERS ---
# ========================================================

# Anonymous results now live in the AnonymousAnalysis table (P1#8).
# (The old per-process RESULTS_CACHE dict was removed: it lost data on
# every restart and broke under multiple gunicorn workers.)

ALLOWED_EXTENSIONS = {'pdf', 'docx', 'doc'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def json_safe(obj):
    """Round-trip through JSON so DB-stored payloads are always serializable
    (numpy types from the ML pipeline become plain python types)."""
    return json.loads(json.dumps(obj, default=str))


# ========================================================
# --- Wave 5 #29: rejection retrospectives (pure helpers) ---
# ========================================================
# Pure functions over lists of JobApplication/Retrospective rows — no Flask
# request context needed, so they are unit-testable in isolation.

RETRO_PIPELINE = ("wishlist", "applied", "oa", "interview", "offer")
RETRO_PIPELINE_LABELS = {sid: label for sid, label in TRACKER_STAGES}
RETRO_WIZARD_STEPS = ("facts", "patterns", "hypotheses", "experiment")
RETRO_EXPERIMENT_STATUSES = ("planned", "running", "done")
RETRO_SMALL_SAMPLE_N = 5
RETRO_MAX_TEXT = 5000

# Valid forward transitions for Experiment.status (same-status allowed).
RETRO_STATUS_TRANSITIONS = {
    "planned": ("running", "done"),
    "running": ("done",),
    "done": (),
}

# Keywords scanned in application notes when jd_url yields no domain.
_RETRO_SOURCE_KEYWORDS = ("linkedin", "naukri", "indeed", "referral",
                          "wellfound", "angel", "unstop", "hackathon",
                          "cold email", "company website", "careers page",
                          "campus")


def retro_source_of(app_row):
    """Best-effort application source: the jd_url domain when present, else a
    keyword scan of the notes, else "unknown". Clearly labeled best-effort
    wherever it is shown."""
    url = (app_row.jd_url or "").strip()
    if url:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url if "://" in url else "https://" + url)
            netloc = (parsed.netloc or "").lower()
            if netloc.startswith("www."):
                netloc = netloc[4:]
            if netloc:
                return netloc
        except Exception:
            pass
    notes = (app_row.notes or "").lower()
    for kw in _RETRO_SOURCE_KEYWORDS:
        if kw in notes:
            return kw
    return "unknown"


def retro_funnel_stats(apps):
    """Funnel over the pipeline stages; "rejected" is a side-track.

    reached[s] = applications whose CURRENT stage is s or any later pipeline
    stage (an application sitting in "offer" necessarily passed through the
    earlier stages). conversions[i] gives reached[to]/reached[from] between
    successive stages.
    """
    counts = {sid: 0 for sid in RETRO_PIPELINE}
    rejected = 0
    for a in apps:
        if a.stage in counts:
            counts[a.stage] += 1
        elif a.stage == "rejected":
            rejected += 1
    order = {sid: i for i, sid in enumerate(RETRO_PIPELINE)}
    reached = {}
    for sid in RETRO_PIPELINE:
        reached[sid] = sum(c for t, c in counts.items() if order[t] >= order[sid])
    conversions = []
    for i in range(len(RETRO_PIPELINE) - 1):
        prev, nxt = RETRO_PIPELINE[i], RETRO_PIPELINE[i + 1]
        denom, num = reached[prev], reached[nxt]
        rate = round(100.0 * num / denom, 1) if denom else None
        conversions.append({
            "from": prev, "to": nxt,
            "from_label": RETRO_PIPELINE_LABELS.get(prev, prev),
            "to_label": RETRO_PIPELINE_LABELS.get(nxt, nxt),
            "rate": rate, "numerator": num, "denominator": denom,
        })
    return {
        "stages": [{"id": sid, "label": RETRO_PIPELINE_LABELS.get(sid, sid),
                    "count": counts[sid], "reached": reached[sid]}
                   for sid in RETRO_PIPELINE],
        "conversions": conversions,
        "rejected": rejected,
        "total": len(apps),
    }


def retro_role_breakdown(apps, top_n=8):
    """Top roles by application volume with rejection/response rates.

    Each row carries a small_sample flag when n < RETRO_SMALL_SAMPLE_N —
    the trust rule for every breakdown."""
    rows = {}
    for a in apps:
        key = (a.role or "").strip().lower() or "(no role)"
        row = rows.setdefault(key, {"role": (a.role or "").strip() or "(no role)",
                                    "applications": 0, "rejected": 0,
                                    "responded": 0, "offers": 0})
        row["applications"] += 1
        if a.stage == "rejected":
            row["rejected"] += 1
        if a.stage in ("oa", "interview", "offer"):
            row["responded"] += 1
        if a.stage == "offer":
            row["offers"] += 1
    out = []
    for row in rows.values():
        n = row["applications"]
        row["rejection_rate"] = round(100.0 * row["rejected"] / n, 1) if n else 0.0
        row["response_rate"] = round(100.0 * row["responded"] / n, 1) if n else 0.0
        row["small_sample"] = n < RETRO_SMALL_SAMPLE_N
        out.append(row)
    out.sort(key=lambda r: (-r["applications"], r["role"]))
    return out[:top_n]


def retro_rejection_stage_breakdown(retrospectives):
    """Where rejections cluster: retrospectives grouped by the user-reported
    stage the process reached. Blank/invalid stages land in "unknown" —
    reported separately, never silently dropped."""
    counts = {}
    unknown = 0
    for r in retrospectives:
        key = (r.stage_reached or "").strip().lower()
        if not key or key not in RETRO_PIPELINE:
            unknown += 1
            continue
        counts[key] = counts.get(key, 0) + 1
    rows = [{"stage": sid, "label": RETRO_PIPELINE_LABELS.get(sid, sid),
             "rejections": counts.get(sid, 0),
             "small_sample": counts.get(sid, 0) < RETRO_SMALL_SAMPLE_N}
            for sid in RETRO_PIPELINE]
    return {"rows": rows, "unknown": unknown,
            "total": sum(counts.values()) + unknown}


def retro_source_breakdown(apps, top_n=8):
    """Applications grouped by best-effort source with rates. Every row is
    flagged best_effort=True so the UI can say so."""
    rows = {}
    for a in apps:
        src = retro_source_of(a)
        row = rows.setdefault(src, {"source": src, "applications": 0,
                                    "rejected": 0, "responded": 0,
                                    "best_effort": True})
        row["applications"] += 1
        if a.stage == "rejected":
            row["rejected"] += 1
        if a.stage in ("oa", "interview", "offer"):
            row["responded"] += 1
    out = []
    for row in rows.values():
        n = row["applications"]
        row["rejection_rate"] = round(100.0 * row["rejected"] / n, 1) if n else 0.0
        row["response_rate"] = round(100.0 * row["responded"] / n, 1) if n else 0.0
        row["small_sample"] = n < RETRO_SMALL_SAMPLE_N
        out.append(row)
    out.sort(key=lambda r: (-r["applications"], r["source"]))
    return out[:top_n]


def retro_suggestions(funnel, roles, rejection_stages, sources):
    """Trust-rule suggestion cards. EVERY card carries three labels:

      - "Observed pattern"  — a number from the user's own data
      - "Plausible cause (hypothesis)" — explicitly a hypothesis, never a fact
      - "Recommended test" — one concrete experiment to run

    Nothing is stated as a fact; every cause is framed as a hypothesis."""
    cards = []

    def _add(pattern, cause, test, n):
        cards.append({
            "observed_pattern": pattern,
            "plausible_cause": cause,
            "recommended_test": test,
            "sample_size": n,
            "small_sample": n < RETRO_SMALL_SAMPLE_N,
        })

    # 1. Weakest funnel conversion.
    measurable = [c for c in funnel["conversions"]
                  if c["rate"] is not None and c["denominator"] >= 2]
    if measurable:
        weak = min(measurable, key=lambda c: c["rate"])
        _add(
            "Only {rate}% of applications that reached {frm} advanced to "
            "{to} (n={d} reached {frm}).".format(
                rate=weak["rate"], frm=weak["from_label"],
                to=weak["to_label"], d=weak["denominator"]),
            "Hypothesis: the materials that got you to {frm} may not be tuned "
            "for what the {to} stage screens for.".format(
                frm=weak["from_label"], to=weak["to_label"]),
            "Log a retrospective for one such rejection, pick the matching "
            "hypothesis starter, and run a 3-week experiment targeting the "
            "{frm}-to-{to} step.".format(frm=weak["from_label"],
                                         to=weak["to_label"]),
            weak["denominator"],
        )

    # 2. Role with the highest rejection rate.
    scored = [r for r in roles if r["applications"] >= 2 and r["rejected"] > 0]
    if scored:
        worst = max(scored, key=lambda r: (r["rejection_rate"], r["rejected"]))
        _add(
            '"{role}" has the highest rejection rate: {rate}% '
            "({rej} of {n} applications).".format(
                role=worst["role"], rate=worst["rejection_rate"],
                rej=worst["rejected"], n=worst["applications"]),
            "Hypothesis: the resume/portfolio emphasis may be misaligned with "
            "what this role title typically screens for — or the role-fit bar "
            "is simply higher.",
            'Do a JD-vs-resume keyword pass for the next 5 "{role}" '
            "applications and compare the response rate.".format(role=worst["role"]),
            worst["applications"],
        )

    # 3. Where rejections cluster by stage reached.
    clustered = [r for r in rejection_stages["rows"] if r["rejections"] > 0]
    if clustered:
        top = max(clustered, key=lambda r: r["rejections"])
        _add(
            "Most logged rejections happened after reaching the {label} stage "
            "({n} rejections).".format(label=top["label"], n=top["rejections"]),
            "Hypothesis: something specific to the {label} stage — preparation, "
            "materials, or expectations — may be the bottleneck.".format(
                label=top["label"]),
            "Pick one experiment template for stage preparation and run it for "
            "your next 3 processes that reach this stage.",
            top["rejections"],
        )

    # 4. Source with the lowest response rate (best-effort detection).
    resp = [s for s in sources
            if s["applications"] >= 2 and s["source"] != "unknown"]
    if resp:
        low = min(resp, key=lambda s: s["response_rate"])
        _add(
            'Applications via "{src}" (best-effort source detection) show the '
            "lowest response rate: {rate}% (n={n}).".format(
                src=low["source"], rate=low["response_rate"],
                n=low["applications"]),
            "Hypothesis: this channel may surface roles with higher "
            "competition or poorer fit — or your materials may not be tuned "
            "for its audience.",
            "Shift your next 10 applications toward your best-responding "
            "source and compare response rates.",
            low["applications"],
        )
    return cards


def _retro_experiment_to_dict(exp):
    """JSON-serializable Experiment row (safe for API responses)."""
    linked = exp.linked_application
    created = exp.created_at
    updated = exp.updated_at
    return {
        "id": exp.id,
        "hypothesis": exp.hypothesis,
        "change": exp.change,
        "target": exp.target,
        "duration": exp.duration,
        "result": exp.result,
        "status": exp.status,
        "linked_application_id": exp.linked_application_id,
        "linked_company": linked.company if linked else None,
        "linked_role": linked.role if linked else None,
        "created_at": created.isoformat() if created else None,
        "updated_at": updated.isoformat() if updated else None,
    }


def _momentum_hook(user_id, event_type):
    """Award momentum XP for a completed career action (Wave 5 #27).

    Defensive by design: called AFTER the handler's own commit succeeds, and
    never raises — momentum.record_event() itself swallows errors, and this
    wrapper adds one more guard so an XP failure can never break the main
    flow (the response is already decided by then).
    """
    try:
        momentum_mod.record_event(user_id, event_type)
    except Exception:
        log.exception("Momentum hook failed for event %s (non-fatal)",
                      event_type)


# ---------------------------------------------------------------------------
# Peer mock interviews (Wave 5 #28): shared constants and helpers
# ---------------------------------------------------------------------------

#: Roles a peer room can target, aligned with the roles tagged in the
#: 1,050-question interview bank. 'data' covers data_analyst +
#: data_scientist; 'general' draws category-only.
PEER_ROLES = ("sde", "frontend", "backend", "data", "devops", "pm",
              "general")

#: Room categories, aligned with the bank's own categories: 'technical'
#: covers the bank's technical set plus dsa / system_design / puzzles;
#: 'mixed' draws from everything.
PEER_CATEGORIES = ("behavioral", "technical", "hr", "mixed")
PEER_CATEGORY_MAP = {
    "behavioral": ("behavioral", "hr"),
    "technical": ("technical", "dsa", "system_design", "puzzles"),
    "hr": ("hr",),
    "mixed": ("behavioral", "technical", "hr", "dsa", "system_design",
              "puzzles"),
}
PEER_ROLE_MAP = {
    "sde": ("sde",),
    "frontend": ("frontend",),
    "backend": ("backend",),
    "data": ("data_analyst", "data_scientist"),
    "devops": ("devops",),
    "pm": ("pm",),
    "general": (),
}
PEER_DIFFICULTIES = ("Easy", "Medium", "Hard")

PEER_STATUSES = ("open", "waiting", "in_progress", "done", "cancelled")
N_PEER_QUESTIONS = 6
PEER_ANSWER_MAX_CHARS = 10000
PEER_COMMENT_MAX_CHARS = 5000
PEER_RUBRIC_DIMS = ("clarity", "structure", "depth", "relevance",
                    "confidence")


def _peer_bank():
    """The shared interview bank (mock_interview.py; 1,050 questions)."""
    from studybuddy.mock_interview import _load_bank
    return _load_bank()


def _peer_companies():
    """[(company, question_count)] for the bank, most questions first."""
    from collections import Counter
    counts = Counter()
    try:
        for q in _peer_bank() or []:
            for c in (q.get("companies") or []):
                counts[c] += 1
    except Exception:
        log.exception("Peer company list failed")
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _peer_questions(role, category, n=N_PEER_QUESTIONS, exclude=None,
                    company=None, difficulty=None):
    """Draw n bank questions for a peer room.

    Filters by role + mapped category, optionally narrowed to one
    company and/or difficulty. Filters relax stepwise (difficulty ->
    company -> role) until the pool can fill the round, so a room
    always gets questions. Never raises: returns [] only when the
    bank itself is unreadable.
    """
    try:
        bank = _peer_bank() or []
    except Exception:
        log.exception("Peer question draw failed: bank unreadable")
        return []
    cats = PEER_CATEGORY_MAP.get(category) or PEER_CATEGORY_MAP["behavioral"]
    roles = PEER_ROLE_MAP.get(role, ())
    excluded = set(exclude or [])

    def _pool(use_role, use_company, use_difficulty):
        out = []
        for q in bank:
            if q.get("category") not in cats or q.get("id") in excluded:
                continue
            if use_role and roles and not any(
                    r in (q.get("roles") or []) for r in roles):
                continue
            if use_company and company and company not in (
                    q.get("companies") or []):
                continue
            if use_difficulty and difficulty and \
                    q.get("difficulty") != difficulty:
                continue
            out.append(q)
        return out

    pool = []
    for flags in ((True, True, True), (True, True, False),
                  (True, False, False), (False, False, False)):
        pool = _pool(*flags)
        if len(pool) >= max(1, n):
            break
    rng = random.Random()
    rng.shuffle(pool)
    return pool[:max(0, n)]


def _peer_feedback_received(user_id):
    """Aggregate the rubric feedback a user has received across rooms.

    Returns {count, averages {dim: float}, weakest (dim or None),
    sessions_done} — the hub's 'how peers rate you' summary.
    """
    summary = {"count": 0, "averages": {}, "weakest": None,
               "sessions_done": 0}
    try:
        rows = PeerFeedback.query.filter_by(reviewee_id=user_id).all()
        summary["count"] = len(rows)
        if rows:
            for dim in PEER_RUBRIC_DIMS:
                vals = [getattr(r, dim) for r in rows
                        if getattr(r, dim) is not None]
                if vals:
                    summary["averages"][dim] = round(
                        sum(vals) / len(vals), 1)
            if summary["averages"]:
                summary["weakest"] = min(
                    summary["averages"],
                    key=lambda d: summary["averages"][d])
        summary["sessions_done"] = PeerRoom.query.filter(
            PeerRoom.status == "done",
            (PeerRoom.host_id == user_id)
            | (PeerRoom.guest_id == user_id)).count()
    except Exception:
        log.exception("Peer feedback summary failed")
    return summary


def _peer_token():
    """Mint a unique 12-char room token (never raises)."""
    for _ in range(25):
        token = secrets.token_hex(6)
        try:
            if not PeerRoom.query.filter_by(token=token).first():
                return token
        except Exception:
            log.exception("Peer token uniqueness check failed")
            break
    return secrets.token_hex(6) + uuid.uuid4().hex[:4]


def _peer_room_or_none(token):
    """Fetch a room by token; None for missing/blank tokens (never raises)."""
    token = (token or "").strip()
    if not token:
        return None
    try:
        return PeerRoom.query.filter_by(token=token).first()
    except Exception:
        log.exception("Peer room lookup failed")
        return None


def _peer_is_participant(room, user_id):
    return (room is not None and user_id is not None
            and (user_id == room.host_id or user_id == room.guest_id))


def _peer_partner_id(room, user_id):
    """The other participant's id, or None when there is no partner yet."""
    if room is None or user_id is None:
        return None
    if user_id == room.host_id:
        return room.guest_id
    if user_id == room.guest_id:
        return room.host_id
    return None


def _peer_blocked(a_id, b_id):
    """True when either user has blocked the other (never raises)."""
    if not a_id or not b_id or a_id == b_id:
        return False
    try:
        return (db.session.query(PeerBlock.id).filter(
            ((PeerBlock.blocker_id == a_id) & (PeerBlock.blocked_id == b_id))
            | ((PeerBlock.blocker_id == b_id) & (PeerBlock.blocked_id == a_id))
        ).first() is not None)
    except Exception:
        log.exception("Peer block check failed")
        return True  # fail closed: treat as blocked on DB errors


def _peer_blocked_ids(user_id):
    """Ids of users blocked by / blocking this user (never raises)."""
    ids = set()
    if not user_id:
        return ids
    try:
        rows = PeerBlock.query.filter(
            (PeerBlock.blocker_id == user_id)
            | (PeerBlock.blocked_id == user_id)).all()
        for r in rows:
            ids.add(r.blocker_id)
            ids.add(r.blocked_id)
    except Exception:
        log.exception("Peer blocked-ids lookup failed")
    ids.discard(user_id)
    return ids


def _peer_parse_slot(value):
    """Parse a slot string ('YYYY-MM-DD HH:MM' or ISO '...T...'); None if bad."""
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _peer_question_by_id(question_id):
    """Bank entry for a question id, or None (never raises)."""
    try:
        for q in (_peer_bank() or []):
            if q.get("id") == question_id:
                return q
    except Exception:
        log.exception("Peer bank lookup failed")
    return None


def get_db_url():
    db_url = os.getenv("DATABASE_URL", "sqlite:///acra.db")
    # Heroku/Render style postgres:// -> SQLAlchemy postgresql://
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return db_url


def is_safe_next(target):
    """Only allow relative redirects after login."""
    return target and target.startswith("/") and not target.startswith("//")


# ========================================================
# --- GOOGLE OAUTH (Authlib) ---
# ========================================================

def _google_configured():
    """True when Google OAuth credentials are present in the environment."""
    return bool((os.getenv("GOOGLE_CLIENT_ID") or "").strip()
                and (os.getenv("GOOGLE_CLIENT_SECRET") or "").strip())


def _register_google_oauth(app):
    """Register the Google OpenID client (identity scopes only)."""
    oauth.register(
        "google",
        client_id=os.getenv("GOOGLE_CLIENT_ID").strip(),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET").strip(),
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        # Identity only: we never read Drive, Gmail, Calendar, etc.
        client_kwargs={"scope": "openid email profile"},
    )


def _process_google_login(userinfo):
    """Find-or-create the user for a verified Google identity.

    Never creates a duplicate account for the same email: an existing
    password user with the same email gets their Google identity linked.
    Returns (user, is_new). Raises ValueError on unverifiable identity.
    """
    sub = (userinfo.get("sub") or "").strip()
    email = (userinfo.get("email") or "").strip().lower()
    if not sub or not email or "@" not in email:
        raise ValueError("Google did not return a verifiable identity. Please try again.")
    if userinfo.get("email_verified") is False:
        raise ValueError("Your Google email is not verified. Please verify it and try again.")

    user = User.query.filter_by(google_sub=sub).first()
    if user:
        return user, False
    user = User.query.filter_by(email=email).first()
    if user:
        user.google_sub = sub
        if not user.avatar_url:
            user.avatar_url = (userinfo.get("picture") or "")[:512] or None
        db.session.commit()
        log.info("Linked Google identity to existing account %s", email)
        return user, False
    # Brand-new account. The password hash is a random secret that can never
    # validate, so this account is Google-sign-in-only (password_hash stays
    # NOT NULL, no schema change needed).
    user = User(
        name=(userinfo.get("name") or email.split("@")[0])[:120],
        email=email,
        google_sub=sub,
        avatar_url=(userinfo.get("picture") or "")[:512] or None,
    )
    user.set_password(secrets.token_hex(32))
    db.session.add(user)
    db.session.commit()
    log.info("Created account via Google sign-in: %s", email)
    return user, True


# ========================================================
# --- PROFILE SNAPSHOT CACHE ---
# ========================================================

def _fresh_snapshot(user_id, provider, max_age_seconds=_SNAPSHOT_TTL_SECONDS):
    """Return the user's cached snapshot for a provider, or None if stale."""
    row = ProfileSnapshot.query.filter_by(user_id=user_id, provider=provider).first()
    if not row or not row.fetched_at:
        return None
    age = (datetime.now(UTC).replace(tzinfo=None)
           - row.fetched_at.replace(tzinfo=None)).total_seconds()
    return row if age < max_age_seconds else None


def _store_snapshot(user_id, provider, username, data):
    """Upsert the cached snapshot for a (user, provider)."""
    row = ProfileSnapshot.query.filter_by(user_id=user_id, provider=provider).first()
    payload = json_safe(data)
    if row:
        row.username = username
        row.data = payload
        row.fetched_at = datetime.now(UTC)
    else:
        row = ProfileSnapshot(user_id=user_id, provider=provider,
                              username=username, data=payload)
        db.session.add(row)
    db.session.commit()
    return row


# ========================================================
# --- APP FACTORY ---
# ========================================================

def _create_all_race_safe():
    """``db.create_all()`` for SQLite dev/test, tolerant of worker races.

    Two gunicorn workers booting against a fresh SQLite file race inside
    create_all: both pass the checkfirst probe, both issue CREATE TABLE,
    and the loser dies with ``table ... already exists`` — which kills
    the whole master (``Worker failed to boot``). Seen in the Lighthouse
    CI boot (2026-10-22): ``--workers 2`` + fresh sqlite file. Postgres
    is immune (advisory lock in ``_ensure_prod_schema``). Retry briefly:
    once the winning worker finishes, create_all is a no-op. Only the
    ``already exists`` race is tolerated; anything else still raises.
    """
    import time

    from sqlalchemy.exc import OperationalError

    last = None
    for _ in range(20):
        try:
            db.create_all()
            return
        except OperationalError as e:
            if "already exists" not in str(e.orig):
                raise
            last = e
            db.session.remove()
            time.sleep(0.5)
    raise last


def _ensure_prod_schema(app):
    """ARC-03 self-heal for production Postgres (idempotent, additive-only).

    Production schema is Alembic-managed, but the free-tier deploy has no
    release step, so ``flask db upgrade`` never ran there: the database
    (created in the create_all era) lacks newer tables/columns and every
    User-table query 500s (register/login were down after the domain move
    surfaced it). At boot we therefore run an ADDITIVE-ONLY repair:

    1. create any missing tables (``db.create_all`` is checkfirst),
    2. add any missing columns on existing tables, giving NOT NULL columns
       a safe literal default so existing rows don't break,
    3. stamp Alembic to ``head`` so migration state matches the models.

    Nothing is ever dropped or altered. A Postgres advisory lock keeps the
    gunicorn workers from racing; any failure is logged and boot continues
    (a booted app beats a crash loop).
    """
    import sqlalchemy as sa
    from sqlalchemy import text

    log = logging.getLogger("acra")
    lock_conn = None
    try:
        # Serialize workers: one repairs, the others WAIT for it (up to
        # ~15s) instead of serving traffic against a half-repaired schema
        # — the first post-deploy register used to 500 that way. If the
        # lock never frees, proceed anyway: every repair step below is
        # idempotent (create_all checkfirst / ADD COLUMN IF NOT EXISTS).
        import time as _time
        lock_conn = db.engine.connect()
        got = False
        for _attempt in range(15):
            got = lock_conn.execute(
                text("SELECT pg_try_advisory_lock(727273)")).scalar()
            if got:
                break
            _time.sleep(1)
        if not got:
            log.info("schema-ensure: lock busy 15s; running idempotent "
                     "repair without the lock")

        insp = sa.inspect(db.engine)
        existing_tables = set(insp.get_table_names())
        db.create_all()
        insp = sa.inspect(db.engine)
        added = []
        for table in db.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # created by create_all just now, fully formed
            try:
                have = {c["name"] for c in insp.get_columns(table.name)}
            except Exception:
                continue
            for col in table.columns:
                if col.name in have:
                    continue
                ddl_type = col.type.compile(dialect=db.engine.dialect)
                stmt = (f'ALTER TABLE "{table.name}" '
                        f'ADD COLUMN IF NOT EXISTS "{col.name}" {ddl_type}')
                if not col.nullable and col.server_default is None:
                    # Existing rows can't use Python-side defaults; give
                    # the column a literal SQL default so they backfill.
                    if isinstance(col.type, sa.Boolean):
                        stmt += " DEFAULT false"
                    elif isinstance(col.type, sa.Integer):
                        stmt += " DEFAULT 0"
                    elif isinstance(col.type, sa.Float):
                        stmt += " DEFAULT 0"
                    elif isinstance(col.type, (sa.String, sa.Text)):
                        stmt += " DEFAULT ''"
                    elif isinstance(col.type, sa.DateTime):
                        stmt += " DEFAULT now()"
                    elif isinstance(col.type, sa.Date):
                        stmt += " DEFAULT CURRENT_DATE"
                    elif isinstance(col.type, sa.JSON):
                        stmt += " DEFAULT '{}'"
                db.session.execute(text(stmt))
                added.append(f"{table.name}.{col.name}")
        db.session.commit()
        if added:
            log.warning("schema-ensure: added missing columns: %s",
                        ", ".join(added))
        try:
            from flask_migrate import stamp
            # Alembic's stamp() applies the ini's logging config
            # (fileConfig, disable_existing_loggers=True), which DISABLES
            # every logger created so far and replaces the root handlers —
            # in production that would silence the app's own loggers for
            # the life of the process. Snapshot and restore around it.
            _root = logging.getLogger()
            _saved_disabled = {
                name: lg.disabled
                for name, lg in logging.Logger.manager.loggerDict.items()
                if isinstance(lg, logging.Logger)}
            _saved_root_handlers = list(_root.handlers)
            _saved_root_level = _root.level
            try:
                stamp(revision="head")
            finally:
                for name, lg in logging.Logger.manager.loggerDict.items():
                    if isinstance(lg, logging.Logger):
                        lg.disabled = _saved_disabled.get(name, False)
                _root.handlers = _saved_root_handlers
                _root.setLevel(_saved_root_level)
        except Exception:
            log.warning("schema-ensure: alembic stamp skipped", exc_info=True)
        log.info("schema-ensure: production schema verified")
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        log.exception("schema-ensure failed; continuing boot")
    finally:
        if lock_conn is not None:
            try:
                lock_conn.execute(text("SELECT pg_advisory_unlock(727273)"))
                lock_conn.close()
            except Exception:
                pass


def create_app(config_overrides=None):
    app = Flask(__name__)

    # ARC-01/ARC-12: env-validated config (Development/Testing/Production).
    # from_object only sets defaults; the explicit assignments below and
    # config_overrides keep final say. Production fails fast on missing secrets.
    from app.config import resolve_config, validate_for_env
    _config_cls = resolve_config(config_overrides)
    app.config.from_object(_config_cls)
    validate_for_env(_config_cls)

    # --- SECRET_KEY policy (P1#7) ---
    # Refuse to boot in production without a real secret; in local dev use
    # an ephemeral key with a loud warning (sessions won't survive restarts).
    secret = os.getenv("SECRET_KEY")
    _insecure = {None, "", "change-me-to-a-long-random-secret",
                 "unified-ai-platform-secret-2026"}
    _is_prod_env = bool(os.getenv("FLASK_ENV") == "production"
                       or os.getenv("RENDER") or os.getenv("DYNO"))
    if secret in _insecure:
        if _is_prod_env:
            raise RuntimeError(
                "SECRET_KEY is not set (or is still the placeholder). "
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\" "
                "and set it as an environment variable.")
        secret = secrets.token_hex(32)
        # ARC-08: was print(); now a proper log record.
        log.warning("SECRET_KEY is not set - using an ephemeral key for this "
                    "local dev run. Set SECRET_KEY in .env for real use.")
    app.secret_key = secret

    # --- Session cookie hardening (scorecard S1) ---
    # Secure cookies only in production: local dev runs over plain HTTP and
    # would silently lose sessions with the Secure flag set.
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = _is_prod_env

    app.config["SQLALCHEMY_DATABASE_URI"] = get_db_url()
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["UPLOAD_FOLDER"] = os.path.join(os.path.dirname(__file__), "uploads")
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB max

    if config_overrides:
        app.config.update(config_overrides)

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    db.init_app(app)
    # ARC-03: Alembic migrations. render_as_batch only for SQLite (batch mode
    # is a SQLite-only workaround; on Postgres it can break migrations).
    if migrate is not None:
        _db_url = app.config.get("SQLALCHEMY_DATABASE_URI") or ""
        _is_sqlite = _db_url.startswith("sqlite")
        migrate.init_app(app, db, render_as_batch=_is_sqlite)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"  # ARC-01: auth blueprint endpoint
    login_manager.login_message_category = "info"
    # SEC-06 (revised 2026-10-23): session protection stays on, but in
    # "basic" mode. "strong" binds the session to the client IP + UA and
    # destroys the login on any mismatch — behind ProxyFix that IP is the
    # user's real egress address, which rotates constantly on CGNAT /
    # mobile networks (verified live: consecutive requests from one
    # machine egressed via different IPs). Real users were silently
    # logged out mid-task: progress marks failed with 401 and dashboards
    # fell back to the anonymous view. "basic" still detects identifier
    # changes (marks the session not-fresh) without killing the login.
    # 12h absolute lifetime, no sliding refresh.
    login_manager.session_protection = "basic"
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
    app.config["SESSION_REFRESH_EACH_REQUEST"] = False
    csrf.init_app(app)
    limiter.init_app(app)
    oauth.init_app(app)
    from studybuddy.i18n import init_babel  # UX-05 i18n (lazy; no-op if Flask-Babel missing)
    init_babel(app)
    if _google_configured():
        _register_google_oauth(app)
    if app.config.get("TESTING"):
        limiter.enabled = False

    register_routes(app)
    register_error_handlers(app)
    register_security_headers(app)

    with app.app_context():
        # ARC-03: DDL at worker boot is gone in production — schema is managed
        # by Alembic migrations (flask db upgrade as a release step). We keep
        # create_all() for SQLite dev/test and the TESTING fixture so the
        # 8k+ test suite keeps working without a migration run per test.
        # SKIP_DB_INIT=1 forces the skip (used when generating migrations).
        _db_url = app.config.get("SQLALCHEMY_DATABASE_URI") or ""
        _is_prod_db = _db_url.startswith(("postgresql", "postgres"))
        if os.environ.get("SKIP_DB_INIT") == "1":
            pass  # no DDL at all (used when generating migrations)
        elif not _is_prod_db:
            _create_all_race_safe()
        else:
            # ARC-03: no release step runs `flask db upgrade` on the free
            # tier, so repair/verify the schema additively at boot.
            _ensure_prod_schema(app)

    return app


def register_security_headers(app):
    """Security headers + ProxyFix (scorecard: headers/cookies/proxy gaps).

    Render terminates TLS at its edge proxy, so we trust exactly one hop:
    request.is_secure and remote_addr then reflect the real client, which
    also makes Flask-Limiter's per-IP limits meaningful again.
    """
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    # ARC-08: request IDs + structured access logs (no payloads).
    from studybuddy.observability import (
        configure_logging,
        register_request_tracking,
    )
    configure_logging(app)
    register_request_tracking(app)

    @app.before_request
    def _mint_csp_nonce():
        # Minted before the view renders so templates can embed the same
        # nonce the CSP header will carry (see after_request below).
        import secrets

        from flask import g
        g.csp_nonce = secrets.token_urlsafe(16)

    @app.after_request
    def _set_security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy",
                                "strict-origin-when-cross-origin")
        resp.headers.setdefault("Permissions-Policy",
                                "camera=(), microphone=(), geolocation=()")
        # SEC-05: CSP in report-only mode. Violations go to /csp-report
        # (rate-limited, logged without payloads). 2026-10-25 rollout:
        # nonces on all 60 inline script blocks + all 20 inline event
        # handlers converted to addEventListener (incl. JS-generated
        # buttons via delegation); unpkg dropped from the policy.
        # Use the per-request nonce minted in _mint_csp_nonce
        # (before_request): templates render BEFORE after_request runs,
        # so a nonce created here could never match the rendered pages —
        # the pipeline silently produced nonce="" until 2026-10-22.
        import secrets
        from flask import g
        nonce = getattr(g, "csp_nonce", None) or secrets.token_urlsafe(16)
        g.csp_nonce = nonce
        csp = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}' https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "report-uri /csp-report"
        )
        # Enforced by default since the 2026-10-25 nonce rollout (all
        # inline blocks nonced, all handlers converted, enforce-mode
        # browser sweep clean). Set CSP_REPORT_ONLY=1 in env to
        # temporarily revert to report-only.
        import os as _os
        header = "Content-Security-Policy-Report-Only" if _os.environ.get("CSP_REPORT_ONLY") == "1" \
            else "Content-Security-Policy"
        resp.headers.setdefault(header, csp)
        if request.is_secure:
            resp.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains")
        return resp

    # SEC-05: CSP violation reporting endpoint (rate-limited, no payloads
    # logged). CSRF-exempt: browsers send violation reports without any
    # token, so CSRFProtect 400'd every report and the pipeline never
    # logged a thing (found via Lighthouse best-practices, 2026-10-22).
    @app.route('/csp-report', methods=['POST'])
    @limiter.limit("60 per hour")
    @csrf.exempt
    def csp_report():
        try:
            # Log only the violated directive + blocked URI, never full payload
            data = request.get_json(silent=True) or {}
            report = data.get("csp-report", {})
            log.warning("CSP violation: %s blocked %s",
                        report.get("violated-directive"),
                        report.get("blocked-uri"))
        except Exception:
            pass
        return "", 204

    # Make csp_nonce() available in all templates
    @app.context_processor
    def _csp_nonce_processor():
        from flask import g
        return {"csp_nonce": lambda: getattr(g, "csp_nonce", "")}


def _load_benchmark_scores(path=None):
    """#8: parsed benchmarks/results.json, or None when absent/unreadable.

    Surfaced verbatim on /version so deploys can be checked against the
    gold-set benchmark without shell access. Never raises — a missing
    or malformed file simply omits the key from the response.
    """
    import json as _json
    p = path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "benchmarks", "results.json")
    try:
        with open(p, encoding="utf-8") as f:
            data = _json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _error_payload(code, message):
    """JSON for API routes, styled page otherwise (P2#10)."""
    if request.path.startswith("/api/"):
        return jsonify({"error": message}), code
    return render_template("error.html", code=code, message=message), code


def _ai_degraded_response(feature_name="This feature"):
    """LLM-09: Graceful degradation when AI is unavailable.

    Returns a user-friendly payload explaining that rule-based results
    are shown instead, with no technical jargon.
    """
    msg = (
        f"{feature_name} normally uses AI, which isn't available right now. "
        "Showing rule-based results instead — they're less personalized "
        "but still useful. AI will return automatically when the service recovers.")
    if request.path.startswith("/api/"):
        return jsonify({"warning": msg, "ai_degraded": True}), 200
    flash(msg, "warning")
    return None


def register_error_handlers(app):
    # ARC-09: central handler for StudyBuddyError subclasses.
    app.register_error_handler(StudyBuddyError, handle_studybuddy_error)

    @app.errorhandler(400)
    def bad_request(e):
        return _error_payload(400, getattr(e, "description", None)
                              or "That request wasn't quite right. Please try again.")

    @app.errorhandler(CSRFError)
    def csrf_error(e):
        log.warning("CSRF validation failed: %s", e.description)
        return _error_payload(400, "Security check failed (expired form). "
                                   "Please reload the page and try again.")

    @app.errorhandler(404)
    def not_found(e):
        return _error_payload(404, "The page you asked for doesn't exist.")

    @app.errorhandler(405)
    def method_not_allowed(e):
        return _error_payload(405, getattr(e, "description", None)
                              or "That action isn't allowed with this method.")

    @app.errorhandler(429)
    def rate_limited(e):
        # SEC-04: JSON 429 with Retry-After so clients can back off.
        # SEC-14: log the hit (hashed IP/user, endpoint only — no PII).
        from studybuddy.security_events import log_security_event
        try:
            from flask import request as _rq
            # url_rule is the route pattern (no user data); fall back to
            # the raw path only if routing failed.
            _rule = getattr(_rq, "url_rule", None)
            _endpoint = str(_rule)[:120] if _rule else _rq.path[:120]
        except Exception:
            _endpoint = "-"
        log_security_event("rate_limit_hit", details={"endpoint": _endpoint})
        retry_after = 60
        try:
            if str(e.description).isdigit():
                retry_after = int(e.description)
        except Exception:
            pass
        from flask import make_response as _mr
        # _error_payload returns (body, code); rebuild with the header
        body, _code = _error_payload(429, "You're moving a bit fast. "
                                          "Please wait a moment and try again.")
        r = _mr(body, 429)
        r.headers["Retry-After"] = str(retry_after)
        return r

    @app.errorhandler(500)
    def server_error(e):
        log.exception("Unhandled 500 error")
        return _error_payload(500, "Something went wrong on our side. "
                                   "Please try again in a moment.")


# ========================================================
# --- ROUTES ---
# ========================================================

def _jd_match_suggestions(report, jd_text):
    """Qualitative fix suggestions via Gemini, with a rule-based fallback.

    Returns (suggestions, ai_powered). The numeric score never depends on
    the LLM, so a Gemini outage degrades suggestions only - never the
    report itself.
    """
    fallback = generate_rule_based_suggestions(report)
    prompt = (
        "You are a senior career coach. A resume was scored against the "
        "job description below. Resume experience: ~{} years. "
        "Match score: {}/100. Missing skills/keywords (most important "
        "first): {}. Matched skills: {}.\n\n"
        "JOB DESCRIPTION (truncated):\n{}\n\n"
        'Return ONLY a JSON object like {{"suggestions": [{{"section": "...", '
        '"issue": "...", "fix": "..."}}]}} with at most 6 concrete, '
        "section-by-section fixes (Summary, Experience, Skills, Education, "
        "Formatting). Each fix must reference the actual JD above - no "
        "generic advice."
    ).format(
        report.get("resume_experience_years") or 0,
        report.get("score"),
        ", ".join((report.get("missing_keywords") or [])[:12]) or "none",
        ", ".join((report.get("matched_skills") or [])[:8]) or "none",
        (jd_text or "")[:4000],
    )
    # Robust JSON parsing: strip fences, retry once (constructor pattern).
    for _attempt in range(2):
        try:
            raw = call_gemini_ai(prompt)
        except GeminiError as e:
            log.warning("JD-match suggestions: Gemini unavailable: %s", e)
            return fallback, False
        try:
            parsed = _parse_json_safely(raw)
            items = parsed.get("suggestions") if isinstance(parsed, dict) else None
            cleaned = []
            for it in (items or [])[:6]:
                if not isinstance(it, dict):
                    continue
                section = str(it.get("section") or "Overall")[:60]
                issue = str(it.get("issue") or "").strip()[:300]
                fix = str(it.get("fix") or "").strip()[:600]
                if issue and fix:
                    cleaned.append({"section": section, "issue": issue, "fix": fix})
            if cleaned:
                return cleaned, True
            log.warning("JD-match suggestions: model returned no usable items")
        except (ValueError, json.JSONDecodeError):
            log.warning("JD-match suggestions: non-JSON model output, retrying")
    return fallback, False


def _linkedin_suggestions(report, fields):
    """Polished LinkedIn fixes via Gemini, with a rule-based fallback.

    Returns (suggestions, ai_powered). The numeric score never depends on
    the LLM, so a Gemini outage degrades suggestions only - never the
    report itself.
    """
    fallback = [{"section": s["section"], "fix": s["fix"]}
                for s in (report.get("suggestions") or [])]
    weakest = sorted((report.get("breakdown") or {}).items(),
                     key=lambda kv: kv[1])[:3]
    prompt = (
        "You are a LinkedIn profile coach. A profile scored {}/100 with the "
        "weakest areas: {}. Profile sections (truncated):\n"
        "HEADLINE: {}\nABOUT: {}\nEXPERIENCE: {}\nSKILLS: {}\n\n"
        'Return ONLY a JSON object like {{"suggestions": [{{"section": "...", '
        '"fix": "..."}}]}} with at most 8 concrete, prioritized fixes. Each '
        "fix must reference the actual profile text above - no generic advice."
    ).format(
        report.get("score"),
        ", ".join(f"{k} ({v})" for k, v in weakest) or "none",
        (fields.get("headline") or "")[:600],
        (fields.get("about") or "")[:1500],
        (fields.get("experience") or "")[:2000],
        (fields.get("skills") or "")[:800],
    )
    for _attempt in range(2):
        try:
            raw = call_gemini_ai(prompt)
        except GeminiError as e:
            log.warning("LinkedIn suggestions: Gemini unavailable: %s", e)
            return fallback, False
        try:
            parsed = _parse_json_safely(raw)
            items = parsed.get("suggestions") if isinstance(parsed, dict) else None
            cleaned = []
            for it in (items or [])[:8]:
                if not isinstance(it, dict):
                    continue
                section = str(it.get("section") or "Profile")[:60]
                fix = str(it.get("fix") or "").strip()[:600]
                if fix:
                    cleaned.append({"section": section, "fix": fix})
            if cleaned:
                return cleaned, True
            log.warning("LinkedIn suggestions: model returned no usable items")
        except (ValueError, json.JSONDecodeError):
            log.warning("LinkedIn suggestions: non-JSON model output, retrying")
    return fallback, False


def _interview_feedback(scored_answers, questions):
    """Polish per-answer strengths/improvements via Gemini, rule-based fallback.

    Returns (feedback_list, ai_powered). Numeric scores never depend on the
    LLM; a Gemini outage degrades the wording only.
    """
    fallback = [generate_rule_based_feedback(s, q)
                for s, q in zip(scored_answers, questions, strict=False)]
    compact = []
    for s, q in zip(scored_answers, questions, strict=False):
        compact.append({
            "q": (q.get("question") or "")[:300],
            "key_points": (q.get("key_points") or [])[:4],
            "scores": s["scores"],
            "answer": (s.get("answer_text") or "")[:1200],
        })
    prompt = (
        "You are an expert interview coach. Below are a candidate's answers "
        "with rubric scores (correctness/40, communication/25, depth/20, "
        "edge_cases/15). For EACH answer return one strengths item and up to "
        "3 improvements, referencing the actual answer text - no generic advice.\n"
        'Return ONLY JSON like {"feedback": [{"strengths": ["..."], '
        '"improvements": ["..."]}]} with one entry per answer, in order.\n'
        "ANSWERS:\n" + json.dumps(compact)[:12000]
    )
    for _attempt in range(2):
        try:
            raw = call_gemini_ai(prompt)
        except GeminiError as e:
            log.warning("Interview feedback: Gemini unavailable: %s", e)
            return fallback, False
        try:
            parsed = _parse_json_safely(raw)
            items = parsed.get("feedback") if isinstance(parsed, dict) else None
            if isinstance(items, list) and len(items) == len(compact):
                cleaned = []
                for it in items:
                    if not isinstance(it, dict):
                        cleaned.append({"strengths": [], "improvements": []})
                        continue
                    cleaned.append({
                        "strengths": [str(x)[:400] for x in (it.get("strengths") or [])[:3]],
                        "improvements": [str(x)[:400] for x in (it.get("improvements") or [])[:4]],
                    })
                return cleaned, True
            log.warning("Interview feedback: model returned unusable shape")
        except (ValueError, json.JSONDecodeError):
            log.warning("Interview feedback: non-JSON model output, retrying")
    return fallback, False


def _coach_polish(report, text):
    """Polish the cleaned rewrite + drills via Gemini, rule-based fallback.

    Returns (cleaned_version, drills, ai_powered). Scores never depend on
    the LLM.
    """
    fallback_cleaned = report.get("cleaned_version") or ""
    fallback_drills = report.get("drills") or []
    prompt = (
        "You are a communication coach for job interviews. A candidate wrote "
        "this answer (clarity score {}/100; top issues: {}):\n{}\n\n"
        "1) Rewrite it cleanly: same meaning, confident active voice, no filler, "
        "tight sentences. 2) Give 5 rewrite drills as short 'before' sentences "
        "with the issue named.\n"
        'Return ONLY JSON like {{"cleaned": "...", "drills": '
        '[{{"sentence": "...", "issues": ["..."]}}]}}.'
    ).format(
        report.get("clarity_score"),
        ", ".join(h["word"] for h in (report.get("filler_hits") or [])[:6]) or "none",
        (text or "")[:4000],
    )
    for _attempt in range(2):
        try:
            raw = call_gemini_ai(prompt)
        except GeminiError as e:
            log.warning("Coach polish: Gemini unavailable: %s", e)
            return fallback_cleaned, fallback_drills, False
        try:
            parsed = _parse_json_safely(raw)
            if isinstance(parsed, dict) and parsed.get("cleaned"):
                after_by_sentence = {d.get("sentence"): d.get("after")
                                     for d in fallback_drills}
                drills = []
                for d in (parsed.get("drills") or [])[:5]:
                    if isinstance(d, dict) and d.get("sentence"):
                        sentence = str(d["sentence"])[:500]
                        drills.append({
                            "sentence": sentence,
                            "issues": [str(x)[:60] for x in (d.get("issues") or [])[:4]],
                            "after": after_by_sentence.get(sentence) or "",
                        })
                return (str(parsed["cleaned"])[:8000],
                        drills or fallback_drills, True)
            log.warning("Coach polish: model returned unusable shape")
        except (ValueError, json.JSONDecodeError):
            log.warning("Coach polish: non-JSON model output, retrying")
    return fallback_cleaned, fallback_drills, False


def _assessment_explanations(clusters):
    """Personalized 'why you fit' blurbs via Gemini, with rule-based fallback.

    Returns (clusters_with_why, ai_powered). Scoring never depends on the
    LLM - only the explanatory text does.
    """
    for c in clusters:
        c["why"] = rule_based_why(c)
    summaries = "; ".join(
        "{} ({}% match, signals: {})".format(
            c["name"], c["match"], ", ".join(c.get("top_signals") or [])[:2] or "general pattern")
        for c in clusters
    )
    prompt = (
        "You are a career counselor. A student took a career-discovery quiz. "
        "Top matches: {}. For EACH career, write a 2-sentence 'why you fit' "
        "blurb grounded in the signals shown, plus one concrete first step. "
        'Return ONLY JSON like {{"blurbs": [{{"id": "<career-id>", '
        '"why": "...", "first_step": "..."}}]}} using these ids: {}.'
    ).format(summaries, ", ".join(c["id"] for c in clusters))
    for _attempt in range(2):
        try:
            raw = call_gemini_ai(prompt)
        except GeminiError as e:
            log.warning("Assessment explanations: Gemini unavailable: %s", e)
            return clusters, False
        try:
            parsed = _parse_json_safely(raw)
            items = parsed.get("blurbs") if isinstance(parsed, dict) else None
            by_id = {c["id"]: c for c in clusters}
            applied = 0
            for it in (items or [])[:len(clusters)]:
                if not isinstance(it, dict):
                    continue
                c = by_id.get(str(it.get("id") or ""))
                if not c:
                    continue
                why = str(it.get("why") or "").strip()[:500]
                step = str(it.get("first_step") or "").strip()[:300]
                if why:
                    c["why"] = why + (" First step: " + step if step else "")
                    applied += 1
            if applied:
                return clusters, True
            log.warning("Assessment explanations: model returned no usable items")
        except (ValueError, json.JSONDecodeError):
            log.warning("Assessment explanations: non-JSON model output, retrying")
    return clusters, False


def _rewrite_bullet_ai(bullet):
    """AI-polished bullet rewrites via Gemini, with rule-based fallback.

    Returns (rewrites, ai_powered). The rule-based variants from
    bullet_rewriter.improve_bullet always work; Gemini adds an AI-polished
    alternative when available.
    """
    fallback = improve_bullet(bullet)
    prompt = (
        "You are a senior career coach rewriting resume bullets. Rewrite "
        "this resume bullet into 2 stronger versions: one emphasizing "
        "quantified impact, one emphasizing leadership/scope. Use strong "
        "action verbs. Keep each under 25 words. If the bullet has no "
        "numbers, insert a bracketed placeholder like [X%] for the metric.\n\n"
        f"BULLET: {bullet[:800]}\n\n"
        'Return ONLY a JSON object like {"rewrites": [{"label": "...", '
        '"text": "...", "tip": "..."}]} with exactly 2 items.'
    )
    for _attempt in range(2):
        try:
            raw = call_gemini_ai(prompt)
        except GeminiError as e:
            log.warning("Bullet rewrite: Gemini unavailable: %s", e)
            return fallback, False
        try:
            parsed = _parse_json_safely(raw)
            items = parsed.get("rewrites") if isinstance(parsed, dict) else None
            cleaned = []
            for it in (items or [])[:2]:
                if not isinstance(it, dict):
                    continue
                text = str(it.get("text") or "").strip()[:400]
                if text and text != bullet.strip():
                    cleaned.append({
                        "label": str(it.get("label") or "AI-polished")[:60],
                        "text": text,
                        "tip": str(it.get("tip") or "AI-polished alternative.")[:200],
                    })
            if cleaned:
                return fallback + cleaned, True
            log.warning("Bullet rewrite: model returned no usable items")
        except (ValueError, json.JSONDecodeError):
            log.warning("Bullet rewrite: non-JSON model output, retrying")
    return fallback, False


def _generate_cover_ai(resume_text, jd_text, tone):
    """Tailored cover letter + outreach via Gemini, with rule-based fallback.

    Returns ((cover_letter, outreach), ai_powered). Follows the constructor
    pattern: fenced-JSON-tolerant parsing, retry once.
    """
    fallback = (rule_based_cover_letter(resume_text, jd_text, tone),
                rule_based_outreach(resume_text, jd_text, tone))
    prompt = (
        "You are a senior career coach. Write a tailored cover letter and a "
        "short cold-outreach message (LinkedIn/email style, under 80 words) "
        f"for the candidate below. Tone: {tone}. The cover letter must "
        "reference specific requirements from the job description — no "
        "generic filler. Keep the letter under 220 words.\n\n"
        f"CANDIDATE RESUME (truncated):\n{(resume_text or '')[:4000]}\n\n"
        f"JOB DESCRIPTION (truncated):\n{(jd_text or '')[:4000]}\n\n"
        'Return ONLY a JSON object like {"cover_letter": "...", "outreach": "..."}.'
    )
    for _attempt in range(2):
        try:
            raw = call_gemini_ai(prompt)
        except GeminiError as e:
            log.warning("Cover generator: Gemini unavailable: %s", e)
            return fallback, False
        try:
            parsed = _parse_json_safely(raw)
            if not isinstance(parsed, dict):
                raise ValueError("non-dict output")
            letter = str(parsed.get("cover_letter") or "").strip()
            outreach = str(parsed.get("outreach") or "").strip()
            if letter and outreach and len(letter) > 100:
                return (letter[:6000], outreach[:1500]), True
            log.warning("Cover generator: model returned unusable items")
        except (ValueError, json.JSONDecodeError):
            log.warning("Cover generator: non-JSON model output, retrying")
    return fallback, False


def _rotate_session_on_login():
    """Session-fixation defense (SEC-06): drop any pre-login session data so
    the session cookie is re-issued fresh at the moment of login."""
    session.clear()


def register_routes(app):

    # ---------- ULTIMATE LAB (91 power tools) ----------
    from studybuddy.ultimate_routes import register_ultimate_routes
    register_ultimate_routes(app, limiter)

    # ---------- MAIN HUB ----------
    @app.route('/')
    def index():
        """Study-Buddy Landing Page (The Hub).

        Stats are computed from the live banks (all loaders are
        module-cached, so this costs nothing per request) — never
        hardcoded. Stale landing numbers were a 2026-10-24 eval finding.
        """
        return render_template(
            'index.html',
            num_interview=interview_hub_data()["total_questions"],
            num_aptitude=len(aptitude_all()),
            num_quiz=sum(c for _, c in quiz_skills()),
            num_dsa=len(a2z_problems()))

    @app.route('/try')
    @limiter.limit("20 per hour")
    def try_demo():
        """PROD-03: Zero-friction demo — no signup, no LLM, stores nothing."""
        return render_template('try_demo.html')

    @app.route('/privacy')
    def privacy():
        """Privacy Policy page."""
        return render_template('privacy.html')

    @app.route('/accessibility')
    def accessibility():
        """Accessibility statement (UX-03)."""
        return render_template('accessibility.html')

    @app.route('/terms')
    def terms():
        """Terms of Service page."""
        return render_template('terms.html')

    @app.route('/health')
    def health():
        return jsonify({'status': 'ok'})

    # OPS-05: liveness (no DB), readiness (DB + migrations + config),
    # version (build metadata; never secrets).
    @app.route('/healthz')
    def healthz():
        return jsonify({'status': 'ok'})

    @app.route('/readyz')
    def readyz():
        checks = {}
        ok = True
        # DB reachable? Timed (#17): latency is surfaced so uptime
        # dashboards can alert on a slow-but-alive database, not just
        # a dead one. Measured around the existing SELECT 1 probe, so
        # the statement budget for this route is unchanged.
        import time
        _t0 = time.perf_counter()
        try:
            db.session.execute(db.text("SELECT 1"))
            checks["db"] = "ok"
        except Exception as e:  # noqa: BLE001 - readiness must not raise
            checks["db"] = f"error: {type(e).__name__}"
            ok = False
        checks["db_latency_ms"] = round((time.perf_counter() - _t0) * 1000, 3)
        # Migrations at head?
        try:
            from alembic.config import Config as AlembicConfig
            from alembic.script import ScriptDirectory
            # migrations/ lives next to unified_app.py (repo root)
            mig_dir = os.path.join(os.path.dirname(__file__), "migrations")
            if os.path.isdir(mig_dir):
                script = ScriptDirectory.from_config(
                    AlembicConfig(os.path.join(mig_dir, "alembic.ini")))
                heads = script.get_heads()
                from alembic.runtime.migration import MigrationContext
                with db.engine.connect() as conn:
                    ctx = MigrationContext.configure(conn)
                    current = ctx.get_current_heads()
                checks["migrations"] = ("ok" if set(current) == set(heads)
                                        else f"behind: {current} != {heads}")
                if set(current) != set(heads):
                    ok = False
            else:
                checks["migrations"] = "no migrations dir"
        except Exception as e:  # noqa: BLE001
            checks["migrations"] = f"error: {type(e).__name__}"
            ok = False
        # Required config present?
        checks["secret_key"] = ("ok" if app.secret_key else "missing")
        if not app.secret_key:
            ok = False
        return jsonify({"ready": ok, "checks": checks}), 200 if ok else 503

    @app.route('/version')
    def version():
        import subprocess
        sha = "unknown"
        try:
            sha = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=5,
                cwd=os.path.dirname(__file__)).stdout.strip() or "unknown"
        except Exception:  # noqa: BLE001 - version must not raise
            pass
        # OSS-06: semantic version for releases
        app_version = "1.0.0"
        schema_rev = "unknown"
        try:
            from alembic.runtime.migration import MigrationContext
            with db.engine.connect() as conn:
                ctx = MigrationContext.configure(conn)
                heads = ctx.get_current_heads()
                schema_rev = ",".join(heads) if heads else "none"
        except Exception:  # noqa: BLE001
            pass
        payload = {
            "app": "study-buddy",
            "version": app_version,
            "git_sha": sha,
            "schema_revision": schema_rev,
            "python": __import__("sys").version.split()[0],
        }
        # #8: surface gold-set benchmark scores when a results file exists.
        scores = _load_benchmark_scores()
        if scores is not None:
            payload["benchmarks"] = scores
        return jsonify(payload)

    # ---------- AUTH (ARC-01) ----------
    # /register, /login, /logout now live in app/blueprints/auth_bp.py
    # (identical URLs; endpoints pinned via explicit endpoint= names).
    from app.blueprints.auth_bp import auth_bp
    app.register_blueprint(auth_bp)

    # ---------- SEC-09: Email verification ----------
    @app.route('/verify-email/<token>')
    def verify_email(token):
        user_id = verify_email_token(token, app.config['SECRET_KEY'])
        if not user_id:
            flash('This verification link is invalid or has expired.', 'error')
            return redirect(url_for('auth.login'))
        user = db.session.get(User, user_id)
        if not user:
            flash('Account not found.', 'error')
            return redirect(url_for('auth.login'))
        if not user.email_verified:
            user.email_verified = True
            user.email_verified_at = datetime.now(UTC)
            db.session.commit()
            flash('Email verified! You can now use all features.', 'success')
        else:
            flash('Email already verified.', 'info')
        return redirect(url_for('dashboard'))

    @app.route('/resend-verification', methods=['POST'])
    @login_required
    def resend_verification():
        # In production, send email via mailer. For now, flash the link
        # (dev mode). Production should integrate with email service.
        token = generate_email_token(current_user.id, app.config['SECRET_KEY'])
        link = url_for('verify_email', token=token, _external=True)
        log.info("verification_link_issued user_id_hash=%s",
                 _safe_user_ref_id(current_user.id))
        flash(f'Verification link: {link}', 'info')
        return redirect(url_for('dashboard'))

    # ---------- SEC-09: Password reset ----------
    @app.route('/forgot-password', methods=['GET', 'POST'])
    @limiter.limit("5 per hour")
    def forgot_password():
        # Generic response to avoid account enumeration (SEC-09)
        done_msg = ('If an account exists with that email, '
                    'a reset link has been sent.')
        if request.method == 'POST':
            email = (request.form.get('email') or '').strip().lower()
            user = User.query.filter_by(email=email).first()
            if user:
                raw = generate_reset_token()
                token_hash = generate_password_hash(raw)
                expires = datetime.now(UTC) + timedelta(hours=1)
                prt = PasswordResetToken(user_id=user.id,
                                         token_hash=token_hash,
                                         expires_at=expires)
                db.session.add(prt)
                db.session.commit()
                link = url_for('reset_password', token=raw, _external=True)
                log.info("password_reset_issued user_id_hash=%s",
                         _safe_user_ref_id(user.id))
                # TODO: send via email in production; flash for dev
                flash(f'Reset link (dev): {link}', 'info')
            flash(done_msg, 'info')
            return redirect(url_for('auth.login'))
        return render_template('forgot_password.html')

    @app.route('/reset-password/<token>', methods=['GET', 'POST'])
    @limiter.limit("10 per hour")
    def reset_password(token):
        # Find valid token (compare hashes)
        candidates = PasswordResetToken.query.filter(
            PasswordResetToken.expires_at > datetime.now(UTC),
            PasswordResetToken.used_at.is_(None)).all()
        prt = None
        for c in candidates:
            if check_password_hash(c.token_hash, token):
                prt = c
                break
        if not prt:
            flash('This reset link is invalid or has expired.', 'error')
            return redirect(url_for('forgot_password'))
        if request.method == 'POST':
            password = request.form.get('password') or ''
            pw_ok, pw_msg = check_password_policy(password)
            if not pw_ok:
                return render_template('reset_password.html', token=token,
                                       error=pw_msg)
            user = db.session.get(User, prt.user_id)
            user.set_password(password)
            prt.used_at = datetime.now(UTC)
            db.session.commit()
            log.info("password_reset_completed user_id_hash=%s",
                     _safe_user_ref_id(user.id))
            flash('Password updated. Please log in.', 'success')
            return redirect(url_for('auth.login'))
        return render_template('reset_password.html', token=token)

    # ---------- GOOGLE SIGN-IN (OAuth 2.0, only when configured) ----------
    if _google_configured():
        @app.route('/auth/google/login')
        def google_login():
            if current_user.is_authenticated:
                return redirect(url_for('dashboard'))
            nxt = request.args.get('next', '')
            if is_safe_next(nxt):
                session['google_next'] = nxt
            redirect_uri = url_for('google_callback', _external=True)
            return oauth.google.authorize_redirect(redirect_uri)

        @app.route('/auth/google/callback')
        def google_callback():
            # Authlib validates the `state` parameter by default, which
            # defeats CSRF/login-CSRF on the OAuth flow.
            try:
                oauth.google.authorize_access_token()
            except Exception as e:
                log.warning("Google OAuth token exchange failed: %r", e)
                flash('Google sign-in failed. Please try again.', 'error')
                return redirect(url_for('auth.login'))
            try:
                userinfo = oauth.google.userinfo()
            except Exception:
                try:
                    userinfo = oauth.google.parse_id_token()
                except Exception as e:
                    log.warning("Google userinfo fetch failed: %r", e)
                    flash('Could not read your Google profile. Please try again.', 'error')
                    return redirect(url_for('auth.login'))
            try:
                user, is_new = _process_google_login(userinfo or {})
            except ValueError as e:
                flash(str(e), 'error')
                return redirect(url_for('auth.login'))
            _rotate_session_on_login()
            login_user(user, remember=True)
            flash('Welcome{}!'.format(
                f', {user.name}' if is_new else f' back, {user.name}'),
                'success')
            nxt = session.pop('google_next', None)
            return redirect(nxt if is_safe_next(nxt) else url_for('dashboard'))

    # ---------- CONNECTED PROFILE (/profile) ----------
    @app.route('/profile')
    @login_required
    def profile():
        """Connected accounts: Google sign-in, GitHub, LeetCode."""
        github_snap = ProfileSnapshot.query.filter_by(
            user_id=current_user.id, provider='github').first()
        leetcode_snap = ProfileSnapshot.query.filter_by(
            user_id=current_user.id, provider='leetcode').first()
        return render_template(
            'profile.html',
            google_configured=_google_configured(),
            google_connected=bool(current_user.google_sub),
            github_username=current_user.github_username,
            github_snapshot=(github_snap.data if github_snap else None),
            leetcode_username=current_user.leetcode_username,
            leetcode_snapshot=(leetcode_snap.data if leetcode_snap else None))

    # PRIV-02: Export my data (JSON download)
    @app.route('/account/export')
    @login_required
    @limiter.limit("10 per hour")
    def account_export():
        """Download all personal data as JSON (DPDP data-principal right)."""
        from flask import Response
        import json as _json
        user = current_user
        data = {
            "user": {
                "email": user.email,
                "name": user.name,
                "github_username": user.github_username,
                "leetcode_username": user.leetcode_username,
                "created_at": str(user.created_at),
            },
            "exported_at": str(_utcnow_naive()),
        }
        # #15: the LLM consent record (PRIV-03) is personal data too —
        # when it was given and under which policy version. Key is always
        # present (null when the user never consented) so consumers can
        # rely on the export shape.
        try:
            consent = LLMConsent.query.filter_by(user_id=user.id).first()
            data["llm_consent"] = (
                {
                    "consented_at": str(consent.consented_at),
                    "policy_version": consent.policy_version,
                }
                if consent is not None else None)
        except Exception:
            data["llm_consent"] = None
        # Collect all user-owned rows across known tables
        for model, name in [
            (ResumeAnalysis, "resume_analyses"),
            (ResumeVersion, "resume_versions"),
            (JDMatchReport, "jd_matches"),
            (BulletRewrite, "bullet_rewrites"),
            (CoverDoc, "cover_docs"),
            (LinkedInReport, "linkedin_reports"),
            (JobApplication, "job_applications"),
            (InterviewSession, "interview_sessions"),
            (AptitudeAttempt, "aptitude_attempts"),
            (QuizAttempt, "quiz_attempts"),
            (STARDraft, "star_drafts"),
            (RoadmapProgress, "roadmap_progress"),
            (CareerAssessment, "career_assessments"),
            (Retrospective, "retrospectives"),
        ]:
            try:
                rows = model.query.filter_by(user_id=user.id).all()
                data[name] = [
                    {c.name: str(getattr(r, c.name))
                     for c in r.__table__.columns}
                    for r in rows
                ]
            except Exception:
                data[name] = []
        payload = _json.dumps(data, indent=2, default=str)
        return Response(
            payload,
            mimetype="application/json",
            headers={"Content-Disposition":
                     f"attachment; filename=studybuddy-export-{user.id}.json"})

    # PRIV-03: LLM consent — check + record
    def _llm_consent_given(user_id):
        """True if user has consented to LLM processing (current policy version)."""
        try:
            c = LLMConsent.query.filter_by(user_id=user_id).first()
            return c is not None and c.policy_version == CONSENT_POLICY_VERSION
        except Exception:
            return False  # table may not exist yet (no migration); treat as not consented

    @app.route('/consent/llm', methods=['GET', 'POST'])
    @login_required
    def llm_consent():
        """Show LLM data-use notice; record consent on POST."""
        if request.method == 'POST':
            if request.form.get('agree') == 'yes':
                try:
                    existing = LLMConsent.query.filter_by(
                        user_id=current_user.id).first()
                    if existing:
                        existing.consented_at = _utcnow_naive()
                        existing.policy_version = CONSENT_POLICY_VERSION
                    else:
                        db.session.add(LLMConsent(
                            user_id=current_user.id,
                            policy_version=CONSENT_POLICY_VERSION,
                            notice_text=CONSENT_NOTICE))
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                # Return to where they came from
                nxt = request.form.get('next') or url_for('index')
                return redirect(nxt)
            flash('You need to agree to use AI features.', 'warning')
        return render_template('llm_consent.html',
                               notice=CONSENT_NOTICE,
                               next=request.args.get('next', ''))

    @app.route('/consent/llm/withdraw', methods=['POST'])
    @login_required
    def llm_consent_withdraw():
        """Withdraw LLM consent (PRIV-03)."""
        try:
            LLMConsent.query.filter_by(user_id=current_user.id).delete()
            db.session.commit()
            flash('AI consent withdrawn. AI features are now disabled for your account.', 'info')
        except Exception:
            db.session.rollback()
        return redirect(url_for('profile'))

    # PRIV-05: BYOK — user manages their own API key
    @app.route('/profile/ai-key', methods=['GET', 'POST'])
    @login_required
    def ai_key_settings():
        """Let user save/remove their own LLM API key (BYOK mode)."""
        from studybuddy.ai_gateway import llm_mode
        if request.method == 'POST':
            action = request.form.get('action')
            if action == 'save':
                key = (request.form.get('api_key') or '').strip()
                provider = (request.form.get('provider') or 'openrouter').strip()
                if len(key) < 10:
                    flash('That key looks too short.', 'error')
                else:
                    try:
                        existing = UserAPIKey.query.filter_by(
                            user_id=current_user.id).first()
                        if existing:
                            existing.provider = provider
                            existing.set_key(key)
                        else:
                            rec = UserAPIKey(user_id=current_user.id,
                                             provider=provider)
                            rec.set_key(key)
                            db.session.add(rec)
                        db.session.commit()
                        flash('Your API key was saved (encrypted).', 'success')
                    except Exception:
                        db.session.rollback()
                        flash('Could not save the key.', 'error')
            elif action == 'remove':
                try:
                    UserAPIKey.query.filter_by(
                        user_id=current_user.id).delete()
                    db.session.commit()
                    flash('Your API key was removed.', 'info')
                except Exception:
                    db.session.rollback()
            return redirect(url_for('ai_key_settings'))
        has_key = UserAPIKey.query.filter_by(
            user_id=current_user.id).first() is not None
        return render_template('ai_key.html', has_key=has_key,
                               llm_mode=llm_mode())

    def _get_user_key(user_id):
        """PRIV-05: decrypt the user's BYOK key for a single request. Never logged."""
        try:
            rec = UserAPIKey.query.filter_by(user_id=user_id).first()
            return rec.get_key() if rec else None
        except Exception:
            return None

    # Make consent status available in templates
    @app.context_processor
    def _consent_processor():
        from flask_login import current_user as _cu
        try:
            authed = _cu.is_authenticated
        except Exception:
            authed = False
        return {"llm_consent_given": (
            _llm_consent_given(_cu.id) if authed else True)}

    # PRIV-02: Delete my account (hard delete with cascade)
    @app.route('/account/delete', methods=['GET', 'POST'])
    @login_required
    @limiter.limit("5 per hour")
    def account_delete():
        """Hard-delete user + all data. Requires password re-auth + confirmation."""
        from flask_login import logout_user
        if request.method == 'POST':
            password = request.form.get('password', '')
            confirm = request.form.get('confirm', '')
            if confirm != 'DELETE':
                flash('Type DELETE to confirm account deletion.', 'error')
                return render_template('account_delete.html')
            if not current_user.check_password(password):
                flash('Incorrect password.', 'error')
                return render_template('account_delete.html')
            uid = current_user.id
            # Delete all user-owned rows (cascade handles most, be explicit)
            for model in [ResumeAnalysis, ResumeVersion, JDMatchReport,
                          BulletRewrite, CoverDoc, LinkedInReport,
                          JobApplication, InterviewSession, AptitudeAttempt,
                          QuizAttempt, STARDraft, RoadmapProgress,
                          CareerAssessment, Retrospective, CertPlan,
                          StudyDoc, Experiment, CommunicationAnalysis,
                          DSAProgress]:
                try:
                    model.query.filter_by(user_id=uid).delete()
                except Exception:
                    pass
            # Finally delete the user
            user = db.session.get(User, uid)
            db.session.delete(user)
            db.session.commit()
            logout_user()
            flash('Your account and all data have been permanently deleted.', 'info')
            return redirect(url_for('index'))
        return render_template('account_delete.html')

    @app.route('/profile/connect/github', methods=['POST'])
    @login_required
    @limiter.limit("10 per hour")
    def profile_connect_github():
        username = (request.form.get('username') or '').strip()
        try:
            validate_github_username(username)
        except GithubAPIError as e:
            flash(str(e), 'error')
            return redirect(url_for('profile'))
        fresh = _fresh_snapshot(current_user.id, 'github')
        if fresh and (fresh.username or '').lower() == username.lower():
            current_user.github_username = fresh.username
            db.session.commit()
            flash(f'GitHub reconnected: @{fresh.username}', 'success')
            return redirect(url_for('profile'))
        try:
            result = analyze_github_profile(username)
        except GithubAPIError as e:
            flash(str(e), 'error')
            return redirect(url_for('profile'))
        current_user.github_username = result['username']
        _store_snapshot(current_user.id, 'github', result['username'], result)
        db.session.commit()
        flash('GitHub connected: @{} — profile score {}/100'.format(
            result['username'], result['score']), 'success')
        return redirect(url_for('profile'))

    @app.route('/profile/unlink/github', methods=['POST'])
    @login_required
    def profile_unlink_github():
        current_user.github_username = None
        ProfileSnapshot.query.filter_by(
            user_id=current_user.id, provider='github').delete()
        db.session.commit()
        flash('GitHub disconnected.', 'info')
        return redirect(url_for('profile'))

    @app.route('/profile/connect/leetcode', methods=['POST'])
    @login_required
    @limiter.limit("10 per hour")
    def profile_connect_leetcode():
        username = (request.form.get('username') or '').strip()
        try:
            validate_leetcode_username(username)
        except LeetCodeAPIError as e:
            flash(str(e), 'error')
            return redirect(url_for('profile'))
        fresh = _fresh_snapshot(current_user.id, 'leetcode')
        if fresh and (fresh.username or '').lower() == username.lower():
            current_user.leetcode_username = fresh.username
            db.session.commit()
            flash(f'LeetCode reconnected: {fresh.username}', 'success')
            return redirect(url_for('profile'))
        try:
            stats = fetch_leetcode_stats(username)
        except LeetCodeAPIError as e:
            flash(str(e), 'error')
            return redirect(url_for('profile'))
        current_user.leetcode_username = stats['username']
        _store_snapshot(current_user.id, 'leetcode', stats['username'], stats)
        db.session.commit()
        flash('LeetCode connected: {} ({} solved)'.format(
            stats['username'], stats['solved']['total']), 'success')
        return redirect(url_for('profile'))

    @app.route('/profile/unlink/leetcode', methods=['POST'])
    @login_required
    def profile_unlink_leetcode():
        current_user.leetcode_username = None
        ProfileSnapshot.query.filter_by(
            user_id=current_user.id, provider='leetcode').delete()
        db.session.commit()
        flash('LeetCode disconnected.', 'info')
        return redirect(url_for('profile'))

    # ---------- PUBLIC LEETCODE STAT CARD ----------
    @app.route('/cards/leetcode/<username>.svg')
    @limiter.limit("60 per hour")
    def leetcode_card(username):
        """Embeddable SVG stat card (github-readme-stats style). Public, since
        cards are meant to be embedded in READMEs and portfolio pages."""
        try:
            stats = get_card_stats(username)
        except LeetCodeAPIError as e:
            return Response(render_error_svg(str(e)), mimetype='image/svg+xml')
        return Response(render_leetcode_card_svg(stats), mimetype='image/svg+xml')

    # ---------- RESUME ANALYZER ----------
    @app.route('/resume')
    def resume_home():
        """Resume Analyzer upload page."""
        has_saved_resume = False
        if current_user.is_authenticated:
            has_saved_resume = db.session.query(
                ResumeAnalysis.query
                .filter_by(user_id=current_user.id)
                .filter(ResumeAnalysis.resume_text.isnot(None))
                .exists()).scalar()
        return render_template('resume_index.html',
                               has_saved_resume=has_saved_resume)

    # ENG-05.5: "what a parser sees" — extracted text + detected fields.
    # Helps candidates understand exactly what the analyzer ingested.
    @app.route('/api/parse_preview', methods=['POST'])
    @limiter.limit("20 per hour")
    def parse_preview():
        file = request.files.get('file') or request.files.get('resume')
        if not file or not file.filename:
            return jsonify({'error': 'No file uploaded.'}), 400
        data = file.read()
        try:
            from studybuddy.parsing import parse_document
            res = parse_document(data, filename=file.filename, kind="resume")
        except _ParseError as pe:
            # SEC-14: upload rejected — log the category, never the
            # filename or message (may carry PII).
            log_security_event("upload_reject",
                               details={"endpoint": "parse_preview",
                                        "reason": "parse_reject"})
            return jsonify({'error': str(pe)}), 400
        return jsonify({
            'text': res.text[:20000],
            'text_truncated': len(res.text) > 20000,
            'fields': res.fields,
            'layout': res.layout,
            'quality': res.quality,
            'warnings': res.warnings,
        })

    @app.route('/analyze', methods=['POST'])
    @limiter.limit("20 per hour")
    def analyze():
        """Handle resume upload and analysis. Works anonymously; when logged in
        the result is also persisted as a ResumeAnalysis row.

        Note: fetch-based upload (resume_script.js) sends the session CSRF
        token in the X-CSRFToken header. Login/register/goals forms carry
        CSRF tokens in hidden inputs."""
        if 'resume' not in request.files:
            return jsonify({'error': 'No file uploaded'}), 400

        file = request.files['resume']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400

        if not allowed_file(file.filename):
            # SEC-14: rejected upload (bad extension) — no filename in log.
            log_security_event("upload_reject",
                               details={"endpoint": "analyze",
                                        "reason": "bad_file_type"})
            return jsonify({'error': 'Invalid file type. Please upload PDF or DOCX.'}), 400

        # P2#13: uuid prefix prevents filename collisions between uploads.
        filename = f"{uuid.uuid4().hex[:8]}_{secure_filename(file.filename)}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)

        try:
            file.save(filepath)

            # Step 1: Extract text (ENG-05 + SEC-08 hardened pipeline:
            # content-validated, sandboxed, original deleted after parse)
            try:
                _parsed = _parse_upload_file(filepath, filename=filename, kind="resume")
            except _ParseError as _pe:
                return jsonify({'error': str(_pe)}), 400
            raw_text = _parsed.text
            # stash parser warnings/fields for the response ("what a parser sees")
            _parse_warnings = _parsed.warnings
            _parse_fields = _parsed.fields

            if not raw_text or len(raw_text.strip()) < 50:
                return jsonify({'error': 'Could not extract enough text from the resume.'}), 400

            # Analysis pipeline (identical to original)
            cleaned_text = clean_text(raw_text)
            sections = extract_sections(cleaned_text)
            email = extract_email(raw_text)
            phone = extract_phone(raw_text)
            links = extract_links(raw_text)
            skills = extract_skills(cleaned_text)
            skill_names = get_skill_names(skills)
            skills_by_category = get_skills_by_category(skills)
            education = extract_education_details(sections.get('education', ''))
            experience_years = extract_experience_years(cleaned_text)
            ats_result = calculate_ats_score(cleaned_text, sections, skills, experience_years)
            feedback = generate_feedback(ats_result, sections, skills, experience_years)
            summary = get_overall_summary(ats_result, skills, sections)
            recommendations = get_recommendations(cleaned_text, skill_names, top_k=8)
            skill_gaps = analyze_skill_gaps(skill_names, recommendations)
            roadmap = generate_roadmap(skill_gaps, skill_names)
            total_learning_time = estimate_total_time(roadmap)
            career_paths = suggest_career_paths(skill_names, experience_years)

            top_role = recommendations[0]['title'] if recommendations else 'Software Engineer'
            interview_qs = generate_interview_questions(top_role, skill_names[:5])

            # Resume-insights layer: bullet evidence + honest fix checklist
            # (same math as the scorer; projected score is an estimate).
            bullet_insights = analyze_bullets(cleaned_text, sections)
            fix_checklist = build_fix_checklist(
                ats_result, bullet_insights, sections, cleaned_text,
                num_skills=len({(s.get('name') or '').strip().lower()
                                for s in skills if isinstance(s, dict)}))
            dimension_notes_d = dimension_notes(
                ats_result, bullet_insights, sections, fix_checklist)

            result = {
                'contact': {'email': email, 'phone': phone, 'links': links},
                'sections_found': list(sections.keys()),
                'skills': skills,
                'skill_names': skill_names,
                'skills_by_category': skills_by_category,
                'education': education,
                'experience_years': experience_years,
                'ats': ats_result,
                'feedback': feedback,
                'summary': summary,
                'recommendations': recommendations,
                'skill_gaps': skill_gaps,
                'roadmap': roadmap,
                'total_learning_time': total_learning_time,
                'career_paths': career_paths,
                'interview_questions': interview_qs,
                'bullet_insights': bullet_insights,
                'fix_checklist': fix_checklist,
                'dimension_notes': dimension_notes_d,
                'filename': filename
            }

            # Persist for logged-in users; anonymous users get a DB row with
            # a 24h TTL (survives restarts, works across workers).
            if current_user.is_authenticated:
                row = ResumeAnalysis(
                    user_id=current_user.id,
                    filename=filename,
                    ats_score=(ats_result.get('total_score')
                               if isinstance(ats_result, dict) else None),
                    result_json=json_safe(result),
                    resume_text=cleaned_text[:20000],
                )
                db.session.add(row)
                db.session.commit()
                _purge_expired_anonymous()
                # Wave 5 #27: a completed resume analysis earns XP (logged-in only).
                _momentum_hook(current_user.id, 'resume_analyzed')
                return jsonify({'success': True,
                                'redirect': url_for('view_result', analysis_id=f'db-{row.id}')})

            analysis_id = uuid.uuid4().hex
            anon = AnonymousAnalysis(
                id=analysis_id,
                filename=filename,
                result_json=json_safe(result),
                expires_at=_utcnow_naive() + timedelta(seconds=_ANON_TTL_SECONDS),
            )
            db.session.add(anon)
            db.session.commit()
            _purge_expired_anonymous()
            return jsonify({'success': True,
                            'redirect': url_for('view_result', analysis_id=analysis_id)})

        except Exception:
            log.exception("Resume analysis failed")
            return jsonify({'error': 'Analysis failed. Please try again with a different file.'}), 500
        finally:
            # P2#14: failed analyses must not leak uploaded files.
            try:
                os.remove(filepath)
            except OSError:
                pass

    # ---------- JD-MATCHED ATS SCORING (Wave 1 #1) ----------
    @app.route('/jd-match', methods=['POST'])
    @limiter.limit("10 per hour")
    def jd_match():
        """Score the user's resume against a pasted job description.

        Login required (reports are persisted per user). Accepts JSON or
        multipart form: jd_text + resume_source ("upload"|"latest"), with an
        optional "resume" file for the upload path. The page sends the CSRF
        token in the X-CSRFToken header, so this route is NOT CSRF-exempt.
        """
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to match your resume against a job description.'}), 401

        if request.is_json:
            payload = request.get_json(silent=True) or {}
            jd_text = (payload.get('jd_text') or '').strip()
            resume_source = (payload.get('resume_source') or 'upload').strip()
        else:
            jd_text = (request.form.get('jd_text') or '').strip()
            resume_source = (request.form.get('resume_source') or 'upload').strip()

        # --- Server-side JD validation ---
        if not jd_text:
            return jsonify({'error': 'Please paste a job description first.'}), 400
        if len(jd_text) < JD_MIN_CHARS:
            return jsonify({'error': 'The job description looks too short '
                                     f'(minimum {JD_MIN_CHARS} characters). Please paste the full posting.'
                                     }), 400
        if len(jd_text) > JD_MAX_CHARS:
            return jsonify({'error': 'The job description is too long '
                                     f'(maximum {JD_MAX_CHARS:,} characters). Please trim it and try again.'
                                     }), 400

        # --- Resolve resume text ---
        resume_text = None
        filename = None
        uploaded_path = None
        try:
            if resume_source == 'latest':
                row = (ResumeAnalysis.query
                       .filter_by(user_id=current_user.id)
                       .filter(ResumeAnalysis.resume_text.isnot(None))
                       .order_by(ResumeAnalysis.created_at.desc()).first())
                if not row:
                    return jsonify({'error': 'No saved resume with extractable text found. '
                                             'Please upload your resume instead.'}), 400
                resume_text = row.resume_text
                filename = row.filename
            elif resume_source == 'version':
                if request.is_json:
                    version_id = (payload.get('version_id') or 0)
                else:
                    version_id = request.form.get('version_id', type=int) or 0
                try:
                    version_id = int(version_id)
                except (TypeError, ValueError):
                    version_id = 0
                version = ResumeVersion.query.filter_by(id=version_id,
                                                        user_id=current_user.id).first()
                if not version:
                    return jsonify({'error': 'Please choose one of your saved resume versions.'}), 400
                resume_text = version.resume_text
                filename = version.name
            else:
                if 'resume' not in request.files:
                    return jsonify({'error': 'No file uploaded.'}), 400
                file = request.files['resume']
                if not file or file.filename == '':
                    return jsonify({'error': 'No file selected.'}), 400
                if not allowed_file(file.filename):
                    return jsonify({'error': 'Invalid file type. Please upload PDF or DOCX.'}), 400
                filename = f"{uuid.uuid4().hex[:8]}_{secure_filename(file.filename)}"
                uploaded_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(uploaded_path)
                # ENG-05 + SEC-08 hardened pipeline (content-validated, sandboxed)
                try:
                    _parsed = _parse_upload_file(uploaded_path, filename=file.filename,
                                                 kind="resume")
                except _ParseError as _pe:
                    return jsonify({'error': str(_pe)}), 400
                raw_text = _parsed.text
                if not raw_text or len(raw_text.strip()) < 50:
                    return jsonify({'error': 'Could not extract enough text from the resume.'}), 400
                resume_text = clean_text(raw_text)
        finally:
            if uploaded_path:
                try:
                    os.remove(uploaded_path)
                except OSError:
                    pass

        # --- Deterministic server-side scoring ---
        cleaned = resume_text or ''
        sections = extract_sections(cleaned)
        skills = extract_skills(cleaned)
        skill_names = get_skill_names(skills)
        experience_years = extract_experience_years(cleaned)
        contact = {'email': extract_email(cleaned), 'phone': extract_phone(cleaned)}
        report = score_jd_match(cleaned, skill_names, experience_years,
                                sections, contact, jd_text)
        suggestions, ai_powered = _jd_match_suggestions(report, jd_text)
        report['suggestions'] = suggestions
        report['ai_powered'] = ai_powered
        report['filename'] = filename

        # --- Persist per user ---
        try:
            match_row = JDMatchReport(
                user_id=current_user.id,
                job_title=(report.get('job_title') or '')[:255],
                score=report.get('score'),
                report_json=json_safe(report),
            )
            db.session.add(match_row)
            db.session.commit()
        except Exception:
            log.exception("JD-match report persistence failed")
            db.session.rollback()
            return jsonify({'error': 'Could not save your report. Please try again.'}), 500

        return jsonify({
            'success': True,
            'report_id': match_row.id,
            'report': json_safe(report),
            'saved_url': url_for('view_jd_match', report_id=match_row.id),
        })

    @app.route('/jd-match/<int:report_id>')
    @login_required
    def view_jd_match(report_id):
        """Render a saved JD-match report (owner only)."""
        row = JDMatchReport.query.filter_by(id=report_id,
                                            user_id=current_user.id).first_or_404()
        return render_template('jd_match_report.html', report=row.report_json,
                               report_id=row.id, created_at=row.created_at)

    # ---------- AI BULLET REWRITER (Wave 1 #2) ----------
    @app.route('/rewrite-bullet', methods=['POST'])
    @limiter.limit("10 per hour")
    def rewrite_bullet():
        """Rewrite a resume bullet into stronger variants.

        Login required (rewrites are persisted per user). Accepts JSON:
        {bullet_text}. The page sends the CSRF token in the X-CSRFToken
        header, so this route is NOT CSRF-exempt.
        """
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to rewrite bullets.'}), 401

        payload = request.get_json(silent=True) or {}
        bullet_text = (payload.get('bullet_text') or '').strip()

        # --- Server-side validation ---
        if not bullet_text:
            return jsonify({'error': 'Please enter a bullet to rewrite.'}), 400
        if len(bullet_text) < BULLET_MIN_CHARS:
            return jsonify({'error': f'That bullet looks too short '
                                     f'(minimum {BULLET_MIN_CHARS} characters).'}), 400
        if len(bullet_text) > BULLET_MAX_CHARS:
            return jsonify({'error': f'That bullet is too long '
                                     f'(maximum {BULLET_MAX_CHARS} characters).'}), 400

        try:
            rewrites, ai_powered = _rewrite_bullet_ai(bullet_text)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

        try:
            row = BulletRewrite(
                user_id=current_user.id,
                original=bullet_text,
                rewrites=json_safe(rewrites),
                ai_powered=ai_powered,
            )
            db.session.add(row)
            db.session.commit()
        except Exception:
            log.exception("Bullet rewrite persistence failed")
            db.session.rollback()
            return jsonify({'error': 'Could not save your rewrite. Please try again.'}), 500

        return jsonify({
            'success': True,
            'rewrite_id': row.id,
            'original': bullet_text,
            'rewrites': json_safe(rewrites),
            'ai_powered': ai_powered,
        })

    # ---------- RESUME VERSIONS (Wave 1 #3) ----------
    @app.route('/resume-versions', methods=['GET'])
    def list_resume_versions():
        """List the user's saved resume versions (newest first)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to manage resume versions.'}), 401
        versions = (ResumeVersion.query
                    .filter_by(user_id=current_user.id)
                    .order_by(ResumeVersion.created_at.desc()).all())
        return jsonify({
            'success': True,
            'versions': [{
                'id': v.id,
                'name': v.name,
                'is_default': v.is_default,
                'created_at': v.created_at.isoformat(),
                'preview': (v.resume_text or '')[:200],
            } for v in versions],
        })

    @app.route('/resume-versions', methods=['POST'])
    @limiter.limit("20 per hour")
    def create_resume_version():
        """Save a named resume version. JSON: {name, resume_text} or
        {name, source: "latest"} to snapshot the latest saved analysis.
        CSRF token via X-CSRFToken header (not exempt)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to save resume versions.'}), 401
        payload = request.get_json(silent=True) or {}
        name = (payload.get('name') or '').strip()
        source = (payload.get('source') or 'text').strip()
        resume_text = (payload.get('resume_text') or '').strip()

        if not name:
            return jsonify({'error': 'Please give this version a name.'}), 400
        if len(name) > 120:
            return jsonify({'error': 'Version name is too long (max 120 characters).'}), 400
        if source == 'latest':
            row = (ResumeAnalysis.query
                   .filter_by(user_id=current_user.id)
                   .filter(ResumeAnalysis.resume_text.isnot(None))
                   .order_by(ResumeAnalysis.created_at.desc()).first())
            if not row or not (row.resume_text or '').strip():
                return jsonify({'error': 'No saved resume with extractable text found.'}), 400
            resume_text = row.resume_text.strip()
        if len(resume_text) < 50:
            return jsonify({'error': 'Resume text looks too short to save as a version.'}), 400
        if len(resume_text) > 50000:
            return jsonify({'error': 'Resume text is too long (max 50,000 characters).'}), 400

        existing = ResumeVersion.query.filter_by(user_id=current_user.id).count()
        try:
            version = ResumeVersion(
                user_id=current_user.id,
                name=name,
                resume_text=resume_text,
                is_default=(existing == 0),  # first version becomes the default
            )
            db.session.add(version)
            db.session.commit()
        except Exception:
            log.exception("Resume version save failed")
            db.session.rollback()
            return jsonify({'error': 'Could not save this version. Please try again.'}), 500
        return jsonify({'success': True, 'version': {
            'id': version.id, 'name': version.name,
            'is_default': version.is_default,
            'created_at': version.created_at.isoformat(),
        }})

    @app.route('/resume-versions/<int:version_id>', methods=['GET'])
    def get_resume_version(version_id):
        """Fetch one version with its full text (owner only)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        version = ResumeVersion.query.filter_by(id=version_id,
                                                user_id=current_user.id).first()
        if not version:
            return jsonify({'error': 'Version not found.'}), 404
        return jsonify({'success': True, 'version': {
            'id': version.id,
            'name': version.name,
            'is_default': version.is_default,
            'resume_text': version.resume_text,
            'created_at': version.created_at.isoformat(),
        }})

    @app.route('/resume-versions/<int:version_id>/set-default', methods=['POST'])
    @limiter.limit("20 per hour")
    def set_default_resume_version(version_id):
        """Mark one version as the default (used by JD-match "version" source)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        version = ResumeVersion.query.filter_by(id=version_id,
                                                user_id=current_user.id).first()
        if not version:
            return jsonify({'error': 'Version not found.'}), 404
        try:
            (ResumeVersion.query.filter_by(user_id=current_user.id)
             .update({'is_default': False}))
            version.is_default = True
            db.session.commit()
        except Exception:
            log.exception("Set-default version failed")
            db.session.rollback()
            return jsonify({'error': 'Could not update the default version.'}), 500
        return jsonify({'success': True, 'version_id': version.id})

    @app.route('/resume-versions/<int:version_id>/delete', methods=['POST'])
    @limiter.limit("20 per hour")
    def delete_resume_version(version_id):
        """Delete a saved version (POST-only, CSRF-protected)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        version = ResumeVersion.query.filter_by(id=version_id,
                                                user_id=current_user.id).first()
        if not version:
            return jsonify({'error': 'Version not found.'}), 404
        try:
            was_default = version.is_default
            db.session.delete(version)
            db.session.commit()
            if was_default:
                # Promote the newest remaining version to default.
                nxt = (ResumeVersion.query.filter_by(user_id=current_user.id)
                       .order_by(ResumeVersion.created_at.desc()).first())
                if nxt:
                    nxt.is_default = True
                    db.session.commit()
        except Exception:
            log.exception("Delete version failed")
            db.session.rollback()
            return jsonify({'error': 'Could not delete this version.'}), 500
        return jsonify({'success': True})

    @app.route('/resume-versions/<int:version_id>/diff', methods=['GET'])
    def diff_resume_version(version_id):
        """Line diff of a version against another (?against=<id>, default:
        the next-older version). Returns structured lines for safe rendering."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        version = ResumeVersion.query.filter_by(id=version_id,
                                                user_id=current_user.id).first()
        if not version:
            return jsonify({'error': 'Version not found.'}), 404
        against_id = request.args.get('against', type=int)
        if against_id:
            other = ResumeVersion.query.filter_by(id=against_id,
                                                  user_id=current_user.id).first()
            if not other:
                return jsonify({'error': 'Comparison version not found.'}), 404
        else:
            other = (ResumeVersion.query
                     .filter(ResumeVersion.user_id == current_user.id,
                             ResumeVersion.id != version.id,
                             ResumeVersion.created_at <= version.created_at)
                     .order_by(ResumeVersion.created_at.desc()).first())
            if not other:
                return jsonify({'error': 'No earlier version to compare against.'}), 400
        import difflib
        old_lines = (other.resume_text or '').splitlines()
        new_lines = (version.resume_text or '').splitlines()
        out_lines = []
        added = removed = 0
        for line in difflib.unified_diff(old_lines, new_lines, lineterm='', n=2):
            if line.startswith('+++') or line.startswith('---'):
                continue
            if line.startswith('@@'):
                out_lines.append({'type': 'hunk', 'text': line})
            elif line.startswith('+'):
                out_lines.append({'type': 'add', 'text': line[1:]})
                added += 1
            elif line.startswith('-'):
                out_lines.append({'type': 'del', 'text': line[1:]})
                removed += 1
            else:
                out_lines.append({'type': 'ctx', 'text': line[1:] if line.startswith(' ') else line})
        return jsonify({
            'success': True,
            'version': {'id': version.id, 'name': version.name},
            'against': {'id': other.id, 'name': other.name},
            'summary': {'added': added, 'removed': removed},
            'lines': out_lines[:500],
        })

    # ---------- RESUME VERSIONS ULTIMATE (F3: diff+impact, tailor, linkage) ----------
    @app.route('/resume-versions/compare-jd', methods=['POST'])
    @limiter.limit("20 per hour")
    def compare_versions_for_jd_route():
        """Which of two saved versions scores higher against a JD (F3.1)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        data = request.get_json(silent=True) or {}
        va = ResumeVersion.query.filter_by(id=data.get('version_a'),
                                           user_id=current_user.id).first()
        vb = ResumeVersion.query.filter_by(id=data.get('version_b'),
                                           user_id=current_user.id).first()
        jd_text = (data.get('jd_text') or '').strip()
        if not va or not vb:
            return jsonify({'error': 'Both versions must exist.'}), 404
        if not jd_text:
            return jsonify({'error': 'Job description is required.'}), 400
        from studybuddy.resume_versions import compare_versions_for_jd
        try:
            result = compare_versions_for_jd(va.resume_text, vb.resume_text,
                                             jd_text, name_a=va.name, name_b=vb.name)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        return jsonify({'success': True, **result})

    @app.route('/resume-versions/tailor', methods=['POST'])
    @limiter.limit("20 per hour")
    def tailor_resume_version():
        """One-click JD-tailored variant saved as a new version (F3.2)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        data = request.get_json(silent=True) or {}
        version = ResumeVersion.query.filter_by(id=data.get('version_id'),
                                                user_id=current_user.id).first()
        jd_text = (data.get('jd_text') or '').strip()
        if not version:
            return jsonify({'error': 'Version not found.'}), 404
        if not jd_text:
            return jsonify({'error': 'Job description is required.'}), 400
        from studybuddy.resume_versions import tailor_resume_for_jd
        try:
            result = tailor_resume_for_jd(version.resume_text, jd_text)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        try:
            new_v = ResumeVersion(user_id=current_user.id,
                                  name=result['name'][:120],
                                  resume_text=result['tailored_text'])
            db.session.add(new_v)
            db.session.commit()
        except Exception:
            log.exception("Tailor save failed")
            db.session.rollback()
            return jsonify({'error': 'Could not save the tailored version.'}), 500
        return jsonify({'success': True, 'version_id': new_v.id, **result})

    @app.route('/resume-versions/performance', methods=['GET'])
    def resume_version_performance():
        """Which saved version actually got interviews/offers (F3.3)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        from studybuddy.resume_versions import version_performance
        versions = {v.id: v.name for v in
                    ResumeVersion.query.filter_by(user_id=current_user.id).all()}
        apps = [{'version_id': a.resume_version_id,
                 'version_name': versions.get(a.resume_version_id),
                 'stage': a.stage}
                for a in JobApplication.query.filter_by(user_id=current_user.id).all()
                if a.resume_version_id]
        return jsonify({'success': True,
                        'performance': version_performance(apps),
                        'linked_applications': len(apps)})

    # ---------- COVER LETTER + OUTREACH GENERATOR (Wave 1 #4) ----------
    @app.route('/generate-cover', methods=['POST'])
    @limiter.limit("10 per hour")
    def generate_cover():
        """Generate a tailored cover letter + cold-outreach message.

        Login required. JSON: {jd_text, tone, resume_text?, version_id?}.
        Resume resolves from version_id (owner-checked) -> resume_text ->
        latest saved analysis. CSRF via X-CSRFToken header (not exempt).
        """
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to generate cover letters.'}), 401
        payload = request.get_json(silent=True) or {}
        data, error = validate_cover_inputs(payload)
        if error:
            return jsonify({'error': error}), 400

        # --- Resolve resume text ---
        resume_text = data["resume_text"]
        if data["version_id"]:
            try:
                vid = int(data["version_id"])
            except (TypeError, ValueError):
                vid = 0
            version = ResumeVersion.query.filter_by(id=vid,
                                                    user_id=current_user.id).first()
            if not version:
                return jsonify({'error': 'Please choose one of your saved resume versions.'}), 400
            resume_text = version.resume_text
        if not resume_text:
            row = (ResumeAnalysis.query
                   .filter_by(user_id=current_user.id)
                   .filter(ResumeAnalysis.resume_text.isnot(None))
                   .order_by(ResumeAnalysis.created_at.desc()).first())
            if not row or not (row.resume_text or '').strip():
                return jsonify({'error': 'No resume found. Upload or save a resume version first.'}), 400
            resume_text = row.resume_text.strip()

        (cover_letter, outreach), ai_powered = _generate_cover_ai(
            resume_text, data["jd_text"], data["tone"])
        title = (data["jd_text"].strip().splitlines() or ["Role"])[0][:120]

        try:
            docs = []
            for doc_type, content in (("cover_letter", cover_letter),
                                      ("outreach", outreach)):
                doc = CoverDoc(
                    user_id=current_user.id,
                    doc_type=doc_type,
                    title=title,
                    tone=data["tone"],
                    content=content,
                    ai_powered=ai_powered,
                )
                db.session.add(doc)
                docs.append(doc)
            db.session.commit()
        except Exception:
            log.exception("Cover doc persistence failed")
            db.session.rollback()
            return jsonify({'error': 'Could not save your documents. Please try again.'}), 500

        return jsonify({
            'success': True,
            'cover_letter': cover_letter,
            'outreach': outreach,
            'ai_powered': ai_powered,
            'tone': data["tone"],
            'doc_ids': [d.id for d in docs],
        })

    @app.route('/cover-docs', methods=['GET'])
    def list_cover_docs():
        """List the user's generated cover letters / outreach messages."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        docs = (CoverDoc.query
                .filter_by(user_id=current_user.id)
                .order_by(CoverDoc.created_at.desc())
                .limit(50).all())
        return jsonify({'success': True, 'docs': [{
            'id': d.id, 'doc_type': d.doc_type, 'title': d.title,
            'tone': d.tone, 'ai_powered': d.ai_powered,
            'created_at': d.created_at.isoformat(),
            'preview': (d.content or '')[:200],
        } for d in docs]})

    # ---------- LINKEDIN PROFILE GRADER (Wave 1 #5) ----------
    @app.route('/linkedin-grade', methods=['POST'])
    @limiter.limit("10 per hour")
    def linkedin_grade():
        """Grade pasted LinkedIn profile text (TEXT ONLY - we never scrape LinkedIn).

        Login required (reports are persisted per user). Accepts JSON or form
        data: headline, about, experience, skills. The page sends the CSRF
        token in the X-CSRFToken header, so this route is NOT CSRF-exempt.
        """
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to grade your LinkedIn profile.'}), 401

        if request.is_json:
            payload = request.get_json(silent=True) or {}
        else:
            payload = request.form.to_dict()
        fields, error = validate_linkedin_inputs(payload)
        if error:
            return jsonify({'error': error}), 400

        # --- Deterministic server-side grading ---
        report = grade_linkedin_profile(**fields)
        suggestions, ai_powered = _linkedin_suggestions(report, fields)
        report['suggestions'] = suggestions
        report['ai_powered'] = ai_powered
        report['summary'] = generate_rule_based_summary(report)

        # --- Persist per user ---
        try:
            row = LinkedInReport(
                user_id=current_user.id,
                score=report.get('score'),
                report_json=json_safe(report),
            )
            db.session.add(row)
            db.session.commit()
        except Exception:
            log.exception("LinkedIn report persistence failed")
            db.session.rollback()
            return jsonify({'error': 'Could not save your report. Please try again.'}), 500

        return jsonify({
            'success': True,
            'report_id': row.id,
            'report': json_safe(report),
            'saved_url': url_for('view_linkedin_report', report_id=row.id),
        })

    @app.route('/linkedin/<int:report_id>')
    @login_required
    def view_linkedin_report(report_id):
        """Render a saved LinkedIn grading report (owner only)."""
        row = LinkedInReport.query.filter_by(id=report_id,
                                             user_id=current_user.id).first_or_404()
        return render_template('linkedin_report.html', report=row.report_json,
                               report_id=row.id, created_at=row.created_at)

    # ---------- APPLICATION TRACKER KANBAN (Wave 1 #6) ----------
    def _tracker_app_to_dict(a):
        return {
            'id': a.id, 'company': a.company, 'role': a.role, 'stage': a.stage,
            'jd_url': a.jd_url, 'notes': a.notes,
            'applied_date': a.applied_date.isoformat() if a.applied_date else None,
            'stage_updated_at': a.stage_updated_at.isoformat() if a.stage_updated_at else None,
            'created_at': a.created_at.isoformat() if a.created_at else None,
            'resume_version_id': a.resume_version_id,
        }

    def _validate_tracker_payload(payload, require_company_role=True):
        """Validate tracker add/edit fields. Returns (data, error)."""
        data = {}
        company = (payload.get('company') or '').strip()
        role = (payload.get('role') or '').strip()
        if require_company_role:
            if not company:
                return None, 'Company name is required.'
            if not role:
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

    def _tracker_stats(user_id):
        """Stage counts, response rate, and avg current dwell per stage."""
        apps = JobApplication.query.filter_by(user_id=user_id).all()
        counts = {sid: 0 for sid, _ in TRACKER_STAGES}
        for a in apps:
            counts[a.stage] = counts.get(a.stage, 0) + 1
        in_pipeline = counts['applied'] + counts['oa'] + counts['interview'] + counts['offer'] + counts['rejected']
        responded = counts['oa'] + counts['interview'] + counts['offer']
        response_rate = round(100.0 * responded / in_pipeline, 1) if in_pipeline else 0.0
        now = datetime.now(UTC)
        dwell = {}
        for sid, _ in TRACKER_STAGES:
            days = []
            for a in apps:
                if a.stage != sid or not a.stage_updated_at:
                    continue
                ts = a.stage_updated_at
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                days.append(max(0.0, (now - ts).total_seconds() / 86400.0))
            dwell[sid] = round(sum(days) / len(days), 1) if days else 0.0
        # Follow-up nudges: active-pipeline applications sitting in one
        # stage for 14+ days (wishlist/offer/rejected are not nudged).
        stale = []
        for a in apps:
            if a.stage not in ('applied', 'oa', 'interview') or not a.stage_updated_at:
                continue
            ts = a.stage_updated_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            days = (now - ts).days
            if days >= 14:
                stale.append({'id': a.id, 'company': a.company,
                              'role': a.role, 'stage': a.stage, 'days': days})
        stale.sort(key=lambda s: -s['days'])
        return {'counts': counts, 'total': len(apps),
                'response_rate': response_rate, 'avg_days_in_stage': dwell,
                'stale': stale}

    @app.route('/tracker')
    @login_required
    def tracker_board():
        """Kanban board of the user's job applications."""
        apps = (JobApplication.query.filter_by(user_id=current_user.id)
                .order_by(JobApplication.created_at.desc()).all())
        by_stage = {sid: [] for sid, _ in TRACKER_STAGES}
        for a in apps:
            by_stage.setdefault(a.stage, []).append(a)
        return render_template('tracker.html', stages=TRACKER_STAGES,
                               by_stage=by_stage, stats=_tracker_stats(current_user.id))

    @app.route('/tracker/add', methods=['POST'])
    @limiter.limit("60 per hour")
    def tracker_add():
        """Add a job application. CSRF via X-CSRFToken header (not exempt)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to track applications.'}), 401
        payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
        payload = payload or {}
        stage = (payload.get('stage') or 'wishlist').strip()
        if stage not in TRACKER_STAGE_IDS:
            return jsonify({'error': 'Invalid stage.'}), 400
        data, error = _validate_tracker_payload(payload)
        if error:
            return jsonify({'error': error}), 400
        app_row = JobApplication(user_id=current_user.id, stage=stage, **data)
        db.session.add(app_row)
        db.session.commit()
        return jsonify({'success': True, 'application': _tracker_app_to_dict(app_row)}), 201

    @app.route('/tracker/<int:app_id>/move', methods=['POST'])
    def tracker_move(app_id):
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
        if app_row.stage != stage:
            app_row.stage = stage
            app_row.stage_updated_at = datetime.now(UTC)
            db.session.commit()
        return jsonify({'success': True, 'application': _tracker_app_to_dict(app_row)})

    @app.route('/tracker/<int:app_id>/edit', methods=['POST'])
    def tracker_edit(app_id):
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

    @app.route('/tracker/<int:app_id>/delete', methods=['POST'])
    def tracker_delete(app_id):
        """Delete an application. CSRF via X-CSRFToken header."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        app_row = JobApplication.query.filter_by(id=app_id,
                                                 user_id=current_user.id).first()
        if not app_row:
            return jsonify({'error': 'Application not found.'}), 404
        db.session.delete(app_row)
        db.session.commit()
        return jsonify({'success': True})

    @app.route('/tracker/stats')
    @login_required
    def tracker_stats():
        """JSON stats for the user's tracker."""
        return jsonify({'success': True, 'stats': _tracker_stats(current_user.id)})

    # ---------- CAREER-DISCOVERY ASSESSMENT (Wave 1 #7) ----------
    @app.route('/assessment')
    @login_required
    def assessment_quiz():
        """Guided career-discovery quiz (16 questions, stepper UI)."""
        return render_template('assessment.html', questions=public_questions())

    @app.route('/assessment/submit', methods=['POST'])
    @limiter.limit("10 per hour")
    def assessment_submit():
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

        return jsonify({
            'success': True,
            'assessment_id': row.id,
            'result': json_safe(result),
            'saved_url': url_for('view_assessment', assessment_id=row.id),
        })

    @app.route('/assessment/<int:assessment_id>')
    @login_required
    def view_assessment(assessment_id):
        """Render a saved assessment result (owner only)."""
        row = CareerAssessment.query.filter_by(id=assessment_id,
                                               user_id=current_user.id).first_or_404()
        return render_template('assessment_result.html',
                               clusters=row.top_clusters,
                               assessment_id=row.id, created_at=row.created_at)

    # ---------- COMPANY-SPECIFIC MOCK INTERVIEWS (Wave 2 #13) ----------
    @app.route('/interview')
    def interview_home():
        """Mock interview setup: company mock or resume-based mock."""
        from collections import Counter

        from studybuddy.mock_interview import _load_bank
        bank = _load_bank()
        company_counts = Counter()
        for q in bank:
            for comp in q.get("companies", []):
                company_counts[comp] += 1
        recent = []
        if current_user.is_authenticated:
            recent = (InterviewSession.query
                      .filter_by(user_id=current_user.id)
                      .order_by(InterviewSession.id.desc())
                      .limit(5).all())
        return render_template('interview.html', options=interview_options(),
                               total_questions=len(bank),
                               company_counts=company_counts,
                               recent_sessions=recent,
                               is_authed=current_user.is_authenticated)

    @app.route('/api/interview/intel/<company>')
    def interview_intel_api(company):
        """Company intel card data (public bank aggregates)."""
        from studybuddy.mock_interview import company_intel
        try:
            return jsonify(company_intel(company))
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400

    @app.route('/interview/start', methods=['POST'])
    @limiter.limit("10 per hour")
    def interview_start():
        """Start a mock interview session. Login required (sessions are per-user).

        Accepts JSON or form data: company, role, category, num_questions.
        CSRF token comes via the X-CSRFToken header, so this route is NOT
        CSRF-exempt.
        """
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to start a mock interview.'}), 401
        payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
        cleaned, error = validate_interview_setup(payload or {})
        if error:
            return jsonify({'error': error}), 400
        questions = pick_questions(cleaned["company"], cleaned["role"],
                                   cleaned["category"], n=cleaned["n"])
        if not questions:
            return jsonify({'error': 'No questions available for that selection.'}), 400
        try:
            row = InterviewSession(
                user_id=current_user.id,
                company=cleaned["company"], role=cleaned["role"],
                category=cleaned["category"],
                questions_json=json_safe(questions),
                answers_json=[],
            )
            db.session.add(row)
            db.session.commit()
        except Exception:
            log.exception("Interview session creation failed")
            db.session.rollback()
            return jsonify({'error': 'Could not start the interview. Please try again.'}), 500
        return jsonify({'success': True, 'session_id': row.id,
                        'questions': json_safe(questions)})

    @app.route('/interview/resume/start', methods=['POST'])
    @limiter.limit("10 per hour")
    def interview_resume_start():
        """Start a resume-based mock interview.

        The candidate uploads a resume (PDF/DOCX) or pastes its text;
        questions are generated deterministically from the resume's
        own projects, skills and experience, each with rubric key
        points built from those facts (studybuddy.resume_interview).
        """
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to start a mock interview.'}), 401
        from studybuddy.parsing import ParseError as _ResumeParseError
        from studybuddy.parsing import parse_document
        from studybuddy.resume_interview import (
            extract_profile,
            generate_resume_questions,
        )
        text = ""
        upload = request.files.get('resume')
        if upload is not None and upload.filename:
            try:
                result = parse_document(upload.read(),
                                        filename=upload.filename,
                                        kind='resume')
            except _ResumeParseError as exc:
                return jsonify({'error': str(exc)}), 400
            if not result.ok:
                return jsonify({'error': result.error or
                                'Could not read that file.'}), 400
            text = result.text
        elif request.is_json:
            text = (request.get_json(silent=True) or {}).get('resume_text') or ''
        else:
            text = request.form.get('resume_text') or ''
        text = (text or '').strip()
        if len(text) < 200:
            return jsonify({'error': 'That resume looks too short to build '
                            'a personal mock from. Upload the full resume '
                            '(PDF/DOCX) or paste the complete text.'}), 400
        profile = extract_profile(text)
        questions = generate_resume_questions(profile, n=12)
        if not questions:
            return jsonify({'error': 'We could not find projects, skills '
                            'or experience in that resume. Add a Projects '
                            'and Skills section and try again.'}), 400
        try:
            row = InterviewSession(
                user_id=current_user.id,
                company='Resume-Based Mock', role='resume',
                category='resume',
                questions_json=json_safe(questions),
                answers_json=[],
            )
            db.session.add(row)
            db.session.commit()
        except Exception:
            log.exception("Resume interview session creation failed")
            db.session.rollback()
            return jsonify({'error': 'Could not start the interview. '
                            'Please try again.'}), 500
        return jsonify({'success': True, 'session_id': row.id,
                        'questions': json_safe(questions),
                        'profile': {
                            'name': profile['name'],
                            'skills': profile['skills'][:8],
                            'projects': [p['title']
                                         for p in profile['projects']],
                            'experience_entries': len(profile['experience']),
                        }})

    @app.route('/interview/<int:session_id>/submit', methods=['POST'])
    @limiter.limit("10 per hour")
    def interview_submit(session_id):
        """Score all answers for a session against the rubric.

        Body: {"answers": [{"question_id": "...", "answer": "..."}]}.
        Scoring is deterministic and server-side; Gemini only polishes the
        feedback wording (rule-based fallback always works).
        """
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to submit answers.'}), 401
        row = InterviewSession.query.filter_by(id=session_id,
                                               user_id=current_user.id).first()
        if not row:
            return jsonify({'error': 'Interview session not found.'}), 404
        payload = request.get_json(silent=True) or {}
        raw_answers = payload.get("answers")
        if not isinstance(raw_answers, list) or not raw_answers:
            return jsonify({'error': 'Provide a non-empty answers list.'}), 400
        questions = row.questions_json or []
        qmap = {q.get("id"): q for q in questions}
        scored = []
        for item in raw_answers[:15]:  # company mocks cap at 10; resume mocks run to 12
            if not isinstance(item, dict):
                continue
            qid = str(item.get("question_id") or "")
            question = qmap.get(qid)
            if not question:
                continue
            answer = str(item.get("answer") or "")[:8000]
            s = score_answer(question, answer)
            s["answer_text"] = answer
            scored.append(s)
        if not scored:
            return jsonify({'error': 'No answers matched this session\u2019s questions.'}), 400
        feedback, ai_powered = _interview_feedback(scored, [qmap[s["question_id"]] for s in scored])
        for s, fb in zip(scored, feedback, strict=False):
            s["feedback"] = fb
        total = round(sum(s["total"] for s in scored) / len(scored), 1)
        try:
            row.answers_json = json_safe(scored)
            row.total_score = total
            row.ai_powered = ai_powered
            db.session.commit()
        except Exception:
            log.exception("Interview result persistence failed")
            db.session.rollback()
            return jsonify({'error': 'Could not save your results. Please try again.'}), 500
        # Wave 5 #27: a finished mock interview earns XP.
        _momentum_hook(current_user.id, 'mock_completed')
        return jsonify({'success': True, 'total_score': total,
                        'ai_powered': ai_powered,
                        'results': json_safe(scored),
                        'report_url': url_for('view_interview', session_id=row.id)})

    @app.route('/interview/<int:session_id>')
    @login_required
    def view_interview(session_id):
        """Render a saved interview report (owner only)."""
        row = InterviewSession.query.filter_by(id=session_id,
                                               user_id=current_user.id).first_or_404()
        return render_template('interview_report.html', session=row,
                               questions=row.questions_json or [],
                               answers=row.answers_json or [])

    # ---------- COMMUNICATION COACH (Wave 2 #14) ----------
    @app.route('/coach')
    def coach_home():
        """Communication coach: paste or speak an answer, get scored on
        fillers, pacing, STAR structure, impact, confidence and delivery."""
        history = []
        trend = None
        if current_user.is_authenticated:
            rows = (CommunicationAnalysis.query
                    .filter_by(user_id=current_user.id)
                    .order_by(CommunicationAnalysis.created_at.desc())
                    .limit(8).all())
            history = [{
                'id': r.id,
                'score': r.clarity_score,
                'question_type': (r.analysis_json or {}).get(
                    'question_type', 'general'),
                'question': ((r.analysis_json or {}).get('question') or {}).get(
                    'question'),
                'created_at': r.created_at,
            } for r in rows]
            if len(rows) >= 2:
                trend = {'first': rows[-1].clarity_score,
                         'latest': rows[0].clarity_score,
                         'delta': round((rows[0].clarity_score or 0)
                                        - (rows[-1].clarity_score or 0), 1),
                         'count': CommunicationAnalysis.query.filter_by(
                             user_id=current_user.id).count()}
        return render_template('coach.html',
                               is_authed=current_user.is_authenticated,
                               questions=coach_questions(),
                               question_types=COACH_QUESTION_TYPES,
                               history=history, trend=trend)

    @app.route('/coach/analyze', methods=['POST'])
    @limiter.limit("10 per hour")
    def coach_analyze():
        """Analyze a pasted answer. Login required (analyses are persisted).

        Accepts JSON or form data: text. CSRF token via X-CSRFToken header,
        so this route is NOT CSRF-exempt.
        """
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to use the communication coach.'}), 401
        payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
        text, error = validate_coach_inputs(payload or {})
        if error:
            return jsonify({'error': error}), 400
        qtype, question, spoken_seconds, error = validate_coach_extras(
            payload or {})
        if error:
            return jsonify({'error': error}), 400

        report = analyze_transcript(text, question_type=qtype,
                                    spoken_seconds=spoken_seconds)
        if question is not None:
            report['question'] = {'id': question.get('id'),
                                  'question': question.get('question'),
                                  'type': question.get('type')}
        cleaned, drills, ai_powered = _coach_polish(report, text)
        report['cleaned_version'] = cleaned
        report['drills'] = drills
        report['ai_powered'] = ai_powered
        report['original_text'] = text  # needed to render highlights on the saved report

        try:
            row = CommunicationAnalysis(
                user_id=current_user.id,
                clarity_score=report.get('clarity_score'),
                analysis_json=json_safe(report),
            )
            db.session.add(row)
            db.session.commit()
        except Exception:
            log.exception("Coach analysis persistence failed")
            db.session.rollback()
            return jsonify({'error': 'Could not save your analysis. Please try again.'}), 500
        # Wave 5 #27: a completed coach analysis earns XP.
        _momentum_hook(current_user.id, 'coach_analyzed')
        return jsonify({'success': True, 'analysis_id': row.id,
                        'report': json_safe(report),
                        'report_url': url_for('view_coach_report', analysis_id=row.id)})

    @app.route('/coach/video-checklist', methods=['POST'])
    @limiter.limit("30 per hour")
    def coach_video_checklist():
        """Score the video-presence self-checklist (login required)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to use the communication coach.'}), 401
        payload = request.get_json(silent=True) or {}
        scores = payload.get('scores')
        if scores is not None and not isinstance(scores, dict):
            return jsonify({'error': 'Scores must be an object.'}), 400
        return jsonify({'success': True,
                        'result': video_presence_checklist(scores or {})})

    @app.route('/coach/<int:analysis_id>')
    @login_required
    def view_coach_report(analysis_id):
        """Render a saved coach analysis (owner only)."""
        row = CommunicationAnalysis.query.filter_by(id=analysis_id,
                                                   user_id=current_user.id).first_or_404()
        return render_template('coach_report.html', report=row.analysis_json,
                               analysis_id=row.id, created_at=row.created_at)

    # ---------- DSA PRACTICE (Wave 2 #15) ----------
    @app.route('/dsa')
    def dsa_home():
        """DSA practice: filter problems by topic/difficulty.

        The progress tracker is built into this page (user request,
        2026-10-23): the same summary the standalone tracker page uses,
        rendered right under the intro instead of behind a link.
        """
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

    @app.route('/api/dsa/problems')
    def dsa_problem_list():
        """JSON problem list with optional topic/difficulty filters."""
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

    @app.route('/api/dsa/progress')
    def dsa_progress_api():
        """Progress summary JSON for the tracker embedded on /dsa.

        The embedded tracker refreshes from this endpoint when the tab
        becomes visible again, so marks made in another tab (or on a
        problem page) show up without a manual reload. Anonymous
        visitors get the zeroed summary.
        """
        progress_rows = []
        if current_user.is_authenticated:
            rows = DSAProgress.query.filter_by(user_id=current_user.id).all()
            progress_rows = [(r.problem_id, r.status) for r in rows]
        summary = progress_summary(progress_rows)
        return jsonify({'is_authed': current_user.is_authenticated,
                        'a2z': summary['a2z'],
                        'classic': summary['classic']})

    @app.route('/dsa/diagnosis')
    @login_required
    def dsa_diagnosis():
        """Weak-topic diagnosis dashboard (owner only)."""
        rows = DSAProgress.query.filter_by(user_id=current_user.id).all()
        progress = [{"problem_id": r.problem_id, "status": r.status,
                     "hints_used": r.hints_used} for r in rows]
        report = weak_topic_diagnosis(progress)
        return render_template('dsa_diagnosis.html', report=report)

    @app.route('/dsa/a2z')
    def dsa_a2z_page():
        """Striver A2Z sheet — simple rows: solve on the original
        platform, mark progress here."""
        statuses = {}
        if current_user.is_authenticated:
            rows = DSAProgress.query.filter_by(user_id=current_user.id).all()
            statuses = expand_statuses([(r.problem_id, r.status) for r in rows])
        steps = sheet_structure(statuses)
        return render_template('dsa_a2z.html', steps=steps,
                               total=sum(s['total'] for s in steps),
                               solved=sum(s['solved'] for s in steps),
                               lesson_ids=a2z_lesson_ids())

    @app.route('/dsa/a2z/<problem_id>/learn')
    def dsa_a2z_learn(problem_id):
        """In-app lesson for an A2Z theory topic or pattern problem:
        full content, code, and suggested follow-up practice."""
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

    @app.route('/dsa/tracker')
    @login_required
    def dsa_tracker_page():
        """Progress tracker: overall, per-module and per-difficulty."""
        rows = DSAProgress.query.filter_by(user_id=current_user.id).all()
        progress = progress_summary([(r.problem_id, r.status) for r in rows])
        return render_template('dsa_tracker.html', a2z=progress['a2z'],
                               classic=progress['classic'])

    @app.route('/dsa/<problem_id>/untrack', methods=['POST'])
    @limiter.limit("30 per hour")
    def dsa_untrack(problem_id):
        """Remove a progress mark (undo a 'Mark as solved')."""
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

    @app.route('/dsa/<problem_id>')
    def dsa_problem_page(problem_id):
        """Problem workspace: statement, progressive hints, code area."""
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
        row = DSAProgress.query.filter_by(user_id=current_user.id,
                                          problem_id=problem_id).first()
        if not row:
            row = DSAProgress(user_id=current_user.id, problem_id=problem_id)
            db.session.add(row)
        return row

    @app.route('/dsa/<problem_id>/hint', methods=['POST'])
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

    @app.route('/dsa/<problem_id>/approach', methods=['POST'])
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

    @app.route('/dsa/<problem_id>/status', methods=['POST'])
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
            _momentum_hook(current_user.id, 'dsa_solved')
        return jsonify({'success': True, 'status': row.status})

    # ---------- TIMED APTITUDE TESTS (Wave 2 #16) ----------
    def _aptitude_user_stats(user_id):
        """(mastery, mistake_count, recent_attempts) from submitted tests."""
        rows = (AptitudeAttempt.query
                .filter_by(user_id=user_id)
                .filter(AptitudeAttempt.submitted_at.isnot(None))
                .order_by(AptitudeAttempt.submitted_at.desc())
                .limit(40).all())
        history = [r.results_json or [] for r in rows]
        mastery = aptitude_mastery(history)
        wrong, _by_sub = aptitude_mistakes_calc(history)
        recent = [{
            'id': r.id, 'category': r.category,
            'score_pct': (r.summary_json or {}).get('score_pct'),
            'submitted_at': r.submitted_at,
        } for r in rows[:5]]
        return mastery, len(wrong), recent

    @app.route('/aptitude')
    def aptitude_home():
        """Aptitude hub: two tracks, topic practice, company mocks,
        custom timed tests, mistake book and personal mastery."""
        mastery, mistake_count, recent = {}, 0, []
        if current_user.is_authenticated:
            mastery, mistake_count, recent = _aptitude_user_stats(
                current_user.id)
        topics_by_track = {}
        for track in APTITUDE_TRACKS:
            topics_by_track[track] = [
                {'subcategory': sub, 'category': cat, 'count': n,
                 'mastery': mastery.get(sub)}
                for sub, cat, n in aptitude_topics(track)]
        concept_topics = {c['topic'] for c in aptitude_concepts()}
        return render_template(
            'aptitude.html',
            categories=aptitude_categories(),
            total=len(aptitude_all()),
            is_authed=current_user.is_authenticated,
            tracks=aptitude_track_counts(),
            track_meta=APTITUDE_TRACKS,
            topics_by_track=topics_by_track,
            concept_topics=concept_topics,
            company_tests=APTITUDE_COMPANY_TESTS,
            selection_counts=aptitude_selection_counts(),
            mastery=mastery, mistake_count=mistake_count, recent=recent)

    @app.route('/aptitude/start', methods=['POST'])
    @limiter.limit("10 per hour")
    def aptitude_start():
        """Start a timed attempt. Login required. Server stamps the start
        time; the client countdown is display-only.

        CSRF token comes via the X-CSRFToken header, so this route is NOT
        CSRF-exempt.
        """
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to take a test.'}), 401
        payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
        cleaned, error = validate_test_setup(payload or {})
        if error:
            return jsonify({'error': error}), 400
        seed = random.randint(0, 2 ** 31 - 1)
        questions = aptitude_pick_for_setup(cleaned, seed=seed)
        if not questions:
            return jsonify({'error': 'No questions available for that selection.'}), 400
        try:
            row = AptitudeAttempt(
                user_id=current_user.id,
                category=cleaned["category"],
                num_questions=len(questions),
                time_limit_sec=cleaned["time_limit"],
                question_ids=[q["id"] for q in questions],
                started_at=datetime.now(UTC),
            )
            db.session.add(row)
            db.session.commit()
        except Exception:
            log.exception("Aptitude attempt creation failed")
            db.session.rollback()
            return jsonify({'error': 'Could not start the test. Please try again.'}), 500
        return jsonify({'success': True, 'attempt_id': row.id,
                        'questions': json_safe(questions),
                        'time_limit_sec': row.time_limit_sec})

    @app.route('/aptitude/<int:attempt_id>/submit', methods=['POST'])
    @limiter.limit("20 per hour")
    def aptitude_submit(attempt_id):
        """Submit answers. Scoring is fully server-side: the question set
        and correct answers come from the bank, never the client. The time
        window is validated against the server start timestamp.
        """
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to submit.'}), 401
        row = db.session.get(AptitudeAttempt, attempt_id)
        if not row or row.user_id != current_user.id:
            return jsonify({'error': 'Attempt not found.'}), 404
        if row.submitted_at is not None:
            return jsonify({'error': 'This attempt was already submitted.'}), 400
        payload = request.get_json(silent=True) or {}
        raw_answers = payload.get("answers") or {}
        if not isinstance(raw_answers, dict):
            return jsonify({'error': 'Invalid answers format.'}), 400
        answers = {}
        for qid in (row.question_ids or []):
            if qid in raw_answers:
                try:
                    idx = int(raw_answers[qid])
                except (TypeError, ValueError):
                    continue
                if 0 <= idx <= 3:
                    answers[qid] = idx
        now = datetime.now(UTC)
        started = row.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        elapsed = (now - started).total_seconds()
        expired = elapsed > (row.time_limit_sec + SUBMIT_GRACE_SEC)
        results, summary = score_attempt(row.question_ids or [], answers)
        summary["time_used_sec"] = int(min(elapsed, row.time_limit_sec + SUBMIT_GRACE_SEC))
        summary["expired"] = expired
        try:
            row.answers_json = json_safe(answers)
            row.results_json = json_safe(results)
            row.summary_json = json_safe(summary)
            row.expired = expired
            row.submitted_at = now
            db.session.commit()
        except Exception:
            log.exception("Aptitude submit persistence failed")
            db.session.rollback()
            return jsonify({'error': 'Could not save your attempt. Please try again.'}), 500
        # Wave 5 #27: a completed aptitude test earns XP.
        _momentum_hook(current_user.id, 'aptitude_completed')
        return jsonify({'success': True, 'expired': expired,
                        'summary': json_safe(summary),
                        'results': json_safe(results)})

    @app.route('/aptitude/<int:attempt_id>')
    @login_required
    def aptitude_report(attempt_id):
        """Saved attempt report (owner only)."""
        row = db.session.get(AptitudeAttempt, attempt_id)
        if not row or row.user_id != current_user.id:
            abort(404)
        if row.submitted_at is None:
            flash("This attempt has not been submitted yet.", "warning")
            return redirect(url_for('aptitude_home'))
        return render_template('aptitude_result.html', attempt=row,
                               results=row.results_json or [],
                               summary=row.summary_json or {})

    @app.route('/aptitude/practice/<track>/<topic>')
    def aptitude_practice(track, topic):
        """Learn mode: concept primer + untimed topic practice with
        instant feedback, worked explanations and shortcuts."""
        if track not in APTITUDE_TRACKS:
            abort(404)
        from studybuddy.aptitude_test import topic_category
        if topic_category(topic) is None or \
                track != ('verbal' if topic_category(topic) == 'verbal'
                          else 'logical'):
            abort(404)
        questions = aptitude_pick(topic_category(topic), n=20,
                                  seed=random.randint(0, 2 ** 31 - 1),
                                  subcategory=topic)
        return render_template(
            'aptitude_practice.html', track=track,
            track_label=APTITUDE_TRACKS[track]['label'], topic=topic,
            concept=aptitude_concept(topic), questions=questions,
            is_authed=current_user.is_authenticated)

    @app.route('/aptitude/practice/check', methods=['POST'])
    @limiter.limit("120 per hour")
    def aptitude_practice_check():
        """Instant practice check: correctness + explanation + trick."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to practice.'}), 401
        payload = request.get_json(silent=True) or {}
        result = aptitude_check(payload.get('question_id'),
                                payload.get('chosen'))
        if result is None:
            return jsonify({'error': 'Unknown question.'}), 400
        return jsonify({'success': True, 'result': result})

    @app.route('/aptitude/mistakes')
    @login_required
    def aptitude_mistakes():
        """Mistake Book: the latest wrong/skipped state of every
        question across the user's submitted tests, grouped by topic,
        with full review and a one-click retest."""
        rows = (AptitudeAttempt.query
                .filter_by(user_id=current_user.id)
                .filter(AptitudeAttempt.submitted_at.isnot(None))
                .order_by(AptitudeAttempt.submitted_at.desc())
                .limit(40).all())
        history = [r.results_json or [] for r in rows]
        wrong, by_sub = aptitude_mistakes_calc(history)
        groups = {}
        for r in wrong:
            groups.setdefault(r['subcategory'], []).append(r)
        return render_template('aptitude_mistakes.html', wrong=wrong,
                               by_sub=by_sub, groups=groups,
                               wrong_ids=[r['id'] for r in wrong][:50])

    # ---------- STAR ANSWER BUILDER (Wave 2 #17) ----------
    def _star_suggestions(scored, sections, competency):
        """Polish per-section rewrite suggestions via Gemini, rule-based
        fallback. Returns (suggestions, ai_powered). Scores never depend on
        the LLM."""
        fallback = {}
        for s in SECTIONS:
            tip = generate_rule_based_rewrite_tip(s, sections.get(s) or "")
            existing = scored["sections"][s]["suggestions"]
            fallback[s] = ([tip] + existing)[:4]
        comp_label = dict((c[0], c[1]) for c in COMPETENCIES).get(competency,
                                                                 competency)
        compact = {
            "competency": comp_label,
            "sections": {s: (sections.get(s) or "")[:800] for s in SECTIONS},
            "scores": {s: scored["sections"][s]["score"] for s in SECTIONS},
        }
        prompt = (
            "You are an interview coach. A candidate drafted a STAR answer "
            f"for the competency '{comp_label}'. Section scores are out of 25. "
            "For EACH of situation/task/action/result, give up to 2 specific "
            "rewrite suggestions referencing their actual text - no generic "
            "advice. Return ONLY JSON like "
            '{"suggestions": {"situation": ["..."], "task": ["..."], '
            '"action": ["..."], "result": ["..."]}}.\n'
            "DRAFT:\n" + json.dumps(compact)[:6000]
        )
        for _attempt in range(2):
            try:
                raw = call_gemini_ai(prompt)
            except GeminiError as e:
                log.warning("STAR suggestions: Gemini unavailable: %s", e)
                return fallback, False
            try:
                parsed = _parse_json_safely(raw)
                items = parsed.get("suggestions") if isinstance(parsed, dict) else None
                if isinstance(items, dict) and all(
                        isinstance(items.get(s), list) for s in SECTIONS):
                    cleaned = {s: [str(x)[:400] for x in items[s][:3]]
                               for s in SECTIONS}
                    return cleaned, True
                log.warning("STAR suggestions: model returned unusable shape")
            except (ValueError, json.JSONDecodeError):
                log.warning("STAR suggestions: non-JSON model output, retrying")
        return fallback, False

    @app.route('/star')
    def star_home():
        """STAR builder: competency picker, examples, guided editor."""
        return render_template('star.html', competencies=COMPETENCIES,
                               guide=SECTION_GUIDE,
                               is_authed=current_user.is_authenticated)

    @app.route('/api/star/examples')
    def star_examples_api():
        """Annotated STAR examples, optionally filtered by competency."""
        comp = (request.args.get('competency') or '').strip().lower()[:40]
        if comp and comp not in COMPETENCY_IDS:
            return jsonify({'error': 'Unknown competency.'}), 400
        examples = examples_for(comp) if comp else all_examples()
        return jsonify({'examples': json_safe(examples),
                        'count': len(examples)})

    @app.route('/star/score', methods=['POST'])
    @limiter.limit("10 per hour")
    def star_score():
        """Score a STAR draft per section + rewrite suggestions.

        Login required. CSRF token via X-CSRFToken header.
        """
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to score your answer.'}), 401
        payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
        cleaned, error = validate_draft(payload or {})
        if error:
            return jsonify({'error': error}), 400
        scored = score_draft(cleaned["sections"])
        suggestions, ai_powered = _star_suggestions(
            scored, cleaned["sections"], cleaned["competency"])
        return jsonify({'success': True, 'scores': json_safe(scored),
                        'suggestions': json_safe(suggestions),
                        'ai_powered': ai_powered,
                        'competency': cleaned["competency"]})

    @app.route('/star/save', methods=['POST'])
    @limiter.limit("30 per hour")
    def star_save():
        """Persist a STAR draft (with its latest scores, if scored)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to save drafts.'}), 401
        payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
        cleaned, error = validate_draft(payload or {})
        if error:
            return jsonify({'error': error}), 400
        scores = payload.get("scores") if isinstance(payload, dict) else None
        suggestions = payload.get("suggestions") if isinstance(payload, dict) else None
        try:
            row = STARDraft(
                user_id=current_user.id,
                competency=cleaned["competency"],
                situation=cleaned["sections"]["situation"],
                task=cleaned["sections"]["task"],
                action=cleaned["sections"]["action"],
                result=cleaned["sections"]["result"],
                scores_json=json_safe(scores) if isinstance(scores, dict) else None,
                suggestions_json=json_safe(suggestions) if isinstance(suggestions, dict) else None,
                ai_powered=bool(payload.get("ai_powered")) if isinstance(payload, dict) else False,
            )
            db.session.add(row)
            db.session.commit()
        except Exception:
            log.exception("STAR draft save failed")
            db.session.rollback()
            return jsonify({'error': 'Could not save your draft. Please try again.'}), 500
        # Wave 5 #27: a saved STAR story earns XP.
        _momentum_hook(current_user.id, 'star_saved')
        return jsonify({'success': True, 'draft_id': row.id})

    @app.route('/star/drafts')
    @login_required
    def star_drafts():
        """List the user's saved STAR drafts."""
        rows = (STARDraft.query.filter_by(user_id=current_user.id)
                .order_by(STARDraft.updated_at.desc()).all())
        out = [{
            "id": r.id,
            "competency": r.competency,
            "overall": (r.scores_json or {}).get("overall"),
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            "preview": (r.situation or "")[:120],
        } for r in rows]
        if request.args.get("format") == "json":
            return jsonify({"drafts": out})
        return render_template('star_drafts.html', drafts=out,
                               competencies=dict((c[0], c[1]) for c in COMPETENCIES))

    @app.route('/star/drafts/<int:draft_id>/delete', methods=['POST'])
    @limiter.limit("30 per hour")
    def star_draft_delete(draft_id):
        """Delete one of the user's STAR drafts (owner only)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        row = db.session.get(STARDraft, draft_id)
        if not row or row.user_id != current_user.id:
            return jsonify({'error': 'Draft not found.'}), 404
        try:
            db.session.delete(row)
            db.session.commit()
        except Exception:
            log.exception("STAR draft delete failed")
            db.session.rollback()
            return jsonify({'error': 'Could not delete the draft.'}), 500
        return jsonify({'success': True})

    # ---------- LEARNING HUB (Wave 3) ----------
    @app.route('/learn')
    def learn_hub():
        """Learning hub: roadmaps, skill quizzes, certification guidance.

        For signed-in users the hub also carries a personal "continue
        learning" strip (roadmap stages, quiz bests, open mistakes,
        saved cert plans) and a combined skill/cert search index.
        """
        search_index = (
            [{"label": s["name"], "kind": "Roadmap",
              "url": url_for('roadmap_detail', skill_id=s["id"])}
             for s in roadmap_skills()]
            + [{"label": sid.replace('-', ' ').title(), "kind": "Quiz",
                "url": url_for('quizzes_home')}
               for sid, _ in quiz_skills()]
            + [{"label": c["name"], "kind": "Certification",
                "url": url_for('cert_detail', cert_id=c["id"])}
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
    @app.route('/learn/roadmaps')
    def roadmaps_home():
        """Browse skills + run the skill-gap analyzer."""
        return render_template('roadmaps.html',
                               skills=roadmap_skills(),
                               domains=roadmap_domains(),
                               roles=roadmap_roles(),
                               is_authed=current_user.is_authenticated)

    @app.route('/learn/roadmaps/<skill_id>')
    def roadmap_detail(skill_id):
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

    @app.route('/learn/roadmaps/gap', methods=['POST'])
    @limiter.limit("30 per hour")
    def roadmaps_gap():
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

    @app.route('/learn/roadmaps/progress', methods=['POST'])
    @limiter.limit("60 per hour")
    def roadmaps_progress():
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

    @app.route('/learn/roadmaps/dashboard')
    @login_required
    def roadmaps_dashboard():
        """The user's roadmap progress across skills."""
        rows = RoadmapProgress.query.filter_by(user_id=current_user.id).all()
        return render_template('roadmaps_dashboard.html',
                               summary=roadmap_progress_summary(rows),
                               skills={s["id"]: s for s in roadmap_skills()})

    # ----- Feature #19: skill quizzes -----
    @app.route('/learn/quizzes')
    def quizzes_home():
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

    @app.route('/learn/quizzes/start', methods=['POST'])
    @limiter.limit("20 per hour")
    def quizzes_start():
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

    @app.route('/learn/quizzes/<int:attempt_id>/check', methods=['POST'])
    @limiter.limit("120 per hour")
    def quizzes_check(attempt_id):
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

    @app.route('/learn/quizzes/<int:attempt_id>/submit', methods=['POST'])
    @limiter.limit("20 per hour")
    def quizzes_submit(attempt_id):
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
        summary["roadmap_url"] = url_for('roadmap_detail', skill_id=row.skill)
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
                        'report_url': url_for('view_quiz_result', attempt_id=row.id)})

    @app.route('/learn/quizzes/<int:attempt_id>')
    @login_required
    def view_quiz_result(attempt_id):
        """Full review of a submitted quiz attempt (owner only)."""
        row = QuizAttempt.query.filter_by(id=attempt_id,
                                          user_id=current_user.id).first()
        if not row or row.submitted_at is None:
            abort(404)
        return render_template('quiz_result.html', attempt=row,
                               summary=row.summary_json or {},
                               results=row.results_json or [])

    @app.route('/learn/quizzes/history')
    @login_required
    def quizzes_history():
        """The user's quiz attempts + per-skill best scores."""
        attempts = (QuizAttempt.query
                    .filter(QuizAttempt.user_id == current_user.id,
                            QuizAttempt.submitted_at.isnot(None))
                    .order_by(QuizAttempt.submitted_at.desc()).all())
        return render_template('quiz_history.html', attempts=attempts,
                               best=quiz_best_scores(attempts))

    @app.route('/learn/quizzes/mistakes')
    @login_required
    def quizzes_mistakes():
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

    @app.route('/learn/quizzes/drill', methods=['POST'])
    @limiter.limit("20 per hour")
    def quizzes_drill():
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
    @app.route('/learn/certs')
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

    @app.route('/learn/certs/browse')
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
            "url": url_for('cert_detail', cert_id=c["id"]),
        } for c in certs]
        return jsonify({'success': True, 'certifications': summaries})

    @app.route('/learn/certs/<cert_id>')
    def cert_detail(cert_id):
        """Certification detail + study-plan generator."""
        cert = get_cert(cert_id)
        if not cert:
            abort(404)
        return render_template('cert_detail.html', cert=cert,
                               is_authed=current_user.is_authenticated)

    @app.route('/learn/certs/recommend', methods=['POST'])
    @limiter.limit("30 per hour")
    def certs_recommend():
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

    @app.route('/learn/certs/plan', methods=['POST'])
    @limiter.limit("20 per hour")
    def certs_plan():
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

    @app.route('/learn/certs/plans')
    @login_required
    def certs_plans():
        """The user's saved certification study plans."""
        plans = (CertPlan.query
                 .filter_by(user_id=current_user.id)
                 .order_by(CertPlan.created_at.desc()).all())
        cert_names = {c["id"]: c["name"] for c in all_certs()}
        return render_template('cert_plans.html', plans=plans,
                               cert_names=cert_names)

    @app.route('/results/<analysis_id>')
    def view_result(analysis_id):
        """Render a single analysis result (anonymous DB row or saved DB row)."""
        if analysis_id.startswith('db-'):
            if not current_user.is_authenticated:
                return redirect(url_for('auth.login', next=request.path))
            try:
                row_id = int(analysis_id[3:])
            except ValueError:
                abort(404)
            row = db.session.get(ResumeAnalysis, row_id)
            if not row or row.user_id != current_user.id:
                abort(404)
            return render_template('dashboard.html', result=row.result_json,
                                   is_authed=True)
        anon = db.session.get(AnonymousAnalysis, analysis_id)
        if not anon or anon.expires_at < _utcnow_naive():
            return redirect(url_for('resume_home'))
        return render_template('dashboard.html', result=anon.result_json,
                               is_authed=False)

    @app.route('/dashboard')
    @login_required
    def dashboard():
        """User home: analysis history + career goals."""
        analyses = (ResumeAnalysis.query
                    .filter_by(user_id=current_user.id)
                    .order_by(ResumeAnalysis.created_at.desc()).all())
        jd_matches = (JDMatchReport.query
                      .filter_by(user_id=current_user.id)
                      .order_by(JDMatchReport.created_at.desc()).all())
        rewrites = (BulletRewrite.query
                    .filter_by(user_id=current_user.id)
                    .order_by(BulletRewrite.created_at.desc()).all())
        cover_docs = (CoverDoc.query
                      .filter_by(user_id=current_user.id)
                      .order_by(CoverDoc.created_at.desc()).all())
        linkedin_reports = (LinkedInReport.query
                            .filter_by(user_id=current_user.id)
                            .order_by(LinkedInReport.created_at.desc()).all())
        tracker_stats = _tracker_stats(current_user.id)
        assessments = (CareerAssessment.query
                       .filter_by(user_id=current_user.id)
                       .order_by(CareerAssessment.created_at.desc()).all())
        interviews = (InterviewSession.query
                      .filter_by(user_id=current_user.id)
                      .order_by(InterviewSession.created_at.desc()).all())
        coach_reports = (CommunicationAnalysis.query
                         .filter_by(user_id=current_user.id)
                         .order_by(CommunicationAnalysis.created_at.desc()).all())
        aptitude_attempts = (AptitudeAttempt.query
                             .filter(AptitudeAttempt.user_id == current_user.id,
                                     AptitudeAttempt.submitted_at.isnot(None))
                             .order_by(AptitudeAttempt.created_at.desc()).all())
        star_drafts = (STARDraft.query
                       .filter_by(user_id=current_user.id)
                       .order_by(STARDraft.updated_at.desc())
                       .limit(12).all())
        learn_roadmap = roadmap_progress_summary(
            RoadmapProgress.query.filter_by(user_id=current_user.id).all())
        quiz_attempts = (QuizAttempt.query
                         .filter(QuizAttempt.user_id == current_user.id,
                                 QuizAttempt.submitted_at.isnot(None))
                         .order_by(QuizAttempt.submitted_at.desc())
                         .limit(8).all())
        cert_plans = (CertPlan.query
                      .filter_by(user_id=current_user.id)
                      .order_by(CertPlan.created_at.desc())
                      .limit(6).all())
        cert_names = {c["id"]: c["name"] for c in all_certs()}
        goals = (Goal.query.filter_by(user_id=current_user.id)
                 .order_by(Goal.status.asc(), Goal.created_at.desc()).all())
        # Wave 5 #27: small momentum widget (streak + XP + quest preview).
        # Defensive: the dashboard must render even if momentum fails.
        try:
            momentum_widget = momentum_mod.get_momentum_summary(current_user.id)
        except Exception:
            log.exception("Momentum widget failed (non-fatal)")
            momentum_widget = None
        return render_template('history.html', analyses=analyses, goals=goals,
                               jd_matches=jd_matches, rewrites=rewrites,
                               cover_docs=cover_docs, linkedin_reports=linkedin_reports,
                               tracker_stats=tracker_stats, assessments=assessments,
                               interviews=interviews, coach_reports=coach_reports,
                               aptitude_attempts=aptitude_attempts,
                               star_drafts=star_drafts,
                               learn_roadmap=learn_roadmap,
                               quiz_attempts=quiz_attempts,
                               cert_plans=cert_plans, cert_names=cert_names,
                               momentum_widget=momentum_widget,
                               star_competencies=dict((c[0], c[1]) for c in COMPETENCIES))

    # ---------- INDIA SALARY EXPLORER (Wave 1 #8) ----------
    @app.route('/salary')
    def salary_explorer():
        """Role x city-tier x experience salary explorer (curated indicative ranges)."""
        try:
            options = salary_options()
        except SalaryDataError:
            return render_template('error.html',
                                   message="Salary data is temporarily unavailable. Please try again later."), 503
        return render_template('salary.html', options=options,
                               freshness=salary_freshness(),
                               is_authed=current_user.is_authenticated)

    @app.route('/salary/data')
    @limiter.limit("60 per hour")
    def salary_data_api():
        """JSON feed of salary ranges. Unknown filter values yield empty results."""
        try:
            entries = salary_query(role=request.args.get('role') or None,
                                   tier=request.args.get('tier') or None,
                                   band=request.args.get('band') or None)
        except SalaryDataError:
            return jsonify({'success': False,
                            'error': 'Salary data is temporarily unavailable.'}), 503
        return jsonify({'success': True, 'entries': entries})

    # ---------- OPPORTUNITY RADAR (Wave 1 #9) ----------
    def _opp_labels(o):
        """(deadline_label, countdown_label) for a board card."""
        if not o["deadline"]:
            return "", ""
        y, m, d = (int(p) for p in o["deadline"].split("-"))
        label = date(y, m, d).strftime("%d %b %Y")
        n = o["days_left"]
        countdown = "closes today" if n == 0 else ("1 day left" if n == 1 else f"{n} days left")
        return label, countdown

    @app.route('/opportunities')
    def opportunity_board():
        """Curated board of internships, scholarships and exams, sorted by deadline."""
        opp_type = (request.args.get('type') or '').strip()
        if opp_type not in OPP_TYPES:
            opp_type = ""
        try:
            opps = list_opportunities(opp_type=opp_type or None)
            meta = opp_meta()
        except OpportunityDataError:
            return render_template('error.html', message=(
                "Opportunity data is temporarily unavailable. Please try again later.")), 503
        bookmarked = set()
        saved_only = False
        closing_mine = []
        if current_user.is_authenticated:
            bookmarked = {b.opp_id for b in
                          OpportunityBookmark.query.filter_by(user_id=current_user.id).all()}
            # Bookmarked + closing within the soon window -> alert strip.
            closing_mine = [o for o in opps
                            if o["id"] in bookmarked and o["closing_soon"]]
            saved_only = request.args.get('saved') == '1'
            if saved_only:
                opps = [o for o in opps if o["id"] in bookmarked]
        for o in opps:
            o["deadline_label"], o["countdown_label"] = _opp_labels(o)
        # Recently closed (opt-in): deadlines passed within 30 days, so
        # students can see what they missed and plan for the next cycle.
        show_closed = request.args.get('closed') == '1'
        closed = []
        if show_closed:
            try:
                closed = [o for o in list_opportunities(
                    opp_type=opp_type or None, include_past=True)
                    if o["is_past"] and o["days_left"] is not None
                    and o["days_left"] >= -30]
            except OpportunityDataError:
                closed = []
            for o in closed:
                o["deadline_label"], o["countdown_label"] = _opp_labels(o)
        from studybuddy.opportunities import freshness_label as _freshness_label
        return render_template('opportunities.html', opps=opps, meta=meta,
                               types=OPP_TYPES, active_type=opp_type,
                               saved_only=saved_only, bookmarked=bookmarked,
                               closing_mine=closing_mine,
                               show_closed=show_closed, closed=closed,
                               is_authed=current_user.is_authenticated,
                               freshness=_freshness_label(meta))

    @app.route('/opportunities/bookmark', methods=['POST'])
    @limiter.limit("60 per hour")
    def opportunity_bookmark_add():
        """Bookmark an opportunity. CSRF via X-CSRFToken header (not exempt)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to save opportunities.'}), 401
        payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
        opp_id = ((payload or {}).get('opp_id') or '').strip()
        if not opp_id or not opp_exists(opp_id):
            return jsonify({'error': 'Opportunity not found.'}), 404
        existing = OpportunityBookmark.query.filter_by(
            user_id=current_user.id, opp_id=opp_id).first()
        if not existing:
            db.session.add(OpportunityBookmark(user_id=current_user.id, opp_id=opp_id))
            db.session.commit()
        return jsonify({'success': True, 'opp_id': opp_id}), 201

    @app.route('/opportunities/bookmark/remove', methods=['POST'])
    @limiter.limit("60 per hour")
    def opportunity_bookmark_remove():
        """Remove a bookmark. CSRF via X-CSRFToken header (not exempt)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
        opp_id = ((payload or {}).get('opp_id') or '').strip()
        row = OpportunityBookmark.query.filter_by(
            user_id=current_user.id, opp_id=opp_id).first()
        if not row:
            return jsonify({'error': 'Bookmark not found.'}), 404
        db.session.delete(row)
        db.session.commit()
        return jsonify({'success': True, 'opp_id': opp_id})

    # ---------- PORTFOLIO BUILDER (Wave 1 #12) ----------
    def _portfolio_prefill(user):
        """Prefill data for the builder: name + connected coding profiles."""
        prefill = {"name": user.name or "",
                   "github": {"linked": False}, "leetcode": {"linked": False}}
        if user.github_username:
            snap = ProfileSnapshot.query.filter_by(
                user_id=user.id, provider="github").first()
            score = None
            if snap and snap.data:
                try:
                    score = round(float(snap.data.get("score") or 0))
                except (TypeError, ValueError):
                    score = None
            prefill["github"] = {"linked": True, "username": user.github_username,
                                 "score": score}
        if user.leetcode_username:
            snap = ProfileSnapshot.query.filter_by(
                user_id=user.id, provider="leetcode").first()
            solved = rating = None
            if snap and snap.data:
                solved = ((snap.data.get("solved") or {}).get("total"))
                rating = snap.data.get("rating")
            prefill["leetcode"] = {"linked": True, "username": user.leetcode_username,
                                   "solved": solved, "rating": rating}
        return prefill

    def _portfolio_to_dict(row):
        return {
            "display_name": row.display_name, "headline": row.headline or "",
            "bio": row.bio or "", "skills": row.skills or [],
            "projects": row.projects or [], "links": row.links or [],
            "show_github": row.show_github, "show_leetcode": row.show_leetcode,
            "is_published": row.is_published, "slug": row.slug,
        }

    def _portfolio_public_context(row):
        """Build the render context for the public page / download."""
        github = leetcode = None
        if row.show_github and row.user.github_username:
            snap = ProfileSnapshot.query.filter_by(
                user_id=row.user_id, provider="github").first()
            if snap and snap.data:
                try:
                    score = round(float(snap.data.get("score") or 0))
                except (TypeError, ValueError):
                    score = None
                github = {"username": row.user.github_username, "score": score}
        if row.show_leetcode and row.user.leetcode_username:
            snap = ProfileSnapshot.query.filter_by(
                user_id=row.user_id, provider="leetcode").first()
            if snap and snap.data:
                solved = (snap.data.get("solved") or {}).get("total")
                leetcode = {"username": row.user.leetcode_username,
                            "solved_total": solved,
                            "rating": snap.data.get("rating")}
        return {"portfolio": _portfolio_to_dict(row),
                "github": github, "leetcode": leetcode}

    @app.route('/portfolio')
    @login_required
    def portfolio_builder():
        """Portfolio builder page (login required)."""
        row = Portfolio.query.filter_by(user_id=current_user.id).first()
        return render_template('portfolio_builder.html',
                               portfolio=row,
                               portfolio_payload=_portfolio_to_dict(row) if row else None,
                               prefill=_portfolio_prefill(current_user))

    @app.route('/portfolio/save', methods=['POST'])
    @limiter.limit("20 per hour")
    def portfolio_save():
        """Create/update the user's portfolio. CSRF via X-CSRFToken header."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to build your portfolio.'}), 401
        payload = request.get_json(silent=True)
        data, error = validate_portfolio_payload(payload or {})
        if error:
            return jsonify({'error': error}), 400
        # Only feature coding profiles the user actually connected.
        if data["show_github"] and not current_user.github_username:
            data["show_github"] = False
        if data["show_leetcode"] and not current_user.leetcode_username:
            data["show_leetcode"] = False
        row = Portfolio.query.filter_by(user_id=current_user.id).first()
        if row is None:
            taken = {p.slug for p in Portfolio.query.with_entities(Portfolio.slug).all()}
            row = Portfolio(user_id=current_user.id,
                            slug=unique_slug(data["display_name"], taken))
            db.session.add(row)
        row.display_name = data["display_name"]
        row.headline = data["headline"]
        row.bio = data["bio"]
        row.skills = data["skills"]
        row.projects = data["projects"]
        row.links = data["links"]
        row.show_github = data["show_github"]
        row.show_leetcode = data["show_leetcode"]
        row.is_published = data["is_published"]
        row.updated_at = datetime.now(UTC)
        db.session.commit()
        return jsonify({'success': True, 'slug': row.slug,
                        'is_published': row.is_published,
                        'public_url': url_for('public_portfolio', slug=row.slug,
                                              _external=True)})

    @app.route('/p/<slug>')
    def public_portfolio(slug):
        """Public portfolio page — only what the user chose to publish."""
        row = Portfolio.query.filter_by(slug=slug).first()
        if not row or not row.is_published:
            abort(404)
        return render_template('portfolio_public.html',
                               **_portfolio_public_context(row))

    @app.route('/portfolio/download')
    @login_required
    def portfolio_download():
        """Download the portfolio as a standalone HTML file."""
        row = Portfolio.query.filter_by(user_id=current_user.id).first_or_404()
        html = render_template('portfolio_public.html',
                               **_portfolio_public_context(row))
        return Response(html, mimetype='text/html', headers={
            'Content-Disposition':
                f'attachment; filename="portfolio-{row.slug}.html"'})

    # ---------- GOALS ----------
    @app.route('/goals/add', methods=['POST'])
    @login_required
    def goal_add():
        title = (request.form.get('title') or '').strip()
        target_raw = (request.form.get('target_date') or '').strip()
        if not title:
            flash('Goal title cannot be empty.', 'error')
            return redirect(url_for('dashboard'))
        target_date = None
        if target_raw:
            try:
                target_date = date.fromisoformat(target_raw)
            except ValueError:
                flash('Invalid target date.', 'error')
                return redirect(url_for('dashboard'))
        goal = Goal(user_id=current_user.id, title=title, target_date=target_date)
        db.session.add(goal)
        db.session.commit()
        flash('Goal added.', 'success')
        return redirect(url_for('dashboard'))

    @app.route('/goals/<int:goal_id>/toggle', methods=['POST'])
    @login_required
    def goal_toggle(goal_id):
        goal = Goal.query.filter_by(id=goal_id, user_id=current_user.id).first_or_404()
        goal.status = 'done' if goal.status == 'active' else 'active'
        db.session.commit()
        return redirect(url_for('dashboard'))

    @app.route('/goals/<int:goal_id>/delete', methods=['POST'])
    @login_required
    def goal_delete(goal_id):
        goal = Goal.query.filter_by(id=goal_id, user_id=current_user.id).first_or_404()
        db.session.delete(goal)
        db.session.commit()
        flash('Goal removed.', 'info')
        return redirect(url_for('dashboard'))

    @app.route('/practice')
    def practice():
        """Interview and coding practice page.

        The interview tab is served from the JSON mock-interview bank
        (single source of truth); the legacy company_questions CSV is
        no longer used here.
        """
        hub = interview_hub_data()
        return render_template('practice.html',
                               companies_interview=hub["companies"],
                               companies_coding=get_coding_companies(),
                               topics=get_available_topics(),
                               company_stats=hub["stats"],
                               hub_stats=hub,
                               coding_stats=get_company_coding_stats())

    # ---------- RESEARCH ASSISTANT ----------
    @app.route('/research')
    def research_home():
        """Research hub. Signed-in users also get their personal
        research strip (library size, decks due, reviews, reports)."""
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

    # ---------- STUDY INTELLIGENCE (Wave 4 batch 1) ----------
    # Feature #21: source-grounded PDF Q&A with citations (NotebookLM-style)
    @app.route('/research/library')
    @login_required
    def study_library():
        """Per-user PDF study library page."""
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

    @app.route('/study/api/upload', methods=['POST'])
    @limiter.limit("20 per hour")
    def study_upload():
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
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
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

    @app.route('/study/api/ask', methods=['POST'])
    @limiter.limit("60 per hour")
    def study_ask():
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to continue.'}), 401
        """Ask a cited question over one doc (doc_id) or the whole library.

        JSON body: {"doc_id": int|null, "question": str}.
        CSRF token via X-CSRFToken header (not exempt).
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
                candidates = [doc]
            else:
                candidates = StudyDoc.query.filter_by(
                    user_id=current_user.id).order_by(
                    StudyDoc.created_at.desc()).all()
                if not candidates:
                    return jsonify({'error': 'Your library is empty — upload a PDF first.'}), 400
            # Pick the best-scoring doc for multi-doc queries (citation-clean).
            # If chunks exist but nothing matches, still answer from the first
            # doc so answer_question() can abstain honestly instead of 400ing.
            best_doc, best_chunks, best_score = None, [], -1.0
            first_with_chunks = None
            for d in candidates:
                chunks = (d.data or {}).get('chunks') or []
                if not chunks:
                    continue
                if first_with_chunks is None:
                    first_with_chunks = (d, chunks)
                hits = pdfqa_retrieve(chunks, question, top_k=1)
                score = hits[0]['score'] if hits else -1.0
                if score > best_score:
                    best_doc, best_chunks, best_score = d, chunks, score
            if first_with_chunks is None:
                return jsonify({'error': 'No indexed passages found in your library.'}), 400
            if best_doc is None:
                best_doc, best_chunks = first_with_chunks
            result = pdfqa_answer(question, best_doc.title, best_chunks, top_k=5)
            q = StudyQuestion(user_id=current_user.id, doc_id=best_doc.id,
                              question=question[:2000],
                              answer=result.get('answer'),
                              citations=result.get('citations'))
            db.session.add(q)
            db.session.commit()
        except Exception:
            log.exception("Study ask failed")
            db.session.rollback()
            return jsonify({'error': 'Could not answer right now. Please try again.'}), 500
        return jsonify({'success': True, 'doc_id': best_doc.id,
                        'doc_title': best_doc.title,
                        'answer': result.get('answer'),
                        'citations': result.get('citations') or []})

    @app.route('/study/api/documents/<int:doc_id>/history')
    def study_history(doc_id):
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

    @app.route('/study/api/documents/<int:doc_id>', methods=['DELETE'])
    def study_delete(doc_id):
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to continue.'}), 401
        """Delete a doc, its stored file, and its question history."""
        doc = StudyDoc.query.filter_by(id=doc_id, user_id=current_user.id).first()
        if not doc:
            return jsonify({'error': 'Document not found.'}), 404
        try:
            if doc.stored_filename:
                try:
                    os.remove(os.path.join(app.config['UPLOAD_FOLDER'],
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

    # Feature #25: arXiv/DOI import + citation graph
    @app.route('/research/import')
    @login_required
    def study_import_page():
        """Paper import page (arXiv / DOI)."""
        papers = PaperImport.query.filter_by(user_id=current_user.id).order_by(
            PaperImport.created_at.desc()).limit(50).all()
        return render_template('study_import.html', papers=[{
            'id': p.id, 'title': p.title, 'authors': p.authors or [],
            'year': p.year, 'venue': p.venue, 'doi': p.doi,
            'arxiv_id': p.arxiv_id, 'openalex_id': p.openalex_id,
            'source': p.source} for p in papers])

    def _store_paper_import(meta):
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
            fetched_at=_fetched)
        db.session.add(paper)
        db.session.commit()
        return paper

    @app.route('/api/study/import/arxiv', methods=['POST'])
    @limiter.limit("30 per hour")
    def study_import_arxiv():
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
            paper = _store_paper_import(meta)
        except Exception:
            log.exception("arXiv import store failed")
            db.session.rollback()
            return jsonify({'error': 'Could not save this paper.'}), 500
        meta['import_id'] = paper.id
        return jsonify(json_safe(meta))

    @app.route('/api/study/import/doi', methods=['POST'])
    @limiter.limit("30 per hour")
    def study_import_doi():
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
            paper = _store_paper_import(meta)
        except Exception:
            log.exception("DOI import store failed")
            db.session.rollback()
            return jsonify({'error': 'Could not save this paper.'}), 500
        meta['import_id'] = paper.id
        return jsonify(json_safe(meta))

    @app.route('/api/study/import/<path:openalex_id>/graph')
    @limiter.limit("30 per hour")
    def study_import_graph(openalex_id):
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to continue.'}), 401
        """Citation graph around an imported paper (cached 24h)."""
        from urllib.parse import unquote
        graph = fetch_citation_graph(unquote(openalex_id), max_nodes=25)
        if graph.get('error'):
            return jsonify({'error': graph['error']}), 502
        return jsonify(json_safe(graph))

    @app.route('/api/study/import/pdf', methods=['POST'])
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
        filename = f"{uuid.uuid4().hex}.pdf"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        try:
            downloaded = arxiv_download_pdf(pdf_url, filepath)
            if not downloaded and arxiv_id:
                # OpenAlex sometimes points at mirrors that block direct
                # download; arXiv's own PDF URL is deterministic — try it.
                downloaded = arxiv_download_pdf(
                    f"https://arxiv.org/pdf/{arxiv_id}", filepath)
            if not downloaded:
                return jsonify({'error': 'Could not download the PDF.'}), 502
            pages = pdfqa_parse_pages(filepath)
            if not any((p or '').strip() for p in pages):
                raise ValueError("no-text")
            chunks = pdfqa_chunk_pages(pages)
            doc = StudyDoc(
                user_id=current_user.id,
                title=(payload.get('title') or 'Imported paper').strip()[:255],
                stored_filename=filename,
                source='arxiv' if arxiv_id else 'doi',
                source_ref=arxiv_id or doi,
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
                return jsonify({'error': 'No extractable text found in this PDF.'}), 400
            raise
        except Exception:
            log.exception("Study import-PDF failed")
            db.session.rollback()
            try:
                os.remove(filepath)
            except OSError:
                pass
            return jsonify({'error': 'Could not import this PDF into your library.'}), 500
        return jsonify({'success': True, 'doc': {
            'id': doc.id, 'title': doc.title, 'pages': doc.num_pages,
            'chunks': doc.num_chunks}})

    # Feature #24: citation formatter (IEEE/APA/MLA/Vancouver/Chicago + BibTeX)
    @app.route('/research/cite')
    def cite_page():
        """Citation formatter page (public utility)."""
        return render_template('study_cite.html')

    # ================= Live Data Engine: trending research =================
    @app.route('/research/trending')
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

    @app.route('/research/review')
    @login_required
    def litreview_home():
        """Literature review generator page."""
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

    @app.route('/research/api/review/generate', methods=['POST'])
    @limiter.limit("10 per hour")
    def litreview_generate():
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

    @app.route('/research/review/<int:rid>')
    @login_required
    def litreview_view(rid):
        """View a saved literature review."""
        lr = LitReview.query.filter_by(id=rid, user_id=current_user.id).first_or_404()
        return render_template('litreview_view.html', review=lr)

    @app.route('/research/review/<int:rid>/export')
    @login_required
    def litreview_export(rid):
        """Download a review as markdown or HTML."""
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

    @app.route('/research/api/review/<int:rid>', methods=['DELETE'])
    @limiter.limit("30 per hour")
    def litreview_delete(rid):
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
    @app.route('/research/gaps')
    @login_required
    def gapfinder_home():
        """Research-gap finder page (replaces stub)."""
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

    @app.route('/research/api/gaps/analyze', methods=['POST'])
    @limiter.limit("10 per hour")
    def gaps_analyze():
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

    @app.route('/research/api/gaps/<int:gid>')
    @login_required
    def gaps_get(gid):
        """Fetch a saved gap report as JSON."""
        gr = GapReport.query.filter_by(id=gid, user_id=current_user.id).first_or_404()
        return jsonify({'success': True, 'report': gr.data or {},
                        'doc_ids': gr.doc_ids or []})

    # ================= Wave 4 #26: flashcards & paper quizzes =================
    @app.route('/research/flashcards')
    @login_required
    def flashcards_home():
        """Flashcards & quizzes hub (replaces stub)."""
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

    @app.route('/research/api/flashcards/generate', methods=['POST'])
    @limiter.limit("10 per hour")
    def flashcards_generate():
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

    @app.route('/research/flashcards/<int:sid>')
    @login_required
    def flashcards_review(sid):
        """Spaced-repetition review + quiz UI for one deck."""
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

    @app.route('/research/api/flashcards/<int:sid>/progress', methods=['POST'])
    @limiter.limit("60 per hour")
    def flashcards_progress(sid):
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

    @app.route('/research/api/flashcards/<int:sid>/quiz', methods=['POST'])
    @limiter.limit("30 per hour")
    def flashcards_quiz(sid):
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

    @app.route('/research/api/flashcards/<int:sid>/stats')
    @login_required
    def flashcards_stats(sid):
        """Mastery stats for a deck."""
        fs = FlashcardSet.query.filter_by(id=sid,
                                          user_id=current_user.id).first_or_404()
        return jsonify({'success': True,
                        'stats': fc_mastery_stats(fs.data or {})})

    @app.route('/api/cite/format', methods=['POST'])
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

    @app.route('/api/cite/bibtex/export', methods=['POST'])
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

    @app.route('/api/cite/bibtex/import', methods=['POST'])
    @limiter.limit("60 per hour")
    def cite_bibtex_import():
        """Parse pasted BibTeX. JSON body: {"text": "..."}."""
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

    @app.route('/api/cite/guide/<style>')
    def cite_guide(style):
        """In-app style-guide reference for one citation style."""
        style = (style or '').strip().lower()
        if style not in CITATION_STYLES:
            return jsonify({'error': 'Unknown style.'}), 400
        try:
            guide = style_guide(style)
        except Exception:
            log.exception("Cite guide failed")
            return jsonify({'error': 'Guide unavailable.'}), 500
        return jsonify(json_safe(guide))

    # ================= Wave 5 #27: humane momentum system =================
    @app.route('/momentum')
    @login_required
    def momentum_hub():
        """Momentum hub: streak ring, level/XP, quests, league board,
        weekly reflection, opt-out. Forgiveness-first: shields, graceful
        resets, day-one win banner, rejoin CTA when opted out."""
        try:
            summary = momentum_mod.get_momentum_summary(current_user.id)
        except Exception:
            log.exception("Momentum hub failed")
            summary = momentum_mod.empty_summary()
        return render_template('momentum_hub.html', m=summary)

    @app.route('/momentum/api/summary')
    @login_required
    def momentum_summary_api():
        """JSON summary for the dashboard widget."""
        try:
            summary = momentum_mod.get_momentum_summary(current_user.id)
        except Exception:
            log.exception("Momentum summary API failed")
            summary = momentum_mod.empty_summary()
        return jsonify({'success': True, 'summary': summary})

    @app.route('/momentum/api/quest/<quest_id>/claim', methods=['POST'])
    @limiter.limit("30 per hour")
    def momentum_claim_quest(quest_id):
        """Claim a completed weekly quest's 50 XP bonus (idempotent).

        CSRF via X-CSRFToken header (not exempt)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        result = momentum_mod.claim_quest(current_user.id, quest_id)
        if not result.get('ok'):
            reason = result.get('reason') or 'claim failed'
            if reason == 'not completed':
                return jsonify({'error': 'Quest not completed yet.',
                                'count': result.get('count'),
                                'target': result.get('target')}), 400
            if reason == 'unknown quest':
                return jsonify({'error': 'Unknown quest.'}), 404
            return jsonify({'error': 'Could not claim the quest.'}), 400
        return jsonify({'success': True, 'xp': result.get('xp', 0),
                        'duplicate': bool(result.get('duplicate')),
                        'skipped': bool(result.get('skipped'))})

    @app.route('/momentum/api/league/join', methods=['POST'])
    @limiter.limit("30 per hour")
    def momentum_league_join():
        """Opt into this ISO week's league cohort (idempotent).

        CSRF via X-CSRFToken header (not exempt)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        result = momentum_mod.join_league(current_user.id)
        if not result.get('ok'):
            if result.get('reason') == 'opted out':
                return jsonify({'error': 'Rejoin Momentum first to join a league.'}), 400
            return jsonify({'error': 'Could not join the league.'}), 400
        return jsonify({'success': True,
                        'league_key': result.get('league_key'),
                        'duplicate': bool(result.get('duplicate'))})

    @app.route('/momentum/api/league/leave', methods=['POST'])
    @limiter.limit("30 per hour")
    def momentum_league_leave():
        """Opt out of the league board (history kept). CSRF via X-CSRFToken
        header (not exempt)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        result = momentum_mod.leave_league(current_user.id)
        if not result.get('ok'):
            return jsonify({'error': 'Could not leave the league.'}), 400
        return jsonify({'success': True})

    @app.route('/momentum/api/reflect', methods=['POST'])
    @limiter.limit("30 per hour")
    def momentum_reflect():
        """Save this week's 2-minute reflection (upsert).

        JSON body: {"wins": str, "focus_next": str}. CSRF via X-CSRFToken
        header (not exempt)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        payload = request.get_json(silent=True) or {}
        result = momentum_mod.save_reflection(
            current_user.id, payload.get('wins'), payload.get('focus_next'))
        if not result.get('ok'):
            reason = result.get('reason') or 'save failed'
            if reason == 'empty reflection':
                return jsonify({'error': 'Write a line or two first.'}), 400
            if reason == 'opted out':
                return jsonify({'error': 'Rejoin Momentum to log reflections.'}), 400
            return jsonify({'error': 'Could not save your reflection.'}), 400
        return jsonify({'success': True,
                        'week_start': result.get('week_start')})

    @app.route('/momentum/api/opt-out', methods=['POST'])
    @limiter.limit("30 per hour")
    def momentum_opt_out():
        """Opt out of (or back into) Momentum. JSON body: {"opt_out": bool}.

        When opted out, record_event() is a no-op and the hub shows a
        rejoin CTA. CSRF via X-CSRFToken header (not exempt)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        payload = request.get_json(silent=True) or {}
        result = momentum_mod.set_opt_out(current_user.id,
                                          bool(payload.get('opt_out')))
        if not result.get('ok'):
            return jsonify({'error': 'Could not update your preference.'}), 400
        return jsonify({'success': True,
                        'opted_out': result.get('opted_out')})

    # ================= Wave 5 #29: rejection retrospectives =================
    def _retro_get_app_or_json404(application_id):
        """The user's own application, or None (caller returns a graceful
        404 — including for other users' applications)."""
        return JobApplication.query.filter_by(id=application_id,
                                              user_id=current_user.id).first()

    @app.route('/retrospectives')
    @login_required
    def retrospectives_hub():
        """Hub: funnel visualization, breakdowns, hypothesis-labeled
        suggestions, recent retrospectives, experiment log.

        Trust rule: every suggestion card carries "Observed pattern" /
        "Plausible cause (hypothesis)" / "Recommended test" — nothing is
        presented as a fact."""
        try:
            apps = JobApplication.query.filter_by(user_id=current_user.id).all()
            retros = (Retrospective.query.filter_by(user_id=current_user.id)
                      .order_by(Retrospective.updated_at.desc()).all())
            experiments = (Experiment.query.filter_by(user_id=current_user.id)
                           .order_by(Experiment.created_at.desc())
                           .limit(20).all())
            funnel = retro_funnel_stats(apps)
            roles = retro_role_breakdown(apps)
            rej_stages = retro_rejection_stage_breakdown(retros)
            sources = retro_source_breakdown(apps)
            suggestions = retro_suggestions(funnel, roles, rej_stages, sources)
            recent = []
            for r in retros[:10]:
                a = r.application
                recent.append({
                    "id": r.id,
                    "company": a.company if a else "—",
                    "role": a.role if a else "—",
                    "stage_reached": r.stage_reached,
                    "completed": r.completed,
                    "updated_at": r.updated_at,
                    "wizard_url": (url_for('retro_wizard', application_id=r.application_id)
                                   if a else None),
                })
            ctx = {
                "funnel": funnel, "roles": roles,
                "rejection_stages": rej_stages, "sources": sources,
                "suggestions": suggestions, "recent": recent,
                "experiments": [_retro_experiment_to_dict(e) for e in experiments],
                "total_retros": len(retros),
                "small_n": RETRO_SMALL_SAMPLE_N,
            }
        except Exception:
            log.exception("Retrospectives hub failed")
            ctx = {
                "funnel": retro_funnel_stats([]), "roles": [],
                "rejection_stages": retro_rejection_stage_breakdown([]),
                "sources": [], "suggestions": [], "recent": [],
                "experiments": [], "total_retros": 0,
                "small_n": RETRO_SMALL_SAMPLE_N,
            }
        return render_template('retrospectives_hub.html', **ctx)

    @app.route('/retrospectives/new/<int:application_id>')
    @login_required
    def retro_wizard(application_id):
        """4-step guided wizard for one rejection. Graceful 404 unless the
        application belongs to the current user."""
        app_row = _retro_get_app_or_json404(application_id)
        if app_row is None:
            abort(404)
        try:
            retro = (Retrospective.query
                     .filter_by(application_id=app_row.id,
                                user_id=current_user.id).first())
            apps = JobApplication.query.filter_by(user_id=current_user.id).all()
            funnel = retro_funnel_stats(apps)
        except Exception:
            log.exception("Retrospective wizard failed")
            retro, funnel = None, retro_funnel_stats([])
        # Split the stored "Timeline: ..." head back out for the step-1 form.
        timeline_prefill, facts_prefill = "", ""
        if retro and retro.facts:
            facts_prefill = retro.facts
            if facts_prefill.startswith("Timeline:"):
                head, sep, rest = facts_prefill.partition("\n\n")
                if sep:
                    timeline_prefill = head[len("Timeline:"):].strip()
                    facts_prefill = rest
        return render_template(
            'retro_wizard.html',
            application=app_row,
            retro=retro,
            funnel=funnel,
            timeline_prefill=timeline_prefill,
            facts_prefill=facts_prefill,
            starters=retro_data.starters_by_category(),
            templates=retro_data.templates_by_category(),
            categories=retro_data.RETRO_CATEGORIES,
            stages=TRACKER_STAGES,
        )

    @app.route('/retrospectives/api/save', methods=['POST'])
    @login_required
    @limiter.limit("60 per hour")
    def retro_save():
        """Save one wizard step (upsert the per-application Retrospective).

        JSON: {"application_id": int, "step": "facts"|"patterns"|"hypotheses"|"experiment",
               "facts": str, "stage_reached": str, "timeline": str,
               "observed_patterns": str, "hypotheses": str,
               "experiment": {"hypothesis":..,"change":..,"target":..,"duration":..,"experiment_id":..}}
        CSRF via X-CSRFToken header (not exempt)."""
        payload = request.get_json(silent=True) or {}
        step = (payload.get('step') or '').strip()
        if step not in RETRO_WIZARD_STEPS:
            return jsonify({'error': 'Invalid step.'}), 400
        try:
            application_id = int(payload.get('application_id') or 0)
        except (TypeError, ValueError):
            return jsonify({'error': 'Invalid application.'}), 400
        app_row = _retro_get_app_or_json404(application_id)
        if app_row is None:
            return jsonify({'error': 'Application not found.'}), 404

        def _text(value, limit=RETRO_MAX_TEXT):
            return (value or '').strip()[:limit]

        retro = (Retrospective.query
                 .filter_by(application_id=app_row.id,
                            user_id=current_user.id).first())
        if retro is None:
            retro = Retrospective(application_id=app_row.id,
                                  user_id=current_user.id)
            db.session.add(retro)

        try:
            if step == 'facts':
                facts = _text(payload.get('facts'))
                timeline = _text(payload.get('timeline'))
                stage_reached = (payload.get('stage_reached') or '').strip().lower()
                if stage_reached and stage_reached not in TRACKER_STAGE_IDS:
                    return jsonify({'error': 'Invalid stage reached.'}), 400
                retro.stage_reached = stage_reached or None
                # Timeline is stored at the head of the facts text so the
                # wizard can re-render from a single field.
                if timeline and not facts.startswith('Timeline:'):
                    facts = 'Timeline: ' + timeline + ('\n\n' + facts if facts else '')
                retro.facts = facts or None
            elif step == 'patterns':
                retro.observed_patterns = _text(payload.get('observed_patterns')) or None
            elif step == 'hypotheses':
                retro.hypotheses = _text(payload.get('hypotheses')) or None
            elif step == 'experiment':
                exp_payload = payload.get('experiment') or {}
                hypothesis = _text(exp_payload.get('hypothesis'), 2000)
                change = _text(exp_payload.get('change'), 2000)
                target = _text(exp_payload.get('target'), 1000)
                duration = (exp_payload.get('duration') or '').strip()[:50]
                if not hypothesis or not change or not target:
                    return jsonify({'error': 'Hypothesis, change, and target '
                                             'are all required.'}), 400
                experiment_id = exp_payload.get('experiment_id')
                exp_row = None
                if experiment_id not in (None, ''):
                    try:
                        experiment_id = int(experiment_id)
                    except (TypeError, ValueError):
                        return jsonify({'error': 'Invalid experiment.'}), 400
                    exp_row = Experiment.query.filter_by(
                        id=experiment_id, user_id=current_user.id).first()
                    if exp_row is None:
                        return jsonify({'error': 'Experiment not found.'}), 404
                if exp_row is None:
                    exp_row = Experiment(
                        user_id=current_user.id,
                        hypothesis=hypothesis, change=change, target=target,
                        duration=duration or None, status='planned',
                        linked_application_id=app_row.id)
                    db.session.add(exp_row)
                else:
                    exp_row.hypothesis = hypothesis
                    exp_row.change = change
                    exp_row.target = target
                    exp_row.duration = duration or None
            db.session.commit()
        except Exception:
            log.exception("Retrospective save failed")
            db.session.rollback()
            return jsonify({'error': 'Could not save this step.'}), 500
        return jsonify({'success': True, 'step': step,
                        'retrospective_id': retro.id,
                        'completed': retro.completed})

    @app.route('/retrospectives/api/complete/<int:application_id>', methods=['POST'])
    @login_required
    @limiter.limit("60 per hour")
    def retro_complete(application_id):
        """Finalize a retrospective: mark complete and award the
        'retrospective_logged' momentum XP (once per application).
        CSRF via X-CSRFToken header (not exempt)."""
        app_row = _retro_get_app_or_json404(application_id)
        if app_row is None:
            return jsonify({'error': 'Application not found.'}), 404
        try:
            retro = (Retrospective.query
                     .filter_by(application_id=app_row.id,
                                user_id=current_user.id).first())
            if retro is None:
                retro = Retrospective(application_id=app_row.id,
                                      user_id=current_user.id)
                db.session.add(retro)
            first = not retro.completed
            retro.completed = True
            retro.completed_at = datetime.now(UTC)
            db.session.commit()
        except Exception:
            log.exception("Retrospective completion failed")
            db.session.rollback()
            return jsonify({'error': 'Could not complete the retrospective.'}), 500
        if first:
            # Worker 1's helper: calls momentum.record_event (never raises).
            _momentum_hook(current_user.id, 'retrospective_logged')
        return jsonify({'success': True, 'first_completion': first,
                        'xp': 15 if first else 0})

    @app.route('/retrospectives/api/funnel')
    @login_required
    def retro_funnel_api():
        """JSON funnel stats for chart rendering."""
        try:
            apps = JobApplication.query.filter_by(user_id=current_user.id).all()
            funnel = retro_funnel_stats(apps)
        except Exception:
            log.exception("Retrospective funnel API failed")
            funnel = retro_funnel_stats([])
        return jsonify({'success': True, 'funnel': funnel})

    @app.route('/retrospectives/api/experiments', methods=['POST'])
    @login_required
    @limiter.limit("60 per hour")
    def retro_experiment_create():
        """Create an experiment from a template or custom values. The
        hypothesis is stored as a hypothesis, never as a fact.
        CSRF via X-CSRFToken header (not exempt)."""
        payload = request.get_json(silent=True) or {}
        hypothesis = (payload.get('hypothesis') or '').strip()
        change = (payload.get('change') or '').strip()
        target = (payload.get('target') or '').strip()
        duration = (payload.get('duration') or '').strip()[:50]
        if not hypothesis or not change or not target:
            return jsonify({'error': 'Hypothesis, change, and target are '
                                     'all required.'}), 400
        if len(hypothesis) > 2000 or len(change) > 2000 or len(target) > 1000:
            return jsonify({'error': 'One of the fields is too long.'}), 400
        linked_id = payload.get('linked_application_id')
        linked_row = None
        if linked_id not in (None, ''):
            try:
                linked_id = int(linked_id)
            except (TypeError, ValueError):
                return jsonify({'error': 'Invalid linked application.'}), 400
            linked_row = _retro_get_app_or_json404(linked_id)
            if linked_row is None:
                return jsonify({'error': 'Application not found.'}), 404
        try:
            exp = Experiment(
                user_id=current_user.id, hypothesis=hypothesis,
                change=change, target=target, duration=duration or None,
                status='planned',
                linked_application_id=linked_row.id if linked_row else None)
            db.session.add(exp)
            db.session.commit()
        except Exception:
            log.exception("Experiment creation failed")
            db.session.rollback()
            return jsonify({'error': 'Could not create the experiment.'}), 500
        return jsonify({'success': True,
                        'experiment': _retro_experiment_to_dict(exp)}), 201

    @app.route('/retrospectives/api/experiments/<int:exp_id>/result', methods=['POST'])
    @login_required
    @limiter.limit("60 per hour")
    def retro_experiment_result(exp_id):
        """Log an experiment outcome and/or move it along
        planned -> running -> done. CSRF via X-CSRFToken header (not exempt)."""
        exp = Experiment.query.filter_by(id=exp_id,
                                         user_id=current_user.id).first()
        if exp is None:
            return jsonify({'error': 'Experiment not found.'}), 404
        payload = request.get_json(silent=True) or {}
        status = (payload.get('status') or '').strip()
        if status not in RETRO_EXPERIMENT_STATUSES:
            return jsonify({'error': 'Invalid status.'}), 400
        result = (payload.get('result') or '').strip()
        if len(result) > RETRO_MAX_TEXT:
            return jsonify({'error': 'Result is too long.'}), 400
        if (status != exp.status
                and status not in RETRO_STATUS_TRANSITIONS.get(exp.status, ())):
            return jsonify({'error': 'Cannot move an experiment from %s to %s.'
                                     % (exp.status, status)}), 400
        try:
            exp.status = status
            if result:
                exp.result = result
            db.session.commit()
        except Exception:
            log.exception("Experiment result save failed")
            db.session.rollback()
            return jsonify({'error': 'Could not save the outcome.'}), 500
        return jsonify({'success': True,
                        'experiment': _retro_experiment_to_dict(exp)})

    @app.route('/retrospectives/api/experiments')
    @login_required
    def retro_experiments_list():
        """List the user's experiments, newest first."""
        try:
            exps = (Experiment.query.filter_by(user_id=current_user.id)
                    .order_by(Experiment.created_at.desc()).all())
            items = [_retro_experiment_to_dict(e) for e in exps]
        except Exception:
            log.exception("Experiment list failed")
            items = []
        return jsonify({'success': True, 'experiments': items})

    # ---------- PEER MOCK INTERVIEWS (Wave 5 #28) ----------
    # Structured practice: share-token rooms, question cards from the shared
    # 339-question interview bank, per-question timers, typed answers, role
    # swaps, lightweight scheduling, reciprocal rubric feedback, XP on finish,
    # and report/block safety. All POSTs rate-limited; CSRF via the
    # X-CSRFToken header (not exempt).

    def _peer_api_error(message, code):
        return jsonify({'error': message}), code

    @app.route('/peer')
    @login_required
    def peer_hub():
        """Hub: create-room form, open-rooms board, and my rooms."""
        me = current_user.id
        blocked_ids = _peer_blocked_ids(me)
        try:
            open_q = (PeerRoom.query
                      .filter(PeerRoom.status == 'open',
                              PeerRoom.invite_only.is_(False),
                              PeerRoom.host_id != me)
                      .order_by(PeerRoom.created_at.desc())
                      .limit(60))
            open_rooms = [r for r in open_q.all()
                          if r.host_id not in blocked_ids]
            my_rooms = (PeerRoom.query
                        .filter((PeerRoom.host_id == me)
                                | (PeerRoom.guest_id == me))
                        .order_by(PeerRoom.created_at.desc())
                        .limit(40).all())
        except Exception:
            log.exception("Peer hub query failed")
            open_rooms, my_rooms = [], []
        try:
            bank_total = len(_peer_bank() or [])
        except Exception:
            bank_total = 0
        return render_template('peer_hub.html', open_rooms=open_rooms,
                               my_rooms=my_rooms, roles=PEER_ROLES,
                               categories=PEER_CATEGORIES,
                               companies=_peer_companies(),
                               bank_total=bank_total,
                               my_feedback=_peer_feedback_received(me),
                               rubric_dims=PEER_RUBRIC_DIMS,
                               nav_active='practice')

    @app.route('/peer/api/rooms', methods=['POST'])
    @login_required
    @limiter.limit("20 per hour")
    def peer_create_room():
        """Create a room: JSON {role, category, company?, difficulty?,
        num_questions?, invite_only?, scheduled_at?}.

        Returns the share token + room URL. Draws the first question round
        from the shared interview bank immediately.
        """
        payload = request.get_json(silent=True) or {}
        role = (payload.get('role') or 'general').strip().lower()
        category = (payload.get('category') or 'behavioral').strip().lower()
        if role not in PEER_ROLES:
            return _peer_api_error('Unknown role. Pick one of: %s.'
                                   % ', '.join(PEER_ROLES), 400)
        if category not in PEER_CATEGORIES:
            return _peer_api_error('Unknown category. Pick one of: %s.'
                                   % ', '.join(PEER_CATEGORIES), 400)
        company = (payload.get('company') or '').strip() or None
        if company and company not in dict(_peer_companies()):
            return _peer_api_error('Unknown company.', 400)
        difficulty = (payload.get('difficulty') or '').strip() or None
        if difficulty and difficulty not in PEER_DIFFICULTIES:
            return _peer_api_error('Unknown difficulty. Pick one of: %s.'
                                   % ', '.join(PEER_DIFFICULTIES), 400)
        try:
            num_questions = int(payload.get('num_questions')
                                or N_PEER_QUESTIONS)
        except (TypeError, ValueError):
            return _peer_api_error('num_questions must be a number.', 400)
        if not 3 <= num_questions <= 12:
            return _peer_api_error('num_questions must be between 3 and '
                                   '12.', 400)
        invite_only = bool(payload.get('invite_only'))
        scheduled_at = None
        if payload.get('scheduled_at'):
            scheduled_at = _peer_parse_slot(payload.get('scheduled_at'))
            if scheduled_at is None:
                return _peer_api_error(
                    'scheduled_at must look like "YYYY-MM-DD HH:MM".', 400)
        try:
            token = _peer_token()
            questions = _peer_questions(role, category, n=num_questions,
                                        company=company,
                                        difficulty=difficulty)
            qids = [q['id'] for q in questions]
            room = PeerRoom(token=token, host_id=current_user.id, role=role,
                            category=category, company=company,
                            difficulty=difficulty,
                            num_questions=num_questions, status='open',
                            scheduled_at=scheduled_at,
                            invite_only=invite_only,
                            interviewer_id=current_user.id,
                            question_ids=qids, drawn_ids=list(qids),
                            round_no=0)
            db.session.add(room)
            db.session.commit()
        except Exception:
            db.session.rollback()
            log.exception("Peer room creation failed")
            return _peer_api_error('Could not create the room. '
                                   'Please try again.', 500)
        return jsonify({'success': True, 'token': token,
                        'url': url_for('peer_room', token=token),
                        'questions': len(qids)})

    @app.route('/peer/room/<token>')
    @login_required
    def peer_room(token):
        """Session room: question cards, timers, answers, swap/finish,
        scheduling, and (after finish) reciprocal feedback + summary."""
        room = _peer_room_or_none(token)
        if room is None:
            abort(404)
        me = current_user.id
        is_participant = _peer_is_participant(room, me)
        can_join = (not is_participant and room.guest_id is None
                    and room.status in ('open', 'waiting')
                    and not _peer_blocked(me, room.host_id))
        questions = []
        for qid in (room.question_ids or []):
            q = _peer_question_by_id(qid)
            if q:
                questions.append({
                    'id': q['id'], 'question': q.get('question'),
                    'category': q.get('category'),
                    'difficulty': q.get('difficulty'),
                    'time_minutes': q.get('time_minutes') or 5,
                    'key_points': q.get('key_points') or [],
                })
        answers = {}
        if is_participant:
            try:
                for a in PeerAnswer.query.filter_by(room_id=room.id).all():
                    answers.setdefault(a.question_id, {})[a.user_id] = \
                        a.answer_text
            except Exception:
                log.exception("Peer answers lookup failed")
        feedbacks = []
        try:
            for f in (PeerFeedback.query.filter_by(room_id=room.id)
                      .order_by(PeerFeedback.created_at.asc()).all()):
                feedbacks.append({
                    'reviewer_id': f.reviewer_id,
                    'reviewee_id': f.reviewee_id,
                    'scores': {d: getattr(f, d) for d in PEER_RUBRIC_DIMS},
                    'comments': f.comments,
                })
        except Exception:
            log.exception("Peer feedback lookup failed")
        partner_id = _peer_partner_id(room, me)
        partner = None
        if partner_id:
            try:
                partner = db.session.get(User, partner_id)
            except Exception:
                partner = None
        i_am_interviewer = (room.interviewer_id == me)
        my_feedback_done = any(f['reviewer_id'] == me for f in feedbacks)
        fb_recap = None
        if room.status == 'done' and is_participant and feedbacks:
            def _avg(scores):
                vals = [scores[d] for d in PEER_RUBRIC_DIMS
                        if scores.get(d) is not None]
                return round(sum(vals) / len(vals), 1) if vals else None
            received = next((f for f in feedbacks
                             if f['reviewee_id'] == me), None)
            given = next((f for f in feedbacks
                          if f['reviewee_id'] == partner_id), None)
            fb_recap = {
                'received_avg': _avg(received['scores']) if received else None,
                'received': received,
                'given_avg': _avg(given['scores']) if given else None,
                'given': given,
            }
            if received:
                weakest = min(
                    (d for d in PEER_RUBRIC_DIMS
                     if received['scores'].get(d) is not None),
                    key=lambda d: received['scores'][d], default=None)
                fb_recap['weakest'] = weakest
        return render_template('peer_room.html', room=room, questions=questions,
                               answers=answers, feedbacks=feedbacks,
                               fb_recap=fb_recap,
                               is_participant=is_participant, can_join=can_join,
                               partner=partner, partner_id=partner_id,
                               i_am_interviewer=i_am_interviewer,
                               my_feedback_done=my_feedback_done,
                               rubric_dims=PEER_RUBRIC_DIMS,
                               nav_active='practice')

    @app.route('/peer/api/room/<token>/join', methods=['POST'])
    @login_required
    @limiter.limit("30 per hour")
    def peer_join_room(token):
        """Guest joins an open room. Hosts can't join their own room;
        blocked users can't join each other."""
        room = _peer_room_or_none(token)
        if room is None:
            return _peer_api_error('Room not found.', 404)
        me = current_user.id
        if me == room.host_id:
            return _peer_api_error("You can't join your own room — share the "
                                   'link with a peer instead.', 403)
        if room.status in ('done', 'cancelled'):
            return _peer_api_error('This session is already %s.'
                                   % room.status, 400)
        if room.guest_id is not None:
            return _peer_api_error('This room already has a partner.', 400)
        if room.status == 'in_progress':
            return _peer_api_error('This session already started.', 400)
        if _peer_blocked(me, room.host_id):
            return _peer_api_error('You cannot join this room.', 403)
        try:
            room.guest_id = me
            now_naive = datetime.now(UTC).replace(tzinfo=None)
            if room.scheduled_at and room.scheduled_at > now_naive:
                room.status = 'waiting'
            else:
                room.status = 'in_progress'
            db.session.commit()
        except Exception:
            db.session.rollback()
            log.exception("Peer join failed")
            return _peer_api_error('Could not join the room. '
                                   'Please try again.', 500)
        return jsonify({'success': True, 'status': room.status,
                        'scheduled_at': (room.scheduled_at.isoformat()
                                         if room.scheduled_at else None)})

    @app.route('/peer/api/room/<token>/start', methods=['POST'])
    @login_required
    @limiter.limit("30 per hour")
    def peer_start_room(token):
        """Move a scheduled ('waiting') room into 'in_progress'."""
        room = _peer_room_or_none(token)
        if room is None:
            return _peer_api_error('Room not found.', 404)
        if not _peer_is_participant(room, current_user.id):
            return _peer_api_error('Only room participants can start the '
                                   'session.', 403)
        if room.status == 'in_progress':
            return jsonify({'success': True, 'status': 'in_progress'})
        if room.status != 'waiting':
            return _peer_api_error('Only scheduled rooms can be started '
                                   '(current: %s).' % room.status, 400)
        if room.guest_id is None:
            return _peer_api_error('Waiting for a peer to join first.', 400)
        try:
            room.status = 'in_progress'
            db.session.commit()
        except Exception:
            db.session.rollback()
            log.exception("Peer start failed")
            return _peer_api_error('Could not start the session.', 500)
        return jsonify({'success': True, 'status': 'in_progress'})

    @app.route('/peer/api/room/<token>/answer', methods=['POST'])
    @login_required
    @limiter.limit("60 per hour")
    def peer_save_answer(token):
        """Save the current interviewee's typed answer to a question card."""
        room = _peer_room_or_none(token)
        if room is None:
            return _peer_api_error('Room not found.', 404)
        me = current_user.id
        if not _peer_is_participant(room, me):
            return _peer_api_error('Only room participants can answer.', 403)
        if room.status == 'done':
            return _peer_api_error('This session is finished — answers are '
                                   'locked.', 400)
        if room.status == 'cancelled':
            return _peer_api_error('This session was cancelled.', 400)
        if room.guest_id is None:
            return _peer_api_error('Waiting for a peer to join first.', 400)
        if me == room.interviewer_id:
            return _peer_api_error('Only the interviewee answers — ask the '
                                   'questions, or swap roles to answer.', 403)
        payload = request.get_json(silent=True) or {}
        qid = (payload.get('question_id') or '').strip()
        answer_text = (payload.get('answer_text') or '').strip()
        if qid not in (room.question_ids or []):
            return _peer_api_error('Unknown question for this round.', 404)
        if not answer_text:
            return _peer_api_error('Write your answer first.', 400)
        if len(answer_text) > PEER_ANSWER_MAX_CHARS:
            return _peer_api_error('Answer is too long (max %d characters).'
                                   % PEER_ANSWER_MAX_CHARS, 400)
        try:
            q = _peer_question_by_id(qid) or {}
            existing = (PeerAnswer.query
                        .filter_by(room_id=room.id, user_id=me,
                                   question_id=qid).first())
            if existing:
                existing.answer_text = answer_text
            else:
                db.session.add(PeerAnswer(room_id=room.id, user_id=me,
                                         question_id=qid,
                                         question_text=q.get('question'),
                                         answer_text=answer_text))
            db.session.commit()
        except Exception:
            db.session.rollback()
            log.exception("Peer answer save failed")
            return _peer_api_error('Could not save your answer.', 500)
        return jsonify({'success': True})

    @app.route('/peer/api/room/<token>/swap', methods=['POST'])
    @login_required
    @limiter.limit("20 per hour")
    def peer_swap_roles(token):
        """Flip interviewer/interviewee and draw a fresh question round."""
        room = _peer_room_or_none(token)
        if room is None:
            return _peer_api_error('Room not found.', 404)
        me = current_user.id
        if not _peer_is_participant(room, me):
            return _peer_api_error('Only room participants can swap roles.',
                                   403)
        if room.status != 'in_progress':
            return _peer_api_error('Role swaps are only available while the '
                                   'session is in progress.', 400)
        if room.guest_id is None:
            return _peer_api_error('Waiting for a peer to join first.', 400)
        partner_id = _peer_partner_id(room, me)
        try:
            drawn = list(room.drawn_ids or []) + list(room.question_ids or [])
            questions = _peer_questions(
                room.role, room.category,
                n=room.num_questions or N_PEER_QUESTIONS, exclude=drawn,
                company=room.company, difficulty=room.difficulty)
            qids = [q['id'] for q in questions]
            room.interviewer_id = partner_id
            room.question_ids = qids
            room.drawn_ids = drawn + qids
            room.round_no = (room.round_no or 0) + 1
            db.session.commit()
        except Exception:
            db.session.rollback()
            log.exception("Peer role swap failed")
            return _peer_api_error('Could not swap roles.', 500)
        return jsonify({'success': True, 'round_no': room.round_no,
                        'interviewer_id': room.interviewer_id,
                        'questions': len(qids)})

    @app.route('/peer/api/room/<token>/finish', methods=['POST'])
    @login_required
    @limiter.limit("20 per hour")
    def peer_finish_room(token):
        """Either participant ends the session: marks done + awards
        momentum XP ('peer_session_completed') to both participants."""
        room = _peer_room_or_none(token)
        if room is None:
            return _peer_api_error('Room not found.', 404)
        if not _peer_is_participant(room, current_user.id):
            return _peer_api_error('Only room participants can finish the '
                                   'session.', 403)
        if room.status == 'done':
            return jsonify({'success': True, 'already': True})
        if room.status == 'cancelled':
            return _peer_api_error('This session was cancelled.', 400)
        participants = [uid for uid in (room.host_id, room.guest_id)
                        if uid]
        try:
            room.status = 'done'
            db.session.commit()
        except Exception:
            db.session.rollback()
            log.exception("Peer finish failed")
            return _peer_api_error('Could not finish the session.', 500)
        for uid in participants:
            _momentum_hook(uid, 'peer_session_completed')
        return jsonify({'success': True, 'participants': participants})

    @app.route('/peer/api/room/<token>/cancel', methods=['POST'])
    @login_required
    @limiter.limit("20 per hour")
    def peer_cancel_room(token):
        """Host cancels a room before it is done."""
        room = _peer_room_or_none(token)
        if room is None:
            return _peer_api_error('Room not found.', 404)
        if current_user.id != room.host_id:
            return _peer_api_error('Only the host can cancel the room.', 403)
        if room.status in ('done', 'cancelled'):
            return _peer_api_error('This session is already %s.'
                                   % room.status, 400)
        try:
            room.status = 'cancelled'
            db.session.commit()
        except Exception:
            db.session.rollback()
            log.exception("Peer cancel failed")
            return _peer_api_error('Could not cancel the room.', 500)
        return jsonify({'success': True})

    @app.route('/peer/api/room/<token>/feedback', methods=['POST'])
    @login_required
    @limiter.limit("20 per hour")
    def peer_submit_feedback(token):
        """Submit rubric feedback for the partner (both directions allowed).

        JSON: {reviewee_id, clarity, structure, depth, relevance,
        confidence, comments?}. Only after the session is done.
        """
        room = _peer_room_or_none(token)
        if room is None:
            return _peer_api_error('Room not found.', 404)
        me = current_user.id
        if not _peer_is_participant(room, me):
            return _peer_api_error('Only room participants can give '
                                   'feedback.', 403)
        if room.status != 'done':
            return _peer_api_error('Feedback opens after the session is '
                                   'finished.', 400)
        payload = request.get_json(silent=True) or {}
        try:
            reviewee_id = int(payload.get('reviewee_id'))
        except (TypeError, ValueError):
            return _peer_api_error('reviewee_id must be a participant id.',
                                   400)
        if reviewee_id == me:
            return _peer_api_error("You can't review yourself — review your "
                                   'peer.', 400)
        if reviewee_id != _peer_partner_id(room, me):
            return _peer_api_error('You can only review your session '
                                   'partner.', 403)
        scores = {}
        for dim in PEER_RUBRIC_DIMS:
            try:
                val = int(payload.get(dim))
            except (TypeError, ValueError):
                return _peer_api_error('%s must be an integer 1-5.' % dim,
                                       400)
            if not 1 <= val <= 5:
                return _peer_api_error('%s must be between 1 and 5.' % dim,
                                       400)
            scores[dim] = val
        comments = (payload.get('comments') or '').strip() or None
        if comments and len(comments) > PEER_COMMENT_MAX_CHARS:
            return _peer_api_error('Comments are too long (max %d '
                                   'characters).' % PEER_COMMENT_MAX_CHARS,
                                   400)
        try:
            existing = (PeerFeedback.query
                        .filter_by(room_id=room.id, reviewer_id=me,
                                   reviewee_id=reviewee_id).first())
            if existing:
                for dim, val in scores.items():
                    setattr(existing, dim, val)
                existing.comments = comments
            else:
                db.session.add(PeerFeedback(room_id=room.id, reviewer_id=me,
                                           reviewee_id=reviewee_id,
                                           comments=comments, **scores))
            db.session.commit()
        except Exception:
            db.session.rollback()
            log.exception("Peer feedback save failed")
            return _peer_api_error('Could not save your feedback.', 500)
        return jsonify({'success': True})

    @app.route('/peer/api/rooms/<int:room_id>/slots', methods=['POST'])
    @login_required
    @limiter.limit("20 per hour")
    def peer_propose_slots(room_id):
        """Host proposes time slots (comma-separated 'YYYY-MM-DD HH:MM')."""
        try:
            room = db.session.get(PeerRoom, room_id)
        except Exception:
            log.exception("Peer room lookup failed")
            return _peer_api_error('Room not found.', 404)
        if room is None:
            return _peer_api_error('Room not found.', 404)
        if current_user.id != room.host_id:
            return _peer_api_error('Only the host can propose time slots.',
                                   403)
        if room.status in ('done', 'cancelled'):
            return _peer_api_error('This session is already %s.'
                                   % room.status, 400)
        payload = request.get_json(silent=True) or {}
        raw = payload.get('slots') or ''
        if isinstance(raw, list):
            parts = [str(s) for s in raw]
        else:
            parts = [p.strip() for p in str(raw).split(',')]
        parts = [p for p in parts if p]
        if not parts:
            return _peer_api_error('Propose at least one time slot.', 400)
        if len(parts) > 10:
            return _peer_api_error('Keep it to 10 slots or fewer.', 400)
        parsed = []
        for part in parts:
            dt = _peer_parse_slot(part)
            if dt is None:
                return _peer_api_error('Slot "%s" is not a valid '
                                       '"YYYY-MM-DD HH:MM".' % part, 400)
            parsed.append(dt.strftime('%Y-%m-%d %H:%M'))
        try:
            room.proposed_slots = parsed
            db.session.commit()
        except Exception:
            db.session.rollback()
            log.exception("Peer slot proposal failed")
            return _peer_api_error('Could not save the slots.', 500)
        return jsonify({'success': True, 'slots': parsed})

    @app.route('/peer/api/room/<token>/slot/accept', methods=['POST'])
    @login_required
    @limiter.limit("20 per hour")
    def peer_accept_slot(token):
        """Guest accepts one proposed slot; the room becomes scheduled."""
        room = _peer_room_or_none(token)
        if room is None:
            return _peer_api_error('Room not found.', 404)
        me = current_user.id
        if not _peer_is_participant(room, me):
            return _peer_api_error('Only room participants can accept a '
                                   'slot.', 403)
        if me == room.host_id:
            return _peer_api_error('The host proposes slots; the guest '
                                   'accepts one.', 403)
        if room.status in ('done', 'cancelled'):
            return _peer_api_error('This session is already %s.'
                                   % room.status, 400)
        payload = request.get_json(silent=True) or {}
        slot = _peer_parse_slot(payload.get('slot'))
        if slot is None:
            return _peer_api_error('slot must look like "YYYY-MM-DD HH:MM".',
                                   400)
        slot_str = slot.strftime('%Y-%m-%d %H:%M')
        if slot_str not in (room.proposed_slots or []):
            return _peer_api_error('That slot was not proposed by the host.',
                                   400)
        try:
            room.scheduled_at = slot
            if slot > datetime.now(UTC).replace(tzinfo=None):
                room.status = 'waiting'
            else:
                room.status = 'in_progress'
            db.session.commit()
        except Exception:
            db.session.rollback()
            log.exception("Peer slot accept failed")
            return _peer_api_error('Could not accept the slot.', 500)
        return jsonify({'success': True, 'status': room.status,
                        'scheduled_at': slot_str})

    @app.route('/peer/api/report', methods=['POST'])
    @login_required
    @limiter.limit("10 per hour")
    def peer_report_user():
        """Report + block another user (safety). JSON: {user_id, reason?}."""
        payload = request.get_json(silent=True) or {}
        try:
            target_id = int(payload.get('user_id'))
        except (TypeError, ValueError):
            return _peer_api_error('user_id must be a user id.', 400)
        if target_id == current_user.id:
            return _peer_api_error("You can't report yourself.", 400)
        try:
            target = db.session.get(User, target_id)
        except Exception:
            log.exception("Peer report target lookup failed")
            return _peer_api_error('User not found.', 404)
        if target is None:
            return _peer_api_error('User not found.', 404)
        reason = (payload.get('reason') or '').strip() or None
        if reason and len(reason) > 2000:
            return _peer_api_error('Reason is too long.', 400)
        try:
            existing = (PeerBlock.query
                        .filter_by(blocker_id=current_user.id,
                                   blocked_id=target_id).first())
            if not existing:
                db.session.add(PeerBlock(blocker_id=current_user.id,
                                        blocked_id=target_id, reason=reason))
                db.session.commit()
        except Exception:
            db.session.rollback()
            log.exception("Peer report failed")
            return _peer_api_error('Could not record the report.', 500)
        return jsonify({'success': True})

    # ---------- API ENDPOINTS (Shared) ----------
    @app.route('/api/interview-questions/<company>')
    def api_interview_questions(company):
        questions = hub_company_questions(
            company=company,
            category=request.args.get('category'),
            difficulty=request.args.get('difficulty'))
        return jsonify(questions)

    @app.route('/api/coding-practice')
    @app.route('/api/coding-practice/<company>')
    def api_coding_practice(company=None):
        questions = get_coding_questions(company=company,
                                         topic=request.args.get('topic'),
                                         difficulty=request.args.get('difficulty'))
        return jsonify(questions)

    @app.route('/api/companies')
    def api_companies():
        return jsonify({'interview': interview_hub_data()["companies"],
                        'coding': get_coding_companies()})

    @app.route('/api/topics')
    def api_topics():
        return jsonify(get_available_topics())

    # ---------- ENG v2 engines (P2 batch) ----------
    @app.route('/api/eng/jd-v2/match', methods=['POST'])
    @limiter.limit("30 per hour")
    def api_eng_jd_v2_match():
        p = request.get_json(force=True) or {}
        if len((p.get("jd_text") or "").strip()) < 50:
            return jsonify({"error": "Paste a fuller job description (50+ characters)."}), 400
        return jsonify(eng_jd_v2.match_jd_v2(
            p.get("jd_text", ""), p.get("resume_text", ""),
            p.get("resume_skills", []), p.get("resume_years", 0),
            p.get("resume_education", [])))

    @app.route('/api/eng/jobs/recommend', methods=['POST'])
    @limiter.limit("30 per hour")
    def api_eng_jobs_recommend():
        p = request.get_json(force=True) or {}
        return jsonify(eng_jobs.recommend_jobs(
            p.get("cards", []), p.get("resume_skills", []),
            p.get("resume_years", 0),
            geography=p.get("geography"), level=p.get("level")))

    @app.route('/api/eng/jobs/paste-jd', methods=['POST'])
    @limiter.limit("30 per hour")
    def api_eng_jobs_paste_jd():
        p = request.get_json(force=True) or {}
        return jsonify(eng_jobs.paste_jd_flow(
            p.get("jd_text", ""), title=p.get("title", "Pasted role")))

    @app.route('/api/eng/interview/score-v2', methods=['POST'])
    @limiter.limit("30 per hour")
    def api_eng_interview_score_v2():
        p = request.get_json(force=True) or {}
        res = eng_interview_v2.score_answer(
            p.get("question", ""), p.get("answer", ""),
            p.get("key_points", []))
        # NEVER render res["score"] without res["rubric"] + res["improvements"]
        return jsonify(res)

    @app.route('/api/chat_research', methods=['POST'])
    @limiter.limit("30 per hour")
    def api_chat_research():
        """Gemini-powered Document Intelligence Chat with Resilience.

        SEC-12: login required — this burns LLM quota, so anonymous callers
        get 401 (same policy as /api/study/import/pdf and the Ultimate Lab).

        Accepts the frontend's `history` (last exchanges) and merges it with
        the server-side ResearchSession when logged in, so follow-up
        questions keep context. When the caller is logged in, the
        conversation is appended to a per-user ResearchSession (returned as
        research_session_id)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to continue.'}), 401
        try:
            data = request.get_json(silent=True) or {}
            # P2#17: never send the literal string "None" to the model.
            user_msg = (data.get('message') or '').strip()
            if not user_msg:
                return jsonify({'error': 'Please type a question first.'}), 400
            doc_text = data.get('document_text', '') or ''

            if not _ai_ready():
                return jsonify({'response': "<strong>API Key Missing</strong><br><br>Please add your Gemini API Key to the .env file."}), 200

            # --- Conversation history (P1#4) ---
            # Prefer the server-side session (authoritative, tamper-proof);
            # fall back to the client-supplied history for anonymous users.
            history_lines = []
            server_session = None
            client_session_id = data.get('research_session_id')
            if current_user.is_authenticated and client_session_id:
                try:
                    server_session = ResearchSession.query.filter_by(
                        id=int(client_session_id),
                        user_id=current_user.id).first()
                except (ValueError, TypeError):
                    server_session = None
            if server_session and (server_session.data or {}).get("messages"):
                for m in (server_session.data["messages"])[-12:]:
                    role = "User" if m.get("role") == "user" else "Assistant"
                    history_lines.append("{}: {}".format(
                        role, str(m.get("content", ""))[:1500]))
            else:
                client_history = data.get('history') or []
                if isinstance(client_history, list):
                    for h in client_history[-12:]:
                        if isinstance(h, dict) and str(h.get('content') or '').strip():
                            role = "User" if h.get('role') == 'user' else "Assistant"
                            history_lines.append("{}: {}".format(
                                role, str(h['content'])[:1500]))
            history_block = ("\nCONVERSATION SO FAR (for context):\n" + "\n".join(history_lines)
                             if history_lines else "")

            prompt = f"""
            You are a PhD-level Research Assistant. You are grounded 100% in the following document text.
            {history_block}
            DOCUMENT TEXT:
            {doc_text[:15000]}

            USER QUESTION:
            {user_msg}

            INSTRUCTIONS:
            1. Answer with professional depth and technical precision.
            2. Use a formal, academic tone. Avoid all emojis.
            3. Structure your response with Markdown (use bolding for key terms, bullet points for lists, and headers if appropriate).
            4. If the answer is directly in the text, cite the context professionally.
            5. If the answer is not present, state so clearly in a formal manner.
            6. Use the conversation history above to resolve follow-up references like "it", "that section", or "the second point".
            """

            try:
                response_text = call_gemini_ai(prompt)
            except GeminiError as e:
                return jsonify({'error': str(e)}), 502

            # Persist the exchange for logged-in users.
            saved_session_id = None
            if current_user.is_authenticated:
                saved_session_id = _save_research_exchange(
                    current_user.id, client_session_id, user_msg, response_text, doc_text)

            payload = {'response': response_text}
            if saved_session_id:
                payload['research_session_id'] = saved_session_id
            return jsonify(payload)

        except Exception:
            log.exception("Research chat failed")
            return jsonify({'error': 'The research assistant hit an unexpected error. Please try again.'}), 500

    @app.route('/api/constructor_generate', methods=['POST'])
    @limiter.limit("10 per hour")
    def api_constructor_generate():
        """AI-powered IEEE Research Paper Generation from a REAL repository.

        SEC-12: login required — this burns LLM quota, so anonymous callers
        get 401 (same policy as /api/study/import/pdf and the Ultimate Lab).

        Fetches live repository facts from the GitHub API and grounds the
        prompt in them, so statistics reflect the actual repo (P1#3)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to continue.'}), 401
        try:
            data = request.get_json(silent=True) or {}
            # P2#20: reject empty input server-side (never generate about "None").
            repo_url = (data.get('repo_url') or '').strip()
            if not repo_url:
                return jsonify({'error': 'Please paste a GitHub repository URL first.'}), 400
            author = (data.get('author') or 'Anonymous').strip() or 'Anonymous'
            institution = (data.get('institution') or 'Independent Research').strip() \
                or 'Independent Research'

            if not _ai_ready():
                return jsonify({'error': 'API Key Missing. Add GEMINI_API_KEY, GROQ_API_KEY, or OPENROUTER_API_KEY to use the constructor.'}), 400

            match = re.match(
                r'https?://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$', repo_url, re.IGNORECASE)
            if not match:
                return jsonify({'error': 'That doesn\u2019t look like a GitHub repository URL '
                                         '(expected https://github.com/owner/repo).'}), 400
            owner, repo = match.group(1), match.group(2)

            try:
                repo_data = _fetch_repo_data(owner, repo)
            except GithubRepoError as e:
                return jsonify({'error': str(e)}), 502

            lang_lines = "\n".join(
                f"- {lang}: {pct}%"
                for lang, pct in repo_data["top_languages"]) or "- Unknown"
            prompt = (
                "Generate a strictly professional IEEE Research Paper draft based on the REAL "
                "repository data below. Author: {author}. Institution: {inst}.\n\n"
                "REPOSITORY FACTS (from the live GitHub API - base EVERY statistic on these, "
                "never invent numbers):\n"
                "- Repository: {full_name}\n"
                "- Description: {desc}\n"
                "- Stars: {stars} | Forks: {forks}\n"
                "- Primary language: {primary_lang}\n"
                "- Language breakdown:\n{langs}\n"
                "- Files indexed: {files}\n"
                "- Top-level directories: {dirs}\n"
                "- Representative files: {sample}\n"
                "- Has automated tests: {tests}\n"
                "- Has Docker: {docker}\n"
                "- Has CI workflows: {ci}\n"
                "- README excerpt: {readme}\n\n"
                "You are a PhD-level architect. Provide an EXTREMELY IN-DEPTH technical response "
                "in JSON format.\n\n"
                "REQUIRED JSON KEYS (Must be exactly these):\n"
                "- title: Academic Title\n"
                "- abstract: 200 word technical summary\n"
                "- introduction: 250+ word technical introduction\n"
                "- lit_review: 200+ word literature review\n"
                "- architecture: 300+ word architecture breakdown\n"
                "- implementation: 250+ word technical implementation details\n"
                "- results: 200+ word results/analysis\n"
                "- conclusion: Formal conclusion\n"
                "- stats: {{\"files\": {files}, \"complexity\": \"{complexity}\", "
                "\"confidence\": \"High - grounded in live repository data\"}}\n\n"
                "STRICTION: Return ONLY raw JSON. No conversational filler. Use exactly the keys "
                "listed above."
            ).format(
                author=author, inst=institution,
                full_name=repo_data["full_name"], desc=repo_data["description"],
                stars=repo_data["stars"], forks=repo_data["forks"],
                primary_lang=repo_data["primary_language"], langs=lang_lines,
                files=repo_data["file_count"],
                dirs=", ".join(repo_data["top_dirs"]) or "flat layout",
                sample=", ".join(repo_data["sample_files"][:15]) or "n/a",
                tests=repo_data["has_tests"], docker=repo_data["has_docker"],
                ci=repo_data["has_ci"],
                readme=repo_data["readme"] or "No README found.",
                complexity=repo_data["complexity"],
            )
            # RAG-07: inject integrity guardrails into the prompt
            prompt = build_grounded_prompt(prompt)
            # Keep the repo facts text for post-process number checking
            repo_facts_text = (
                f"{repo_data['full_name']} {repo_data['description']} "
                f"stars {repo_data['stars']} forks {repo_data['forks']} "
                f"{repo_data['primary_language']} files {repo_data['file_count']} "
                f"{lang_lines}"
            )

            # P1#5: robust JSON parsing - strip fences, retry once, then fail
            # gracefully with 502 (never 500 on model deviation).
            analysis = None
            attempts = 0
            while attempts < 2 and analysis is None:
                attempts += 1
                try:
                    response_text = call_gemini_ai(prompt)
                except GeminiError as e:
                    return jsonify({'error': str(e)}), 502
                try:
                    analysis = _parse_json_safely(response_text)
                except (ValueError, json.JSONDecodeError):
                    log.warning("Constructor: model returned non-JSON (attempt %d)", attempts)
                    analysis = None
            if analysis is None:
                return jsonify({'error': 'The AI returned an unreadable draft. '
                                         'Please try again in a moment.'}), 502
            # RAG-07: post-process with integrity guardrails
            analysis, guardrail_report = post_process_paper(
                analysis, repo_facts_text, author=author)
            # Verify citations (best-effort; network failures leave intact)
            for key in ("lit_review", "introduction", "abstract"):
                if isinstance(analysis.get(key), str):
                    verified, removed = verify_citations(analysis[key])
                    analysis[key] = verified
                    if removed:
                        guardrail_report.setdefault(
                            "citations_removed", []).extend(removed)
            log.info("constructor_guardrails applied: %s",
                     {k: len(v) if isinstance(v, list) else v
                      for k, v in guardrail_report.items()})
            return jsonify({'success': True, 'analysis': analysis,
                            'guardrails': guardrail_report})

        except Exception:
            log.exception("Constructor failed")
            return jsonify({'error': 'Paper generation hit an unexpected error. Please try again.'}), 500

    # ================= Wave 6 #30: career health review =================
    def _hr_to_dict(review):
        dims = review.dimensions_json or {}
        return {
            "month_key": review.month_key,
            "dimensions": [
                {"id": d["id"], "title": d["title"],
                 "description": d["description"],
                 **{k: v for k, v in (dims.get(d["id"]) or {}).items()
                    if k != "signals"}}
                for d in HR_DIMENSIONS
            ],
            "narrative": review.narrative,
            "next_action": review.next_action,
            "created_at": (review.created_at.isoformat()
                           if review.created_at else None),
        }

    def _hr_generate_current(user_id):
        """Generate (idempotently) the current month's review for the user.

        Returns (review, created). XP is awarded only when the row is newly
        INSERTED — regenerating an existing month never re-awards.
        """
        mk = hr_current_month_key()
        review, created = hr_generate_review(user_id, mk)
        xp_awarded = False
        if created:
            try:
                res = momentum_mod.record_event(user_id, "health_review_completed")
                xp_awarded = bool(res.get("ok") and not res.get("skipped")
                                  and res.get("xp"))
            except Exception:
                log.exception("health_review: XP award failed")
        return review, created, xp_awarded

    @app.route('/review')
    @login_required
    def health_review_hub():
        """Current month's review (auto-generated on first view): five
        dimension cards with score bars, trend arrows, confidence and
        evidence, plus the next-action callout and a history of past months.

        Trust rule: no opaque employability score — every number carries its
        evidence, and locked dimensions say exactly what would unlock them.
        """
        review, _created, _xp = _hr_generate_current(current_user.id)
        history = (HealthReview.query
                   .filter(HealthReview.user_id == current_user.id,
                           HealthReview.month_key != review.month_key)
                   .order_by(HealthReview.month_key.desc()).all())
        return render_template('health_review_hub.html',
                               review=_hr_to_dict(review),
                               month_key=review.month_key,
                               is_current=True,
                               history=[{"month_key": r.month_key,
                                         "next_action": r.next_action}
                                        for r in history])

    @app.route('/review/<month_key>')
    @login_required
    def health_review_month(month_key):
        """View a past month's review. Bad format -> 400; no row for this
        user (including other users' rows) -> 404."""
        if not hr_valid_month_key(month_key):
            abort(400)
        review = (HealthReview.query
                  .filter_by(user_id=current_user.id, month_key=month_key).first())
        if review is None:
            abort(404)
        history = (HealthReview.query
                   .filter(HealthReview.user_id == current_user.id,
                           HealthReview.month_key != review.month_key)
                   .order_by(HealthReview.month_key.desc()).all())
        return render_template('health_review_hub.html',
                               review=_hr_to_dict(review),
                               month_key=review.month_key,
                               is_current=(month_key == hr_current_month_key()),
                               history=[{"month_key": r.month_key,
                                         "next_action": r.next_action}
                                        for r in history])

    @app.route('/review/api/regenerate', methods=['POST'])
    @login_required
    @limiter.limit("20 per hour")
    def health_review_regenerate():
        """Regenerate the current month's review. CSRF via the X-CSRFToken
        header (not exempt). XP is awarded only when the review row is first
        created, never on updates."""
        try:
            review, created, xp_awarded = _hr_generate_current(current_user.id)
            return jsonify({"ok": True, "month_key": review.month_key,
                            "created": created, "xp_awarded": xp_awarded,
                            "review": _hr_to_dict(review)})
        except Exception:
            log.exception("health_review regenerate failed")
            return jsonify({"error": "Could not regenerate the review. "
                                     "Please try again."}), 500

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
        return db.session.get(OfficeHour, oh_id)

    def _oh_active_booking_count(oh_id):
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

    @app.route('/mentors')
    @login_required
    def mentors_hub():
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

    @app.route('/mentors/<int:mentor_id>')
    @login_required
    def mentor_profile(mentor_id):
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

    @app.route('/mentors/api/office-hours/<int:oh_id>/book', methods=['POST'])
    @login_required
    @limiter.limit("20 per hour")
    def mentor_book_session(oh_id):
        """Book a seat. JSON: {question_text?, template_id?}. Enforces
        past/double-book/capacity rules. CSRF via X-CSRFToken header."""
        oh = _oh_session_or_404(oh_id)
        if oh is None:
            return _mentor_api_error('Office hour not found.', 404)
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
        return jsonify({'success': True, 'booking_id': booking.id,
                        'session_status': oh.status}), 201

    @app.route('/mentors/api/bookings/<int:booking_id>/cancel', methods=['POST'])
    @login_required
    @limiter.limit("20 per hour")
    def mentor_cancel_booking(booking_id):
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

    @app.route('/mentors/api/bookings/<int:booking_id>/attend', methods=['POST'])
    @login_required
    @limiter.limit("60 per hour")
    def mentor_mark_attended(booking_id):
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

    @app.route('/mentors/api/bookings/<int:booking_id>/note', methods=['POST'])
    @login_required
    @limiter.limit("60 per hour")
    def mentor_save_note(booking_id):
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

    @app.route('/mentors/api/bookings/<int:booking_id>/feedback', methods=['POST'])
    @login_required
    @limiter.limit("20 per hour")
    def mentor_submit_feedback(booking_id):
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

    @app.route('/mentors/api/my-bookings')
    @login_required
    def mentor_my_bookings():
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

    @app.route('/mentors/apply')
    @login_required
    def mentor_apply_form():
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

    @app.route('/mentors/api/apply', methods=['POST'])
    @login_required
    @limiter.limit("5 per hour")
    def mentor_apply_submit():
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

    @app.route('/mentors/api/report', methods=['POST'])
    @login_required
    @limiter.limit("10 per hour")
    def mentor_report():
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

    @app.route('/mentors/api/admin/verify/<int:mentor_id>', methods=['POST'])
    @login_required
    @limiter.limit("30 per hour")
    def mentor_admin_verify(mentor_id):
        """Verify a mentor (set verified=True). SEC-17: requires the
        server-side admin role (User.is_admin) or a STUDYBUDDY_ADMINS
        bootstrap email; the action is audit-logged."""
        forbidden = _mentor_admin_required()
        if forbidden is not None:
            return forbidden
        mentor = db.session.get(Mentor, mentor_id)
        if mentor is None:
            return _mentor_api_error('Mentor not found.', 404)
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

    @app.route('/mentors/api/admin/office-hours', methods=['POST'])
    @login_required
    @limiter.limit("30 per hour")
    def mentor_admin_create_session():
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


def _save_research_exchange(user_id, client_session_id, user_msg, ai_response, doc_text):
    """Append a chat exchange to the user's ResearchSession. Returns session id.

    Reuses the client-provided session when it belongs to the user AND the
    document hasn't changed (doc_hash); otherwise starts a new session so
    conversations about different papers never mix."""
    doc_hash = hashlib.sha256(doc_text[:4000].encode("utf-8", "ignore")).hexdigest()

    sess = None
    if client_session_id:
        try:
            sess = ResearchSession.query.filter_by(id=int(client_session_id),
                                                   user_id=user_id).first()
        except (ValueError, TypeError):
            sess = None
        if sess and (sess.data or {}).get("doc_hash") != doc_hash:
            sess = None  # different document -> new session

    if sess is None:
        title = user_msg if len(user_msg) <= 60 else user_msg[:57] + "..."
        sess = ResearchSession(user_id=user_id, title=title,
                               data={"messages": [], "doc_hash": doc_hash})
        db.session.add(sess)
        db.session.flush()  # assign id

    messages = list((sess.data or {}).get("messages", []))
    messages.append({"role": "user", "content": user_msg})
    messages.append({"role": "assistant", "content": ai_response})
    # Bound session size: keep the most recent 40 messages.
    messages = messages[-40:]
    sess.data = {"messages": messages, "doc_hash": doc_hash}
    db.session.commit()
    return sess.id


# --- AI Resiliency Helper (google-genai SDK, P1#9) ---
def call_gemini_direct(prompt, max_attempts=4):
    """Raw Gemini call (no fallback). Used by the AI gateway as one provider.

    Raises on any failure — the gateway catches and tries the next provider.
    """
    client = _make_gemini_client()
    if client is None:
        raise GeminiError(
            "AI features need an API key. Add GEMINI_API_KEY, GROQ_API_KEY, or "
            "OPENROUTER_API_KEY to your .env file (all have free tiers) and restart.")

    try:
        model_names = _discover_model_names(client)
    except Exception as e:
        log.warning("Gemini model discovery failed: %r", e)
        msg = str(e)
        if ("API_KEY_INVALID" in msg or "API key not valid" in msg
                or "API_KEY_EXPIRED" in msg):
            raise GeminiError(
                "Your Gemini API key was rejected. Check GEMINI_API_KEY "
                "in your .env file and try again.")
        raise GeminiError(
            "Could not reach the AI service. Please try again in a moment.")
    if not model_names:
        raise GeminiError(
            "No usable AI models are available for this API key right now.")

    log.info("Gemini: %d eligible models, trying up to %d",
             len(model_names), max_attempts)
    for model_name in model_names[:max_attempts]:
        try:
            response = client.models.generate_content(
                model=model_name, contents=prompt)
            text = (getattr(response, "text", "") or "").strip()
            if text:
                return text
            log.warning("Gemini model %s returned empty text", model_name)
        except Exception as e:
            log.warning("Gemini model %s failed: %r", model_name, e)
            continue

    raise GeminiError(
        "The AI service is temporarily unavailable. Please try again shortly.")


def call_gemini_ai(prompt, max_attempts=4):
    """AI generation via the multi-provider gateway (Gemini -> Groq -> OpenRouter).

    Keeps the historic name so all call sites work unchanged; failures raise
    GeminiError with user-safe messages, as before.
    """
    from studybuddy import ai_gateway
    try:
        return ai_gateway.generate(prompt)
    except ai_gateway.AIError as e:
        raise GeminiError(str(e))


app = create_app()


def _resolve_debug(env=None):
    """Decide Flask debug mode. Debug is NEVER allowed in production-like envs.

    SEC-11: the old ``app.run(debug=True)`` exposed the Werkzeug debugger
    (remote code execution) if it ever ran outside localhost.
    """
    env = os.environ if env is None else env
    want = env.get("FLASK_DEBUG") == "1"
    if want and (env.get("FLASK_ENV") == "production" or env.get("RENDER")):
        raise RuntimeError(
            "Refusing to run with FLASK_DEBUG=1 in a production environment.")
    return want


if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("  Study-Buddy")
    _port = int(os.environ.get("PORT", "5000"))
    print(f"  Starting server at http://localhost:{_port}")
    print("=" * 60 + "\n")
    app.run(debug=_resolve_debug(), port=_port, use_reloader=False)
