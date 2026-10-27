"""Typed Ultimate Lab tool registry — ARC-05 (complete, Lab Phase 0).

All 91 tools are **schema-wrapped**: every tool has a pydantic input
model, an explicit handler registered in :data:`TOOL_HANDLERS` (plain
direct imports — no ``importlib`` / ``getattr`` on user input), and
auth + rate-limit metadata. The first 22 were hand-written; the
remaining 71 were promoted mechanically from their registry field defs
(see hidden_work/gen_lab_migration.py for the mapping rules) and
verified against the legacy dispatcher by an equivalence harness
before the legacy branch was deleted.

Use :func:`run_typed_tool` for every tool; it mirrors the legacy
``run_tool(tool_id, payload) -> (result, error)`` contract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from pydantic import BaseModel, BeforeValidator, Field, ValidationError
from typing_extensions import Annotated

# ---------------------------------------------------------------------------
# Explicit backend imports — the allowlist. Adding a tool means adding an
# import + a ToolHandler entry below. Nothing here is resolved from strings.
# ---------------------------------------------------------------------------
from studybuddy.bullet_rewriter import (  # noqa: E402
    metric_finder,
    star_convert,
    verb_power_report,
)
from studybuddy.career_assessment import ai_role_fit  # noqa: E402
from studybuddy.cert_guide import cert_roi  # noqa: E402
from studybuddy.communication_coach import clarity_score  # noqa: E402
from studybuddy.cover_generator import outreach_score  # noqa: E402
from studybuddy.dsa_practice import pattern_mastery  # noqa: E402
from studybuddy.github_integration import grade_readme  # noqa: E402
from studybuddy.jd_matcher import (  # noqa: E402
    competitiveness_checklist,
    illustrative_screening_checklist,
    keyword_action_plan,
)
from studybuddy.learning_roadmaps import weekly_planner  # noqa: E402
from studybuddy.linkedin_grader import recruiter_search_check  # noqa: E402
from studybuddy.mock_interview import voice_interview_prep  # noqa: E402
from studybuddy.pdf_qa import qa_to_notes  # noqa: E402
from studybuddy.aptitude_test import pick_questions as aptitude_pick  # noqa: E402
from studybuddy.aptitude_test import score_attempt as aptitude_score  # noqa: E402
from studybuddy.portfolio import check_demo_links  # noqa: E402
from studybuddy.salary_data import evaluate_offer  # noqa: E402
from studybuddy.skill_quizzes import weakness_drill  # noqa: E402
from studybuddy.star_builder import story_bank_manager  # noqa: E402


# ---------------------------------------------------------------------------
# Coercion helpers — accept what the legacy dispatcher accepted.
# ---------------------------------------------------------------------------
def _as_json_list(v: Any) -> list:
    """Accept a JSON string, a newline/comma-separated string, or a list."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return parsed
            return [parsed]
        except (ValueError, TypeError):
            pass
        if "\n" in s:
            return [ln.strip() for ln in s.splitlines() if ln.strip()]
        return [p.strip() for p in s.split(",") if p.strip()]
    return [v]


