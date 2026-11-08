"""Generate synthetic gold-standard resumes for ENG-09 benchmark.

SYNTHETIC DATA — READ THIS FIRST:
Every resume below is INVENTED. Names, companies, numbers, and histories
are fictional; they describe no real person and were never submitted
anywhere. They exist so the extraction benchmark (benchmarks/run.py) and
the ATS scorer benchmark (scripts/benchmark_resume.py --cases) have a
varied, reproducible regression set. NEVER present them as real resumes,
real applicants, or real benchmark results. The honest claim these support
is only: "on these synthetic cases, the engine extracts in these bands
and preserves this ranking."

Coverage (120 cases): 10 roles x 12. Per role the mix is 4 strong, 4
average, 2 weak, 1 thin, 1 keyword-stuffed gaming case; experience spans
fresher (0 yrs), junior (1-3 yrs), and senior (5+ yrs). Two cases are
deliberately hard for the parser (one international +34 phone, one
ALL-CAPS name with a double space) — they measure real parser limits, so
field accuracy on the full set is honestly below 1.00 (see docs).

Deterministic: random.seed(42). Re-running reproduces the same 120 files.
Also writes benchmarks/resume_cases.json ({id, label, text,
expected_band}) for the ATS scorer benchmark; gaming cases carry
expected_band=None (diagnostic only, like the inline gaming case).

Usage:
    python benchmarks/generate_gold.py   # writes 120 files + cases JSON
"""
import json
import os
import random

random.seed(42)

FIRST = ["Aarav", "Priya", "Rohan", "Sneha", "Arjun", "Kavya", "Vikram",
         "Ananya", "Aditya", "Meera", "Nikhil", "Isha", "Karan", "Divya",
         "Rahul", "Sana", "Manish", "Pooja", "Varun", "Nisha", "Imran",
         "Lakshmi", "Suresh", "Tanvi", "Amit", "Ritu", "Harsh", "Neha",
         "Gaurav", "Shreya", "Farhan", "Anika", "Dev", "Maya", "Zaid",
         "Kiran", "Rohit", "Simran", "Abhishek", "Tara"]
LAST = ["Sharma", "Patel", "Singh", "Gupta", "Mehta", "Reddy", "Khan",
        "Iyer", "Nair", "Verma", "Kulkarni", "Joshi", "Rao", "Das",
        "Chopra", "Bose", "Menon", "Agarwal", "Saxena", "Pillai",
        "Fernandez", "D'Souza", "Kapoor", "Malhotra", "Chatterjee",
        "Nambiar", "Shetty", "Bhat", "Trivedi", "Kaur"]
COMPANIES = ["TechCorp", "DataSystems", "CloudNine", "InnovateLabs",
             "NexaTech", "BrightSoft", "WebScale", "StartupXYZ", "DataWorks",
             "InsightLabs", "DesignStudio", "SoftServe", "Northwind",
             "CodeCraft", "PixelWorks", "CloudForge", "DataNest", "AppWorks",
             "TechNova", "ZenithSoft"]
EDU = [("B.Tech Computer Science", "Indian Institute of Technology Delhi"),
       ("B.Tech Computer Science", "NIT Trichy"),
       ("B.Tech Computer Science", "VIT Vellore"),
       ("B.E. Information Technology", "Pune University"),
       ("B.E. Computer Engineering", "Mumbai University"),
       ("B.Sc Computer Science", "University of Mumbai"),
       ("B.Sc Computer Science", "Delhi University"),
       ("B.Tech Computer Science", "SRM University"),
       ("B.Sc Statistics", "University of Mumbai"),
       ("MCA", "IGNOU"),
       ("M.Tech Computer Science", "IIT Bombay"),
       ("B.Com", "Delhi University")]

