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


# ---------------------------------------------------------------------------
# Eval 3 anti-gaming regression tests (2026-10-31).
#
# An external eval found a keyword-stuffed junk resume scoring 63 vs 72
# for a clean resume — far too narrow. The scorer now hardens two
# vectors (see models/ats_scorer.py):
#   (a) repetition caps / diminishing returns: repeated occurrences of
#       the same term beyond a threshold contribute progressively less;
#   (b) context-aware skill detection: bare comma-separated skill lists
#       score lower than skills embedded in achievement bullets.
# ---------------------------------------------------------------------------

#: Realistic keyword-stuffed junk: every section present, a 50-skill
#: bare list, and template filler bullets that repeat the same six
#: keywords next to distinct action verbs and fabricated metrics.
JUNK_STUFFED = """Alex Gamer
alex.gamer@gmail.com | +91 99999 88888
SUMMARY
Results-driven technology professional with a passion for synergy and digital transformation.
EXPERIENCE
Senior Specialist, HustleCorp, 2019-2025
Developed cutting-edge solutions leveraging Python, Docker, Kubernetes, AWS, React and TensorFlow, improving outcomes by 7%.
Designed cutting-edge solutions leveraging Python, Docker, Kubernetes, AWS, React and TensorFlow, improving outcomes by 14%.
Implemented cutting-edge solutions leveraging Python, Docker, Kubernetes, AWS, React and TensorFlow, improving outcomes by 21%.
Managed cutting-edge solutions leveraging Python, Docker, Kubernetes, AWS, React and TensorFlow, improving outcomes by 28%.
Created cutting-edge solutions leveraging Python, Docker, Kubernetes, AWS, React and TensorFlow, improving outcomes by 35%.
Built cutting-edge solutions leveraging Python, Docker, Kubernetes, AWS, React and TensorFlow, improving outcomes by 42%.
Led cutting-edge solutions leveraging Python, Docker, Kubernetes, AWS, React and TensorFlow, improving outcomes by 49%.
Launched cutting-edge solutions leveraging Python, Docker, Kubernetes, AWS, React and TensorFlow, improving outcomes by 56%.
Optimized cutting-edge solutions leveraging Python, Docker, Kubernetes, AWS, React and TensorFlow, improving outcomes by 63%.
Reduced cutting-edge solutions leveraging Python, Docker, Kubernetes, AWS, React and TensorFlow, improving outcomes by 70%.
Increased cutting-edge solutions leveraging Python, Docker, Kubernetes, AWS, React and TensorFlow, improving outcomes by 77%.
Automated cutting-edge solutions leveraging Python, Docker, Kubernetes, AWS, React and TensorFlow, improving outcomes by 84%.
EDUCATION
B.Tech Computer Science, Some University, 2015-2019
SKILLS
Python, Docker, Kubernetes, AWS, React, TensorFlow, JavaScript, TypeScript, Node.js, Java, SQL, MongoDB, PostgreSQL, Git, Linux, Django, Flask, FastAPI, Redis, GraphQL, PyTorch, Keras, Computer Vision, Pandas, NumPy, Scikit-learn, Airflow, Apache Spark, Kafka, Jenkins, Terraform, Ansible, Elasticsearch, Vue, Angular, Next.js, Express, Spring, MySQL, SQLite, Cassandra, Neo4j, Figma, Adobe Photoshop, Illustrator, Selenium, Cypress, Jest, PyTest, JUnit
PROJECTS
Delivered multiple enterprise projects using modern technologies and best practices.
"""


def test_stuffed_junk_scores_far_below_clean():
    junk = score(JUNK_STUFFED)
    clean = score(STRONG_ML)
    gap = clean["total_score"] - junk["total_score"]
    assert gap >= 25.0, f"anti-gaming gap too narrow: clean={clean['total_score']}, junk={junk['total_score']}"


def test_stuffed_junk_stays_low_in_absolute_terms():
    # Even a well-formatted stuffed resume must not reach "good" territory.
    assert score(JUNK_STUFFED)["total_score"] < 55.0


def test_stuffed_junk_discloses_gaming_signals():
    breakdown = score(JUNK_STUFFED)["breakdown"]
    assert "repetition" in breakdown["content"]["details"]
    assert "near-duplicate" in breakdown["content"]["details"]
    assert "list-only" in breakdown["skills"]["details"]
    assert "duplicated-bullet" in breakdown["experience"]["details"]


def test_clean_score_stays_in_band():
    # Pre-change (2026-10-31, measured) STRONG_ML scored 77.9; the
    # anti-gaming hardening must not tank honest resumes.
    total = score(STRONG_ML)["total_score"]
    assert abs(total - 77.9) <= 4.0, f"honest score moved too far: {total}"
    assert 68.0 <= total <= 82.0


def test_embedded_skills_beat_bare_lists():
    """Same 8 skills: embedded in achievement bullets must beat a bare
    comma-separated list by a clear margin (context-aware detection)."""
    header = ("Sam Lee\nsam.lee@gmail.com | +91 98111 22233\n"
              "SUMMARY\nBackend engineer with 3 years of experience.\n")
    skills_block = "Python, SQL, Docker, Kubernetes, AWS, Git, Linux, React"
    bare = (header + "EXPERIENCE\nBackend Engineer, Acme, 2022-2025\n"
            "Maintained internal dashboards and handled support tickets.\n"
            "EDUCATION\nB.Tech, Some University, 2018-2022\n"
            "SKILLS\n" + skills_block + "\n")
    embedded = (header + "EXPERIENCE\nBackend Engineer, Acme, 2022-2025\n"
                "Built a Python API backed by SQL that served 10,000 requests per day.\n"
                "Deployed Docker containers on Kubernetes clusters hosted on AWS, cutting release time by 40%.\n"
                "Automated Git workflows on Linux servers, saving 20 hours per month.\n"
                "Created a React dashboard used by 200 customers.\n"
                "EDUCATION\nB.Tech, Some University, 2018-2022\n"
                "SKILLS\n" + skills_block + "\n")
    bare_skills = score(bare)["breakdown"]["skills"]["score"]
    embedded_skills = score(embedded)["breakdown"]["skills"]["score"]
    assert embedded_skills > bare_skills + 3.0, \
        f"embedded={embedded_skills}, bare={bare_skills}"


def test_repeated_skill_mentions_decay():
    """Repeating one skill dozens of times must not raise its score —
    occurrences beyond the cap decay toward zero."""
    base = ("Robin Hood\nrobin.hood@gmail.com\n"
            "EXPERIENCE\nDeveloper, Sherwood, 2021-2024\n"
            "Built a Python tool that processed 500 files per day.\n"
            "Maintained Python scripts for the team.\n"
            "SKILLS\nPython, Git\n")
    stuffed = base + "\n".join("Python " * 20 for _ in range(15)) + "\n"
    base_skills = score(base)["breakdown"]["skills"]["score"]
    stuffed_skills = score(stuffed)["breakdown"]["skills"]["score"]
    assert stuffed_skills <= base_skills, \
        f"repetition raised the skills score: base={base_skills}, stuffed={stuffed_skills}"


def test_template_bullets_collapse():
    """22 copies of one bullet template must earn ~1/22 of the bullet
    points, not 22/22."""
    assert "1/13 strong" in score(JUNK_STUFFED)["breakdown"]["content"]["details"]