def _as_json_value(v: Any) -> Any:
    """Accept an already-parsed value or a JSON string."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return v
    return v


def _as_json_object(v: Any) -> dict:
    """Like _as_json_value but the result MUST be a JSON object.

    Fields whose handlers call .get() on the value (e.g. the JD-match
    report) previously accepted any JsonValue: a non-JSON string passed
    validation untouched and the handler died with AttributeError,
    surfaced to users as an opaque 'Tool failed'. Raising here turns
    that into a clear field-level validation error instead.
    """
    parsed = _as_json_value(v)
    if not isinstance(parsed, dict):
        raise ValueError("must be a JSON object (e.g. {\"score\": 72, ...})")
    return parsed


JsonList = Annotated[list, BeforeValidator(_as_json_list)]
JsonValue = Annotated[Any, BeforeValidator(_as_json_value)]
JsonObject = Annotated[dict, BeforeValidator(_as_json_object)]


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------
class ToolMeta(BaseModel):
    """Per-tool contract metadata (ARC-05 acceptance criteria)."""

    tool_id: str
    feature: str
    name: str
    description: str = ""
    requires_login: bool = True  # SEC-01: Lab tools require auth
    rate_limit: str = "60 per hour"
    needs_network: bool = False
    uses_llm: bool = False
    reads_db: bool = False
    writes_db: bool = False
    data_source: str = "user input (rule-based)"
    owner: str = "study-buddy-core"
    status: str = "migrated"  # or "legacy-unmigrated"


class ToolOutput(BaseModel):
    """Outputs are JSON-safe dicts (schemas vary per tool; v1 pragmatic)."""

    model_config = {"extra": "allow"}

    def jsonable(self) -> dict:
        data = self.model_dump()
        # ensure JSON-serialisable
        return json.loads(json.dumps(data, default=str))


@dataclass
class ToolHandler:
    """One allowlisted tool: schema + direct function reference."""

    meta: ToolMeta
    input_model: type[BaseModel]
    fn: Callable[..., Any]
    extra_kwargs: dict = field(default_factory=dict)
    # Generated (legacy-promoted) handlers drop None-valued optionals
    # before calling fn, replicating the legacy dispatcher, which
    # skipped empty optional fields instead of passing None.
    drop_none: bool = False

    def run(self, payload: dict) -> tuple[Optional[dict], Optional[str]]:
        try:
            validated = self.input_model.model_validate(payload)
        except ValidationError as exc:
            first = exc.errors()[0]
            loc = ".".join(str(p) for p in first["loc"])
            return None, f"Field '{loc}': {first['msg']}."
        kwargs = validated.model_dump()
        if self.drop_none:
            kwargs = {k: v for k, v in kwargs.items() if v is not None}
        try:
            result = self.fn(**kwargs, **self.extra_kwargs)
        except (ValueError, TypeError) as exc:
            return None, str(exc) or "Invalid input."
        except Exception as exc:  # never leak tracebacks
            return None, f"Tool failed: {type(exc).__name__}."
        try:
            return json.loads(json.dumps(result, default=str)), None
        except (ValueError, TypeError):
            return None, "Tool returned a non-serialisable result."


# ---------------------------------------------------------------------------
# Input schemas (top-20 tools)
# ---------------------------------------------------------------------------
class _NonEmpty(BaseModel):
    model_config = {"extra": "forbid"}


class PrescreenIn(_NonEmpty):
    resume_text: str = Field(min_length=1, max_length=60000)
    report: JsonObject


class KeywordPlanIn(_NonEmpty):
    resume_text: str = Field(min_length=1, max_length=60000)
    report: JsonObject


class OfferOddsIn(_NonEmpty):
    report: JsonObject
    applicants_estimate: Optional[int] = Field(default=None, ge=0)


class StarConvertIn(_NonEmpty):
    bullet_text: str = Field(min_length=1, max_length=2000)


class VerbPowerIn(_NonEmpty):
    bullets: JsonList


class MetricFinderIn(_NonEmpty):
    bullet_text: str = Field(min_length=1, max_length=2000)


class OutreachScoreIn(_NonEmpty):
    message: str = Field(min_length=1, max_length=10000)
    recipient_name: str = Field(default="", max_length=200)
    company: str = Field(default="", max_length=200)


class SearchCheckIn(_NonEmpty):
    headline: str = Field(min_length=1, max_length=500)
    about: str = Field(min_length=1, max_length=5000)
    skills: str = Field(min_length=1, max_length=2000)
    target_queries: JsonList


class ReadmeGradeIn(_NonEmpty):
    readme_text: str = Field(min_length=1, max_length=60000)


class AiFitIn(_NonEmpty):
    user_skills: JsonList


class SalaryEvaluateIn(_NonEmpty):
    role: str = Field(min_length=1, max_length=200)
    tier: str = Field(min_length=1, max_length=100)
    band: str = Field(min_length=1, max_length=100)
    offer_lpa: float = Field(ge=0, le=1000)


class LinkCheckIn(_NonEmpty):
    projects: JsonValue


class VoicePrepIn(_NonEmpty):
    question: JsonValue
    answer_text: str = Field(min_length=1, max_length=20000)
    seconds_taken: int = Field(ge=0, le=3600)


class PatternMasteryIn(_NonEmpty):
    solved_ids: JsonList
    attempted_ids: JsonList = Field(default_factory=list)


class ClarityScoreIn(_NonEmpty):
    transcript: str = Field(min_length=1, max_length=60000)


class StoryBankIn(_NonEmpty):
    stories: JsonValue


class WeeklyPlannerIn(_NonEmpty):
    roadmap_stages: JsonValue
    hours_per_week: int = Field(default=6, ge=1, le=80)


class WeaknessDrillIn(_NonEmpty):
    skill: str = Field(min_length=1, max_length=200)
    past_results: JsonValue
    n: int = Field(default=10, ge=1, le=50)


class CertRoiIn(_NonEmpty):
    cert_id: str = Field(min_length=1, max_length=200)
    current_lpa: float = Field(ge=0, le=1000)


class QaToNotesIn(_NonEmpty):
    qa_pairs: JsonValue
    title: str = Field(default="Study Notes", max_length=300)


class AptitudePickIn(_NonEmpty):
    category: Optional[str] = Field(default=None, max_length=50)
    n: int = Field(default=10, ge=1, le=50)
    seed: Optional[int] = Field(default=None)


class AptitudeScoreIn(_NonEmpty):
    question_ids: list[str] = Field(min_length=1, max_length=50)
    answers: dict[str, str]


def _meta(tool_id: str, feature: str, name: str, **kw: Any) -> ToolMeta:
    return ToolMeta(tool_id=tool_id, feature=feature, name=name, **kw)


# ---------------------------------------------------------------------------
# The allowlist: tool_id -> handler. Dispatch goes through this dict ONLY.
# ---------------------------------------------------------------------------
TOOL_HANDLERS: dict[str, ToolHandler] = {}


def _reg(meta: ToolMeta, input_model: type[BaseModel], fn: Callable[..., Any],
         **kw: Any) -> None:
    TOOL_HANDLERS[meta.tool_id] = ToolHandler(
        meta=meta, input_model=input_model, fn=fn, **kw
    )


_reg(_meta("jd/prescreen", "JD-Matched ATS Scoring", "Illustrative Screening Checklist"),
     PrescreenIn, illustrative_screening_checklist)
_reg(_meta("jd/keyword-plan", "JD-Matched ATS Scoring", "Missing-Keyword Action Plan"),
     KeywordPlanIn, keyword_action_plan)
_reg(_meta("jd/offer-odds", "JD-Matched ATS Scoring", "Competitiveness Checklist"),
     OfferOddsIn, competitiveness_checklist)
_reg(_meta("bullets/star-convert", "AI Bullet Rewriter", "STAR Auto-Converter"),
     StarConvertIn, star_convert)
_reg(_meta("bullets/verb-power", "AI Bullet Rewriter", "Verb Power Ranking"),
     VerbPowerIn, verb_power_report)
_reg(_meta("bullets/metric-finder", "AI Bullet Rewriter", "Metric Finder"),
     MetricFinderIn, metric_finder)
_reg(_meta("outreach/score", "Outreach", "Outreach Score"),
     OutreachScoreIn, outreach_score)
_reg(_meta("linkedin/search-check", "LinkedIn Grader", "Recruiter Search Check"),
     SearchCheckIn, recruiter_search_check)
_reg(_meta("github/readme-grade", "GitHub Integration", "README Grade",
           needs_network=False, data_source="user input (rule-based)"),
     ReadmeGradeIn, grade_readme)
_reg(_meta("assessment/ai-fit", "Career Assessment", "AI Role Fit"),
     AiFitIn, ai_role_fit)
_reg(_meta("salary/evaluate", "Salary Data", "Evaluate Offer",
           data_source="curated JSON"),
     SalaryEvaluateIn, evaluate_offer)
_reg(_meta("portfolio/link-check", "Portfolio", "Check Demo Links",
           needs_network=True, rate_limit="30 per hour",
           data_source="live HTTP fetch"),
     LinkCheckIn, check_demo_links)
_reg(_meta("mock/voice-prep", "Mock Interview", "Voice Interview Prep"),
     VoicePrepIn, voice_interview_prep)
_reg(_meta("dsa/pattern-mastery", "DSA Practice", "Pattern Mastery"),
     PatternMasteryIn, pattern_mastery)
_reg(_meta("coach/clarity-score", "Communication Coach", "Clarity Score"),
     ClarityScoreIn, clarity_score)
_reg(_meta("star/story-bank", "STAR Builder", "Story Bank Manager"),
     StoryBankIn, story_bank_manager)
_reg(_meta("roadmaps/weekly-planner", "Learning Roadmaps", "Weekly Planner"),
     WeeklyPlannerIn, weekly_planner)
_reg(_meta("quizzes/weakness-drill", "Skill Quizzes", "Weakness Drill"),
     WeaknessDrillIn, weakness_drill)
_reg(_meta("certs/roi", "Cert Guide", "Cert ROI", data_source="curated JSON"),
     CertRoiIn, cert_roi)
_reg(_meta("pdfqa/qa-to-notes", "PDF Q&A", "Q&A to Notes"),
     QaToNotesIn, qa_to_notes)
_reg(_meta("aptitude/pick-questions", "Aptitude Test", "Pick Questions",
           data_source="curated JSON (815 questions)"),
     AptitudePickIn, aptitude_pick)
_reg(_meta("aptitude/score", "Aptitude Test", "Score Attempt",
           data_source="curated JSON"),
     AptitudeScoreIn, aptitude_score)


# --- Legacy validators (mirror ultimate_routes._coerce_value) ---
from datetime import date as _date_cls  # noqa: E402


def _as_legacy_number(v):
    """int when the raw value has no dot, else float (legacy rule)."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            raise ValueError("must be a number")
        try:
            return int(s) if "." not in s else float(s)
        except ValueError:
            raise ValueError("must be a number")
    raise ValueError("must be a number")