# Role specs: canonical skill names (must be in models/skill_extractor's
# vocabulary — verified against data/skills_db.csv), companies, and
# quantified bullet templates. Slots: {n} small int, {pct} percent,
# {users} scale.
ROLE_SPECS = {
    "backend": {
        "titles": ["Software Engineer", "Backend Developer",
                   "Backend Engineer"],
        "skills": ["Python", "Java", "Django", "Flask", "FastAPI",
                   "PostgreSQL", "MySQL", "MongoDB", "Redis", "Docker",
                   "Kubernetes", "AWS", "SQL", "Git", "Kafka", "Node.js",
                   "Go", "CI/CD", "Linux", "Bash"],
        "bullets": [
            "Built a payment service in Python and Django serving {users} users daily with 99.9 percent availability",
            "Designed a PostgreSQL schema migration that cut query latency by {pct} percent",
            "Implemented Redis caching that reduced database load by {pct} percent",
            "Automated deployment pipelines with Docker and Kubernetes, cutting release time by {pct} percent",
            "Developed REST APIs with Flask and FastAPI for a logistics platform used by {users} clients",
            "Optimized slow SQL queries and improved checkout throughput by {pct} percent",
            "Led a team of {n} engineers through design reviews and on-call rotations",
            "Built monitoring dashboards that resolved incidents {pct} percent faster",
        ],
        "projects": [
            "Open-source contributor to a task queue library used by {users} developers",
            "Built a personal analytics dashboard tracking fitness data for {users} users",
            "Created a URL shortener service with rate limiting and analytics",
        ],
    },
    "frontend": {
        "titles": ["Frontend Developer", "Frontend Engineer",
                   "UI Engineer"],
        "skills": ["JavaScript", "TypeScript", "React", "Angular", "Vue",
                   "HTML", "CSS", "Tailwind CSS", "Figma", "Git", "Jest",
                   "Cypress", "Node.js", "GraphQL", "Next.js", "Svelte",
                   "Bootstrap"],
        "bullets": [
            "Built a React and TypeScript component library adopted by {n} product teams",
            "Designed responsive interfaces with HTML, CSS, and Tailwind CSS that improved mobile conversion by {pct} percent",
            "Implemented GraphQL data layers that reduced page load times by {pct} percent",
            "Optimized bundle sizes and raised Lighthouse scores by {pct} points across flagship pages",
            "Led accessibility improvements that resolved {n} audit issues",
            "Delivered a checkout redesign used by {users} users",
            "Created Figma-to-code workflows that improved designer handoff speed",
            "Built marketing sites with Vue and Angular for retail clients",
        ],
        "projects": [
            "Personal portfolio site with interactive demos, visited by {users} users",
            "Open-source contributor to a charting library",
            "Built a real-time dashboard with WebSocket updates",
        ],
    },
    "fullstack": {
        "titles": ["Full Stack Developer", "Software Engineer",
                   "Full-Stack Engineer"],
        "skills": ["Python", "JavaScript", "TypeScript", "React", "Node.js",
                   "Django", "Flask", "PostgreSQL", "MongoDB", "Docker",
                   "AWS", "Git", "GraphQL", "SQL", "Express", "Next.js",
                   "HTML", "CSS", "Redis", "Jest"],
        "bullets": [
            "Built an e-commerce platform with React and Django serving {users} users",
            "Developed REST and GraphQL APIs that powered mobile and web clients",
            "Implemented authentication and payment flows used by {users} customers",
            "Optimized database queries and caching, cutting page load by {pct} percent",
            "Shipped a real-time notification system with Node.js and Redis",
            "Automated CI/CD pipelines that reduced release errors by {pct} percent",
            "Led the migration from a monolith to services, improving deploy frequency",
            "Built admin dashboards in React that saved the support team {n} hours a week",
        ],
        "projects": [
            "E-commerce clone with cart, search, and checkout flows",
            "Chat application with real-time messaging for {users} users",
            "Task manager app with authentication and reminders",
        ],
    },
    "data-analyst": {
        "titles": ["Data Analyst", "Business Analyst", "Analytics Engineer"],
        "skills": ["SQL", "Python", "Excel", "Tableau", "Power BI",
                   "PostgreSQL", "MySQL", "Git", "Bash", "Apache Spark",
                   "Hadoop", "Airflow", "Scala", "Linux", "Pandas",
                   "NumPy"],
        "bullets": [
            "Created Tableau dashboards and Power BI reports used by {n} business teams",
            "Analyzed sales data with SQL and presented findings that increased campaign performance by {pct} percent",
            "Built automated Excel reports that saved analysts {n} hours every week",
            "Designed PostgreSQL queries that cut dashboard load time by {pct} percent",
            "Cleaned and modeled datasets with Python and Pandas for monthly reporting",
            "Presented insights to leadership that shaped pricing decisions",
            "Automated data quality checks that caught bad data before it reached reports",
            "Built a churn analysis that identified {pct} percent of at-risk customers",
        ],
        "projects": [
            "Data analysis project on public datasets with charts and a written report",
            "Built a sales forecasting dashboard for a retail dataset",
            "Created a KPI tracker for a student club with {users} members",
        ],
    },
    "data-engineer": {
        "titles": ["Data Engineer", "Big Data Engineer", "ETL Developer"],
        "skills": ["Python", "SQL", "PostgreSQL", "MySQL", "Apache Spark",
                   "Hadoop", "Airflow", "Kafka", "AWS", "Docker",
                   "Kubernetes", "Git", "Scala", "Bash", "Linux", "Redis"],
        "bullets": [
            "Built batch and streaming pipelines in Python processing data for {users} users daily",
            "Designed a PostgreSQL warehouse schema and Airflow workflows that cut pipeline failures by {pct} percent",
            "Optimized Spark jobs that reduced processing time by {pct} percent",
            "Automated data quality checks that resolved bad data before it reached reports",
            "Managed a migration from MySQL to PostgreSQL that improved query speed",
            "Built Kafka consumers that ingested {n} events per second",
            "Created monitoring for pipeline health that alerted teams within minutes",
            "Mentored {n} analysts on SQL best practices",
        ],
        "projects": [
            "Data pipeline portfolio project ingesting public datasets with daily refreshed charts",
            "Built a log aggregation pipeline with Kafka and Spark",
            "Created an ELT framework for small teams",
        ],
    },
    "ml-engineer": {
        "titles": ["Machine Learning Engineer", "ML Engineer",
                   "AI Engineer"],
        "skills": ["Python", "TensorFlow", "PyTorch", "Scikit-learn",
                   "Pandas", "NumPy", "SQL", "AWS", "Docker", "Kubernetes",
                   "Git", "Bash", "Linux", "Apache Spark", "PostgreSQL",
                   "Flask", "FastAPI", "Hugging Face", "LangChain"],
        "bullets": [
            "Built a recommendation model with PyTorch that increased click-through by {pct} percent",
            "Deployed a TensorFlow classification service serving {users} requests daily",
            "Designed a feature pipeline with Pandas and Spark that cut training data prep by {pct} percent",
            "Fine-tuned a Hugging Face model that improved extraction accuracy by {pct} percent",
            "Built an evaluation harness that caught {pct} percent of regressions before release",
            "Created a LangChain agent prototype that answered support questions",
            "Optimized model inference latency by {pct} percent with batching and caching",
            "Presented model results to product teams and shaped the roadmap",
        ],
        "projects": [
            "Image classifier with reasonable accuracy on a public dataset",
            "Built a sentiment analysis API with FastAPI",
            "Created a movie recommendation system with collaborative filtering",
        ],
    },
    "qa": {
        "titles": ["QA Engineer", "Quality Assurance Engineer",
                   "Test Engineer", "SDET"],
        "skills": ["Python", "Java", "JavaScript", "Selenium", "Jest",
                   "Cypress", "JUnit", "PyTest", "Git", "SQL", "Bash",
                   "Linux", "Docker", "Jenkins", "CI/CD", "JIRA"],
        "bullets": [
            "Built Selenium test suites that caught {pct} percent of regressions before release",
            "Automated API testing with PyTest, covering {n} endpoints",
            "Designed a CI/CD quality gate that blocked {n} bad deploys a month",
            "Created Cypress end-to-end tests for checkout flows used by {users} users",
            "Reduced manual regression time by {pct} percent with automation",
            "Wrote JUnit tests that raised coverage from {pct} percent to {pct} percent",
            "Filed and tracked {n} defects through triage to closure",
            "Built a test data generator that unblocked {n} teams",
        ],
        "projects": [
            "Built a test automation framework for a demo web app",
            "Created a load-testing script with {n} virtual users",
            "Wrote a bug-bash checklist adopted by the QA team",
        ],
    },
    "devops": {
        "titles": ["DevOps Engineer", "Site Reliability Engineer",
                   "Platform Engineer"],
        "skills": ["Docker", "Kubernetes", "AWS", "Azure", "GCP",
                   "Terraform", "Jenkins", "Git", "GitLab", "Bash",
                   "Linux", "Python", "CI/CD", "GitHub", "GitLab CI",
                   "PowerShell", "Ansible"],
        "bullets": [
            "Automated infrastructure with Terraform across {n} AWS accounts",
            "Built Kubernetes clusters that served {users} users with 99.9 percent uptime",
            "Designed CI/CD pipelines that cut deploy time by {pct} percent",
            "Implemented monitoring and alerting that resolved incidents {pct} percent faster",
            "Migrated {n} services to containers, reducing infrastructure costs by {pct} percent",
            "Created runbooks that cut onboarding time for on-call engineers",
            "Automated backups and disaster recovery drills for production databases",
            "Built a GitLab CI template adopted by {n} teams",
        ],
        "projects": [
            "Built a home lab Kubernetes cluster with monitoring",
            "Created a Terraform module for static site hosting",
            "Wrote a deployment dashboard for {n} services",
        ],
    },
    "mobile": {
        "titles": ["Mobile Developer", "Android Developer",
                   "iOS Developer", "Flutter Developer"],
        "skills": ["Kotlin", "Swift", "Flutter", "Android Development",
                   "iOS Development", "Java", "Git", "Figma", "SQL",
                   "Firebase", "Dart", "SQLite", "GraphQL"],
        "bullets": [
            "Built an Android app with Kotlin downloaded by {users} users",
            "Developed an iOS app with Swift that reached a {pct} rating on the App Store",
            "Created a Flutter app that shipped on Android and iOS from one codebase",
            "Implemented offline sync with Firebase that worked without connectivity",
            "Optimized app startup time by {pct} percent",
            "Integrated GraphQL APIs for real-time features",
            "Redesigned onboarding that improved activation by {pct} percent",
            "Set up CI/CD for mobile builds that cut release time by {pct} percent",
        ],
        "projects": [
            "Built a campus event app with Flutter used by {users} students",
            "Created a fitness tracker with offline support",
            "Published a habit-tracking app on the Play Store",
        ],
    },
    "fresher": {
        "titles": ["Software Engineer", "Junior Developer",
                   "Associate Software Engineer"],
        "skills": ["Python", "Java", "JavaScript", "SQL", "Git", "React",
                   "HTML", "CSS", "Node.js", "Django", "Flask", "MongoDB",
                   "C", "C++", "TypeScript"],
        "bullets": [
            "Built a campus event app with Flask and SQLite used by {users} students",
            "Implemented a pathfinding visualizer in JavaScript",
            "Created a machine learning mini project that classified images",
            "Developed a dashboard feature in React that displayed sales data",
            "Fixed frontend bugs and wrote documentation for a component library",
            "Built a library management web app used by {users} students",
            "Created a REST API with Flask for a college project",
            "Contributed to an open-source project by fixing {n} issues",
        ],
        "projects": [
            "E-commerce clone with React and Flask, demoed to {users} users",
            "Chat application with real-time messaging using websockets",
            "Task manager app with authentication and reminders",
            "Data analysis project on public datasets with charts",
        ],
    },
}

# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _fill(template):
    """Fill {n}/{pct}/{users} slots with random values."""
    out = template
    while "{n}" in out:
        out = out.replace("{n}", str(random.randint(2, 12)), 1)
    while "{pct}" in out:
        out = out.replace("{pct}", str(random.choice(
            [10, 15, 20, 25, 30, 35, 40, 45, 50, 60])), 1)
    while "{users}" in out:
        out = out.replace("{users}", str(random.choice(
            [200, 300, 500, 1000, 2000, 5000, 10000, 50000, 100000,
             300000])), 1)
    return out


def _person(i):
    """A unique fictional person (name, email, phone)."""
    while True:
        name = f"{random.choice(FIRST)} {random.choice(LAST)}"
        if name not in _person.used:
            _person.used.add(name)
            break
    first, last = name.split(" ", 1)
    clean_last = last.lower().replace(" ", "").replace("'", "")
    email = f"{first.lower()}.{clean_last}{i}@example.com"
    phone = f"+91 {random.choice('6789')}{random.randint(1000, 9999)} " \
            f"{random.randint(10000, 99999)}"
    return name, email, phone


_person.used = set()


def _skills_block(skills):
    return "SKILLS\n" + ", ".join(skills)


def _edu_block(level="junior"):
    """Graduation year consistent with the experience level (a senior who
    graduated in 2024 after working since 2019 would not be realistic)."""
    degree, uni = random.choice(EDU)
    years = {"senior": (2015, 2019), "junior": (2021, 2023),
             "fresher": (2024, 2025)}[level]
    year = random.randint(*years)
    return f"EDUCATION\n{degree}, {uni}, {year}"


