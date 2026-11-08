"""Generate the JD gold set: 30 realistic job descriptions (10 roles x 3
levels) with human-defined expected match signals, plus the matched /
mismatched gold-resume links used by benchmarks/run_jd.py.

Every must-have skill is a canonical name from data/skills_db.csv (verified
recognised by models/skill_extractor.py). The JD text embeds the skills in
natural prose so the extractor's recall on JD text is a real measurement.

Writes: benchmarks/jd_gold.json
    [{id, role, level, title, company, text, must_have_skills,
      nice_to_have_skills, experience_years_min,
      matched_resume_id, mismatched_resume_id}]

Deterministic: no randomness beyond a fixed seed for resume selection.
"""

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.generate_gold import ROLES  # noqa: E402

GOLD_DIR = Path(__file__).resolve().parent / "resume_gold"
OUT = Path(__file__).resolve().parent / "jd_gold.json"

COMPANIES = [
    "TechMahindra Digital", "NexaSoft Solutions", "CloudNine Labs",
    "BrightStack Technologies", "DataWorks India", "PixelForge Systems",
    "QuantumLeap Analytics", "SecureNet Infosystems", "AgileMind Labs",
    "NextGen Robotics",
]

LEVELS = {
    "junior": {"label": "Junior", "min_years": 0, "years_text": "0-2 years"},
    "mid": {"label": "Mid-Level", "min_years": 2, "years_text": "2-4 years"},
    "senior": {"label": "Senior", "min_years": 5, "years_text": "5+ years"},
}