def _as_legacy_bool(v):
    """Legacy true-set: True / "true" / "on" / "1" / 1; else False."""
    return v in (True, "true", "on", "1", 1)


def _as_legacy_date(v):
    """Legacy date rule: strip()[:10] + fromisoformat."""
    if isinstance(v, _date_cls):
        return v
    if isinstance(v, str):
        try:
            return _date_cls.fromisoformat(v.strip()[:10])
        except ValueError:
            raise ValueError("must be YYYY-MM-DD.")
    raise ValueError("must be YYYY-MM-DD.")


LegacyNumber = Annotated[int | float, BeforeValidator(_as_legacy_number)]
LegacyBool = Annotated[bool, BeforeValidator(_as_legacy_bool)]
LegacyDate = Annotated[_date_cls, BeforeValidator(_as_legacy_date)]


class _GeneratedBase(BaseModel):
    """Generated models ignore unknown keys (legacy ignored them too)."""

    model_config = {"extra": "ignore"}

from studybuddy.arxiv_import import bulk_import
from studybuddy.arxiv_import import reading_queue
from studybuddy.arxiv_import import related_papers
from studybuddy.career_assessment import assessment_delta
from studybuddy.career_assessment import roadmap_export
from studybuddy.cert_guide import exam_day_simulator
from studybuddy.cert_guide import renewal_status
from studybuddy.citation_fmt import find_duplicates
from studybuddy.citation_fmt import in_text_citation
from studybuddy.citation_fmt import match_journal_style
from studybuddy.communication_coach import hr_round_simulator
from studybuddy.communication_coach import video_presence_checklist
from studybuddy.cover_generator import followup_sequence
from studybuddy.cover_generator import suggest_recipients
from studybuddy.dsa_practice import mock_oa
from studybuddy.dsa_practice import spaced_repetition_queue
from studybuddy.flashcards import anki_export
from studybuddy.flashcards import exam_sprint
from studybuddy.flashcards import weak_card_loop
from studybuddy.gap_finder import feasibility_score
from studybuddy.gap_finder import gap_to_proposal
from studybuddy.gap_finder import trend_cross_check
from studybuddy.github_integration import commit_messages_to_bullets
from studybuddy.github_integration import pin_optimizer
from studybuddy.health_review import action_nudges
from studybuddy.health_review import monthly_report
from studybuddy.health_review import peer_benchmarks
from studybuddy.learning_roadmaps import agentic_ai_track
from studybuddy.learning_roadmaps import project_milestones
from studybuddy.linkedin_grader import content_plan
from studybuddy.linkedin_grader import profile_jd_gap
from studybuddy.lit_review import cluster_by_theme
from studybuddy.lit_review import gap_highlight_review
from studybuddy.lit_review import length_preset
from studybuddy.mentor_extras import alumni_outcomes
from studybuddy.mentor_extras import async_qa_digest
from studybuddy.mentor_extras import prep_packet
from studybuddy.mock_interview import company_intel
from studybuddy.mock_interview import redo_tracker
from studybuddy.momentum import reward_tiers
from studybuddy.momentum import squad_leaderboard
from studybuddy.momentum import streak_freeze_status
from studybuddy.opportunities import deadline_alerts
from studybuddy.opportunities import fit_score
from studybuddy.opportunities import to_tracker_payload
from studybuddy.pdf_qa import answer_across_docs
from studybuddy.pdf_qa import verify_citations
from studybuddy.peer_mocks import match_partners
from studybuddy.peer_mocks import recurring_slot
from studybuddy.peer_mocks import score_rubric
from studybuddy.portfolio import generate_case_study
from studybuddy.portfolio import recruiter_preview
from studybuddy.resume_versions import compare_versions_for_jd
from studybuddy.resume_versions import diff_resume_versions
from studybuddy.resume_versions import tailor_resume_for_jd
from studybuddy.resume_versions import version_performance
from studybuddy.retro_insights import pattern_alerts
from studybuddy.retro_insights import track_experiment
from studybuddy.retro_insights import win_library
from studybuddy.salary_data import band_comparison
from studybuddy.salary_data import negotiation_coach
from studybuddy.skill_quizzes import certification_exam_mode
from studybuddy.skill_quizzes import leaderboard
from studybuddy.star_builder import predict_followups
from studybuddy.star_builder import thirty_second_pitch
from studybuddy.tracker_analytics import compare_offers
from studybuddy.tracker_analytics import followup_nudges
from studybuddy.tracker_analytics import pipeline_analytics
from studybuddy.trending import paper_to_project_ideas
from studybuddy.trending import personalized_feed
from studybuddy.trending import weekly_digest


