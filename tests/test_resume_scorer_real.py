"""Differential + regression tests for the 2026-10-25 ATS scorer rebuild.

Background: users reported two different resumes receiving the SAME score
and result. Root causes found (all fixed in this change):
  1. clean_text() stripped % and $, so quantified achievements were
     silently 0 for every resume (metrics counted on cleaned text).
  2. extract_experience_years() counted education dates as work years
     (a fresher with only "B.Com, 2022-2025" got 3 years).
  3. Bucketed component scores saturated: any two decent resumes hit
     every cap and landed on identical totals.
These tests pin the fixes: different inputs MUST score differently, and
each evidence type MUST move its component in the expected direction.
"""

from models.ats_scorer import calculate_ats_score
from models.skill_extractor import extract_skills
from parser.section_extractor import extract_experience_years, extract_sections
from parser.text_cleaning import clean_text

STRONG_ML = """
Aarav Sharma
aarav.sharma@gmail.com | +91 98765 43210 | linkedin.com/in/aaravsharma | github.com/aaravsharma
SUMMARY
Machine learning engineer with 4 years of experience building computer vision systems.
EXPERIENCE
Senior ML Engineer, VisionTech, 2021-2025
Developed a real-time object detection pipeline that increased checkout accuracy by 32% and reduced manual review effort by 45%.
Led a team of 5 engineers.
Built a Keras-based defect classifier adopted by 12 factories, cutting inspection costs by $240,000 per year.
Improved inference latency by 60% through model quantization.
ML Engineer, DataLabs, 2019-2021
Created an image segmentation service used by 40 hospitals.
Automated annotation workflows, saving 300 hours per month.
EDUCATION
B.Tech Computer Science, IIT Delhi, 2015-2019
SKILLS
Python, TensorFlow, Keras, PyTorch, Computer Vision, Machine Learning, OpenCV, SQL, Docker, Kubernetes, AWS, Git, Linux, NumPy, Pandas
PROJECTS
Retail Shelf Monitor: real-time CV system deployed in 30 stores.
"""

STRONG_WEB = """
Priya Nair
priya.nair@outlook.com | +91 98200 12345 | linkedin.com/in/priyanair | github.com/priyanair
SUMMARY
Full-stack developer with 5 years of experience building SaaS products.
EXPERIENCE
Senior Software Engineer, CloudKart, 2020-2025
Designed and launched a subscription billing platform processing $2M in annual payments.
Reduced page load time by 55% and increased conversion by 18%.
Mentored 4 junior developers.
Led migration to microservices, cutting deployment time by 70%.
Software Engineer, WebNest, 2018-2020
Built a React dashboard used by 500 companies.
Improved test coverage from 40% to 85%.
EDUCATION
B.E. Information Technology, Pune University, 2014-2018
SKILLS
JavaScript, TypeScript, React, Node.js, Express, MongoDB, PostgreSQL, HTML, CSS, GraphQL, Redis, Docker, AWS, Jenkins, Git, Jest, Next.js
PROJECTS
Open-source CLI tool with 2,000 GitHub stars.
"""

WEAK = """
Rahul Verma
rahul.verma@gmail.com | 9876543210
SUMMARY
Student looking for opportunities.
EDUCATION
B.Com, Delhi University, 2022-2025
SKILLS
MS Word, Excel, Communication
PROJECTS
College fest website made with friends.
"""


def score(text):
    cleaned = clean_text(text)
    sections = extract_sections(cleaned)
    skills = extract_skills(cleaned)
    exp = extract_experience_years(cleaned, sections=sections)
    return calculate_ats_score(cleaned, sections, skills, exp)


def test_metrics_survive_cleaning():
    cleaned = clean_text("Increased accuracy by 32% and saved $240,000 per year.")
    assert "32%" in cleaned
    assert "$240,000" in cleaned
    result = score(STRONG_ML)
    assert "quantified achievements" in result["breakdown"]["content"]["details"]
    content_detail = result["breakdown"]["content"]["details"]
    # At least the % metrics must be detected now (old code found zero).
    assert "0 quantified" not in content_detail


def test_removing_metrics_lowers_score():
    base = score(STRONG_ML)
    stripped = STRONG_ML.replace("by 32%", "").replace("by 45%", "")
    stripped = stripped.replace("by $240,000 per year", "").replace("by 60%", "")
    stripped = stripped.replace("by 40 hospitals", "").replace("300 hours per month", "many hours")
    stripped = stripped.replace("in 30 stores", "")
    no_metrics = score(stripped)
    assert no_metrics["total_score"] < base["total_score"] - 2.0


