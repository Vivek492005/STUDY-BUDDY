#!/usr/bin/env python3
"""Resume ATS benchmark harness (P2 benchmark item).

Runs the production resume pipeline — clean_text -> extract_sections ->
extract_skills -> extract_experience_years -> calculate_ats_score — over a
case set and prints per-case plus aggregate scores. The ATS number is a
heuristic checklist signal, NOT a prediction of any real ATS outcome.

SYNTHETIC REGRESSION SET — READ THIS FIRST:
The bundled cases below are INVENTED resumes written for regression
testing. They describe no real person, they were never submitted anywhere,
and their scores are NOT claims about any real applicant. They exist so a
change to the scorer shows up as a diff in this report. NEVER present them
as real data, real resumes, or real benchmark results. The honest claim
this harness supports is only: "on these synthetic cases, the engine
scores in these bands and preserves this ranking."

REAL DATA (not shipped): pass --real-dir with .txt/.md resumes collected
with consent, or --cases with a JSON list of
{"id", "label", "text", "expected_band"} objects. Real cases are scored
exactly the same way; band checks only apply when expected_band is set.

Usage:
    python scripts/benchmark_resume.py                 # synthetic set
    python scripts/benchmark_resume.py --real-dir DIR  # score real files
    python scripts/benchmark_resume.py --cases FILE.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.ats_scorer import calculate_ats_score  # noqa: E402
from models.skill_extractor import extract_skills  # noqa: E402
from parser.section_extractor import (  # noqa: E402
    extract_experience_years,
    extract_sections,
)
from parser.text_cleaning import clean_text  # noqa: E402

# Bands are expectations about the SYNTHETIC cases' intent: a resume
# written to be strong should score high, a thin one should score low.
# They are not universal cutoffs for real resumes.
BANDS = {
    "strong": (70.0, 100.0),
    "average": (40.0, 70.0),
    "weak": (0.0, 40.0),
    "thin": (0.0, 25.0),
}


def score_resume_text(text: str) -> dict:
    """Score one resume text through the production pipeline."""
    cleaned = clean_text(text or "")
    sections = extract_sections(cleaned)
    skills = extract_skills(cleaned)
    experience_years = extract_experience_years(cleaned, sections=sections)
    result = calculate_ats_score(cleaned, sections, skills, experience_years)
    breakdown = (result or {}).get("breakdown", {}) or {}
    components = {}
    for key, val in breakdown.items():
        if isinstance(val, dict) and "score" in val:
            components[key] = val["score"]
    total = result.get("total_score", sum(components.values()))
    return {
        "total": round(float(total), 1),
        "grade": result.get("grade"),
        "gaming_detected": bool(result.get("gaming_detected")),
        "components": components,
        "num_skills": len(skills),
        "skill_names": sorted(s["name"] for s in skills),
        "sections": sorted(sections),
        "experience_years": experience_years,
        "word_count": len(cleaned.split()),
    }


def run_cases(cases: list[dict]) -> list[dict]:
    """Score every case; attach band verdicts where expected_band is set."""
    results = []
    for case in cases:
        scored = score_resume_text(case.get("text", ""))
        band = case.get("expected_band")
        verdict = None
        if band and band in BANDS:
            lo, hi = BANDS[band]
            if band == "strong":
                # Upper-open: a very strong resume may score up to 100.
                verdict = "PASS" if scored["total"] >= lo else "FAIL"
            else:
                verdict = "PASS" if lo <= scored["total"] < hi else "FAIL"
        results.append({"id": case.get("id"), "label": case.get("label"),
                        "expected_band": band, "verdict": verdict, **scored})
    return results


def summarize(results: list[dict]) -> dict:
    """Aggregate statistics over scored results."""
    totals = [r["total"] for r in results]
    checked = [r for r in results if r["verdict"] is not None]
    by_band: dict[str, list[float]] = {}
    for r in results:
        if r["expected_band"]:
            by_band.setdefault(r["expected_band"], []).append(r["total"])
    ranking_ok = None
    if all(b in by_band for b in ("strong", "average", "weak")):
        means = {b: sum(v) / len(v) for b, v in by_band.items()}
        ranking_ok = means["strong"] > means["average"] > means["weak"]
    else:
        means = {b: sum(v) / len(v) for b, v in by_band.items()}
    return {
        "n": len(results),
        "mean": round(sum(totals) / len(totals), 1) if totals else 0.0,
        "min": min(totals) if totals else 0.0,
        "max": max(totals) if totals else 0.0,
        "band_checks": len(checked),
        "band_passes": sum(1 for r in checked if r["verdict"] == "PASS"),
        "band_means": {b: round(m, 1) for b, m in means.items()},
        "ranking_strong_gt_average_gt_weak": ranking_ok,
    }


def print_report(results: list[dict], title: str) -> dict:
    """Print a human-readable report; return the summary dict."""
    summary = summarize(results)
    print(f"\n=== {title} ===")
    print(f"cases: {summary['n']}  mean: {summary['mean']}  "
          f"min: {summary['min']}  max: {summary['max']}")
    if summary["band_means"]:
        print("band means: " + ", ".join(
            f"{b}={m}" for b, m in summary["band_means"].items()))
    if summary["ranking_strong_gt_average_gt_weak"] is not None:
        print("ranking strong > average > weak: "
              f"{summary['ranking_strong_gt_average_gt_weak']}")
    if summary["band_checks"]:
        print(f"band checks: {summary['band_passes']}/{summary['band_checks']} PASS")
    for r in results:
        comp = ", ".join(f"{k}={v}" for k, v in r["components"].items())
        band = f" band={r['expected_band']}:{r['verdict']}" if r["verdict"] else ""
        print(f"\n[{r['id']}] {r['label']}: total={r['total']} "
              f"grade={r.get('grade')}{band}")
        if r.get("gaming_detected"):
            print("  gaming_detected=True (repetition guard tripped)")
        print(f"  words={r['word_count']} skills={r['num_skills']} "
              f"exp_years={r['experience_years']} sections={','.join(r['sections'])}")
        print(f"  {comp}")
    return summary


def load_real_dir(path: str) -> list[dict]:
    """Load .txt/.md resumes from a directory (no band expectations)."""
    cases = []
    for name in sorted(os.listdir(path)):
        if not name.lower().endswith((".txt", ".md")):
            continue
        with open(os.path.join(path, name), encoding="utf-8") as f:
            cases.append({"id": os.path.splitext(name)[0], "label": name,
                          "text": f.read(), "expected_band": None})
    return cases


def load_cases_file(path: str) -> list[dict]:
    """Load a JSON case list: [{id, label, text, expected_band?}]."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("cases file must contain a JSON list")
    return data