class VersionsDiffIn(_GeneratedBase):
    text_a: str = Field(min_length=1)
    text_b: str = Field(min_length=1)
    name_a: str = Field(default=None)
    name_b: str = Field(default=None)


class VersionsJdCompareIn(_GeneratedBase):
    text_a: str = Field(min_length=1)
    text_b: str = Field(min_length=1)
    jd_text: str = Field(min_length=1)
    experience_years: LegacyNumber = Field(default=None)


class VersionsTailorIn(_GeneratedBase):
    resume_text: str = Field(min_length=1)
    jd_text: str = Field(min_length=1)
    version_name: str = Field(default=None)


class VersionsPerformanceIn(_GeneratedBase):
    applications: JsonValue = Field(...)


class OutreachRecipientsIn(_GeneratedBase):
    role_title: str = Field(min_length=1)
    company_name: str = Field(default=None)


class OutreachFollowupIn(_GeneratedBase):
    name: str = Field(min_length=1)
    role_title: str = Field(min_length=1)
    company_name: str = Field(default=None)
    tone: str = Field(default=None, json_schema_extra={"options": ['professional', 'enthusiastic', 'concise']})


class LinkedinContentPlanIn(_GeneratedBase):
    skills: JsonList = Field(...)
    weeks: LegacyNumber = Field(default=None)


class LinkedinJdGapIn(_GeneratedBase):
    headline: str = Field(min_length=1)
    about: str = Field(min_length=1)
    experience: str = Field(min_length=1)
    skills: JsonList = Field(...)
    jd_text: str = Field(min_length=1)


class GithubCommitsToBulletsIn(_GeneratedBase):
    commit_messages: JsonList = Field(...)
    repo_name: str = Field(default=None)


class GithubPinOptimizerIn(_GeneratedBase):
    repos: JsonValue = Field(...)
    target_role: str = Field(default=None)


class AssessmentRoadmapIn(_GeneratedBase):
    cluster: JsonObject = Field(...)
    weeks: LegacyNumber = Field(default=None)


class AssessmentDeltaIn(_GeneratedBase):
    old_results: JsonValue = Field(...)
    new_results: JsonValue = Field(...)


class SalaryNegotiateIn(_GeneratedBase):
    situation: str = Field(min_length=1)


class SalaryBandsIn(_GeneratedBase):
    role: str = Field(min_length=1)
    tier: str = Field(min_length=1)


class PortfolioCaseStudyIn(_GeneratedBase):
    project: JsonObject = Field(...)


class PortfolioRecruiterPreviewIn(_GeneratedBase):
    portfolio: JsonObject = Field(...)


class MockCompanyIntelIn(_GeneratedBase):
    company: str = Field(min_length=1)


class MockRedoTrackerIn(_GeneratedBase):
    attempts: JsonValue = Field(...)


class DsaMockOaIn(_GeneratedBase):
    num_questions: LegacyNumber = Field(default=None)
    minutes: LegacyNumber = Field(default=None)


class DsaSpacedRepetitionIn(_GeneratedBase):
    mistakes: JsonValue = Field(...)


class CoachVideoPresenceIn(_GeneratedBase):
    self_scores: JsonObject = Field(default=None)


class CoachHrRoundIn(_GeneratedBase):
    question_id: str = Field(default=None, json_schema_extra={"options": ['', 'tell_me', 'why_us', 'strength_weakness', 'why_leave', 'salary_expect']})


class StarPredictFollowupsIn(_GeneratedBase):
    story_text: str = Field(min_length=1)


class Star30sPitchIn(_GeneratedBase):
    story_text: str = Field(min_length=1)


class RoadmapsAgenticAiTrackIn(_GeneratedBase):
    pass


