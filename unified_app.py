"""
Study-Buddy-Powered Excellence Platform
Main Entry Point

Phase 1 (Foundation): Flask-SQLAlchemy + Flask-Login persistence.
- Users can register / log in (email + password, Werkzeug hashing).
- Resume analyses are saved per-user when logged in; /analyze still works anonymously.
- /dashboard (login required) shows analysis history + career goals.
- Research chat sessions are saved per-user when logged in.
"""
import csv
import hashlib
import io
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
    render_template_string,
    request,
    session,
    url_for,
)
from flask_login import (
    UserMixin,
    current_user,
    fresh_login_required,
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
import studybuddy.mentor_health as mentor_health_mod
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
    reading_queue,
    recent_citations,
    search_papers,
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
# Monolith split Phase 2 (audit fix, 2026-10-28): the DSA helpers
# (all_problems, filter_problems, progress_summary, ...) moved with the
# routes to studybuddy/dsa_routes.py. Only a2z_problems is still used
# here (landing-page DSA count below).
from studybuddy.dsa_practice import a2z_problems
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
from studybuddy.pdf_qa import chunk_pages as pdfqa_chunk_pages
from studybuddy.pdf_qa import parse_pdf_pages as pdfqa_parse_pages
from studybuddy.portfolio import unique_slug, validate_portfolio_payload
from studybuddy.role_cards import enrich_clusters
from studybuddy.salary_data import SalaryDataError
from studybuddy.salary_data import city_adjusted_median as salary_city_adjusted
from studybuddy.salary_data import disclaimer as salary_disclaimer
from studybuddy.salary_data import get_freshness as salary_freshness
from studybuddy.salary_data import lookup as salary_lookup
from studybuddy.salary_data import scripts_for_situation as salary_scripts_for
from studybuddy.career_graph import career_snapshot
from studybuddy.deadlines import DEADLINE_KINDS, upcoming_deadlines
from studybuddy.interview_kit import build_kit
from studybuddy.freshness import freshness_status
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
    # Session revocation (audit fix, 2026-10-28): bumped on every
    # password change/reset. A before_request hook (see create_app)
    # compares the version stored in the session cookie against this
    # column and logs out stale sessions, so a stolen session stops
    # working as soon as the victim resets their password. Default 0;
    # see migrations/versions/*_session_version.py (Postgres needs the
    # migration — dev SQLite gets it via create_all).
    session_version = db.Column(db.Integer, default=0, nullable=False)

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
    # Phase 0: deadline engine — nullable so the additive prod schema
    # self-heal covers existing rows.
    deadline = db.Column(db.Date, nullable=True)
    deadline_kind = db.Column(db.String(32), nullable=True)  # oa|interview|followup|offer|other
    # Phase 4: when the current deadline was set (drives the deadlines-2
    # quest; cleared together with the deadline itself).
    deadline_set_at = db.Column(db.DateTime, nullable=True)
    resume_version_id = db.Column(db.Integer, db.ForeignKey("resume_version.id"),
                                 nullable=True, index=True)  # which version was sent
    stage_updated_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                                 nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),  # ARC-04
                           onupdate=lambda: datetime.now(UTC), nullable=True)

    user = db.relationship("User", backref=db.backref("job_applications", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class Referral(db.Model):
    """A referral ask tracked by a user (Career plan Phase 1).

    application_id is nullable: a referral can exist before (or without)
    a tracker card, and deleting an application NULLs the link instead of
    deleting the referral history.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    application_id = db.Column(db.Integer, db.ForeignKey("job_application.id"),
                               nullable=True, index=True)
    contact_name = db.Column(db.String(120), nullable=False)
    channel = db.Column(db.String(20), nullable=False, default="other")  # linkedin|email|other
    status = db.Column(db.String(20), nullable=False, default="asked")  # asked|replied|referred|declined
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           onupdate=lambda: datetime.now(UTC), nullable=True)

    user = db.relationship("User", backref=db.backref("referrals", lazy="dynamic",
                                                      cascade="all, delete-orphan"))
    application = db.relationship("JobApplication", backref=db.backref("referrals", lazy="dynamic"))


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
    # Phase 4: reason taxonomy id OR user free text (never forced into the
    # taxonomy); interview feedback entries; opt-in anonymous sharing.
    reason = db.Column(db.String(200))
    interview_feedback = db.Column(db.JSON)  # list of feedback dicts, cap 20
    anonymous_share = db.Column(db.Boolean, nullable=False, default=False)

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
    # Phase 4 outcome tracking: helped|no-change|hurt|pending + free note.
    outcome = db.Column(db.String(20))
    outcome_note = db.Column(db.Text)
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
    # Import upgrade: retraction + OA-version provenance (nullable so the
    # additive prod schema self-heal covers existing rows).
    is_retracted = db.Column(db.Boolean, default=False)
    oa_version = db.Column(db.String(32))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("paper_imports", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class Collection(db.Model):
    """A token-shared, read-only set of imported papers (import upgrade)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    paper_ids = db.Column(db.JSON)  # [PaperImport ids, owner order]
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("collections", lazy="dynamic",
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
    # Phase 3 sections (plain-text JSON lists, validated in portfolio.py).
    certifications = db.Column(db.JSON)
    experience = db.Column(db.JSON)
    education = db.Column(db.JSON)
    achievements = db.Column(db.JSON)
    contact = db.Column(db.JSON)
    # Phase 3 theme: classic|dark|minimal|bold (None on old rows = classic).
    theme = db.Column(db.String(32), default="classic")
    show_github = db.Column(db.Boolean, default=False, nullable=False)
    show_leetcode = db.Column(db.Boolean, default=False, nullable=False)
    is_published = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           onupdate=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("portfolio", uselist=False,
                                                      cascade="all, delete-orphan"))


class PortfolioView(db.Model):
    """One row per public portfolio page load (Phase 3 analytics).

    Privacy: counts only — NO IP, NO user-agent, NO referrer is stored.
    """
    __tablename__ = "portfolio_view"
    id = db.Column(db.Integer, primary_key=True)
    portfolio_id = db.Column(db.Integer, db.ForeignKey("portfolio.id"),
                             nullable=False, index=True)
    viewed_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                          nullable=False)
    path = db.Column(db.String(120))


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

    Seed profiles (is_seed=True) ship with the app as ILLUSTRATIVE
    profiles: they are never verified and never bookable (Phase 5
    honesty rule). Real mentors join via MentorApplication and get
    verified=True only after an admin approves them. user_id links the
    profile to the mentor's own STUDY-BUDDY account when one exists
    (null for seed rows).
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


class MentorQA(db.Model):
    """One curated Q&A library entry (Phase 5).

    Library entries are general career knowledge attributed to the
    community knowledge base (mentor_id NULL, attribution set). A real
    mentor's own answer links mentor_id to a NON-seed mentor row; seeds
    never author library entries.
    """
    __tablename__ = "mentor_qa"

    id = db.Column(db.Integer, primary_key=True)
    question = db.Column(db.Text, nullable=False)
    answer = db.Column(db.Text, nullable=False)
    topic = db.Column(db.String(64), nullable=False, default="general",
                      index=True)
    mentor_id = db.Column(db.Integer, db.ForeignKey("mentor.id"),
                          nullable=True, index=True)
    attribution = db.Column(db.String(120), nullable=False,
                            default="Community knowledge base")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           nullable=False)


class MentorQuestion(db.Model):
    """A student's question in a real mentor's inbox (Phase 5).

    This is an inbox, not an answered Q&A: status stays 'open' until a
    real mentor answers elsewhere. No answer is ever auto-generated.
    topic is a mentor path key when the asker picked one, else NULL
    (general questions are visible to every mentor's dashboard).
    """
    __tablename__ = "mentor_question"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False,
                        index=True)
    question = db.Column(db.Text, nullable=False)
    topic = db.Column(db.String(64), nullable=True, index=True)
    status = db.Column(db.String(20), nullable=False, default="open",
                       index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           nullable=False)


class MentorReview(db.Model):
    """One review of a mentor by a student who actually interacted with
    them (Phase 5). One review per (user, mentor); rating 1-5. Averages
    are computed only from these rows — never a default or fake value.
    """
    __tablename__ = "mentor_review"

    __table_args__ = (db.UniqueConstraint("user_id", "mentor_id",
                                         name="uq_mentor_review_user_mentor"),)

    id = db.Column(db.Integer, primary_key=True)
    mentor_id = db.Column(db.Integer, db.ForeignKey("mentor.id"),
                          nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False,
                        index=True)
    rating = db.Column(db.Integer, nullable=False)
    text = db.Column(db.Text)
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


class LabRun(db.Model):
    """One Ultimate Lab tool execution (private to its owner).

    History is the Lab's memory: every authenticated run records its
    tool, a hash of the canonical input payload, and the JSON output so
    runs can be listed, re-run, saved, and deleted. Rows are never
    shared with other users and never used for training.
    """
    __tablename__ = "lab_run"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    tool_id = db.Column(db.String(200), nullable=False, index=True)
    feature = db.Column(db.String(120), nullable=False, default="")
    input_hash = db.Column(db.String(64), nullable=False, default="")
    input_json = db.Column(db.Text, nullable=False, default="{}")
    output_json = db.Column(db.Text, nullable=False, default="{}")
    basis = db.Column(db.String(30), nullable=False, default="rule-based")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("lab_runs", lazy="dynamic",
                                                      cascade="all, delete-orphan"))


class LabSave(db.Model):
    """A starred LabRun output (kept in My Lab)."""
    __tablename__ = "lab_save"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    run_id = db.Column(db.Integer, db.ForeignKey("lab_run.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("lab_saves", lazy="dynamic",
                                                      cascade="all, delete-orphan"))
    run = db.relationship("LabRun", backref=db.backref("saves", lazy="dynamic",
                                                       cascade="all, delete-orphan"))
    __table_args__ = (db.UniqueConstraint("user_id", "run_id",
                                          name="uq_lab_save_user_run"),)


class LabToolFavorite(db.Model):
    """A favourited Lab tool (quick access in My Lab)."""
    __tablename__ = "lab_tool_favorite"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    tool_id = db.Column(db.String(200), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("lab_tool_favorites",
                                                      lazy="dynamic",
                                                      cascade="all, delete-orphan"))
    __table_args__ = (db.UniqueConstraint("user_id", "tool_id",
                                          name="uq_lab_fav_user_tool"),)


class LabPathProgress(db.Model):
    """Per-user progress on a guided Lab path (one row per completed step)."""
    __tablename__ = "lab_path_progress"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    path = db.Column(db.String(80), nullable=False, index=True)
    step = db.Column(db.String(80), nullable=False)
    run_id = db.Column(db.Integer, db.ForeignKey("lab_run.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("lab_path_progress",
                                                      lazy="dynamic",
                                                      cascade="all, delete-orphan"))
    __table_args__ = (db.UniqueConstraint("user_id", "path", "step",
                                          name="uq_lab_path_user_step"),)


