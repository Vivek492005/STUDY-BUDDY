"""WS2-ENG tests: ENG-07 JD matcher v2, ENG-10 outcomes, ENG-11 jobs, ENG-12 interview v2."""

from datetime import date, timedelta

import pytest

from studybuddy import eng_jd_v2, eng_outcomes, eng_jobs, eng_interview_v2

JD_SAMPLE = """Senior Backend Engineer

Requirements:
- 4+ years of experience building REST APIs with Python (required)
- Strong SQL and PostgreSQL skills required
- Bachelor's degree in Computer Science or related field
- Docker plus Kubernetes experience is a plus

Nice to have:
- Familiarity with React
- Preferred: AWS experience

Responsibilities:
- Design and ship backend services
"""


# ---------------------------------------------------------------------------
# ENG-07: JD matcher v2
# ---------------------------------------------------------------------------

def test_section_split_finds_requirements_and_nice():
    sections = eng_jd_v2.split_jd_sections(JD_SAMPLE)
    assert "requirements" in sections
    assert "nice_to_have" in sections
    assert "responsibilities" in sections


def test_classify_must_cues():
    assert eng_jd_v2.classify_requirement("Python skills required", "requirements") == "must"
    assert eng_jd_v2.classify_requirement("Must have SQL experience", "overview") == "must"


def test_classify_nice_cues():
    assert eng_jd_v2.classify_requirement("Preferred: AWS experience", "requirements") == "nice"
    assert eng_jd_v2.classify_requirement("Docker experience is a plus", "requirements") == "nice"


def test_cue_overrides_section_default():
    # "plus" inside Requirements is still nice-to-have.
    req = eng_jd_v2.classify_requirement("Kubernetes is a plus", "requirements")
    assert req == "nice"


def test_years_extraction():
    assert eng_jd_v2.extract_years_requirement("4+ years of experience") == 4
    assert eng_jd_v2.extract_years_requirement("3-5 years experience") == 5
    assert eng_jd_v2.extract_years_requirement("no years mentioned") is None


def test_education_extraction():
    edu = eng_jd_v2.extract_education_requirement("Bachelor's degree in CS; Master's preferred")
    assert "Bachelor's" in edu
    assert "Master's" in edu
    assert eng_jd_v2.extract_education_requirement("B.Tech required") == ["B.Tech/B.E."]


def test_skills_come_from_taxonomy():
    parsed = eng_jd_v2.extract_jd_v2(JD_SAMPLE)
    all_skills = {s for r in parsed["requirements"] for s in r["skills"]}
    assert "Python" in all_skills
    assert "PostgreSQL" in all_skills or "SQL" in all_skills


def test_requirements_carry_years_and_education():
    parsed = eng_jd_v2.extract_jd_v2(JD_SAMPLE)
    years_reqs = [r for r in parsed["requirements"] if r["years"]]
    assert years_reqs and years_reqs[0]["years"] == 4
    edu_reqs = [r for r in parsed["requirements"] if r["education"]]
    assert edu_reqs


def test_perfect_match_scores_100():
    resume = ("Senior backend engineer with 5 years building REST APIs with Python, "
              "strong SQL and PostgreSQL skills. Docker and Kubernetes daily. "
              "Bachelor's degree in Computer Science. React and AWS too. "
              "Designed and shipped backend services.")
    skills = ["Python", "SQL", "PostgreSQL", "Docker", "Kubernetes", "React", "AWS"]
    report = eng_jd_v2.match_jd_v2(JD_SAMPLE, resume, skills, resume_years=5,
                                   resume_education=["Bachelor's"])
    assert report["score"] == 100.0
    assert report["missing_must_haves"] == []


def test_empty_resume_scores_low():
    report = eng_jd_v2.match_jd_v2(JD_SAMPLE, "", [], resume_years=0)
    assert report["score"] < 30
    assert report["missing_must_haves"], "must-haves should surface"


def test_missing_must_haves_ranked_by_impact():
    resume = "Python developer, 5 years. Bachelor's in CS."
    report = eng_jd_v2.match_jd_v2(JD_SAMPLE, resume, ["Python"], resume_years=5,
                                   resume_education=["Bachelor's"])
    missing = report["missing_must_haves"]
    assert missing
    impacts = [m["impact"] for m in missing]
    assert impacts == sorted(impacts, reverse=True)
    for m in missing:
        assert m["how_to_evidence"]["bullet_templates"]
        assert "genuinely" in m["how_to_evidence"]["condition"]


def test_honesty_note_present():
    report = eng_jd_v2.match_jd_v2(JD_SAMPLE, "", [])
    assert "genuinely" in report["honesty_note"]
    assert "never invent" in report["honesty_note"].lower()