class RoadmapsProjectMilestonesIn(_GeneratedBase):
    roadmap_stages: JsonValue = Field(...)


class QuizzesCertExamModeIn(_GeneratedBase):
    skill: str = Field(min_length=1)
    exam_id: str = Field(default=None, json_schema_extra={"options": ['generic', 'aws-clf-c02', 'aws-saa-c03', 'gcp-ace', 'az-900', 'cka']})


class QuizzesLeaderboardIn(_GeneratedBase):
    entries: JsonValue = Field(...)
    current_user_id: str = Field(default=None)
    top_n: LegacyNumber = Field(default=None)


class CertsExamDaySimIn(_GeneratedBase):
    cert_id: str = Field(min_length=1)


class CertsRenewalIn(_GeneratedBase):
    cert_id: str = Field(min_length=1)
    earned_date: str = Field(min_length=1)


class PdfqaAnswerAcrossDocsIn(_GeneratedBase):
    query: str = Field(min_length=1)
    docs: JsonValue = Field(...)
    top_k: LegacyNumber = Field(default=None)


class PdfqaVerifyCitationsIn(_GeneratedBase):
    answer_dict: JsonValue = Field(...)


class ArxivBulkImportIn(_GeneratedBase):
    ids_text: JsonList = Field(...)
    max_ids: LegacyNumber = Field(default=None)


class ArxivRelatedPapersIn(_GeneratedBase):
    openalex_id: str = Field(min_length=1)
    max_results: LegacyNumber = Field(default=None)


class ArxivReadingQueueIn(_GeneratedBase):
    papers: JsonValue = Field(...)
    interests: JsonValue = Field(default=None)
    minutes_per_paper: LegacyNumber = Field(default=None)


class CitationsInTextIn(_GeneratedBase):
    meta: JsonObject = Field(...)
    style: str = Field(default=None, json_schema_extra={"options": ['apa', 'mla', 'ieee', 'vancouver', 'chicago']})


class CitationsJournalStyleIn(_GeneratedBase):
    sample_citation: str = Field(min_length=1)


class CitationsFindDuplicatesIn(_GeneratedBase):
    references: JsonValue = Field(...)
    threshold: LegacyNumber = Field(default=None)


class LitreviewClusterIn(_GeneratedBase):
    papers: JsonValue = Field(...)


class LitreviewGapHighlightIn(_GeneratedBase):
    papers: JsonValue = Field(...)


class LitreviewLengthPresetIn(_GeneratedBase):
    preset: str = Field(default=None, json_schema_extra={"options": ['survey', 'standard', 'chapter']})


class GapsTrendCrosscheckIn(_GeneratedBase):
    gap_text: str = Field(min_length=1)
    trending_papers: JsonValue = Field(...)


class GapsFeasibilityIn(_GeneratedBase):
    gap_text: str = Field(min_length=1)
    skills: JsonValue = Field(default=None)


class GapsToProposalIn(_GeneratedBase):
    gap_text: str = Field(min_length=1)
    context: str = Field(default=None)


class FlashcardsAnkiExportIn(_GeneratedBase):
    cards: JsonValue = Field(...)
    deck_name: str = Field(default=None)


class FlashcardsExamSprintIn(_GeneratedBase):
    cards: JsonValue = Field(...)
    seconds_per_card: LegacyNumber = Field(default=None)


class FlashcardsWeakCardLoopIn(_GeneratedBase):
    card_stats: JsonObject = Field(...)
    cards: JsonValue = Field(...)


class TrendingPersonalizedFeedIn(_GeneratedBase):
    papers: JsonValue = Field(...)
    skills: JsonValue = Field(...)
    limit: LegacyNumber = Field(default=None)


class TrendingPaperToProjectsIn(_GeneratedBase):
    paper: JsonObject = Field(...)


class TrendingWeeklyDigestIn(_GeneratedBase):
    papers: JsonValue = Field(...)
    skills: JsonValue = Field(default=None)
    top_n: LegacyNumber = Field(default=None)


class OpportunitiesDeadlineAlertsIn(_GeneratedBase):
    opportunities: JsonValue = Field(...)


class OpportunitiesFitScoreIn(_GeneratedBase):
    opportunity: JsonValue = Field(...)
    skills: JsonValue = Field(...)


class OpportunitiesToTrackerIn(_GeneratedBase):
    opportunity: JsonObject = Field(...)


class TrackerPipelineAnalyticsIn(_GeneratedBase):
    applications: JsonValue = Field(...)


class TrackerFollowupNudgesIn(_GeneratedBase):
    applications: JsonValue = Field(...)
    nudge_after_days: LegacyNumber = Field(default=None)


class TrackerCompareOffersIn(_GeneratedBase):
    offers: JsonValue = Field(...)


class MomentumStreakFreezeIn(_GeneratedBase):
    last_active: LegacyDate = Field(...)
    freezes_used_week: LegacyNumber = Field(...)


class MomentumSquadLeaderboardIn(_GeneratedBase):
    members: JsonValue = Field(...)


class MomentumRewardTiersIn(_GeneratedBase):
    current_streak: LegacyNumber = Field(...)
    total_xp: LegacyNumber = Field(...)


class PeermocksMatchPartnersIn(_GeneratedBase):
    users: JsonValue = Field(...)


class PeermocksScoreRubricIn(_GeneratedBase):
    scores: JsonObject = Field(...)


class PeermocksRecurringSlotIn(_GeneratedBase):
    day: str = Field(min_length=1)
    time_hhmm: str = Field(min_length=1)
    duration_min: LegacyNumber = Field(default=None)