def _exp_entry(title, company, dates, bullets):
    lines = [f"{title}, {company}, {dates}"]
    for b in bullets:
        lines.append(f"- {_fill(b)}")
    return "\n".join(lines)


def _summary(role_title, level, years):
    if level == "senior":
        return (
            f"Summary\n{role_title} with {years} years of experience "
            "building reliable systems for high-traffic products. "
            "Specialized in clean code, mentoring engineers, and "
            "measurable impact."
        )
    if level == "junior":
        return (
            f"Summary\n{role_title} with {years} years of experience "
            "working on web applications and internal tools. Looking to "
            "deepen technical skills and contribute to larger systems."
        )
    return (
        f"Summary\nRecent computer science graduate with strong project "
        f"experience in {role_title.lower()} work. Eager to start a "
        "software engineering career and learn production practices."
    )


def render_strong(spec, role_key, name, email, phone):
    """Rich resume: summary + 2 experience entries + skills + education
    + projects. Targets the 'strong' ATS band (>= 70)."""
    rs = ROLE_SPECS[role_key]
    title = random.choice(rs["titles"])
    level = spec["level"]
    years = {"senior": random.randint(5, 8), "junior": random.randint(2, 3),
             "fresher": 0}[level]
    co1, co2 = random.sample(COMPANIES, 2)
    if level == "senior":
        dates1, dates2 = "2019 - Present", "2017 - 2019"
        exp1 = _exp_entry(title, co1, dates1,
                          random.sample(rs["bullets"], 4))
        exp2 = _exp_entry(title, co2, dates2,
                          random.sample(rs["bullets"], 3))
    elif level == "junior":
        dates1, dates2 = "2023 - Present", "2022 - 2023"
        exp1 = _exp_entry(title, co1, dates1,
                          random.sample(rs["bullets"], 4))
        exp2 = _exp_entry("Software Engineering Intern", co2, dates2,
                          random.sample(rs["bullets"], 3))
    else:  # fresher: internships carry the weight (spread over 2-3 yrs
        # so the experience component reflects real internship history)
        dates1, dates2 = "2023 - 2025", "2022 - 2023"
        exp1 = _exp_entry("Software Engineering Intern", co1, dates1,
                          random.sample(rs["bullets"], 4))
        exp2 = _exp_entry("Software Engineering Intern", co2, dates2,
                          random.sample(rs["bullets"], 3))
    skills = random.sample(rs["skills"],
                           k=min(random.randint(10, 14),
                                 len(rs["skills"])))
    projects = random.sample(rs["projects"], 3)
    proj_block = "PROJECTS\n" + "\n".join(
        f"- {_fill(p)}." if not p.endswith(".") else f"- {_fill(p)}"
        for p in projects)
    text = "\n\n".join([
        name,
        f"{email} | {phone} | linkedin.com/in/"
        f"{name.split(' ', 1)[0].lower()}{name.split(' ', 1)[1].lower()}",
        _summary(title, level, years),
        "EXPERIENCE\n" + exp1 + "\n" + exp2,
        _skills_block(skills),
        _edu_block(level),
        proj_block,
    ])
    return text, {"role": title, "skills": skills,
                  "experience_years": years}