def test_education_dates_not_counted_as_experience():
    edu_only = score(WEAK)
    assert edu_only["breakdown"]["experience"]["score"] <= 2.0
    mixed = score("""
    Priya Singh
    EDUCATION
    B.Tech, IIT Bombay, 2022-2025
    EXPERIENCE
    Software Engineer, Zeta, 2020-2023
    Developed internal tools.
    SKILLS
    Python, SQL
    """)
    # Only the 2020-2023 job counts (3 years), not 3 + 3.
    assert mixed["breakdown"]["experience"]["score"] < 12.0


def test_month_ranges_compute_real_spans():
    years = extract_experience_years(
        "Engineer, X, Jan 2020 - Dec 2023", sections=None)
    assert 3.5 <= years <= 4.1  # was 1 under the old range-counting


def test_different_strong_resumes_score_differently():
    ml = score(STRONG_ML)
    web = score(STRONG_WEB)
    assert ml["total_score"] != web["total_score"]
    assert ml["breakdown"] != web["breakdown"]


def test_same_input_is_deterministic():
    a = score(STRONG_ML)
    b = score(STRONG_ML)
    assert a == b


def test_contact_split_github_vs_portfolio():
    base = "John Doe\njohn@example.com | 9876543210 | linkedin.com/in/john\nSKILLS\nPython\n"
    github_only = score(base + "github.com/johndoe\n")
    portfolio_only = score(base + "See my portfolio at johndoe.dev\n")
    both = score(base + "github.com/johndoe\nportfolio: johndoe.dev\n")
    # Same points, different evidence (details must name the difference).
    assert github_only["breakdown"]["contact"]["details"] != \
        portfolio_only["breakdown"]["contact"]["details"]
    assert both["breakdown"]["contact"]["score"] > \
        github_only["breakdown"]["contact"]["score"]


def test_weak_phrases_penalize_content():
    base = score(STRONG_ML)
    vague = score(STRONG_ML + "\nEXPERIENCE\nWas responsible for various tasks and helped with day to day duties. Was in charge of etc. work.\n")
    assert vague["breakdown"]["content"]["score"] < base["breakdown"]["content"]["score"]


def test_gaming_guard_still_works():
    repeated = " ".join(["Delivered great results with strong skills."] * 40)
    result = score("John Doe\njohn@example.com\nSUMMARY\n" + repeated)
    assert result["gaming_detected"] is True
    assert result["total_score"] < 25.0


def test_weak_scores_far_below_strong():
    assert score(WEAK)["total_score"] + 20.0 < score(STRONG_ML)["total_score"]


def test_skill_curve_is_monotonic_not_cliffed():
    # Real taxonomy names only: invented names match nothing, which is
    # correct extractor behavior (a fake-skill test would test nothing).
    pool = ["Python", "SQL", "Git", "Docker", "AWS", "Linux", "React",
            "Node.js", "HTML", "CSS", "TensorFlow", "Keras", "PyTorch",
            "OpenCV", "Kubernetes", "NumPy", "Pandas", "Flask",
            "MongoDB", "PostgreSQL"]

    def skills_only(n):
        names = ", ".join(pool[:n])
        return score(f"John Doe\nSKILLS\n{names}\n")
    s1 = skills_only(3)["breakdown"]["skills"]["score"]
    s2 = skills_only(10)["breakdown"]["skills"]["score"]
    s3 = skills_only(20)["breakdown"]["skills"]["score"]
    assert 0 < s1 < s2 < s3 <= 25.0


def test_section_content_lines_dont_hijack_sections():
    """Content lines naming a university/college used to be mistaken for
    headers, wiping the whole education/projects section."""
    sections = extract_sections(clean_text(WEAK))
    assert "education" in sections
    assert "projects" in sections
    assert "Delhi University" in sections["education"]


def test_comma_digit_groups_preserved():
    assert clean_text("saved $240,000 per year") == "saved $240,000 per year"
    assert clean_text("Delhi, India") == "Delhi, India"


def test_honesty_note_and_grade_mapping():
    result = score(STRONG_ML)
    assert "not a real ATS score" in result["method_note"]
    assert result["max_score"] == 100
    assert result["grade"] in ("A", "B", "C", "D", "F")
    assert result["percentage"] == result["total_score"]