class BenchmarkContribution(db.Model):
    """An opt-in real resume contributed for benchmark calibration (GFI #5).

    ONLY redacted text is ever stored here, and only with explicit
    consent: `consent` must be True (see
    studybuddy/benchmark_collection.store_contribution, which refuses
    otherwise). The raw pasted text is never persisted, never logged,
    and never leaves the preview request. Deletion is a hard delete —
    the row is gone, so future aggregate reports cannot include it.
    Rows carry no user-identifying content: the export script names
    files by contribution id only.
    """
    __tablename__ = "benchmark_contribution"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    redacted_text = db.Column(db.Text, nullable=False)
    consent = db.Column(db.Boolean, nullable=False, default=False)
    consent_version = db.Column(db.String(20), nullable=False, default="v1")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("benchmark_contributions",
                                                      lazy="dynamic",
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
    # Phase 4: last manual streak repair (one repair per rolling 30 days).
    last_streak_repair_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", backref=db.backref("momentum_settings", uselist=False,
                                                      cascade="all, delete-orphan"))


class TeamLeague(db.Model):
    """A named, invite-code team league (Phase 4).

    Distinct from the weekly ISO-week cohorts (LeagueMember): teams are
    persistent and joined via an unguessable code. Boards expose only
    the requesting user's rank + member count + own XP.
    """
    __tablename__ = "team_league"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    invite_code = db.Column(db.String(32), nullable=False, unique=True,
                            index=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"),
                           nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           nullable=False)


class TeamLeagueMember(db.Model):
    """Membership in a team league (one row per user per league)."""
    __tablename__ = "team_league_member"

    id = db.Column(db.Integer, primary_key=True)
    league_id = db.Column(db.Integer, db.ForeignKey("team_league.id"),
                          nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"),
                        nullable=False, index=True)
    joined_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                          nullable=False)

    __table_args__ = (db.UniqueConstraint("league_id", "user_id",
                                         name="uq_team_league_member"),)


class SalaryEvaluation(db.Model):
    """A user's own offer evaluation (Phase 4).

    Logged so the salary-evaluated badge and salary quests are verifiable
    from data instead of stateless calls. Stores only what the user
    entered plus the band that was shown — never anyone else's offer.
    """
    __tablename__ = "salary_evaluation"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"),
                        nullable=False, index=True)
    role = db.Column(db.String(200))
    tier = db.Column(db.String(32))
    band = db.Column(db.String(32))
    offer_lpa = db.Column(db.Float)
    city = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           nullable=False)


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


class HealthPulse(db.Model):
    """A weekly 1-5 self-check (Phase 5). One row per (user, ISO week);
    scores is {dimension: 1-5} over the five health dimensions. A pulse
    is the user's own rating, never an inferred or opaque score.
    """
    __tablename__ = "health_pulse"

    __table_args__ = (db.UniqueConstraint("user_id", "week_key",
                                         name="uq_health_pulse_user_week"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False,
                        index=True)
    week_key = db.Column(db.String(7), nullable=False, index=True)  # "YYYY-Www"
    scores = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           nullable=False)


class HealthGoal(db.Model):
    """A health-linked goal (Phase 5). Separate from the generic Goal
    model because health goals carry a dimension so action plans can
    list them per dimension. done toggles via PATCH; nothing auto-done.
    """
    __tablename__ = "health_goal"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False,
                        index=True)
    title = db.Column(db.String(255), nullable=False)
    dimension = db.Column(db.String(32), nullable=True, index=True)
    target_date = db.Column(db.Date, nullable=True)
    done = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           nullable=False)


class HealthShare(db.Model):
    """A user's grant of health-summary access to one REAL mentor
    (Phase 5). Seeds can never be share targets. Only the grant is
    stored here; dashboards see counts, never another user's summary.
    """
    __tablename__ = "health_share"

    __table_args__ = (db.UniqueConstraint("user_id", "mentor_id",
                                         name="uq_health_share_user_mentor"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False,
                        index=True)
    mentor_id = db.Column(db.Integer, db.ForeignKey("mentor.id"),
                          nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC),
                           nullable=False)


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
RETRO_WIZARD_STEPS = ("facts", "patterns", "hypotheses", "experiment", "reason")
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
        "outcome": exp.outcome,
        "outcome_note": exp.outcome_note,
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