def render_average(spec, role_key, name, email, phone):
    """Mid resume: summary + 1-2 experience entries + skills + education.
    Targets the 'average' ATS band (40-70)."""
    rs = ROLE_SPECS[role_key]
    title = random.choice(rs["titles"])
    level = spec["level"]
    years = {"senior": random.randint(5, 7), "junior": random.randint(1, 3),
             "fresher": 0}[level]
    co1, co2 = random.sample(COMPANIES, 2)
    if level == "fresher":
        exp = _exp_entry("Software Engineering Intern", co1, "2024 - 2025",
                         random.sample(rs["bullets"], 2))
        entries = exp
    elif level == "junior":
        exp1 = _exp_entry(title, co1, "2023 - Present",
                          random.sample(rs["bullets"], 2))
        exp2 = _exp_entry(title, co2, "2022 - 2023",
                          random.sample(rs["bullets"], 2))
        entries = exp1 + "\n" + exp2
    else:
        # Senior but thin: one entry only, so the score stays in the
        # average band instead of drifting into strong territory.
        entries = _exp_entry(title, co1, "2020 - Present",
                             random.sample(rs["bullets"], 2))
    skills = random.sample(rs["skills"], k=random.randint(5, 8))
    text = "\n\n".join([
        name,
        f"{email} | {phone}",
        _summary(title, level, years),
        "EXPERIENCE\n" + entries,
        _skills_block(skills),
        _edu_block(level),
    ])
    return text, {"role": title, "skills": skills,
                  "experience_years": years}