def test_semantic_fallback_note_when_no_embeddings():
    report = eng_jd_v2.match_jd_v2(JD_SAMPLE, "unrelated resume text", [])
    assert report["semantic_fallback_available"] is False
    notes = [r["semantic_note"] for r in report["per_requirement"] if r["semantic_note"]]
    assert any("no semantic embeddings" in n for n in notes)


def test_semantic_rescue_with_embed_callable():
    def fake_embed(req_text, resume_text):
        return 0.9 if "docker" in req_text.lower() else 0.1

    resume = "I containerize everything daily."
    report = eng_jd_v2.match_jd_v2(JD_SAMPLE, resume, [], embed=fake_embed)
    assert report["semantic_fallback_available"] is True
    notes = [r["semantic_note"] for r in report["per_requirement"] if r["semantic_note"]]
    assert any("semantic fallback" in n for n in notes)


def test_illustrative_checklist_honest_labeling():
    report = {"score": 60, "matched_skills": ["Python"]}
    out = eng_jd_v2.illustrative_screening_checklist(report, "Built APIs with Python in 2023-2024.")
    assert out["label"] == "Illustrative screening checklist"
    assert out["honest_label"] is True
    assert "Illustrative only" in out["disclaimer"]
    assert "verdict" not in out  # predictive-sounding key removed


# ---------------------------------------------------------------------------
# ENG-10: outcome feedback loop
# ---------------------------------------------------------------------------

def test_record_outcome_requires_opt_in():
    store = {}
    with pytest.raises(eng_outcomes.OptInRequired):
        eng_outcomes.record_outcome(store, "a1", "interview", opted_in=False)
    assert store == {}


def test_record_outcome_rejects_bad_stage():
    with pytest.raises(ValueError):
        eng_outcomes.record_outcome({}, "a1", "hired", opted_in=True)


def test_record_stores_no_user_id():
    store = {}
    rec = eng_outcomes.record_outcome(store, "a1", "offer", opted_in=True,
                                      cohort_label="readiness 75+")
    assert "user_id" not in rec and "email" not in rec
    assert rec["stage"] == "offer"


def test_withdraw_removes_record():
    store = {}
    eng_outcomes.record_outcome(store, "a1", "applied", opted_in=True)
    assert eng_outcomes.withdraw_outcome(store, "a1") is True
    assert store == {}


def _fill(store, cohort, n, interviews, offers):
    for i in range(n):
        stage = "offer" if i < offers else ("interview" if i < offers + interviews else "applied")
        eng_outcomes.record_outcome(store, f"{cohort}-{i}", stage, opted_in=True,
                                    cohort_label=cohort)


def test_k_anonymity_suppresses_small_cohorts():
    store = {}
    _fill(store, "readiness 75+", 25, 10, 3)   # publishable
    _fill(store, "readiness <55", 19, 2, 0)    # suppressed (19 < 20)
    agg = eng_outcomes.aggregate_outcomes(store)
    assert [p["cohort"] for p in agg["published"]] == ["readiness 75+"]
    assert agg["suppressed"][0]["cohort"] == "readiness <55"
    assert "k-anonymity" in agg["suppressed"][0]["reason"]


def test_aggregates_carry_sample_sizes_and_ci():
    store = {}
    _fill(store, "readiness 55-74", 40, 12, 4)
    agg = eng_outcomes.aggregate_outcomes(store)
    pub = agg["published"][0]
    assert pub["n"] == 40
    assert pub["interview_rate"] == pytest.approx(16 / 40)
    lo, hi = pub["interview_rate_ci95"]
    assert 0 <= lo <= pub["interview_rate"] <= hi <= 1
    lo, hi = pub["offer_rate_ci95"]
    assert 0 <= lo <= pub["offer_rate"] <= hi <= 1


def test_wilson_interval_edge_cases():
    assert eng_outcomes.wilson_interval(0, 0) == (0.0, 0.0)
    lo, hi = eng_outcomes.wilson_interval(10, 20)
    assert lo < 0.5 < hi


def test_disclaimer_is_correlational_not_causal():
    d = eng_outcomes.CORRELATION_DISCLAIMER.lower()
    assert "not causal" in d
    for word in ("causes", "boosts", "leads to", "guarantees"):
        assert word not in d
    agg = eng_outcomes.aggregate_outcomes({})
    assert agg["disclaimer"] == eng_outcomes.CORRELATION_DISCLAIMER


# ---------------------------------------------------------------------------
# ENG-11: real + legal job recommendations
# ---------------------------------------------------------------------------