def _career_hook(user_id, action, ref_id):
    """Phase 4 per-ref career XP hook (same never-raises contract as
    _momentum_hook). Called AFTER the handler's own commit succeeds."""
    try:
        momentum_mod.career_xp_hook(user_id, action, ref_id)
    except Exception:
        log.exception("Career XP hook failed for action %s (non-fatal)",
                      action)


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
    # PERF-01 (audit Stage 4): this is the WEB ceiling for every upload
    # (Flask rejects larger bodies before any handler runs). Per-kind
    # effective limits, enforced by studybuddy/parsing.py: resumes
    # 5 MB (stricter than this ceiling, so it binds on web too);
    # research papers 16 MB via web (the 30 MB parsing cap binds only
    # for direct/library calls); PDF-QA uploads 16 MB via web (the
    # 25 MB pdf_qa cap binds only for direct calls). The upload forms
    # state these limits in the UI; keep all three places in sync.
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
    # Audit fix (2026-10-28): without refresh_view, @fresh_login_required
    # (used by /account/export) aborts 401 with no login path — a dead
    # end for browser users. Point it at the same login form so a stale
    # session is sent back to re-authenticate, then on to the export.
    login_manager.refresh_view = "auth.login"
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

    @app.before_request
    def _set_locale():
        # Hindi switcher (GFI #1): locale order is ?lang (validated) >
        # lang cookie (the persisted choice) > Accept-Language header.
        # Only locales with a parseable catalog are served (en, hi).
        from flask import g, request

        from studybuddy.i18n import available_locales, resolve_locale
        avail = set(available_locales())

        def _valid(value):
            cand = (value or "").strip().lower().split("-")[0]
            return cand if cand in avail else None

        g.locale = resolve_locale(
            _valid(request.args.get("lang"))
            or _valid(request.cookies.get("lang")),
            request.headers.get("Accept-Language"),
        )

    @app.before_request
    def _enforce_session_version():
        # Session revocation (audit fix, 2026-10-28): User.session_version
        # is bumped on every password change/reset. A session whose
        # stored version no longer matches is logged out immediately,
        # so a stolen session stops working as soon as the victim
        # resets their password (previously it survived until the
        # 12-hour session lifetime expired).
        #
        # Version adoption rules (no per-login-path edits needed):
        # - version present and equal -> pass.
        # - version present and stale -> logout + clear + re-login.
        # - version absent and the session proves a form login
        #   happened in it -> adopt the current version. The proof
        #   is `_id` (set by login_user) OR a fresh flag. `_fresh`
        #   alone is NOT sufficient: Flask-Login's "basic" session
        #   protection flips `_fresh` to False when the client IP
        #   changes (CGNAT/mobile rotation), so a form-login session
        #   whose first post-login request arrives from a new IP
        #   would otherwise be misread as a cookie restoration and
        #   logged out (full-suite regression, 2026-10-28). A
        #   remember-me restoration sets `_user_id` + `_fresh=False`
        #   but never sets `_id`, so it still fails both checks and
        #   must re-authenticate. This grandfathers sessions created
        #   before this deploy and covers every login path (form,
        #   register, Google OAuth) without touching them.
        # - version absent and neither proof present (a remember-me
        #   cookie restoration) -> logout. A cookie-restored session
        #   cannot prove it predates the last password change, so it
        #   must re-authenticate. One-time cost: remember-me users log
        #   in once more after this deploy.
        if not current_user.is_authenticated:
            return None
        from flask_login import logout_user

        stored = session.get("session_version")
        current = current_user.session_version or 0
        if stored is not None:
            if stored == current:
                return None
        elif session.get("_fresh", False) or "_id" in session:
            session["session_version"] = current
            return None
        uid = current_user.id
        logout_user()
        session.clear()
        # Stage 4 fix: session.clear() wipes the "_remember" = "clear"
        # marker logout_user() just set, so the remember cookie would
        # survive this revocation — and the browser would re-present
        # it on the very next request, re-restoring the same
        # version-less session and looping on /login forever. Restore
        # the marker when a remember cookie was actually presented.
        _remember_name = app.config.get(
            "REMEMBER_COOKIE_NAME", "remember_token")
        if _remember_name in request.cookies:
            session["_remember"] = "clear"
        log.info("session_version_revoked user_id_hash=%s",
                 _safe_user_ref_id(uid))
        if request.path.startswith("/api/"):
            return jsonify({"error": "Please log in again."}), 401
        flash("Your session expired after a password change. "
              "Please log in again.", "info")
        return redirect(url_for("auth.login"))

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
        # Hindi switcher (GFI #1): persist an explicitly requested valid
        # locale so the choice survives navigation (?lang is validated;
        # invalid values never set a cookie).
        _lang = request.args.get("lang")
        if _lang:
            from studybuddy.i18n import available_locales as _avail
            cand = _lang.strip().lower().split("-")[0]
            if cand in set(_avail()):
                resp.set_cookie("lang", cand, max_age=365 * 86400,
                                samesite="Lax")
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

    # Hindi switcher (GFI #1): _() translates via the stdlib .po catalog
    # for the request locale resolved in _set_locale. Strings with no Hindi
    # entry fall back to English (the msgid itself), so unmarked pages
    # render exactly as before.
    @app.context_processor
    def _i18n_processor():
        from flask import g, request
        from urllib.parse import urlencode

        from studybuddy.i18n import translate as _tr
        locale = getattr(g, "locale", "en")

        def _switch_url(target_locale):
            # Audit fix (2026-10-28): the old switcher used
            # {{ request.path }}?lang=..., which dropped the page's
            # other query parameters (e.g. /dsa?topic=arrays lost the
            # filter on switch). Rebuild the current URL with lang
            # replaced, everything else preserved.
            args = request.args.to_dict()
            args["lang"] = target_locale
            return request.path + "?" + urlencode(args)

        return {"_": lambda s, **kw: _tr(s, locale=locale, **kw),
                "current_locale": locale,
                "switch_url": _switch_url}


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
    from studybuddy.ultimate_routes import register_ultimate_bp
    register_ultimate_bp(app, limiter)

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
        # OPS-04 (audit Stage 4): prefer the build-time stamp written
        # by scripts/write_version.py (real tag + SHA from the build's
        # .git). Fall back to the old behaviour when no stamp exists
        # (local dev; Docker builds exclude .git by design).
        stamp_sha, stamp_version = None, None
        try:
            from studybuddy._version import APP_VERSION as stamp_version
            from studybuddy._version import GIT_SHA as stamp_sha
        except Exception:  # noqa: BLE001 - no stamp is normal in dev
            pass
        sha = stamp_sha or "unknown"
        if not stamp_sha:
            try:
                sha = subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, timeout=5,
                    cwd=os.path.dirname(__file__)).stdout.strip() or "unknown"
            except Exception:  # noqa: BLE001 - version must not raise
                pass
        # OSS-06: semantic version for releases
        app_version = stamp_version or "1.0.0"
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
                # SEC-01 (audit 2026-10-28): the reset link is NEVER shown
                # to the requester and NEVER logged. The previous code
                # flashed the raw link to an unauthenticated requester,
                # which defeated the generic message below and allowed
                # takeover of any account whose email is known. No mailer
                # exists in this codebase yet, so there is no delivery
                # channel: the token is stored hashed server-side (as
                # before) and the link itself is deliberately discarded
                # here — it is not flashed, not returned, and not written
                # to logs (raw tokens in log storage would recreate the
                # same exposure). Self-service reset therefore requires
                # email integration; see docs/ops/password-reset-email.md.
                # Until then the requester sees only the generic message
                # below, identical for existing and unknown addresses.
                log.info("password_reset_issued user_id_hash=%s",
                         _safe_user_ref_id(user.id))
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
            # Revoke all other sessions (audit fix, 2026-10-28): the
            # session-version hook logs out every session whose stored
            # version no longer matches, including stolen ones.
            user.session_version = (user.session_version or 0) + 1
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
    # Re-auth for a sensitive action (audit fix, 2026-10-28): a
    # remember-me-restored session is not fresh, so it is sent back to
    # the login form before any personal data is downloaded. Fresh
    # (password) sessions pass through. /account/delete already has
    # its own password + DELETE confirmation, so it needs no extra gate.
    @fresh_login_required
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
        # All user-owned rows via the canonical registry
        # (studybuddy/user_data.py) — the old 14-model list silently
        # omitted most user data. Keys for the original 14 are unchanged.
        from studybuddy.user_data import export_user_data
        data.update(export_user_data(user.id))
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
            # Delete ALL user-owned rows via the canonical registry
            # (studybuddy/user_data.py). The old 19-model loop silently
            # swallowed per-model failures AND omitted 40+ tables, so
            # "delete my account and all data" left orphans behind (and
            # could fail outright on Postgres FK enforcement). The new
            # helper fails loudly instead: on error nothing is deleted.
            from studybuddy.user_data import delete_all_user_data
            try:
                counts = delete_all_user_data(uid, db)
            except Exception:
                db.session.rollback()
                log.exception("account delete incomplete user_id_hash=%s",
                              _safe_user_ref_id(uid))
                flash('Account deletion failed. No data was deleted; '
                      'please try again or contact support.', 'error')
                return render_template('account_delete.html')
            log.info("account_delete_counts user_id_hash=%s counts=%s",
                     _safe_user_ref_id(uid), counts)
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
            # sections= excludes education dates from the year estimate
            # (2026-10-25 fix: edu-only dates used to count as work years).
            experience_years = extract_experience_years(cleaned_text, sections=sections)
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
        experience_years = extract_experience_years(cleaned, sections=sections)
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
        try:
            deadlines = upcoming_deadlines(current_user.id)
        except Exception:
            deadlines = []
        return render_template('tracker.html', stages=TRACKER_STAGES,
                               by_stage=by_stage, stats=_tracker_stats(current_user.id),
                               deadlines=deadlines)

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
        changed = app_row.stage != stage
        if changed:
            app_row.stage = stage
            app_row.stage_updated_at = datetime.now(UTC)
            db.session.commit()
            _career_hook(current_user.id, 'tracker_move', app_row.id)
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
        # Phase 1: keep referral history; only unlink it from this application.
        for ref in Referral.query.filter_by(application_id=app_id).all():
            ref.application_id = None
        db.session.delete(app_row)
        db.session.commit()
        return jsonify({'success': True})

    @app.route('/tracker/stats')
    @login_required
    def tracker_stats():
        """JSON stats for the user's tracker."""
        return jsonify({'success': True, 'stats': _tracker_stats(current_user.id)})

    # ---------- CAREER PLAN PHASE 1: tracker spine ----------
    _REFERRAL_CHANNELS = ('linkedin', 'email', 'other')
    _REFERRAL_STATUSES = ('asked', 'replied', 'referred', 'declined')

    def _referral_to_dict(r, app_map=None):
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

    @app.route('/api/tracker/<int:app_id>/kit')
    @limiter.limit("30 per hour")
    def tracker_kit(app_id):
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

    @app.route('/api/tracker/offers/compare', methods=['POST'])
    @limiter.limit("30 per hour")
    def tracker_offers_compare():
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

    @app.route('/api/tracker/referrals')
    def tracker_referrals_list():
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

    @app.route('/api/tracker/referrals', methods=['POST'])
    @limiter.limit("60 per hour")
    def tracker_referrals_add():
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

    @app.route('/api/tracker/referrals/<int:ref_id>', methods=['PATCH'])
    def tracker_referrals_patch(ref_id):
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

    @app.route('/api/tracker/referrals/<int:ref_id>', methods=['DELETE'])
    def tracker_referrals_delete(ref_id):
        """Delete a referral. CSRF via X-CSRFToken header."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        row = Referral.query.filter_by(id=ref_id, user_id=current_user.id).first()
        if not row:
            return jsonify({'error': 'Referral not found.'}), 404
        db.session.delete(row)
        db.session.commit()
        return jsonify({'success': True})

    @app.route('/api/tracker/<int:app_id>/timeline')
    def tracker_timeline(app_id):
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

    @app.route('/api/tracker/digest')
    def tracker_digest():
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

    @app.route('/api/tracker/import', methods=['POST'])
    @limiter.limit("30 per hour")
    def tracker_import():
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
    @app.route('/api/career/snapshot')
    @limiter.limit("30 per hour")
    def career_snapshot_api():
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to continue.'}), 401
        """Read-only career graph snapshot for the current user."""
        snapshot, warnings = career_snapshot(current_user.id)
        return jsonify(json_safe({'success': True, 'snapshot': snapshot,
                                  'warnings': warnings}))

    @app.route('/api/career/freshness')
    @limiter.limit("60 per hour")
    def career_freshness_api():
        """Public: freshness of the curated career datasets (no user data)."""
        from studybuddy.freshness import all_freshness
        return jsonify(json_safe({'success': True,
                                  'datasets': all_freshness()}))

    @app.route('/api/career/deadlines')
    @limiter.limit("30 per hour")
    def career_deadlines_api():
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to continue.'}), 401
        """Upcoming tracker + bookmarked-opportunity deadlines (30 days)."""
        return jsonify(json_safe({'success': True, 'deadlines':
                                  upcoming_deadlines(current_user.id)}))

    @app.route('/tracker/<int:app_id>/deadline', methods=['POST'])
    @limiter.limit("30 per hour")
    def tracker_set_deadline(app_id):
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
            'saved_url': url_for('view_assessment', assessment_id=row.id),
        })

    @app.route('/assessment/<int:assessment_id>')
    @login_required
    def view_assessment(assessment_id):
        """Render a saved assessment result (owner only)."""
        row = CareerAssessment.query.filter_by(id=assessment_id,
                                               user_id=current_user.id).first_or_404()
        # Phase 2: enrich at render time so pre-Phase-2 rows get handoffs too.
        clusters = enrich_clusters(row.top_clusters)
        return render_template('assessment_result.html',
                               clusters=clusters,
                               assessment_id=row.id, created_at=row.created_at)

    @app.route('/api/career/assessment/history')
    @limiter.limit("60 per hour")
    def assessment_history():
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
    # /dsa/* + /api/dsa/* now live in studybuddy/dsa_routes.py
    # (identical URLs, methods, rate limits; endpoints blueprint-prefixed
    # as dsa.* — template url_for call sites updated at move time).
    # Registered with the other blueprints below (see study_library).

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
        # Phase 2: city comparison table — the full city_entries set
        # (60 rows) feeds the template's 15 + toggle rendering.
        from studybuddy.salary_data import city_query as _city_query
        return render_template('salary.html', options=options,
                               city_entries=_city_query(),
                               freshness=salary_freshness(),
                               freshness_status=freshness_status('salary'),
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

    @app.route('/api/salary/curve')
    @limiter.limit("60 per hour")
    def salary_curve_api():
        """Growth curve: one role x tier across all 4 experience bands.

        Public. 400 on unknown role/tier. Ranges are indicative, never live.
        """
        role = (request.args.get('role') or '').strip()
        tier = (request.args.get('tier') or '').strip()
        try:
            options = salary_options()
        except SalaryDataError:
            return jsonify({'success': False,
                            'error': 'Salary data is temporarily unavailable.'}), 503
        if role not in options["roles"] or tier not in options["tiers"]:
            return jsonify({'success': False,
                            'error': 'Unknown role or tier.'}), 400
        try:
            rows = salary_query(role=role, tier=tier)
        except SalaryDataError:
            return jsonify({'success': False,
                            'error': 'Salary data is temporarily unavailable.'}), 503
        order = {b: i for i, b in enumerate(options["experience_bands"])}
        rows.sort(key=lambda r: order.get(r["experience_band"], 99))
        curve = [{"band": r["experience_band"], "min_lpa": r["min_lpa"],
                  "median_lpa": r["median_lpa"], "max_lpa": r["max_lpa"]}
                 for r in rows]
        return jsonify({'success': True, 'role': role, 'tier': tier,
                        'curve': curve, 'disclaimer': salary_disclaimer()})

    @app.route('/api/salary/evaluate', methods=['POST'])
    @limiter.limit("30 per hour")
    def salary_evaluate_api():
        """Evaluate one offer against its indicative band + city adjustment.

        Login required. Body {role, tier, band, offer_lpa, city?}.
        400 on bad numbers or unknown role/tier/band. Unknown city yields a
        null city adjustment (never a guessed multiplier).
        """
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        payload = request.get_json(silent=True) if request.is_json else {}
        payload = payload or {}
        role = (payload.get('role') or '').strip()
        tier = (payload.get('tier') or '').strip()
        band = (payload.get('band') or '').strip()
        raw_offer = payload.get('offer_lpa')
        try:
            offer = float(raw_offer)
        except (TypeError, ValueError):
            return jsonify({'error': 'Offer must be a number in LPA.'}), 400
        if isinstance(raw_offer, bool) or offer != offer or offer in (float('inf'), float('-inf')):
            return jsonify({'error': 'Offer must be a number in LPA.'}), 400
        if offer < 0 or offer > 1000:
            return jsonify({'error': 'Offer looks unrealistic (0-1000 LPA expected).'}), 400
        try:
            entry = salary_lookup(role, tier, band)
        except SalaryDataError:
            return jsonify({'success': False,
                            'error': 'Salary data is temporarily unavailable.'}), 503
        if not entry:
            return jsonify({'error': 'No market data for that role/tier/band.'}), 400
        lo, med, hi = entry["min_lpa"], entry["median_lpa"], entry["max_lpa"]
        if offer < lo:
            situation, note = "below-band", "below the indicative band"
        elif offer <= hi:
            situation, note = "at-band", "within the indicative band"
        else:
            situation, note = "above-band", "above the indicative band"
        city = (payload.get('city') or '').strip()
        city_adjusted = salary_city_adjusted(med, city) if city else None
        scripts = [s["id"] for s in salary_scripts_for(situation)][:3]
        evaluation = {
            "offer_lpa": round(offer, 1),
            "band": {"min_lpa": lo, "median_lpa": med, "max_lpa": hi},
            "percentile_note": f"₹{offer:.1f} LPA is {note} "
                               f"(₹{lo:.1f}–₹{hi:.1f} LPA, median ₹{med:.1f} LPA).",
            "city_adjusted": city_adjusted,
            "suggested_scripts": scripts,
        }
        # Phase 4: log the user's own evaluation (verifiable history for
        # the salary badge/quests). Logging never breaks the response.
        eval_id = None
        try:
            eval_row = SalaryEvaluation(
                user_id=current_user.id, role=role, tier=tier, band=band,
                offer_lpa=round(offer, 1), city=city or None)
            db.session.add(eval_row)
            db.session.commit()
            eval_id = eval_row.id
        except Exception:
            log.exception("Salary evaluation logging failed (non-fatal)")
            db.session.rollback()
        if eval_id is not None:
            _career_hook(current_user.id, 'salary_evaluate', eval_id)
        return jsonify({'success': True, 'evaluation': evaluation,
                        'disclaimer': salary_disclaimer()})

    @app.route('/api/salary/scripts')
    @limiter.limit("60 per hour")
    def salary_scripts_api():
        """Negotiation scripts. No filter -> all 15; unknown situation -> [].

        Public. Never 404s on a filter value.
        """
        situation = (request.args.get('situation') or '').strip() or None
        scripts = salary_scripts_for(situation)
        return jsonify({'success': True, 'scripts': scripts,
                        'disclaimer': salary_disclaimer()})

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
                               freshness=_freshness_label(meta),
                               freshness_status=freshness_status('opportunities'))

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
            _career_hook(current_user.id, 'opportunity_bookmark', opp_id)
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

    # ---------- RADAR PHASE 3 APIs ----------
    @app.route('/api/opportunities/refresh-status')
    @limiter.limit("60 per hour")
    def api_opp_refresh_status():
        """Public freshness + open/closed counts for the radar dataset."""
        try:
            from studybuddy.opportunities import load_dataset
            data = load_dataset()
            meta = data.get("meta") or {}
            opps = data.get("opportunities") or []
        except Exception:
            return jsonify({'error': 'Opportunity data unavailable.'}), 503
        from studybuddy.opportunity_extras import is_open
        open_count = sum(1 for o in opps if is_open(o))
        return jsonify(json_safe({
            'success': True,
            'last_updated': meta.get("last_updated"),
            'last_refresh': meta.get("last_refresh"),
            'total': len(opps),
            'open': open_count,
            'closed': len(opps) - open_count,
        }))

    @app.route('/api/opportunities/matches')
    @limiter.limit("30 per hour")
    def api_opp_matches():
        """Top 20 open opportunities scored against the user's cluster+skills."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        from studybuddy.opportunity_extras import match_opportunities
        matches, note = match_opportunities(current_user.id)
        body = {'success': True, 'matches': matches}
        if note:
            body['note'] = note
        return jsonify(json_safe(body))

    @app.route('/api/opportunities/alerts')
    @limiter.limit("30 per hour")
    def api_opp_alerts():
        """Cumulative 1/3/7-day deadline alerts over bookmarked open opps."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        bookmarked = {b.opp_id for b in OpportunityBookmark.query.filter_by(
            user_id=current_user.id).all()}
        from studybuddy.opportunity_extras import bookmark_alerts
        return jsonify(json_safe({
            'success': True,
            'alerts': bookmark_alerts(current_user.id, bookmarked),
        }))

    @app.route('/api/opportunities/<opp_id>')
    @limiter.limit("30 per hour")
    def api_opp_detail(opp_id):
        """Full entry incl. eligibility checklist + bookmarked flag."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        from studybuddy.opportunity_extras import (eligibility_list,
                                                  raw_opportunity)
        opp = raw_opportunity(opp_id)
        if not opp:
            return jsonify({'error': 'Opportunity not found.'}), 404
        bookmarked = OpportunityBookmark.query.filter_by(
            user_id=current_user.id, opp_id=opp_id).first() is not None
        entry = dict(opp)
        entry['eligibility_list'] = eligibility_list(opp)
        return jsonify(json_safe({
            'success': True, 'opportunity': entry, 'bookmarked': bookmarked,
        }))

    @app.route('/api/opportunities/<opp_id>/apply-to-tracker',
               methods=['POST'])
    @limiter.limit("30 per hour")
    def api_opp_apply_to_tracker(opp_id):
        """Create a wishlist tracker application from an opportunity.

        Idempotent: an existing wishlist row with the same role + notes
        returns its id (200) instead of duplicating.
        """
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        from studybuddy.opportunity_extras import raw_opportunity
        opp = raw_opportunity(opp_id)
        if not opp:
            return jsonify({'error': 'Opportunity not found.'}), 404
        company = (opp.get("organization") or "Opportunity")[:200]
        role = (opp.get("title") or "Opportunity")[:200]
        notes = f"From Opportunity Radar: {opp.get('link') or ''}"
        existing = JobApplication.query.filter_by(
            user_id=current_user.id, stage="wishlist", role=role,
            notes=notes).first()
        if existing:
            return jsonify({'success': True,
                            'application_id': existing.id})
        row = JobApplication(user_id=current_user.id, company=company,
                             role=role, stage="wishlist", notes=notes)
        db.session.add(row)
        db.session.commit()
        return jsonify({'success': True, 'application_id': row.id}), 201

    @app.route('/api/opportunities/<opp_id>/ics')
    @limiter.limit("30 per hour")
    def api_opp_ics(opp_id):
        """Download a calendar file with one all-day event on the deadline."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        from studybuddy.opportunity_extras import build_ics, raw_opportunity
        opp = raw_opportunity(opp_id)
        if not opp:
            return jsonify({'error': 'Opportunity not found.'}), 404
        ics = build_ics(opp)
        if not ics:
            return jsonify({'error': 'This opportunity has no deadline to '
                                     'export (rolling deadline).'}), 400
        return Response(ics, mimetype='text/calendar', headers={
            'Content-Disposition':
                f'attachment; filename="{opp_id}.ics"'})

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
            "certifications": row.certifications or [],
            "experience": row.experience or [],
            "education": row.education or [],
            "achievements": row.achievements or [],
            "contact": row.contact or [],
            "theme": row.theme or "classic",
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
        # Phase 4: career XP only for sections that were empty before this
        # save and are non-empty after it (compare before/after).
        def _sec_filled(r, section):
            value = getattr(r, section, None)
            if value is None:
                return False
            if isinstance(value, str):
                return bool(value.strip())
            if isinstance(value, (list, tuple, dict)):
                return len(value) > 0
            return bool(value)

        before_filled = {s: _sec_filled(row, s)
                         for s in momentum_mod.PORTFOLIO_SECTIONS}
        row.display_name = data["display_name"]
        row.headline = data["headline"]
        row.bio = data["bio"]
        row.skills = data["skills"]
        row.projects = data["projects"]
        row.links = data["links"]
        row.certifications = data["certifications"]
        row.experience = data["experience"]
        row.education = data["education"]
        row.achievements = data["achievements"]
        row.contact = data["contact"]
        row.theme = data["theme"]
        row.show_github = data["show_github"]
        row.show_leetcode = data["show_leetcode"]
        row.is_published = data["is_published"]
        row.updated_at = datetime.now(UTC)
        db.session.commit()
        for section in momentum_mod.PORTFOLIO_SECTIONS:
            if not before_filled[section] and _sec_filled(row, section):
                _career_hook(current_user.id, 'portfolio_section',
                             'section:%s' % section)
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
        # Phase 3 analytics: count the load, never break the page.
        try:
            db.session.add(PortfolioView(portfolio_id=row.id,
                                         path=f"/p/{slug}"))
            db.session.commit()
        except Exception:
            db.session.rollback()
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

    # ---------- PORTFOLIO PHASE 3 APIs ----------
    @app.route('/api/portfolio/theme', methods=['POST'])
    @limiter.limit("30 per hour")
    def api_portfolio_theme():
        """Quick theme switch for the user's portfolio."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        from studybuddy.portfolio import PORTFOLIO_THEMES
        payload = request.get_json(silent=True) or {}
        theme = (payload.get("theme") or "").strip()
        if theme not in PORTFOLIO_THEMES:
            return jsonify({'error': 'Theme must be one of: ' +
                                     ", ".join(PORTFOLIO_THEMES) + "."}), 400
        row = Portfolio.query.filter_by(user_id=current_user.id).first()
        if not row:
            return jsonify({'error': 'No portfolio yet.'}), 404
        row.theme = theme
        db.session.commit()
        return jsonify({'success': True, 'theme': theme})

    @app.route('/api/portfolio/github-import', methods=['POST'])
    @limiter.limit("30 per hour")
    def api_portfolio_github_import():
        """Preview a GitHub user's public repos. NEVER writes anything.

        Unauthenticated GitHub API, 10s timeout. Any failure is a 502 with
        a clear message — repos are never faked.
        """
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        payload = request.get_json(silent=True) or {}
        username = (payload.get("username") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9-]{0,38})", username):
            return jsonify({'error': 'Provide a valid GitHub username.'}), 400
        try:
            resp = requests.get(
                f"https://api.github.com/users/{username}/repos",
                params={"per_page": 100, "sort": "updated"},
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": "STUDY-BUDDY"},
                timeout=10)
            if resp.status_code != 200:
                return jsonify({'error': 'GitHub could not be reached '
                                         f'(HTTP {resp.status_code}). '
                                         'Check the username and try again.'}), 502
            repos = resp.json()
            if not isinstance(repos, list):
                raise ValueError("Unexpected GitHub response.")
        except Exception:
            return jsonify({'error': 'GitHub could not be reached. '
                                     'Check the username and try again.'}), 502
        preview = []
        for r in repos:
            if not isinstance(r, dict):
                continue
            preview.append({
                "name": (r.get("name") or "")[:80],
                "description": (r.get("description") or "")[:400],
                "url": r.get("html_url") or "",
                "stars": r.get("stargazers_count") or 0,
                "language": r.get("language") or "",
            })
        preview.sort(key=lambda p: p["stars"], reverse=True)
        return jsonify(json_safe({'success': True,
                                  'preview': preview[:20]}))

    @app.route('/api/portfolio/github-import/save', methods=['POST'])
    @limiter.limit("30 per hour")
    def api_portfolio_github_import_save():
        """Append chosen repos to the user's portfolio projects.

        Only this explicit call writes. Dedupe by URL, cap 50 projects.
        """
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        payload = request.get_json(silent=True) or {}
        repos = payload.get("repos")
        if not isinstance(repos, list):
            return jsonify({'error': 'Provide a repos list.'}), 400
        row = Portfolio.query.filter_by(user_id=current_user.id).first()
        if not row:
            return jsonify({'error': 'Create your portfolio first.'}), 404
        projects = [p for p in (row.projects or []) if isinstance(p, dict)]
        seen_urls = {p.get("link") for p in projects if p.get("link")}
        added = 0
        for repo in repos:
            if len(projects) >= 50:
                break
            if not isinstance(repo, dict):
                continue
            url = (repo.get("url") or "").strip()
            if (not re.match(r"^https?://", url, re.IGNORECASE)
                    or len(url) > 500 or any(c.isspace() for c in url)):
                continue
            if url in seen_urls:
                continue
            projects.append({
                "title": (repo.get("name") or "Untitled")[:80],
                "description": (repo.get("description") or "")[:400],
                "link": url,
            })
            seen_urls.add(url)
            added += 1
        row.projects = projects
        db.session.commit()
        return jsonify({'success': True, 'added': added,
                        'total': len(projects)})

    @app.route('/api/portfolio/analytics')
    @limiter.limit("30 per hour")
    def api_portfolio_analytics():
        """View counts for the owner's portfolio (counts only, no tracking)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        row = Portfolio.query.filter_by(user_id=current_user.id).first()
        if not row:
            return jsonify({'error': 'No portfolio yet.'}), 404
        views = PortfolioView.query.filter_by(
            portfolio_id=row.id).all()
        today = date.today()
        days = [(today - timedelta(days=i)).isoformat()
                for i in range(29, -1, -1)]
        by_day = {d: 0 for d in days}
        for v in views:
            try:
                key = v.viewed_at.date().isoformat()
            except Exception:
                continue
            if key in by_day:
                by_day[key] += 1
        return jsonify(json_safe({
            'success': True,
            'total_views': len(views),
            'views_last_30d': sum(by_day.values()),
            'by_day': [{'date': d, 'views': by_day[d]} for d in days],
        }))

    @app.route('/api/portfolio/review')
    @limiter.limit("30 per hour")
    def api_portfolio_review():
        """25-point portfolio review: score + per-check pass/fail + hints."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        row = Portfolio.query.filter_by(user_id=current_user.id).first()
        if not row:
            return jsonify({'error': 'No portfolio yet.'}), 404
        p = _portfolio_to_dict(row)
        skills = p["skills"]
        projects = p["projects"]
        links = p["links"]
        bio = p["bio"]
        checks = []

        def check(cid, label, passed, hint):
            checks.append({"id": cid, "label": label,
                           "passed": bool(passed), "hint": hint})

        sections = [("bio", "Bio", bio.strip() != ""),
                    ("skills", "Skills", len(skills) > 0),
                    ("projects", "Projects", len(projects) > 0),
                    ("links", "Links", len(links) > 0),
                    ("certifications", "Certifications",
                     len(p["certifications"]) > 0),
                    ("experience", "Experience", len(p["experience"]) > 0),
                    ("education", "Education", len(p["education"]) > 0),
                    ("achievements", "Achievements",
                     len(p["achievements"]) > 0),
                    ("contact", "Contact", len(p["contact"]) > 0)]
        for sid, label, ok in sections:
            check(f"section-{sid}", f"{label} section filled", ok,
                  f"Add at least one item to the {label} section.")
        check("display-name", "Display name present",
              bool(p["display_name"].strip()),
              "Add a display name in the portfolio builder.")
        check("headline", "Headline present", bool(p["headline"].strip()),
              "Add a one-line headline describing what you do.")
        check("bio-present", "Bio present", bool(bio.strip()),
              "Write a short bio about yourself.")
        check("skills-3", "At least 3 skills", len(skills) >= 3,
              "List at least 3 skills.")
        check("projects-2", "At least 2 projects", len(projects) >= 2,
              "Add a second project.")
        check("links-1", "At least 1 link", len(links) >= 1,
              "Add at least one link (GitHub, LinkedIn, or website).")
        check("certifications-present", "Certifications present",
              len(p["certifications"]) > 0,
              "Add a certification, course, or licence.")
        check("public-page", "Public page reachable",
              bool(p["slug"]) and bool(p["is_published"]),
              "Publish your portfolio so the public page is reachable.")
        check("contact-present", "Contact present", len(p["contact"]) > 0,
              "Add a contact item (email or profile).")
        check("project-descriptions", "Projects have descriptions",
              len(projects) > 0 and all(
                  isinstance(pr, dict) and bool((pr.get("description") or "")
                                                .strip())
                  for pr in projects),
              "Write a one-line description for every project.")
        check("project-urls", "Projects have URLs",
              len(projects) > 0 and all(
                  isinstance(pr, dict) and (pr.get("link") or "")
                  .startswith("http") for pr in projects),
              "Add a valid http(s) link to every project.")
        check("skills-not-bloated", "Skills list not bloated (<= 30)",
              len(skills) <= 30,
              "Trim the skills list to 30 or fewer.")
        check("bio-length", "Bio at least 100 characters",
              len(bio.strip()) >= 100,
              "Expand the bio to at least 100 characters.")
        check("achievements-present", "Achievements present",
              len(p["achievements"]) > 0,
              "Add an achievement, award, or recognition.")
        check("experience-or-education", "Experience or education present",
              len(p["experience"]) > 0 or len(p["education"]) > 0,
              "Add experience or education history.")
        # No empty-string items anywhere in the list sections.
        def _has_empty(items, keys):
            for item in items:
                if isinstance(item, str):
                    if not item.strip():
                        return True
                elif isinstance(item, dict):
                    for key in keys:
                        if not (item.get(key) or "").strip():
                            return True
            return False
        empty_found = (
            _has_empty(skills, []) or
            _has_empty(projects, ["title"]) or
            _has_empty(links, ["label"]) or
            _has_empty(p["certifications"], ["title"]) or
            _has_empty(p["experience"], ["role", "organization"]) or
            _has_empty(p["education"], ["institution", "degree"]) or
            _has_empty(p["achievements"], ["title"]) or
            _has_empty(p["contact"], ["label"]))
        check("no-empty-items", "No empty items in any section",
              not empty_found,
              "Remove blank items from the sections.")
        score = sum(1 for c in checks if c["passed"])
        return jsonify(json_safe({
            'success': True, 'score': score, 'max': 25, 'checks': checks,
        }))

    @app.route('/portfolio/<slug>/print')
    @limiter.limit("30 per hour")
    def portfolio_print(slug):
        """Print-friendly HTML view of a published portfolio.

        No PDF library exists in this environment, so this print view (use
        the browser's Print -> Save as PDF) is the export target instead
        of a faked PDF.
        """
        from html import escape as _esc
        row = Portfolio.query.filter_by(slug=slug).first()
        if not row or not row.is_published:
            abort(404)
        p = _portfolio_to_dict(row)

        def _list(title, items, fmt):
            if not items:
                return ""
            body = "".join(f"<li>{fmt(i)}</li>" for i in items)
            return f"<h2>{_esc(title)}</h2><ul>{body}</ul>"

        def _txt(item):
            return _esc(item) if isinstance(item, str) else ""
        sections = ""
        sections += _list("Skills", p["skills"], _txt)
        sections += _list("Projects", p["projects"], lambda pr: (
            f"<strong>{_esc(pr.get('title') or '')}</strong>"
            + (f" — {_esc(pr.get('description') or '')}"
               if pr.get("description") else "")
            + (f" (<a href=\"{_esc(pr.get('link') or '')}\">link</a>)"
               if pr.get("link") else "")))
        sections += _list("Links", p["links"], lambda ln: (
            f"<a href=\"{_esc(ln.get('url') or '')}\">"
            f"{_esc(ln.get('label') or '')}</a>"))
        sections += _list("Certifications", p["certifications"], lambda c: (
            _esc(c.get("title") or "") +
            (f" — {_esc(c.get('issuer') or '')}" if c.get("issuer") else "") +
            (f" ({_esc(c.get('year') or '')})" if c.get("year") else "")))
        sections += _list("Experience", p["experience"], lambda e: (
            f"<strong>{_esc(e.get('role') or '')}</strong>"
            + (f" at {_esc(e.get('organization') or '')}"
               if e.get("organization") else "")
            + (f" ({_esc(e.get('period') or '')})"
               if e.get("period") else "")
            + (f" — {_esc(e.get('description') or '')}"
               if e.get("description") else "")))
        sections += _list("Education", p["education"], lambda e: (
            f"<strong>{_esc(e.get('institution') or '')}</strong>"
            + (f" — {_esc(e.get('degree') or '')}" if e.get("degree") else "")
            + (f" ({_esc(e.get('period') or '')})"
               if e.get("period") else "")))
        sections += _list("Achievements", p["achievements"], lambda a: (
            f"<strong>{_esc(a.get('title') or '')}</strong>"
            + (f" — {_esc(a.get('description') or '')}"
               if a.get("description") else "")))
        sections += _list("Contact", p["contact"], lambda c: (
            f"{_esc(c.get('label') or '')}: {_esc(c.get('value') or '')}"))
        html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>{_esc(p['display_name'])} — Portfolio</title>