def render_weak(spec, role_key, name, email, phone, force_contact=False):
    """Thin resume: one short experience entry + few skills, sections
    missing. Targets the 'weak' ATS band (0-40)."""
    rs = ROLE_SPECS[role_key]
    title = random.choice(rs["titles"])
    level = spec["level"]
    years = {"junior": random.randint(1, 2), "fresher": 0}[level]
    company = random.choice(COMPANIES)
    # Junior weak resumes stay minimal: 3 skills and a bare year (no
    # range) so the experience component cannot push them into the
    # average band; fresher weak resumes keep the 3-5 skill spread.
    n_skills = 3 if level == "junior" else random.randint(3, 5)
    skills = random.sample(rs["skills"], k=n_skills)
    if force_contact:
        include_email, include_phone = True, True
    else:
        include_email = random.random() < 0.7
        # Consume the phone draw to keep the RNG stream stable, but weak
        # junior resumes list email only: a phone as well pushes the
        # contact component high enough to leave the weak band.
        random.random()
        include_phone = False
    contact = name
    if include_email:
        contact += f"\n{email}"
        if include_phone:
            contact += f" | {phone}"
    # Shorten the bullet: weak resumes do not quantify.
    raw = random.choice(rs["bullets"])
    short = _fill(raw.split(" that ")[0].split(" with ")[0])
    exp = (f"EXPERIENCE\n{title}, {company}, 2024\n"
           f"- {short}.")
    text = "\n\n".join([contact, exp, _skills_block(skills)])
    return text, {"role": title, "skills": skills,
                  "experience_years": years,
                  "email": email if include_email else "",
                  "phone": phone if include_phone else ""}


def render_thin(spec, role_key, name, email, phone):
    """Barely a resume: name + email + one line + two skills.
    Targets the 'thin' ATS band (0-25)."""
    rs = ROLE_SPECS[role_key]
    title = random.choice(rs["titles"])
    skills = random.sample(rs["skills"], k=2)
    text = (f"{name}\n{email}\n"
            f"Aspiring developer seeking opportunities.\n"
            f"{skills[0]}, {skills[1]}")
    # Thin resumes carry no phone number; label it empty (honest).
    return text, {"role": title, "skills": skills, "experience_years": 0,
                  "phone": ""}


GAMING_SENTENCES = {
    "backend": "Developed Python applications with SQL databases and Docker containers for cloud deployment.",
    "frontend": "Built React interfaces with JavaScript components and CSS styling for web applications.",
    "fullstack": "Developed Python backends with React frontends and SQL databases for web applications.",
    "data-analyst": "Analyzed sales data with SQL queries and Tableau dashboards for business reporting.",
    "data-engineer": "Built Python pipelines with SQL warehouses and Spark jobs for data processing.",
    "ml-engineer": "Trained Python models with TensorFlow frameworks and SQL datasets for predictions.",
    "qa": "Tested Python applications with Selenium scripts and SQL checks for quality assurance.",
    "devops": "Deployed Docker containers with Kubernetes clusters and AWS services for cloud infrastructure.",
    "mobile": "Built Flutter apps with Dart code and Firebase backends for mobile users.",
    "fresher": "Developed Python projects with SQL databases and Git version control for learning.",
}