class RetroPatternAlertsIn(_GeneratedBase):
    retrospectives: JsonValue = Field(...)
    min_mentions: LegacyNumber = Field(default=None)


class RetroTrackExperimentIn(_GeneratedBase):
    name: str = Field(default=None)
    variants: JsonValue = Field(...)


class RetroWinLibraryIn(_GeneratedBase):
    wins: JsonValue = Field(...)


class MentorsAsyncQaDigestIn(_GeneratedBase):
    questions: JsonValue = Field(...)
    mentor_name: str = Field(default=None)


class MentorsPrepPacketIn(_GeneratedBase):
    mentee: JsonObject = Field(...)
    mentor: JsonObject = Field(...)
    topic: str = Field(min_length=1)


class MentorsAlumniOutcomesIn(_GeneratedBase):
    mentor_id: str = Field(min_length=1)
    placements: JsonValue = Field(...)


class HealthMonthlyReportIn(_GeneratedBase):
    review: JsonObject = Field(...)


class HealthPeerBenchmarksIn(_GeneratedBase):
    my_scores: JsonObject = Field(...)
    peer_scores: JsonValue = Field(...)


class HealthActionNudgesIn(_GeneratedBase):
    current_scores: JsonValue = Field(...)
    previous_scores: JsonValue = Field(default=None)


_reg(_meta("versions/diff", "Resume Versions", "Version Diff", data_source="user input (rule-based)"),
     VersionsDiffIn, diff_resume_versions, drop_none=True)
_reg(_meta("versions/jd-compare", "Resume Versions", "JD Version Showdown", data_source="user input (rule-based)"),
     VersionsJdCompareIn, compare_versions_for_jd, drop_none=True)
_reg(_meta("versions/tailor", "Resume Versions", "JD Auto-Tailoring", data_source="user input (rule-based)"),
     VersionsTailorIn, tailor_resume_for_jd, drop_none=True)
_reg(_meta("versions/performance", "Resume Versions", "Version Performance Tracker", data_source="user input (rule-based)"),
     VersionsPerformanceIn, version_performance, drop_none=True)
_reg(_meta("outreach/recipients", "Cover Letter + Cold Outreach", "Recipient Suggester", data_source="user input (rule-based)"),
     OutreachRecipientsIn, suggest_recipients, drop_none=True)
_reg(_meta("outreach/followup", "Cover Letter + Cold Outreach", "Follow-Up Sequence", data_source="user input (rule-based)"),
     OutreachFollowupIn, followup_sequence, drop_none=True)
_reg(_meta("linkedin/content-plan", "LinkedIn Grader", "Content Plan", data_source="user input (rule-based)"),
     LinkedinContentPlanIn, content_plan, drop_none=True)
_reg(_meta("linkedin/jd-gap", "LinkedIn Grader", "Profile-vs-JD Gap Analysis", data_source="user input (rule-based)"),
     LinkedinJdGapIn, profile_jd_gap, drop_none=True)
_reg(_meta("github/commits-to-bullets", "GitHub Integration", "Commit-to-Bullet Builder", data_source="user input (rule-based)"),
     GithubCommitsToBulletsIn, commit_messages_to_bullets, drop_none=True)
_reg(_meta("github/pin-optimizer", "GitHub Integration", "Pin Optimizer", data_source="user input (rule-based)"),
     GithubPinOptimizerIn, pin_optimizer, drop_none=True)
_reg(_meta("assessment/roadmap", "Career Assessment", "Roadmap Export", data_source="user input (rule-based)"),
     AssessmentRoadmapIn, roadmap_export, drop_none=True)
_reg(_meta("assessment/delta", "Career Assessment", "Retake Delta", data_source="user input (rule-based)"),
     AssessmentDeltaIn, assessment_delta, drop_none=True)
_reg(_meta("salary/negotiate", "Salary Explorer", "Negotiation Coach", data_source="user input (rule-based)"),
     SalaryNegotiateIn, negotiation_coach, drop_none=True)
_reg(_meta("salary/bands", "Salary Explorer", "Experience-Band Comparison", data_source="curated JSON"),
     SalaryBandsIn, band_comparison, drop_none=True)
_reg(_meta("portfolio/case-study", "Portfolio Builder", "Case-Study Generator", data_source="user input (rule-based)"),
     PortfolioCaseStudyIn, generate_case_study, drop_none=True)
_reg(_meta("portfolio/recruiter-preview", "Portfolio Builder", "Recruiter 30-Second Preview", data_source="user input (rule-based)"),
     PortfolioRecruiterPreviewIn, recruiter_preview, drop_none=True)
_reg(_meta("mock/company-intel", "Mock Interviews", "Company Intel Card", data_source="curated JSON"),
     MockCompanyIntelIn, company_intel, drop_none=True)
_reg(_meta("mock/redo-tracker", "Mock Interviews", "Redo / Improvement Tracker", data_source="user input (rule-based)"),
     MockRedoTrackerIn, redo_tracker, drop_none=True)
_reg(_meta("dsa/mock-oa", "DSA Practice", "Mock OA Simulator", data_source="curated JSON"),
     DsaMockOaIn, mock_oa, drop_none=True)
_reg(_meta("dsa/spaced-repetition", "DSA Practice", "Spaced Repetition Queue", data_source="user input (rule-based)"),
     DsaSpacedRepetitionIn, spaced_repetition_queue, drop_none=True)
_reg(_meta("coach/video-presence", "Communication Coach", "Video Presence Checklist", data_source="user input (rule-based)"),
     CoachVideoPresenceIn, video_presence_checklist, drop_none=True)