def _cards():
    # Dates are relative to today: a hardcoded "fresh" date silently turns
    # expired once MAX_CARD_AGE_DAYS passes (broke 2026-11-19, 31 days after
    # the fixture's 2026-10-19). Relative dates keep the fixture true forever.
    fresh = (date.today() - timedelta(days=1)).isoformat()
    stale = (date.today()
             - timedelta(days=eng_jobs.MAX_CARD_AGE_DAYS + 10)).isoformat()
    return [
        eng_jobs.make_job_card(
            title="Backend Intern", company="Acme", location="Bengaluru",
            level="intern", source="Adzuna", source_url="https://example.com/1",
            last_verified=fresh,
            jd_text="Requirements:\n- Python required\n- SQL required\n\nNice to have:\n- Docker preferred"),
        eng_jobs.make_job_card(
            title="Backend Intern", company="Acme", location="Bengaluru",
            level="intern", source="Adzuna", source_url="https://example.com/1",
            last_verified=fresh, jd_text="Requirements:\n- Python required"),
        eng_jobs.make_job_card(
            title="Frontend Fresher", company="Beta", location="Remote",
            level="fresher", source="RemoteOK", source_url="https://example.com/2",
            last_verified=stale,
            jd_text="Requirements:\n- React required"),
    ]


def test_explanation_string_format():
    card = _cards()[0]
    exp = eng_jobs.explain_match(card, ["Python"])
    assert exp["total"] == 2
    assert exp["explanation"].startswith("Matched 1 of 2 must-haves")
    assert "missing:" in exp["explanation"] and "SQL" in exp["explanation"]


def test_explanation_no_gaps_variant():
    card = _cards()[0]
    exp = eng_jobs.explain_match(card, ["Python", "SQL"])
    assert "no must-have gaps" in exp["explanation"]


def test_source_and_last_verified_mandatory():
    with pytest.raises(ValueError):
        eng_jobs.make_job_card(title="X", company="Y", source="")
    card = eng_jobs.make_job_card(title="X", company="Y", source="User-pasted JD")
    assert card["last_verified"]  # defaults to today


def test_geography_and_level_filters():
    cards = _cards()
    assert len(eng_jobs.filter_jobs(cards, geography="bengaluru")) == 2
    assert len(eng_jobs.filter_jobs(cards, level="fresher")) == 1
    assert len(eng_jobs.filter_jobs(cards, geography="mumbai")) == 0


def test_dedupe_removes_duplicates():
    assert len(eng_jobs.dedupe_jobs(_cards())) == 2  # first two are the same posting


def test_expiry_logic():
    old, new = _cards()[2], _cards()[0]
    assert eng_jobs.is_expired(old) is True
    assert eng_jobs.is_expired(new) is False
    refreshed = eng_jobs.refresh_card(old)
    assert eng_jobs.is_expired(refreshed) is False


def test_recommend_jobs_drops_expired_and_ranks():
    recs = eng_jobs.recommend_jobs(_cards(), ["Python", "SQL"])
    assert all(not r["recommendation"]["expired"] for r in recs)
    assert len(recs) == 1  # dup removed, expired dropped
    assert recs[0]["recommendation"]["explanation"].startswith("Matched")


def test_paste_jd_flow_source_label_and_no_tos_risk():
    card = eng_jobs.paste_jd_flow("Requirements:\n- Python required\n- 2 years experience")
    assert card["source"] == "User-pasted JD"
    assert card["source_url"] is None
    assert any("Python" in s for r in card["must_haves"] for s in r["skills"])
    with pytest.raises(ValueError):
        eng_jobs.paste_jd_flow("   ")


def test_adapters_are_inert_until_configured():
    for cls in (eng_jobs.AdzunaAdapter, eng_jobs.JoobleAdapter, eng_jobs.RemoteOKAdapter):
        adapter = cls()
        with pytest.raises(NotImplementedError, match="no API credential"):
            adapter.fetch("python")
        keyed = cls(api_key="x")
        with pytest.raises(NotImplementedError, match="terms-of-service"):
            keyed.fetch("python")


def test_product_promise_is_honest():
    assert "Curated" in eng_jobs.PRODUCT_PROMISE
    assert "live job feed" not in eng_jobs.PRODUCT_PROMISE.lower()


# ---------------------------------------------------------------------------
# ENG-12: interview scoring v2
# ---------------------------------------------------------------------------

GOOD_ANSWER = (
    "The situation was that our checkout page took 8 seconds to load when I joined. "
    "My task was to cut that under 2 seconds before the festive sale. "
    "I profiled the backend, built a Redis cache layer, and rewrote three N+1 queries in Django. "
    "As a result, p95 latency dropped from 8s to 1.4s, and conversions rose 12% in two weeks."
)
KEY_POINTS = ["slow checkout page load", "caching layer added", "latency reduced with numbers"]