def render_gaming(spec, role_key, name, email, phone):
    """Keyword-stuffed diagnostic: one sentence copy-pasted 40x.
    The repetition guard must flag it (gaming_detected=True) and the
    score must stay low. No expected band (diagnostic only)."""
    rs = ROLE_SPECS[role_key]
    title = random.choice(rs["titles"])
    sentence = GAMING_SENTENCES[role_key]
    # The 3 skills named in the sentence, for honest extraction labels.
    sent_skills = {
        "backend": ["Python", "SQL", "Docker"],
        "frontend": ["React", "JavaScript", "CSS"],
        "fullstack": ["Python", "React", "SQL"],
        "data-analyst": ["SQL", "Tableau"],
        "data-engineer": ["Python", "SQL", "Apache Spark"],
        "ml-engineer": ["Python", "TensorFlow", "SQL"],
        "qa": ["Python", "Selenium", "SQL"],
        "devops": ["Docker", "Kubernetes", "AWS"],
        "mobile": ["Flutter", "Dart", "Firebase"],
        "fresher": ["Python", "SQL", "Git"],
    }[role_key]
    text = (f"{name}\n{email} | {phone}\n\n"
            f"SKILLS\n{', '.join(sent_skills)}\n\n"
            + "\n".join([sentence] * 40))
    return text, {"role": title, "skills": sent_skills,
                  "experience_years": 0}


RENDERERS = {"strong": render_strong, "average": render_average,
             "weak": render_weak, "thin": render_thin,
             "gaming": render_gaming}
BANDS = {"strong": "strong", "average": "average", "weak": "weak",
         "thin": "thin", "gaming": None}

# Per role (12 specs): 4 strong, 4 average, 2 weak, 1 thin, 1 gaming.
ROLE_MIX = [
    ("strong", "senior"), ("strong", "senior"), ("strong", "junior"),
    ("strong", "fresher"), ("average", "junior"), ("average", "junior"),
    ("average", "fresher"), ("average", "senior"), ("weak", "junior"),
    ("weak", "fresher"), ("thin", "fresher"), ("gaming", "junior"),
]
ROLES = ["backend", "frontend", "fullstack", "data-analyst",
         "data-engineer", "ml-engineer", "qa", "devops", "mobile",
         "fresher"]

# Deliberately hard parser cases (honest limitations, documented in docs):
# one international phone, one ALL-CAPS name with a double space.
HARD_CASES = {(3, 9): "intl_phone", (7, 10): "all_caps_name"}


def build_all():
    """Build all 120 gold items + the ATS cases file content."""
    items, cases = [], []
    idx = 0
    for ri, role_key in enumerate(ROLES):
        for si, (quality, level) in enumerate(ROLE_MIX):
            name, email, phone = _person(idx)
            hard = HARD_CASES.get((ri, si))
            if hard == "intl_phone":
                phone = "+34 612 345 789"
            name_display = name
            if hard == "all_caps_name":
                name_display = name.upper().replace(" ", "  ")
            spec = {"quality": quality, "level": level}
            renderer = RENDERERS[quality]
            if quality == "weak":
                text, extra = renderer(spec, role_key, name_display,
                                       email, phone,
                                       force_contact=bool(hard))
            else:
                text, extra = renderer(spec, role_key, name_display,
                                       email, phone)
            labels = {"name": name, "email": extra.get("email", email),
                      "phone": extra.get("phone", phone),
                      "skills": extra["skills"], "role": extra["role"],
                      "role_key": role_key}
            item = {"id": f"syn_{idx:03d}", "text": text, "labels": labels,
                    "quality": quality, "level": level,
                    "experience_years": extra["experience_years"],
                    "expected_band": BANDS[quality]}
            items.append(item)
            cases.append({"id": f"syn_{idx:03d}",
                          "label": f"SYNTHETIC {quality} {extra['role']} "
                                   f"({level})",
                          "text": text,
                          "expected_band": BANDS[quality]})
            idx += 1
    return items, cases


def main():
    out_dir = os.path.join(os.path.dirname(__file__), "resume_gold")
    os.makedirs(out_dir, exist_ok=True)
    # Remove stale gold files so a re-run cannot leave orphans.
    for fn in os.listdir(out_dir):
        if fn.startswith("syn_") and fn.endswith(".json"):
            os.remove(os.path.join(out_dir, fn))
    items, cases = build_all()
    for item in items:
        with open(os.path.join(out_dir, f"{item['id']}.json"), "w") as f:
            json.dump(item, f, indent=2)
    cases_path = os.path.join(os.path.dirname(__file__),
                              "resume_cases.json")
    with open(cases_path, "w") as f:
        json.dump(cases, f, indent=2)
    from collections import Counter
    print(f"Generated {len(items)} synthetic resumes in {out_dir}")
    print("quality mix:", dict(Counter(i["quality"] for i in items)))
    print(f"Wrote {cases_path}")


if __name__ == "__main__":
    main()