# ---------------------------------------------------------------------------
# SYNTHETIC REGRESSION SET (invented resumes — NOT real people, NOT real
# resumes; see the module docstring). Names, companies, and numbers below
# are fictional and exist only to exercise the scorer's components.
# ---------------------------------------------------------------------------

SYNTHETIC_CASES = [
    {
        "id": "syn-strong-backend",
        "label": "SYNTHETIC strong backend engineer (6 yrs)",
        "expected_band": "strong",
        "text": """
Aarav Sharma
Senior Software Engineer
aarav.sharma@example.com | +91 98765 43210 | linkedin.com/in/aaravsharma | github.com/aaravsharma

Summary
Senior Software Engineer with 6 years of experience building distributed
backend systems for high-traffic products. Specialized in Python services,
database design, and cloud infrastructure. Passionate about clean code,
mentoring engineers, and measurable impact.

Experience
Senior Software Engineer, TechCorp, 2021 - Present
Developed a payment processing service in Python and Django that served
50000 users daily with 99.95 percent availability. Designed a PostgreSQL
schema migration strategy that reduced query latency and improved
throughput for checkout flows. Implemented caching with Redis that
reduced database load and increased response speed. Led a team of 8
members and mentored 4 junior engineers through design reviews. Automated
deployment pipelines with Docker and Kubernetes, which reduced release
time and eliminated manual errors. Delivered 12 projects on schedule and
increased system reliability across three product lines.

Software Engineer, StartupXYZ, 2019 - 2021
Built REST APIs with Flask and FastAPI for a logistics platform used by
200 clients. Integrated MongoDB and PostgreSQL storage layers and
optimized slow queries. Created monitoring dashboards that resolved
incidents faster and improved uptime. Collaborated with frontend
engineers using React and TypeScript to deliver customer features.

Education
B.Tech Computer Science, Indian Institute of Technology Delhi, 2019

Skills
Python, Java, JavaScript, TypeScript, SQL, PostgreSQL, MongoDB, Redis,
Docker, Kubernetes, AWS, Git, React, Django, Flask, FastAPI, Pytest

Projects
Open source contributor to a task queue library. Built a personal
analytics dashboard that tracked fitness data for 500 users and
presented weekly trends.
""",
    },
    {
        "id": "syn-strong-data",
        "label": "SYNTHETIC strong data engineer (5 yrs)",
        "expected_band": "strong",
        "text": """
Priya Nair
Data Engineer
priya.nair@example.com | +91 98765 43211 | linkedin.com/in/priyanair | github.com/priyanair

Summary
Data Engineer with 5 years of experience designing data pipelines and
warehouses for analytics teams. Expert in Python, SQL, and cloud data
platforms. Delivered reliable pipelines that powered decisions for
multiple business units.

Experience
Data Engineer, DataWorks, 2021 - Present
Developed batch and streaming pipelines in Python that processed data
for 100000 users every day. Designed a PostgreSQL warehouse schema and
implemented Airflow workflows that reduced pipeline failures. Optimized
slow queries and increased dashboard performance for analysts.
Automated data quality checks that resolved bad data before it reached
reports. Managed a migration from MySQL to PostgreSQL that improved
query speed and reduced storage costs. Mentored 3 analysts on SQL best
practices and delivered 9 projects across reporting and machine
learning teams.

Data Analyst, Insight Labs, 2020 - 2021
Created Tableau dashboards and Power BI reports for retail clients.
Analyzed sales data with SQL and presented findings that increased
campaign performance. Built SQLite prototypes for quick experiments.

Education
B.Sc Statistics, University of Mumbai, 2020

Skills
Python, SQL, PostgreSQL, MySQL, SQLite, MongoDB, Redis, Docker,
Kubernetes, AWS, Git, Java, Scala, JavaScript, TypeScript, Tableau,
Power BI, Airflow

Projects
Built a data pipeline portfolio project that ingested public datasets
and served 2000 users with daily refreshed charts.
""",
    },
    {
        "id": "syn-strong-frontend",
        "label": "SYNTHETIC strong frontend engineer (5 yrs)",
        "expected_band": "strong",
        "text": """
Rohan Mehta
Frontend Engineer
rohan.mehta@example.com | +91 98765 43212 | linkedin.com/in/rohanmehta | github.com/rohanmehta

Summary
Frontend Engineer with 5 years of experience building accessible,
high-performance web applications. Specialized in React, TypeScript, and
design systems used by millions of users.

Experience
Frontend Engineer, WebScale Inc, 2021 - Present
Developed a React and TypeScript component library adopted by 6 product
teams and 40 engineers. Designed responsive interfaces with HTML, CSS,
and Tailwind CSS that improved mobile conversion. Implemented GraphQL
data layers and REST APIs integration that reduced page load times.
Optimized bundle sizes and increased Lighthouse scores across flagship
pages. Led accessibility improvements that resolved audit issues and
achieved compliance targets. Delivered 15 projects including a checkout
redesign used by 300000 users. Mentored junior engineers and created
documentation that increased onboarding speed.

Web Developer, DesignStudio, 2020 - 2021
Built marketing sites with Vue and Angular for retail clients.
Created Figma to code workflows that improved designer handoff.
Integrated Node.js and Express backends for small business tools.

Education
B.E. Information Technology, Pune University, 2020

Skills
JavaScript, TypeScript, React, Angular, Vue, HTML, CSS, Tailwind CSS,
Node.js, Express, GraphQL, REST APIs, Git, Figma, Docker, AWS, SQL

Projects
Personal portfolio site with interactive demos, visited by 8000 users.
Open source contributor to a charting library.
""",
    },
    {
        "id": "syn-senior-manager",
        "label": "SYNTHETIC senior engineering manager (8 yrs)",
        "expected_band": "strong",
        "text": """
Vikram Rao
Engineering Manager
vikram.rao@example.com | +91 98765 43213 | linkedin.com/in/vikramrao | github.com/vikramrao

Summary
Engineering Manager with 8 years of experience leading backend teams
through growth and platform migrations. Combines hands-on Python and
cloud expertise with a record of shipping reliable systems.

Experience
Engineering Manager, CloudNine, 2022 - Present
Managed a team of 12 members building billing infrastructure in Python
and PostgreSQL. Delivered a migration that reduced infrastructure costs
and saved $50000 annually. Implemented service ownership practices that
reduced incidents and improved on-call health. Mentored 5 engineers
into senior roles and established hiring practices that increased team
retention.

Senior Software Engineer, CloudNine, 2019 - 2022
Developed distributed services with Docker and Kubernetes on AWS.
Designed APIs used by 400 clients and optimized database performance.
Automated testing with Pytest that increased coverage and reduced
regressions. Created monitoring that resolved outages faster.

Software Engineer, SoftServe, 2017 - 2019
Built Java and SQL services for enterprise clients. Integrated Redis
caching and MongoDB storage for reporting tools.

Education
B.Tech Computer Science, NIT Trichy, 2017

Skills
Python, Java, SQL, PostgreSQL, MongoDB, Redis, Docker, Kubernetes,
AWS, Git, JavaScript, TypeScript, React, Django, Pytest, Kafka

Projects
Led an internal developer platform project adopted by 100 engineers.
Published articles on scaling teams and incident management.
""",
    },
    {
        "id": "syn-average-dev",
        "label": "SYNTHETIC average developer (2 yrs)",
        "expected_band": "average",
        "text": """
Sneha Kulkarni
Software Developer
sneha.kulkarni@example.com | +91 98765 43214

Summary
Software Developer with 2 years of experience working on web
applications and internal tools. Looking to deepen backend skills and
contribute to larger systems.

Experience
Software Developer, BrightSoft, 2023 - Present
Developed features in Python and Django for a task management product.
Fixed bugs reported by customers and improved small parts of the
codebase. Wrote unit tests with Pytest for new features. Collaborated
with designers to implement user interface changes.

Education
B.E. Computer Engineering, Mumbai University, 2023

Skills
Python, JavaScript, SQL, Django, Git, React

Projects
College capstone project: a library management web app used by 200
students. Built a personal blog with Flask.
""",
    },
    {
        "id": "syn-average-student",
        "label": "SYNTHETIC average student with internship",
        "expected_band": "average",
        "text": """
Aditya Verma
Computer Science Student
aditya.verma@example.com | linkedin.com/in/adityaverma

Experience
Software Engineering Intern, TechStart, 2024 - 2025
Developed a dashboard feature in React that displayed sales data for
internal teams. Fixed frontend bugs and wrote documentation for the
component library. Presented the final project to the engineering team.

Education
B.Tech Computer Science, VIT Vellore, 2025

Skills
Python, Java, JavaScript, SQL, React, Git

Projects
Built a campus event app with Flask and SQLite used by 300 students.
Implemented a pathfinding visualizer in JavaScript. Created a
machine learning mini project that classified images with reasonable
accuracy.
""",
    },
    {
        "id": "syn-average-nocontact",
        "label": "SYNTHETIC average developer, no contact info",
        "expected_band": "average",
        "text": """
Jordan Lee
Software Developer

Summary
Software Developer with 2 years of experience building business
applications and APIs for small companies. Comfortable across the
stack and focused on shipping maintainable code.

Experience
Software Developer, Northwind Traders, 2023 - Present
Developed internal tools in Python and JavaScript that automated
manual reporting work. Designed SQL queries and reports for managers.
Implemented a notification service that delivered updates to staff.
Maintained legacy code and improved documentation for new joiners.

Education
B.Sc Computer Science, State University, 2023

Skills
Python, JavaScript, SQL, PostgreSQL, Git, Django

Projects
Built an inventory tracker for a local store. Created a personal
website showcasing side projects and experiments.
""",
    },
    {
        "id": "syn-fresher-projects",
        "label": "SYNTHETIC fresher, projects-heavy",
        "expected_band": "average",
        "text": """
Kavya Reddy
Aspiring Software Engineer
kavya.reddy@example.com | +91 98765 43215

Summary
Recent computer science graduate with strong project experience in
full stack development. Eager to start a software engineering career
and learn production engineering practices.

Education
B.Tech Computer Science, SRM University, 2025

Skills
Python, JavaScript, SQL, React, Git, Flask, MongoDB

Projects
E-commerce clone built with React and Flask, featuring cart, search,
and checkout flows, demoed to 100 users. Chat application with
real-time messaging using websockets, tested with 50 concurrent
users. Data analysis project on public datasets with charts and a
written report. Task manager app with authentication and due date
reminders.
""",
    },
    {
        "id": "syn-weak-junior",
        "label": "SYNTHETIC weak junior resume",
        "expected_band": "weak",
        "text": """
Rahul Singh
Junior Developer

Experience
Junior Developer, SmallShop IT, 2024 - Present
Worked on website updates and fixed small bugs. Helped senior
developers with testing tasks.

Skills
Python, HTML, CSS
""",
    },
    {
        "id": "syn-weak-short",
        "label": "SYNTHETIC weak short resume",
        "expected_band": "weak",
        "text": """
Meera Joshi

Summary
Motivated graduate looking for opportunities in software.

Skills
Python, Java, SQL, Git

Education
B.Com, Delhi University, 2024
""",
    },
    {
        "id": "syn-thin",
        "label": "SYNTHETIC thin resume (name + one line)",
        "expected_band": "thin",
        "text": """
Nikhil Gupta
nikhil.gupta@example.com
Aspiring developer seeking opportunities.
Python, SQL
""",
    },
    {
        "id": "syn-gaming-repeat",
        "label": "SYNTHETIC diagnostic: copy-paste repetition (no band)",
        "expected_band": None,
        "text": "\n".join(
            ["Developed Python applications with SQL databases and Docker "
             "containers for cloud deployment."] * 40
        ),
    },
]


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--real-dir", default=None,
                        help="Directory of .txt/.md resumes (consented, real)")
    parser.add_argument("--cases", default=None,
                        help="JSON case file [{id, label, text, expected_band?}]")
    parser.add_argument("--json", action="store_true",
                        help="Print results as JSON instead of a report")
    args = parser.parse_args(argv)

    if args.real_dir and args.cases:
        parser.error("--real-dir and --cases are mutually exclusive")
    if args.real_dir:
        cases = load_real_dir(args.real_dir)
        title = f"REAL resumes from {args.real_dir} (no band checks)"
        if not cases:
            print(f"No .txt/.md files found in {args.real_dir}")
            return 2
    elif args.cases:
        cases = load_cases_file(args.cases)
        title = f"Cases from {args.cases}"
    else:
        cases = SYNTHETIC_CASES
        title = ("SYNTHETIC regression set — invented resumes, NOT real data")

    results = run_cases(cases)
    if args.json:
        print(json.dumps({"title": title, "summary": summarize(results),
                          "results": results}, indent=2))
    else:
        summary = print_report(results, title)
        failed = [r for r in results if r["verdict"] == "FAIL"]
        if failed:
            print("\nFAILED band checks: " + ", ".join(r["id"] for r in failed))
            return 1
        if summary["ranking_strong_gt_average_gt_weak"] is False:
            print("\nFAILED ranking check: strong/average/weak means out of order")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