def test_star_detection_full():
    star = eng_interview_v2.star_structure(GOOD_ANSWER)
    assert star["count"] == 4
    assert all(v["present"] for v in star["components"].values())


def test_star_detection_empty():
    star = eng_interview_v2.star_structure("")
    assert star["count"] == 0


def test_keyword_list_coverage_capped():
    answer = "Python, Django, Redis, SQL, caching, latency, optimization, backend, APIs, queues"
    coverage, _ = eng_interview_v2.key_point_coverage(answer, KEY_POINTS)
    assert coverage <= eng_interview_v2.KEYWORD_LIST_COVERAGE_CAP
    assert eng_interview_v2.is_keyword_list(answer) is True
    assert eng_interview_v2.is_keyword_list(GOOD_ANSWER) is False


def test_verbatim_repetition_penalized():
    question = "Tell me about a time you improved performance"
    answer = ("Tell me about a time you improved performance. Well, I did improve "
              "performance on our checkout page by adding caching and it got much faster.")
    penalty = eng_interview_v2.verbatim_repetition_penalty(question, answer)
    assert penalty > 0
    plain = eng_interview_v2.score_answer(question, GOOD_ANSWER, KEY_POINTS)
    copied = eng_interview_v2.score_answer(question, answer, KEY_POINTS)
    assert copied["breakdown"]["anti_gaming"]["points_deducted"] > 0
    assert copied["score"] < plain["score"]


def test_tfidf_fallback_similarity_sane():
    assert eng_interview_v2.tfidf_cosine("hello world", "hello world") == pytest.approx(1.0)
    assert eng_interview_v2.tfidf_cosine("python django", "quantum physics banana") < 0.2


def test_embed_override_used_when_provided():
    # embed: text -> vector; identical direction -> cosine 1.0 regardless of text
    assert eng_interview_v2.semantic_similarity("aaa", "zzz", embed=lambda t: [1.0, 0.0]) == pytest.approx(1.0)
    assert eng_interview_v2.semantic_similarity("aaa", "zzz") == pytest.approx(0.0)  # lexical fallback


def test_score_always_includes_rubric_and_improvements():
    for ans in (GOOD_ANSWER, "", "short"):
        res = eng_interview_v2.score_answer("Q?", ans, KEY_POINTS)
        assert res["rubric"] == eng_interview_v2.RUBRIC
        assert len(res["improvements"]) >= 1
        assert 0 <= res["score"] <= 100
        assert res["engine"] == "rule-based"


def test_good_answer_scores_well():
    res = eng_interview_v2.score_answer("Tell me about a performance win", GOOD_ANSWER, KEY_POINTS)
    assert res["score"] >= 60
    assert res["breakdown"]["key_point_coverage"]["score"] > 20


def test_llm_judge_accepts_valid_json():
    raw = ('{"key_point_coverage": 30, "star_structure": 20, "specificity": 15,'
           ' "clarity_and_length": 10, "improvements": ["add numbers"]}')
    parsed = eng_interview_v2.parse_llm_judge_output(raw)
    assert parsed["valid"] is True
    assert parsed["scores"]["key_point_coverage"] == 30


def test_llm_judge_rejects_garbage():
    assert eng_interview_v2.parse_llm_judge_output("not json")["valid"] is False
    bad = ('{"key_point_coverage": 99, "star_structure": 20, "specificity": 15,'
           ' "clarity_and_length": 10, "improvements": []}')
    assert eng_interview_v2.parse_llm_judge_output(bad)["valid"] is False  # out of range


def test_llm_engine_label_and_fallback():
    res = eng_interview_v2.score_answer(
        "Q?", GOOD_ANSWER, KEY_POINTS,
        llm=lambda prompt: '{"key_point_coverage": 30, "star_structure": 20,'
                           ' "specificity": 15, "clarity_and_length": 10,'
                           ' "improvements": ["x"]}',
        llm_model="test-model")
    assert res["engine"] == "llm"
    assert res["llm_model"] == "test-model"

    def boom(prompt):
        raise RuntimeError("down")
    res2 = eng_interview_v2.score_answer("Q?", GOOD_ANSWER, KEY_POINTS,
                                         llm=boom, llm_model="x")
    assert res2["engine"] == "rule-based"  # falls back, says so


def test_llm_judge_prompt_is_fixed():
    assert "STRICT JSON" in eng_interview_v2.LLM_JUDGE_PROMPT
    assert "{question}" in eng_interview_v2.LLM_JUDGE_PROMPT