<style>body{{font-family:Georgia,serif;max-width:720px;margin:40px auto;
padding:0 20px;color:#111}}h1{{font-size:28px}}h2{{font-size:18px;
border-bottom:1px solid #ccc;padding-bottom:4px}}a{{color:#2447ff}}
@media print{{body{{margin:0}}}}</style></head><body>
<h1>{_esc(p['display_name'])}</h1>
<p><em>{_esc(p['headline'])}</em></p>
<p>{_esc(p['bio'])}</p>
{sections}
<p><small>Printed from STUDY-BUDDY portfolio.</small></p>
</body></html>"""
        return Response(html, mimetype='text/html')

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
    # /research/library + /study/api/* now live in studybuddy/study_library.py
    # (identical URLs, methods, rate limits; endpoints blueprint-prefixed as
    # study_library.* — template url_for call sites updated at move time).
    from studybuddy.study_library import study_library_bp
    app.register_blueprint(study_library_bp)

    # Monolith split Phase 2 (audit fix, 2026-10-28): /dsa/* +
    # /api/dsa/* now live in studybuddy/dsa_routes.py (identical URLs,
    # methods, rate limits; endpoints blueprint-prefixed as dsa.*).
    from studybuddy.dsa_routes import dsa_bp
    app.register_blueprint(dsa_bp)

    # GFI #5: opt-in benchmark resume collection (all routes auth-gated).
    from studybuddy.benchmark_collection import benchmark_bp
    app.register_blueprint(benchmark_bp)


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
            paper, deduped = _store_paper_import(meta)
        except Exception:
            log.exception("arXiv import store failed")
            db.session.rollback()
            return jsonify({'error': 'Could not save this paper.'}), 500
        meta['import_id'] = paper.id
        meta['deduped'] = deduped
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
            paper, deduped = _store_paper_import(meta)
        except Exception:
            log.exception("DOI import store failed")
            db.session.rollback()
            return jsonify({'error': 'Could not save this paper.'}), 500
        meta['import_id'] = paper.id
        meta['deduped'] = deduped
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

    def _fetch_pdf_into_library(pdf_url, title, arxiv_id, doi):
        """Download a PDF and store it as a StudyDoc for the current user.

        Shared by the two-step PDF route and one-click full import so both
        build the identical StudyDoc shape. Returns (doc_dict, err_type)
        where err_type is None on success else "download" | "no-text" |
        "failed". The downloaded file is removed on every failure path;
        on success it stays as the doc's stored_filename.
        """
        if not (pdf_url or '').lower().startswith(('http://', 'https://')):
            return None, "download"
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
        doc, err = _fetch_pdf_into_library(
            pdf_url, payload.get('title'), arxiv_id, doi)
        if err:
            status, message = _PDF_ERRORS[err]
            return jsonify({'error': message}), status
        return jsonify({'success': True, 'doc': doc})

    @app.route('/api/study/import/search', methods=['POST'])
    @limiter.limit("30 per hour")
    def study_import_search():
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

    @app.route('/api/study/import/full', methods=['POST'])
    @limiter.limit("30 per hour")
    def study_import_full():
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

    @app.route('/api/study/import/<int:paper_id>', methods=['DELETE'])
    def study_import_delete(paper_id):
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

    @app.route('/api/study/import/queue', methods=['POST'])
    @limiter.limit("30 per hour")
    def study_import_queue():
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

    @app.route('/api/study/import/citations')
    @limiter.limit("30 per hour")
    def study_import_citations():
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

    @app.route('/api/study/collections', methods=['POST'])
    def study_collection_create():
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

    @app.route('/api/study/collections')
    def study_collection_list():
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in to continue.'}), 401
        """List the user's collections."""
        collections = Collection.query.filter_by(
            user_id=current_user.id).order_by(
                Collection.created_at.desc()).all()
        return jsonify({'collections': [_collection_dict(c) for c in collections]})

    @app.route('/api/study/collections/<token>/papers', methods=['POST'])
    def study_collection_set_papers(token):
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

    @app.route('/research/collections/<token>')
    def study_collection_public(token):
        """Public read-only view of a token-shared collection.

        Rendered inline (no template file) so the frontend owner can
        replace it with a styled template later without a backend change.
        """
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

    # ---------- PHASE 4: team leagues, badges, recap, streak repair ----------
    @app.route('/api/momentum/leagues', methods=['POST'])
    @limiter.limit("30 per hour")
    def momentum_team_league_create():
        """Create a named team league (creator auto-joins, invite code
        returned once). CSRF via X-CSRFToken header (not exempt)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        payload = request.get_json(silent=True) or {}
        result = momentum_mod.create_team_league(current_user.id,
                                                 payload.get('name') or '')
        if not result.get('ok'):
            return jsonify({'error': 'A league name is required.'}), 400
        return jsonify({'success': True, 'league': result['league']}), 201

    @app.route('/api/momentum/leagues/join', methods=['POST'])
    @limiter.limit("30 per hour")
    def momentum_team_league_join():
        """Join a team league via its invite code. Unknown code -> 404;
        already a member -> 200 with already_member. CSRF via
        X-CSRFToken header (not exempt)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        payload = request.get_json(silent=True) or {}
        result = momentum_mod.join_team_league_by_code(
            current_user.id, payload.get('invite_code') or '')
        if not result.get('ok'):
            return jsonify({'error': 'Unknown invite code.'}), 404
        return jsonify({'success': True, 'league_id': result['league_id'],
                        'already_member': bool(result.get('duplicate'))})

    @app.route('/api/momentum/leagues')
    @login_required
    def momentum_team_leagues():
        """The user's team leagues. Privacy: only the requesting user's
        rank + member count + own XP — never other members' identities
        or scores."""
        boards = momentum_mod.team_league_boards(current_user.id)
        return jsonify({'success': True, 'leagues': boards})

    @app.route('/api/momentum/badges')
    @login_required
    def momentum_badges_api():
        """All 20 badges, computed live from the user's rows."""
        badges = momentum_mod.compute_badges(current_user.id)
        return jsonify(json_safe({'success': True, 'badges': badges}))

    @app.route('/api/momentum/recap')
    @login_required
    def momentum_recap_api():
        """This week's recap. Pure read, never writes."""
        recap = momentum_mod.weekly_recap(current_user.id)
        return jsonify({'success': True, 'recap': recap})

    @app.route('/api/momentum/streak/repair', methods=['POST'])
    @limiter.limit("30 per hour")
    def momentum_streak_repair():
        """Manually repair a streak broken by exactly one missed day
        (last active 2 days ago), at most once per 30 days. Repair fills
        the missed day; it is not itself activity. CSRF via X-CSRFToken
        header (not exempt)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        result = momentum_mod.repair_streak(current_user.id)
        if not result.get('ok'):
            reason = result.get('reason') or 'repair failed'
            messages = {
                'no streak': 'No active streak to repair.',
                'no break to repair': 'Your streak is still alive — '
                                      'no repair needed.',
                'gap too long': 'The gap is longer than one missed day; '
                                'a repair cannot restore it.',
                'repair already used': 'A streak repair was already used '
                                       'in the last 30 days.',
            }
            return jsonify({'error': messages.get(reason, reason)}), 400
        return jsonify({'success': True, 'streak': result['streak']})

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
                    "shared": bool(r.anonymous_share),
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

        JSON: {"application_id": int, "step": "facts"|"patterns"|"hypotheses"|"experiment"|"reason",
               "facts": str, "stage_reached": str, "timeline": str,
               "observed_patterns": str, "hypotheses": str,
               "experiment": {"hypothesis":..,"change":..,"target":..,"duration":..,"experiment_id":..},
               "reason": str}
        The "reason" step accepts a taxonomy id from RETRO_REASONS or free
        text alongside it — free text is stored as written, never forced
        into the taxonomy. CSRF via X-CSRFToken header (not exempt)."""
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
                # Phase 4: the wizard's step-1 reason radios also land
                # here (free text wins over a radio pick); the dedicated
                # reason step remains the place that can clear a reason.
                _reason = ((payload.get('reason_other') or
                            payload.get('reason') or '').strip()[:200])
                if _reason:
                    retro.reason = _reason
            elif step == 'patterns':
                retro.observed_patterns = _text(payload.get('observed_patterns')) or None
            elif step == 'hypotheses':
                retro.hypotheses = _text(payload.get('hypotheses')) or None
            elif step == 'reason':
                # Taxonomy id or free text — both stored as written.
                # Empty clears the reason.
                retro.reason = (payload.get('reason') or '').strip()[:200] or None
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
            _career_hook(current_user.id, 'retrospective_complete', retro.id)
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

    # ---------- PHASE 4: reasons, trends, outcomes, feedback, learnings ----
    def _retro_to_dict(retro):
        """Owner-only JSON view of one retrospective, including the
        Phase 4 fields (reason, interview feedback, anonymous flag)."""
        app_row = db.session.get(JobApplication, retro.application_id)
        feedback = retro.interview_feedback or []
        if not isinstance(feedback, list):
            feedback = []
        return {
            'id': retro.id,
            'application_id': retro.application_id,
            'company': app_row.company if app_row else None,
            'role': app_row.role if app_row else None,
            'facts': retro.facts,
            'observed_patterns': retro.observed_patterns,
            'hypotheses': retro.hypotheses,
            'stage_reached': retro.stage_reached,
            'reason': retro.reason,
            'reason_label': retro_data.reason_label(retro.reason)
            if retro.reason else None,
            'completed': retro.completed,
            'completed_at': retro.completed_at.isoformat()
            if retro.completed_at else None,
            'created_at': retro.created_at.isoformat()
            if retro.created_at else None,
            'interview_feedback': feedback,
            'anonymous_share': bool(retro.anonymous_share),
        }

    def _retro_owned_or_404(retro_id):
        retro = Retrospective.query.filter_by(
            id=retro_id, user_id=current_user.id).first()
        return retro

    @app.route('/api/retrospectives/reasons')
    @login_required
    def retro_reasons_api():
        """The 15-entry reason taxonomy for the wizard."""
        reasons = [{'id': r[0], 'label': r[1], 'stage_hint': r[2]}
                   for r in retro_data.RETRO_REASONS]
        return jsonify({'success': True, 'reasons': reasons})

    @app.route('/api/retrospectives/trends')
    @login_required
    def retro_trends_api():
        """90-day trends over the user's COMPLETED retrospectives only.
        Counts come from rows, never invented."""
        cutoff = datetime.now(UTC) - timedelta(days=90)
        retros = Retrospective.query.filter_by(
            user_id=current_user.id, completed=True).all()

        def _when(r):
            when = r.completed_at or r.created_at
            if when is not None and getattr(when, 'tzinfo', None) is None:
                when = when.replace(tzinfo=UTC)
            return when

        window = [r for r in retros if _when(r) and _when(r) >= cutoff]
        by_reason: dict[str, dict] = {}
        by_stage: dict[str, int] = {}
        by_week: dict[str, int] = {}
        for r in window:
            if r.reason and r.reason in retro_data.RETRO_REASON_IDS:
                key, label = r.reason, retro_data.REASON_LABELS[r.reason]
            elif r.reason:
                key, label = 'custom', 'Custom (free text)'
            else:
                key, label = 'unspecified', 'Not recorded'
            entry = by_reason.setdefault(key, {'reason': key, 'label': label,
                                               'count': 0})
            entry['count'] += 1
            stage = r.stage_reached or 'unspecified'
            by_stage[stage] = by_stage.get(stage, 0) + 1
            iso = _when(r).isocalendar()
            week = '%d-W%02d' % (iso[0], iso[1])
            by_week[week] = by_week.get(week, 0) + 1
        trends = {
            'by_reason': sorted(by_reason.values(),
                                key=lambda e: (-e['count'], e['reason'])),
            'by_stage': [{'stage': k, 'count': v}
                         for k, v in sorted(by_stage.items(),
                                            key=lambda kv: (-kv[1], kv[0]))],
            'by_week': [{'week': k, 'count': v}
                        for k, v in sorted(by_week.items())],
            'total': len(window),
        }
        return jsonify({'success': True, 'trends': trends})

    @app.route('/api/retrospectives/experiments/<int:exp_id>',
               methods=['PATCH'])
    @limiter.limit("60 per hour")
    def retro_experiment_outcome(exp_id):
        """Record an experiment's outcome. Owner only (else 404).
        CSRF via X-CSRFToken header (not exempt)."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        exp = Experiment.query.filter_by(
            id=exp_id, user_id=current_user.id).first()
        if exp is None:
            return jsonify({'error': 'Experiment not found.'}), 404
        payload = request.get_json(silent=True) or {}
        outcome = (payload.get('outcome') or '').strip()
        if outcome not in ('helped', 'no-change', 'hurt', 'pending'):
            return jsonify({'error': 'Invalid outcome. Use one of: helped, '
                                     'no-change, hurt, pending.'}), 400
        note = (payload.get('outcome_note') or '').strip()
        if len(note) > 2000:
            return jsonify({'error': 'Outcome note is too long '
                                     '(max 2,000 characters).'}), 400
        exp.outcome = outcome
        exp.outcome_note = note or None
        db.session.commit()
        return jsonify({'success': True,
                        'experiment': _retro_experiment_to_dict(exp)})

    @app.route('/api/retrospectives/experiments')
    @login_required
    def retro_experiments_api():
        """The user's experiments with outcome fields (Phase 4 view)."""
        exps = (Experiment.query.filter_by(user_id=current_user.id)
                .order_by(Experiment.created_at.desc()).all())
        return jsonify({'success': True,
                        'experiments': [_retro_experiment_to_dict(e)
                                        for e in exps]})

    @app.route('/api/retrospectives/<int:retro_id>')
    @login_required
    def retro_detail_api(retro_id):
        """One retrospective, owner only — includes interview feedback."""
        retro = _retro_owned_or_404(retro_id)
        if retro is None:
            return jsonify({'error': 'Retrospective not found.'}), 404
        return jsonify({'success': True,
                        'retrospective': _retro_to_dict(retro)})

    @app.route('/api/retrospectives/<int:retro_id>/feedback',
               methods=['POST'])
    @limiter.limit("60 per hour")
    def retro_feedback_api(retro_id):
        """Append one interview-feedback entry to a retrospective.
        Owner only (else 404). Feedback list caps at 20 entries;
        older entries drop off. CSRF via X-CSRFToken header."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        retro = _retro_owned_or_404(retro_id)
        if retro is None:
            return jsonify({'error': 'Retrospective not found.'}), 404
        payload = request.get_json(silent=True) or {}
        round_name = (payload.get('round') or '').strip()
        if not round_name or len(round_name) > 100:
            return jsonify({'error': 'A round name (max 100 characters) '
                                     'is required.'}), 400
        questions = payload.get('questions_asked')
        if questions is None:
            questions = []
        if not isinstance(questions, list) or len(questions) > 50:
            return jsonify({'error': 'questions_asked must be a list of '
                                     'at most 50 questions.'}), 400
        cleaned_qs = []
        for q in questions:
            if not isinstance(q, str):
                return jsonify({'error': 'Every question must be text.'}), 400
            cleaned_qs.append(q.strip()[:500])
        rating = payload.get('self_rating')
        if isinstance(rating, bool) or not isinstance(rating, int):
            try:
                rating = int(rating)
            except (TypeError, ValueError):
                return jsonify({'error': 'self_rating must be an integer '
                                         'from 1 to 5.'}), 400
        if rating < 1 or rating > 5:
            return jsonify({'error': 'self_rating must be from 1 to 5.'}), 400
        notes = (payload.get('notes') or '').strip()
        if len(notes) > 2000:
            return jsonify({'error': 'Notes are too long '
                                     '(max 2,000 characters).'}), 400
        feedback = retro.interview_feedback or []
        if not isinstance(feedback, list):
            feedback = []
        feedback.append({
            'round': round_name,
            'questions_asked': cleaned_qs,
            'self_rating': rating,
            'notes': notes or None,
            'created_at': datetime.now(UTC).isoformat(),
        })
        retro.interview_feedback = feedback[-20:]
        db.session.commit()
        return jsonify({'success': True,
                        'feedback': retro.interview_feedback})

    @app.route('/api/retrospectives/<int:retro_id>/share-anonymous',
               methods=['POST'])
    @limiter.limit("60 per hour")
    def retro_share_anonymous_api(retro_id):
        """Opt a retrospective into (or out of) the anonymous learnings
        pool. Default is NOT shared. Owner only (else 404). CSRF via
        X-CSRFToken header."""
        if not current_user.is_authenticated:
            return jsonify({'error': 'Please log in.'}), 401
        retro = _retro_owned_or_404(retro_id)
        if retro is None:
            return jsonify({'error': 'Retrospective not found.'}), 404
        payload = request.get_json(silent=True) or {}
        share = payload.get('share')
        if not isinstance(share, bool):
            return jsonify({'error': 'share must be true or false.'}), 400
        retro.anonymous_share = share
        db.session.commit()
        return jsonify({'success': True, 'shared': share})

    @app.route('/api/retrospectives/learnings')
    @limiter.limit("60 per hour")
    def retro_learnings_api():
        """Public anonymous learnings: (reason, stage) counts ONLY over
        retrospectives the owner opted into sharing, taxonomy reasons
        only (free text never leaves the account), and cells with fewer
        than 3 contributors are suppressed to avoid identification."""
        shared = Retrospective.query.filter_by(
            anonymous_share=True).all()
        cells: dict[tuple, int] = {}
        for r in shared:
            if not r.reason or r.reason not in retro_data.RETRO_REASON_IDS:
                continue
            stage = r.stage_reached or 'unspecified'
            key = (r.reason, stage)
            cells[key] = cells.get(key, 0) + 1
        learnings = [{'reason': reason,
                      'label': retro_data.REASON_LABELS[reason],
                      'stage': stage, 'count': count}
                     for (reason, stage), count in sorted(cells.items())
                     if count >= 3]
        return jsonify({'success': True, 'learnings': learnings})

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

    # ================= Phase 5: health pulse, action plans, benchmarks ======
    # Scale rule: monthly-review scores are 0-100; pulses are 1-5. Every
    # Phase 5 surface reports the 1-5 scale (reviews converted via
    # review_score_to_1to5) and names its source. Missing data is null,
    # never 0, and peer benchmarks never include the requesting user.

    def _health_series(user_id):
        """Per-dimension time series [(sort_date, value_1to5, source)].

        Reviews contribute one point per month (converted 0-100 -> 1-5);
        pulses one point per ISO week. Points without a score are skipped
        (locked/unknown is not a zero).
        """
        series = {d["id"]: [] for d in HR_DIMENSIONS}
        try:
            reviews = (HealthReview.query
                       .filter_by(user_id=user_id)
                       .order_by(HealthReview.month_key.asc()).all())
            for r in reviews:
                try:
                    y, m = int(r.month_key[:4]), int(r.month_key[5:7])
                    when = date(y, m, 1)
                except (ValueError, TypeError):
                    continue
                dims = r.dimensions_json or {}
                for dim in series:
                    entry = dims.get(dim) or {}
                    v = mentor_health_mod.review_score_to_1to5(
                        entry.get("score"))
                    if v is not None:
                        series[dim].append((when, v, "review"))
            pulses = (HealthPulse.query
                      .filter_by(user_id=user_id)
                      .order_by(HealthPulse.week_key.asc()).all())
            for p in pulses:
                try:
                    when = datetime.strptime(
                        p.week_key + "-1", "%G-W%V-%u").date()
                except (ValueError, TypeError):
                    continue
                scores = p.scores or {}
                for dim in series:
                    v = scores.get(dim)
                    if isinstance(v, int) and not isinstance(v, bool) \
                            and 1 <= v <= 5:
                        series[dim].append((when, v, "pulse"))
        except Exception:
            log.exception("Health series build failed")
            db.session.rollback()
        for dim in series:
            series[dim].sort(key=lambda t: t[0])
        return series

    def _health_latest(series):
        """{dim: (current_score, score_source, trend)} from a series.

        Trend compares the last two points (up/down/flat); null when
        fewer than two points exist."""
        out = {}
        for dim, points in series.items():
            if not points:
                out[dim] = (None, None, None)
                continue
            current, source = points[-1][1], points[-1][2]
            if len(points) >= 2:
                prev = points[-2][1]
                trend = ("up" if current > prev else
                         "down" if current < prev else "flat")
            else:
                trend = None
            out[dim] = (current, source, trend)
        return out

    @app.route('/api/health/pulse', methods=['POST'])
    @login_required
    @limiter.limit("30 per hour")
    def health_pulse_submit_api():
        """Save this ISO week's pulse. {scores: {dimension: 1-5}}; partial
        scores allowed, but every key must be a real dimension and every
        value an int 1-5 (400 otherwise). Upserts the current week's row.
        """
        payload = request.get_json(silent=True) or {}
        scores = payload.get('scores')
        dim_ids = {d["id"] for d in HR_DIMENSIONS}
        if not isinstance(scores, dict) or not scores:
            return jsonify({'error': 'scores must be a non-empty object.'}), 400
        clean = {}
        for dim, value in scores.items():
            if dim not in dim_ids:
                return jsonify({'error': 'Unknown dimension: %s.' % dim}), 400
            if isinstance(value, bool) or not isinstance(value, int) \
                    or not 1 <= value <= 5:
                return jsonify({'error': 'Scores must be integers 1-5.'}), 400
            clean[dim] = value
        week_key = mentor_health_mod.iso_week_key(date.today())
        try:
            row = HealthPulse.query.filter_by(
                user_id=current_user.id, week_key=week_key).first()
            if row is None:
                row = HealthPulse(user_id=current_user.id, week_key=week_key,
                                  scores=clean)
                db.session.add(row)
            else:
                row.scores = clean
            db.session.commit()
        except Exception:
            log.exception("Health pulse save failed")
            db.session.rollback()
            return jsonify({'error': 'Could not save your pulse. '
                                     'Please try again.'}), 500
        return jsonify({'success': True, 'pulse': {
            'week_key': row.week_key, 'scores': row.scores}})

    @app.route('/api/health/pulse')
    @login_required
    def health_pulse_get_api():
        """Current week's pulse (or null) + history, last 8 weeks newest
        first. Pure read."""
        try:
            rows = (HealthPulse.query.filter_by(user_id=current_user.id)
                    .order_by(HealthPulse.week_key.desc()).limit(8).all())
        except Exception:
            log.exception("Health pulse read failed")
            db.session.rollback()
            rows = []
        week_key = mentor_health_mod.iso_week_key(date.today())
        current = next(({'week_key': r.week_key, 'scores': r.scores}
                        for r in rows if r.week_key == week_key), None)
        return jsonify({'success': True, 'pulse': current, 'history': [
            {'week_key': r.week_key, 'scores': r.scores} for r in rows]})

    @app.route('/api/health/action-plans')
    @login_required
    def health_action_plans_api():
        """Per-dimension action plans. current_score is 1-5 (review scores
        converted) with score_source 'review'|'pulse'|null; trend from the
        last two data points. Actions link only to real routes; goals
        lists the titles of the user's open health goals for the dimension.
        """
        series = _health_series(current_user.id)
        latest = _health_latest(series)
        try:
            goals = HealthGoal.query.filter_by(
                user_id=current_user.id, done=False).all()
        except Exception:
            log.exception("Health goals read failed")
            db.session.rollback()
            goals = []
        plans = []
        for d in HR_DIMENSIONS:
            dim = d["id"]
            current, source, trend = latest[dim]
            plans.append({
                'dimension': dim,
                'label': mentor_health_mod.DIMENSION_LABELS.get(dim, dim),
                'current_score': current,
                'score_source': source,
                'trend': trend,
                'actions': mentor_health_mod.ACTION_PLAN_ACTIONS.get(dim, []),
                'goals': [g.title for g in goals if g.dimension == dim]})
        return jsonify({'success': True, 'plans': plans})

    @app.route('/api/health/trends')
    @login_required
    def health_trends_api():
        """Trend-chart data (pure read). months: last 6 month keys with
        per-dimension scores (null when no review that month — never 0).
        pulses: last 8 weeks, newest first."""
        months = mentor_health_mod.last_n_month_keys(6)
        try:
            reviews = (HealthReview.query
                       .filter_by(user_id=current_user.id)
                       .filter(HealthReview.month_key.in_(months)).all())
            pulses = (HealthPulse.query.filter_by(user_id=current_user.id)
                      .order_by(HealthPulse.week_key.desc()).limit(8).all())
        except Exception:
            log.exception("Health trends read failed")
            db.session.rollback()
            reviews, pulses = [], []
        by_month = {}
        for r in reviews:
            dims = r.dimensions_json or {}
            by_month[r.month_key] = {
                dim: mentor_health_mod.review_score_to_1to5(
                    (dims.get(dim) or {}).get("score"))
                for dim in mentor_health_mod.DIMENSION_LABELS}
        return jsonify({'success': True, 'trends': {
            'months': [{'month_key': mk, 'dimensions': by_month.get(
                mk, {dim: None
                     for dim in mentor_health_mod.DIMENSION_LABELS})}
                for mk in months],
            'pulses': [{'week_key': p.week_key, 'scores': p.scores}
                       for p in pulses]}})

    @app.route('/api/health/benchmarks')
    @login_required
    def health_benchmarks_api():
        """Anonymous peer benchmarks (median, never mean, never any
        individual's score; the requester is excluded). Per dimension and
        per other user: latest monthly review score (converted 1-5) if
        one exists, else latest pulse score. Dimensions with fewer than
        5 peers report peer_median null with the sample size shown; a
        top-level note explains the threshold when any dimension is
        suppressed."""
        dim_ids = list(mentor_health_mod.DIMENSION_LABELS)
        try:
            others = db.session.query(User.id).filter(
                User.id != current_user.id).all()
            other_ids = [u[0] for u in others]
            samples = {dim: [] for dim in dim_ids}
            for uid in other_ids:
                review = (HealthReview.query.filter_by(user_id=uid)
                          .order_by(HealthReview.month_key.desc()).first())
                pulse = (HealthPulse.query.filter_by(user_id=uid)
                         .order_by(HealthPulse.week_key.desc()).first())
                dims = (review.dimensions_json or {}) if review else {}
                for dim in dim_ids:
                    v = None
                    if review is not None:
                        v = mentor_health_mod.review_score_to_1to5(
                            (dims.get(dim) or {}).get("score"))
                    if v is None and pulse is not None:
                        pv = (pulse.scores or {}).get(dim)
                        if isinstance(pv, int) and not isinstance(pv, bool) \
                                and 1 <= pv <= 5:
                            v = pv
                    if v is not None:
                        samples[dim].append(v)
        except Exception:
            log.exception("Health benchmarks failed")
            db.session.rollback()
            samples = {dim: [] for dim in dim_ids}
        benchmarks = []
        suppressed = False
        for dim in dim_ids:
            vals = samples[dim]
            if len(vals) >= 5:
                median = mentor_health_mod.median(vals)
            else:
                median = None
                suppressed = True
            benchmarks.append({'dimension': dim, 'peer_median': median,
                               'sample_size': len(vals)})
        body = {'success': True, 'benchmarks': benchmarks}
        if suppressed:
            body['note'] = ('Peer medians need at least 5 other students; '
                            'smaller samples show null so no one can be '
                            'identified.')
        return jsonify(body)

    @app.route('/api/health/goals', methods=['POST'])
    @login_required
    @limiter.limit("30 per hour")
    def health_goal_create_api():
        """Create a health-linked goal. {title, dimension?, target_date?}.
        dimension, when given, must be a real dimension; target_date, when
        given, must be YYYY-MM-DD (400 otherwise)."""
        payload = request.get_json(silent=True) or {}
        title = (payload.get('title') or '').strip()
        if not title or len(title) > 255:
            return jsonify({'error': 'Title is required (max 255 '
                                     'characters).'}), 400
        dimension = (payload.get('dimension') or '').strip().lower() or None
        if dimension is not None and dimension not in \
                mentor_health_mod.DIMENSION_LABELS:
            return jsonify({'error': 'Unknown dimension: %s.' % dimension}), 400
        target_date = None
        raw_date = (payload.get('target_date') or '').strip()
        if raw_date:
            try:
                target_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                return jsonify({'error': 'target_date must be YYYY-MM-DD.'}), 400
        try:
            goal = HealthGoal(user_id=current_user.id, title=title,
                              dimension=dimension, target_date=target_date,
                              done=False)
            db.session.add(goal)
            db.session.commit()
        except Exception:
            log.exception("Health goal create failed")
            db.session.rollback()
            return jsonify({'error': 'Could not save your goal. '
                                     'Please try again.'}), 500
        return jsonify({'success': True, 'goal': {
            'id': goal.id, 'title': goal.title, 'dimension': goal.dimension,
            'target_date': goal.target_date.isoformat()
            if goal.target_date else None, 'done': goal.done}}), 201

    @app.route('/api/health/goals')
    @login_required
    def health_goal_list_api():
        """The caller's health goals, newest first."""
        try:
            rows = (HealthGoal.query.filter_by(user_id=current_user.id)
                    .order_by(HealthGoal.created_at.desc()).all())
        except Exception:
            log.exception("Health goal list failed")
            db.session.rollback()
            rows = []
        return jsonify({'success': True, 'goals': [{
            'id': g.id, 'title': g.title, 'dimension': g.dimension,
            'target_date': g.target_date.isoformat()
            if g.target_date else None, 'done': g.done,
            'created_at': g.created_at.isoformat()
            if g.created_at else None} for g in rows]})

    @app.route('/api/health/goals/<int:goal_id>', methods=['PATCH'])
    @login_required
    def health_goal_patch_api(goal_id):
        """Toggle a health goal's done flag (owner only, else 404)."""
        goal = HealthGoal.query.filter_by(
            id=goal_id, user_id=current_user.id).first()
        if goal is None:
            return jsonify({'error': 'Goal not found.'}), 404
        payload = request.get_json(silent=True) or {}
        if 'done' not in payload or not isinstance(payload['done'], bool):
            return jsonify({'error': 'done must be a boolean.'}), 400
        try:
            goal.done = payload['done']
            db.session.commit()
        except Exception:
            log.exception("Health goal patch failed")
            db.session.rollback()
            return jsonify({'error': 'Could not update your goal. '
                                     'Please try again.'}), 500
        return jsonify({'success': True, 'goal': {
            'id': goal.id, 'title': goal.title, 'dimension': goal.dimension,
            'done': goal.done}})

    @app.route('/api/health/share-with-mentor', methods=['POST'])
    @login_required
    @limiter.limit("30 per hour")
    def health_share_create_api():
        """Grant one REAL mentor access to your health summary. Seeds are
        rejected (400). Re-sharing returns the existing grant (200 with
        already_shared) instead of an error."""
        payload = request.get_json(silent=True) or {}
        try:
            mentor_id = int(payload.get('mentor_id'))
        except (TypeError, ValueError):
            return jsonify({'error': 'mentor_id must be an integer.'}), 400
        mentor = db.session.get(Mentor, mentor_id)
        if mentor is None:
            return jsonify({'error': 'Mentor not found.'}), 404
        if mentor.is_seed:
            return jsonify({'error': 'Illustrative profiles cannot receive '
                                     'health shares.'}), 400
        try:
            existing = HealthShare.query.filter_by(
                user_id=current_user.id, mentor_id=mentor.id).first()
            if existing is not None:
                return jsonify({'success': True, 'shared': True,
                                'already_shared': True})
            db.session.add(HealthShare(user_id=current_user.id,
                                       mentor_id=mentor.id))
            db.session.commit()
        except Exception:
            log.exception("Health share create failed")
            db.session.rollback()
            return jsonify({'error': 'Could not share. '
                                     'Please try again.'}), 500
        return jsonify({'success': True, 'shared': True,
                        'already_shared': False}), 201

    @app.route('/api/health/shared-with')
    @login_required
    def health_shared_with_api():
        """Mentors this user has shared their health summary with."""
        try:
            rows = (db.session.query(HealthShare, Mentor)
                    .join(Mentor, HealthShare.mentor_id == Mentor.id)
                    .filter(HealthShare.user_id == current_user.id)
                    .order_by(HealthShare.created_at.desc()).all())
        except Exception:
            log.exception("Health shared-with read failed")
            db.session.rollback()
            rows = []
        return jsonify({'success': True, 'shared_with': [{
            'mentor_id': m.id, 'name': m.name,
            'shared_at': s.created_at.isoformat()
            if s.created_at else None} for s, m in rows]})

    @app.route('/api/health/shared-with/<int:mentor_id>', methods=['DELETE'])
    @login_required
    def health_share_delete_api(mentor_id):
        """Revoke a health share (404 when nothing was shared)."""
        share = HealthShare.query.filter_by(
            user_id=current_user.id, mentor_id=mentor_id).first()
        if share is None:
            return jsonify({'error': 'No share found for this mentor.'}), 404
        try:
            db.session.delete(share)
            db.session.commit()
        except Exception:
            log.exception("Health share delete failed")
            db.session.rollback()
            return jsonify({'error': 'Could not revoke the share. '
                                     'Please try again.'}), 500
        return jsonify({'success': True})

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

    # ================= Phase 5: mentor matching, Q&A, reviews, dashboard ====
    # Honesty first: seed mentors are ILLUSTRATIVE profiles (never verified,
    # never bookable, never reviewed). Every serialization carries the
    # illustrative/verified labels from mentor_health_mod.mentor_to_dict.

    @app.route('/mentors/api/admin/applications/<int:application_id>/approve',
                methods=['POST'])
    @login_required
    @limiter.limit("30 per hour")
    def mentor_admin_approve(application_id):
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

    @app.route('/api/mentors/matches')
    @login_required
    @limiter.limit("30 per hour")
    def mentors_matches_api():
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

    @app.route('/api/mentors/qa')
    @limiter.limit("60 per hour")
    def mentors_qa_api():
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

    @app.route('/api/mentors/qa/ask', methods=['POST'])
    @login_required
    @limiter.limit("30 per hour")
    def mentors_qa_ask_api():
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

    @app.route('/api/mentors/qa/mine')
    @login_required
    def mentors_qa_mine_api():
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

    @app.route('/api/mentors/<int:mentor_id>/reviews', methods=['POST'])
    @login_required
    @limiter.limit("30 per hour")
    def mentors_review_create_api(mentor_id):
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

    @app.route('/api/mentors/<int:mentor_id>/reviews')
    @limiter.limit("60 per hour")
    def mentors_review_list_api(mentor_id):
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

    @app.route('/api/mentors/dashboard')
    @login_required
    @limiter.limit("30 per hour")
    def mentors_dashboard_api():
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