_reg(_meta("coach/hr-round", "Communication Coach", "HR Round Simulator", data_source="user input (rule-based)"),
     CoachHrRoundIn, hr_round_simulator, drop_none=True)
_reg(_meta("star/predict-followups", "STAR Builder", "Follow-up Predictor", data_source="user input (rule-based)"),
     StarPredictFollowupsIn, predict_followups, drop_none=True)
_reg(_meta("star/30s-pitch", "STAR Builder", "30-Second Pitch Compressor", data_source="user input (rule-based)"),
     Star30sPitchIn, thirty_second_pitch, drop_none=True)
_reg(_meta("roadmaps/agentic-ai-track", "Learning Roadmaps", "Agentic AI Track", data_source="user input (rule-based)"),
     RoadmapsAgenticAiTrackIn, agentic_ai_track, drop_none=True)
_reg(_meta("roadmaps/project-milestones", "Learning Roadmaps", "Project Milestones", data_source="user input (rule-based)"),
     RoadmapsProjectMilestonesIn, project_milestones, drop_none=True)
_reg(_meta("quizzes/cert-exam-mode", "Skill Quizzes", "Certification Exam Mode", data_source="curated JSON"),
     QuizzesCertExamModeIn, certification_exam_mode, drop_none=True)
_reg(_meta("quizzes/leaderboard", "Skill Quizzes", "Quiz Leaderboard", data_source="user input (rule-based)"),
     QuizzesLeaderboardIn, leaderboard, drop_none=True)
_reg(_meta("certs/exam-day-sim", "Certifications", "Exam Day Simulator", data_source="curated JSON"),
     CertsExamDaySimIn, exam_day_simulator, drop_none=True)
_reg(_meta("certs/renewal", "Certifications", "Renewal Tracker", data_source="curated JSON"),
     CertsRenewalIn, renewal_status, drop_none=True)
_reg(_meta("pdfqa/answer-across-docs", "PDF Q&A", "Answer Across Docs", data_source="user input (rule-based)"),
     PdfqaAnswerAcrossDocsIn, answer_across_docs, drop_none=True)
_reg(_meta("pdfqa/verify-citations", "PDF Q&A", "Citation Verifier", data_source="user input (rule-based)"),
     PdfqaVerifyCitationsIn, verify_citations, drop_none=True)
_reg(_meta("arxiv/bulk-import", "arXiv Import", "Bulk Paper Import", data_source="live HTTP fetch"),
     ArxivBulkImportIn, bulk_import, drop_none=True)
_reg(_meta("arxiv/related-papers", "arXiv Import", "Related Paper Discovery", data_source="live HTTP fetch"),
     ArxivRelatedPapersIn, related_papers, drop_none=True)
_reg(_meta("arxiv/reading-queue", "arXiv Import", "Prioritized Reading Queue", data_source="user input (rule-based)"),
     ArxivReadingQueueIn, reading_queue, drop_none=True)
_reg(_meta("citations/in-text", "Citation Formatter", "In-Text Citation Snippets", data_source="user input (rule-based)"),
     CitationsInTextIn, in_text_citation, drop_none=True)
_reg(_meta("citations/journal-style", "Citation Formatter", "Journal Style Matcher", data_source="user input (rule-based)"),
     CitationsJournalStyleIn, match_journal_style, drop_none=True)
_reg(_meta("citations/find-duplicates", "Citation Formatter", "Duplicate Reference Detector", data_source="user input (rule-based)"),
     CitationsFindDuplicatesIn, find_duplicates, drop_none=True)
_reg(_meta("litreview/cluster", "Literature Review Generator", "Thematic Clustering", data_source="user input (rule-based)"),
     LitreviewClusterIn, cluster_by_theme, drop_none=True)
_reg(_meta("litreview/gap-highlight", "Literature Review Generator", "Gap-Highlight Review", data_source="user input (rule-based)"),
     LitreviewGapHighlightIn, gap_highlight_review, drop_none=True)
_reg(_meta("litreview/length-preset", "Literature Review Generator", "Length Preset", data_source="user input (rule-based)"),
     LitreviewLengthPresetIn, length_preset, drop_none=True)
_reg(_meta("gaps/trend-crosscheck", "Gap Finder", "Trend Cross-Check", data_source="user input (rule-based)"),
     GapsTrendCrosscheckIn, trend_cross_check, drop_none=True)
_reg(_meta("gaps/feasibility", "Gap Finder", "3-Month Feasibility Score", data_source="user input (rule-based)"),
     GapsFeasibilityIn, feasibility_score, drop_none=True)
_reg(_meta("gaps/to-proposal", "Gap Finder", "Gap to Proposal Outline", data_source="user input (rule-based)"),
     GapsToProposalIn, gap_to_proposal, drop_none=True)
_reg(_meta("flashcards/anki-export", "Flashcards", "Anki Export", data_source="user input (rule-based)"),
     FlashcardsAnkiExportIn, anki_export, drop_none=True)
_reg(_meta("flashcards/exam-sprint", "Flashcards", "Timed Exam Sprint", data_source="user input (rule-based)"),
     FlashcardsExamSprintIn, exam_sprint, drop_none=True)
_reg(_meta("flashcards/weak-card-loop", "Flashcards", "Weak-Card-First Loop", data_source="user input (rule-based)"),
     FlashcardsWeakCardLoopIn, weak_card_loop, drop_none=True)
_reg(_meta("trending/personalized-feed", "Trending Research", "Personalized Feed", data_source="user input (rule-based)"),
     TrendingPersonalizedFeedIn, personalized_feed, drop_none=True)