# Per-role JD content. must_have_skills are canonical extractor names.
JD_SPECS = {
    "backend": {
        "titles": {"junior": "Junior Backend Engineer",
                   "mid": "Backend Engineer",
                   "senior": "Senior Backend Engineer"},
        "about": ("We build the APIs and services that power millions of "
                  "daily transactions for our fintech platform."),
        "duties": [
            "Design and build REST APIs and background services in Python and Django",
            "Model data in PostgreSQL and optimise slow queries with proper indexing",
            "Cache hot paths with Redis and keep p95 latency under 200 ms",
            "Write unit and integration tests and review pull requests in Git",
        ],
        "must_have": {"junior": ["Python", "Django", "PostgreSQL", "Git",
                                  "SQL"],
                      "mid": ["Python", "Django", "PostgreSQL", "Docker",
                              "Redis", "Git"],
                      "senior": ["Python", "Django", "PostgreSQL", "Docker",
                                 "AWS", "Kubernetes", "Redis"]},
        "nice_to_have": ["Kafka", "Terraform", "GraphQL"],
    },
    "frontend": {
        "titles": {"junior": "Junior Frontend Developer",
                   "mid": "Frontend Developer",
                   "senior": "Senior Frontend Engineer"},
        "about": ("We craft the web experiences our customers use every day, "
                  "from dashboards to checkout flows."),
        "duties": [
            "Build responsive interfaces in React and TypeScript",
            "Turn Figma designs into pixel-accurate HTML and CSS with Tailwind CSS",
            "Own the component library and keep it accessible and tested with Jest",
            "Optimise bundle size and Core Web Vitals for every release",
        ],
        "must_have": {"junior": ["JavaScript", "React", "HTML", "CSS", "Git"],
                      "mid": ["JavaScript", "TypeScript", "React",
                              "Tailwind CSS", "Jest", "Git"],
                      "senior": ["JavaScript", "TypeScript", "React",
                                 "Next.js", "Cypress", "Figma"]},
        "nice_to_have": ["Svelte", "Vue", "Node.js"],
    },
    "fullstack": {
        "titles": {"junior": "Junior Full-Stack Developer",
                   "mid": "Full-Stack Developer",
                   "senior": "Senior Full-Stack Engineer"},
        "about": ("We own features end to end, from the React interface down "
                  "to the Node.js services and the database."),
        "duties": [
            "Ship full features across React, Node.js and Express",
            "Design schemas in PostgreSQL and MongoDB for new product areas",
            "Containerise services with Docker and ship through CI/CD pipelines",
            "Debug production issues across the stack and own the fix",
        ],
        "must_have": {"junior": ["JavaScript", "React", "Node.js", "Git",
                                  "SQL"],
                      "mid": ["JavaScript", "React", "Node.js", "Express",
                              "PostgreSQL", "Docker"],
                      "senior": ["JavaScript", "React", "Node.js", "Next.js",
                                 "AWS", "Kubernetes", "MongoDB"]},
        "nice_to_have": ["TypeScript", "GraphQL", "Redis"],
    },
    "data-analyst": {
        "titles": {"junior": "Junior Data Analyst",
                   "mid": "Data Analyst",
                   "senior": "Senior Data Analyst"},
        "about": ("We turn raw product and sales data into the dashboards and "
                  "analyses that drive weekly business decisions."),
        "duties": [
            "Write SQL queries against our warehouse to answer business questions",
            "Build and maintain dashboards in Tableau and Power BI",
            "Clean and explore data in Python with Pandas and NumPy",
            "Present findings to stakeholders with clear Excel summaries",
        ],
        "must_have": {"junior": ["SQL", "Excel", "Python", "Tableau"],
                      "mid": ["SQL", "Python", "Pandas", "Tableau",
                              "Power BI", "Excel"],
                      "senior": ["SQL", "Python", "Pandas", "NumPy",
                                 "Tableau", "Power BI", "R"]},
        "nice_to_have": ["Apache Spark", "Statistics", "JIRA"],
    },
    "data-engineer": {
        "titles": {"junior": "Junior Data Engineer",
                   "mid": "Data Engineer",
                   "senior": "Senior Data Engineer"},
        "about": ("We build the pipelines that move terabytes of data from "
                  "our products into the warehouse every day."),
        "duties": [
            "Build batch and streaming pipelines in Python with Apache Spark",
            "Orchestrate workflows with Airflow and monitor them in production",
            "Stream events through Kafka into PostgreSQL and the warehouse",
            "Containerise pipeline jobs with Docker and deploy on AWS",
        ],
        "must_have": {"junior": ["Python", "SQL", "PostgreSQL", "Git"],
                      "mid": ["Python", "SQL", "Apache Spark", "Airflow",
                              "Docker"],
                      "senior": ["Python", "Apache Spark", "Airflow", "Kafka",
                                 "AWS", "Kubernetes"]},
        "nice_to_have": ["Terraform", "Scala", "Hadoop"],
    },
    "ml-engineer": {
        "titles": {"junior": "Junior ML Engineer",
                   "mid": "Machine Learning Engineer",
                   "senior": "Senior ML Engineer"},
        "about": ("We ship machine learning into production: recommendations, "
                  "ranking and language features used by millions."),
        "duties": [
            "Train and evaluate models with PyTorch, TensorFlow and Scikit-learn",
            "Prepare features and datasets in Python with Pandas and NumPy",
            "Serve models behind APIs and monitor them with Hugging Face tooling",
            "Prototype retrieval and agent flows with LangChain",
        ],
        "must_have": {"junior": ["Python", "Scikit-learn", "Pandas", "NumPy",
                                  "SQL"],
                      "mid": ["Python", "PyTorch", "TensorFlow",
                              "Scikit-learn", "Hugging Face"],
                      "senior": ["Python", "PyTorch", "TensorFlow",
                                 "Hugging Face", "LangChain", "Docker",
                                 "AWS"]},
        "nice_to_have": ["Kubernetes", "Keras", "MLOps"],
    },
    "qa": {
        "titles": {"junior": "Junior QA Engineer",
                   "mid": "QA Engineer",
                   "senior": "Senior QA Engineer"},
        "about": ("We own quality end to end: test strategy, automation and "
                  "release sign-off for every product team."),
        "duties": [
            "Write automated tests in Java with Selenium, JUnit and PyTest",
            "Run end-to-end suites with Cypress and Jest and triage failures",
            "File and track defects in JIRA and verify fixes before release",
            "Drive test coverage up through CI/CD with Jenkins",
        ],
        "must_have": {"junior": ["Java", "Selenium", "JIRA", "Git"],
                      "mid": ["Java", "Selenium", "PyTest", "JUnit",
                              "Cypress", "JIRA"],
                      "senior": ["Java", "Selenium", "PyTest", "Jenkins",
                                 "CI/CD", "Python"]},
        "nice_to_have": ["Cypress", "Postman", "SQL"],
    },
    "devops": {
        "titles": {"junior": "Junior DevOps Engineer",
                   "mid": "DevOps Engineer",
                   "senior": "Senior DevOps Engineer"},
        "about": ("We run the platform everything else deploys on: clusters, "
                  "pipelines and observability for 40+ services."),
        "duties": [
            "Run Kubernetes clusters and Docker workloads across AWS",
            "Write infrastructure as code with Terraform and shell scripts in Bash",
            "Own CI/CD pipelines in Jenkins and GitHub Actions",
            "Harden Linux hosts, manage secrets and keep on-call runbooks current",
        ],
        "must_have": {"junior": ["Linux", "Bash", "Docker", "Git"],
                      "mid": ["Docker", "Kubernetes", "Jenkins", "CI/CD",
                              "Linux"],
                      "senior": ["Docker", "Kubernetes", "AWS", "Terraform",
                                 "Jenkins", "GCP"]},
        "nice_to_have": ["Azure", "Ansible", "Prometheus"],
    },
    "mobile": {
        "titles": {"junior": "Junior Android Developer",
                   "mid": "Mobile Developer",
                   "senior": "Senior Mobile Engineer"},
        "about": ("We build the Android and iOS apps our customers open "
                  "every morning, used by over a million people."),
        "duties": [
            "Build native Android features in Kotlin and Java",
            "Ship cross-platform screens in Flutter and Dart",
            "Integrate Firebase for auth, push and analytics",
            "Own the release train and keep crash rates near zero",
        ],
        "must_have": {"junior": ["Kotlin", "Java", "Android Development",
                                  "Git"],
                      "mid": ["Kotlin", "Flutter", "Dart", "Firebase",
                              "Android Development"],
                      "senior": ["Kotlin", "Swift", "Flutter",
                                 "iOS Development", "Firebase", "CI/CD"]},
        "nice_to_have": ["React Native", "Swift", "Figma"],
    },
    "fresher": {
        "titles": {"junior": "Graduate Engineer Trainee",
                   "mid": "Associate Software Engineer",
                   "senior": "Software Engineer"},
        "about": ("Our graduate programme turns strong fundamentals into "
                  "production engineering through mentoring and real projects."),
        "duties": [
            "Write clean code in Python and Java and review it with mentors",
            "Query data with SQL and build small features in HTML and CSS",
            "Fix bugs, write tests and learn Git workflows on real services",
            "Present your project work to the team every sprint",
        ],
        "must_have": {"junior": ["Python", "Java", "SQL", "Git", "HTML"],
                      "mid": ["Python", "Java", "JavaScript", "React", "SQL",
                              "Git"],
                      "senior": ["Python", "Java", "Django", "PostgreSQL",
                                 "AWS", "Docker"]},
        "nice_to_have": ["JavaScript", "React", "Docker"],
    },
}