_reg(_meta("trending/paper-to-projects", "Trending Research", "Paper to Project Ideas", data_source="user input (rule-based)"),
     TrendingPaperToProjectsIn, paper_to_project_ideas, drop_none=True)
_reg(_meta("trending/weekly-digest", "Trending Research", "Weekly Digest", data_source="user input (rule-based)"),
     TrendingWeeklyDigestIn, weekly_digest, drop_none=True)
_reg(_meta("opportunities/deadline-alerts", "Opportunity Radar", "Deadline Alerts", data_source="user input (rule-based)"),
     OpportunitiesDeadlineAlertsIn, deadline_alerts, drop_none=True)
_reg(_meta("opportunities/fit-score", "Opportunity Radar", "Skill-Fit Score", data_source="user input (rule-based)"),
     OpportunitiesFitScoreIn, fit_score, drop_none=True)
_reg(_meta("opportunities/to-tracker", "Opportunity Radar", "Add to Tracker", data_source="user input (rule-based)"),
     OpportunitiesToTrackerIn, to_tracker_payload, drop_none=True)
_reg(_meta("tracker/pipeline-analytics", "Job Tracker", "Pipeline Analytics", data_source="user input (rule-based)"),
     TrackerPipelineAnalyticsIn, pipeline_analytics, drop_none=True)
_reg(_meta("tracker/followup-nudges", "Job Tracker", "Follow-Up Nudges", data_source="user input (rule-based)"),
     TrackerFollowupNudgesIn, followup_nudges, drop_none=True)
_reg(_meta("tracker/compare-offers", "Job Tracker", "Offer Comparator", data_source="user input (rule-based)"),
     TrackerCompareOffersIn, compare_offers, drop_none=True)
_reg(_meta("momentum/streak-freeze", "Momentum", "Streak Freeze Status", data_source="user input (rule-based)"),
     MomentumStreakFreezeIn, streak_freeze_status, drop_none=True)
_reg(_meta("momentum/squad-leaderboard", "Momentum", "Squad Leaderboard", data_source="user input (rule-based)"),
     MomentumSquadLeaderboardIn, squad_leaderboard, drop_none=True)
_reg(_meta("momentum/reward-tiers", "Momentum", "Reward Tiers", data_source="user input (rule-based)"),
     MomentumRewardTiersIn, reward_tiers, drop_none=True)
_reg(_meta("peermocks/match-partners", "Peer Mocks", "Skill-Matched Pairing", data_source="user input (rule-based)"),
     PeermocksMatchPartnersIn, match_partners, drop_none=True)
_reg(_meta("peermocks/score-rubric", "Peer Mocks", "Rubric Scoring", data_source="user input (rule-based)"),
     PeermocksScoreRubricIn, score_rubric, drop_none=True)
_reg(_meta("peermocks/recurring-slot", "Peer Mocks", "Recurring Slot", data_source="user input (rule-based)"),
     PeermocksRecurringSlotIn, recurring_slot, drop_none=True)
_reg(_meta("retro/pattern-alerts", "Retrospectives", "Rejection Pattern Alerts", data_source="user input (rule-based)"),
     RetroPatternAlertsIn, pattern_alerts, drop_none=True)
_reg(_meta("retro/track-experiment", "Retrospectives", "A/B Experiment Tracker", data_source="user input (rule-based)"),
     RetroTrackExperimentIn, track_experiment, drop_none=True)
_reg(_meta("retro/win-library", "Retrospectives", "Win Library", data_source="user input (rule-based)"),
     RetroWinLibraryIn, win_library, drop_none=True)
_reg(_meta("mentors/async-qa-digest", "Mentors", "Async Q&A Digest", data_source="user input (rule-based)"),
     MentorsAsyncQaDigestIn, async_qa_digest, drop_none=True)
_reg(_meta("mentors/prep-packet", "Mentors", "Session Prep Packet", data_source="user input (rule-based)"),
     MentorsPrepPacketIn, prep_packet, drop_none=True)
_reg(_meta("mentors/alumni-outcomes", "Mentors", "Alumni Outcomes", data_source="user input (rule-based)"),
     MentorsAlumniOutcomesIn, alumni_outcomes, drop_none=True)
_reg(_meta("health/monthly-report", "Career Health Review", "Monthly Report", data_source="user input (rule-based)"),
     HealthMonthlyReportIn, monthly_report, drop_none=True)
_reg(_meta("health/peer-benchmarks", "Career Health Review", "Peer Benchmarks", data_source="user input (rule-based)"),
     HealthPeerBenchmarksIn, peer_benchmarks, drop_none=True)
_reg(_meta("health/action-nudges", "Career Health Review", "Action Nudges", data_source="user input (rule-based)"),
     HealthActionNudgesIn, action_nudges, drop_none=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def is_migrated(tool_id: str) -> bool:
    """True when tool_id dispatches through the typed allowlist."""
    return tool_id in TOOL_HANDLERS


def migrated_tool_ids() -> list[str]:
    return sorted(TOOL_HANDLERS)


def run_typed_tool(
    tool_id: str, payload: dict
) -> tuple[Optional[dict], Optional[str]]:
    """Run a migrated tool. Returns (result_dict, None) or (None, error).

    Unknown ids are REJECTED — there is no fallback to dynamic dispatch.
    """
    handler = TOOL_HANDLERS.get(tool_id)
    if handler is None:
        return None, f"Unknown tool '{tool_id}'."
    if not isinstance(payload, dict):
        return None, "Request body must be a JSON object."
    return handler.run(payload)


__all__ = [
    "TOOL_HANDLERS",
    "ToolHandler",
    "ToolMeta",
    "is_migrated",
    "migrated_tool_ids",
    "run_typed_tool",
]