def _pick_resumes():
    """Resolve matched / mismatched gold-resume ids per (role, level).

    matched: same role, strong quality, experience closest to the JD level.
    mismatched: strong, different role, minimal must-have skill overlap.
    Uses labels["role_key"] (written by generate_gold.py) -- titles collide
    across roles ("Software Engineer" is both backend and fullstack), so a
    title->role map would misclassify resumes.
    """
    resumes = []
    for path in sorted(GOLD_DIR.glob("syn_*.json")):
        item = json.loads(path.read_text())
        labels = item["labels"]
        resumes.append({
            "id": item["id"],
            "quality": item["quality"],
            "role_key": labels.get("role_key", "unknown"),
            "exp": labels.get("experience_years", 0),
            "skills": set(labels.get("skills", [])),
        })
    return resumes


def _level_band(level):
    """Gold-resume experience years that count as a match for a JD level."""
    return {"junior": (0, 3), "mid": (1, 3), "senior": (5, 8)}[level]


def build_jds():
    random.seed(42)
    resumes = _pick_resumes()
    strong = [r for r in resumes if r["quality"] == "strong"]
    jds = []
    idx = 0
    for role_key in ROLES:
        spec = JD_SPECS[role_key]
        for level in ("junior", "mid", "senior"):
            idx += 1
            lv = LEVELS[level]
            title = spec["titles"][level]
            must = spec["must_have"][level]
            nice = spec["nice_to_have"]
            company = COMPANIES[(idx - 1) % len(COMPANIES)]
            duties = "\n".join(f"- {d}" for d in spec["duties"])
            must_lines = "\n".join(f"- {s}" for s in must)
            nice_line = ", ".join(nice)
            text = (
                f"{title}\n{company} | Bengaluru, India (Hybrid)\n\n"
                f"About the role\n{spec['about']} We are hiring a "
                f"{title.lower()} with {lv['years_text']} of experience.\n\n"
                f"What you'll do\n{duties}\n\n"
                f"What we're looking for\n"
                f"- {lv['years_text']} of relevant experience\n"
                f"- Hands-on experience with:\n{must_lines}\n"
                f"- Strong problem-solving skills and clear communication\n"
                f"- Nice to have: {nice_line}\n\n"
                f"We offer a competitive salary, health insurance, learning "
                f"budget and flexible hours."
            )
            lo, hi = _level_band(level)
            same_role = [r for r in strong
                         if r["role_key"] == role_key and lo <= r["exp"] <= hi]
            if not same_role:  # fall back to any strong resume of the role
                same_role = [r for r in strong if r["role_key"] == role_key]
            matched = random.choice(same_role)
            must_set = set(must)
            others = [r for r in strong if r["role_key"] != role_key]
            # Hardest negative: least must-have skill overlap.
            others.sort(key=lambda r: (len(r["skills"] & must_set), r["id"]))
            mismatched = others[0]
            jds.append({
                "id": f"jd_{idx:03d}",
                "role": role_key,
                "level": level,
                "title": title,
                "company": company,
                "text": text,
                "must_have_skills": must,
                "nice_to_have_skills": nice,
                "experience_years_min": lv["min_years"],
                "matched_resume_id": matched["id"],
                "mismatched_resume_id": mismatched["id"],
            })
    return jds


def main():
    jds = build_jds()
    OUT.write_text(json.dumps(jds, indent=2) + "\n")
    roles = sorted({j["role"] for j in jds})
    levels = sorted({j["level"] for j in jds})
    print(f"Wrote {len(jds)} JDs to {OUT}")
    print(f"roles: {len(roles)} ({', '.join(roles)})")
    print(f"levels: {levels}")
    print(f"must-have skills total: {sum(len(j['must_have_skills']) for j in jds)}")
    # Sanity: every JD must name a distinct matched resume of the same role.
    bad = [j["id"] for j in jds
           if j["matched_resume_id"] == j["mismatched_resume_id"]]
    assert not bad, f"matched == mismatched for {bad}"
    print("matched != mismatched for all 30 JDs: OK")


if __name__ == "__main__":
    main()
