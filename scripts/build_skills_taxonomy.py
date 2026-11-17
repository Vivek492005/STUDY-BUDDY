"""ENG-06: Build data/taxonomy/skills_taxonomy.json from data/skills_db.csv.

Enriches every CSV skill with:
  - auto-generated alias variants (dot/space/hyphen/compact forms)
  - curated extra aliases for high-traffic skills
  - parent skill (hierarchy) and related skills for key skills

Reproducible: re-run to regenerate the JSON.
"""
import csv
import json
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(REPO, "data", "skills_db.csv")
OUT_PATH = os.path.join(REPO, "data", "taxonomy", "skills_taxonomy.json")

# Pre-existing alias conflict(s) the suite tolerates (first writer wins).
# Any other duplicate is a build failure — see build().
KNOWN_DUPES = {"budgeting"}

# ---------------------------------------------------------------------------
# Curated enrichment for high-traffic / Indian-market skills.
# name -> {"aliases": [...], "parent": "...", "related": [...]}
# ---------------------------------------------------------------------------
# Skills missing from the CSV that we add outright (Indian market + modern stack).
# Each: (name, category, aliases, parent, related)
NEW_SKILLS = [
    ("Redux", "Frontend Framework", ["redux", "redux toolkit", "rtk"],
     "React", ["React", "Zustand", "TypeScript"]),
    ("Postman", "API", ["postman", "postman api"],
     None, ["REST APIs", "Swagger", "API Testing"]),
    ("Tally", "Finance", ["tally", "tally erp", "tally prime", "tallyprime"],
     None, ["Accounting", "GST", "Excel"]),
    ("Data Analysis", "Data Science",
     ["data analysis", "data analytics", "eda", "exploratory data analysis"],
     None, ["Pandas", "SQL", "Power BI", "Tableau"]),
    (".NET", "Backend Framework",
     [".net", "dotnet", "dot net", ".net core", "dotnet core"],
     None, ["C#", "ASP.NET", "Azure"]),
    ("Zustand", "Frontend Framework", ["zustand"],
     "React", ["Redux", "React"]),
]

ENRICH = {
    "React": {
        "aliases": ["reactjs", "react.js", "react js", "react-js"],
        "parent": "JavaScript",
        "related": ["Next.js", "Redux", "TypeScript", "JavaScript"],
    },
    "Next.js": {
        "aliases": ["nextjs", "next js", "next"],
        "parent": "React",
        "related": ["React", "TypeScript", "Vercel"],
    },
    "Angular": {
        "aliases": ["angularjs", "angular.js", "angular js"],
        "parent": "TypeScript",
        "related": ["TypeScript", "RxJS", "React", "Vue.js"],
    },
    "Vue": {
        "aliases": ["vuejs", "vue js", "vue", "vue3", "vue 3"],
        "parent": "JavaScript",
        "related": ["Nuxt.js", "React", "Angular"],
    },
    "JavaScript": {
        # ECMAScript edition names are aliases of JavaScript itself
        # (pinned by test_javascript_ecma_editions); the 2026-11-16
        # rebuild dropped es2016+ because they lived only in the JSON.
        "aliases": ["js", "ecmascript", "es6", "es2015", "es2016", "es2017",
                    "es2018", "es2019", "es2020", "es2021", "es2022",
                    "es2023", "es2024", "vanilla js", "vanilla javascript"],
        "related": ["TypeScript", "Node.js", "React"],
    },
    "TypeScript": {
        "aliases": ["ts", "typescriptlang"],
        "parent": "JavaScript",
        "related": ["JavaScript", "Angular", "React"],
    },
    "Node.js": {
        "aliases": ["nodejs", "node js", "node"],
        "parent": "JavaScript",
        "related": ["Express.js", "JavaScript", "TypeScript"],
    },
    "Express": {
        "aliases": ["expressjs", "express js", "express"],
        "parent": "Node.js",
        "related": ["Node.js", "NestJS", "Fastify"],
    },
    "HTML": {"aliases": ["html5", "hypertext markup language"],
             "related": ["CSS", "JavaScript"]},
    "CSS": {"aliases": ["css3", "cascading style sheets"],
            "related": ["HTML", "SASS", "Tailwind CSS"]},
    "Tailwind CSS": {"aliases": ["tailwindcss", "tailwind"],
                     "parent": "CSS", "related": ["CSS", "Bootstrap"]},
    "Bootstrap": {"aliases": ["bootstrap 5", "bootstrap5"],
                  "parent": "CSS", "related": ["Tailwind CSS", "CSS"]},
    "SASS": {"aliases": ["sass", "scss"], "parent": "CSS",
             "related": ["CSS", "LESS"]},
    "jQuery": {"aliases": ["jquery"], "parent": "JavaScript",
               "related": ["JavaScript", "AJAX"]},
    "Python": {
        "aliases": ["python3", "py", "cpython"],
        "related": ["Django", "Flask", "pandas", "NumPy"],
    },
    "Django": {"aliases": ["django rest framework", "drf", "django-rest-framework"],
               "parent": "Python", "related": ["Flask", "FastAPI", "Python"]},
    "Flask": {"aliases": ["flask api"], "parent": "Python",
              "related": ["Django", "FastAPI", "Python"]},
    "FastAPI": {"aliases": ["fast api"], "parent": "Python",
                "related": ["Django", "Flask", "Python"]},
    "Java": {"aliases": ["jdk", "jre", "java 8", "java 11", "java 17"],
             "related": ["Spring", "Kotlin", "Maven"]},
    "Spring": {"aliases": ["spring boot", "springboot", "spring framework", "spring mvc"],
               "parent": "Java", "related": ["Java", "Hibernate", "Microservices"]},
    "Kotlin": {"aliases": ["kotlinlang"], "parent": "Java",
               "related": ["Java", "Android Development"]},
    "C": {"aliases": ["c programming", "c language", "ansi c"],
          "related": ["C++", "Embedded Systems"]},
    "C++": {"aliases": ["cpp", "cplusplus", "c plus plus"], "parent": "C",
            "related": ["C", "DSA", "STL"]},
    "C#": {"aliases": ["csharp", "c sharp", "c-sharp"], "parent": ".NET",
           "related": [".NET", "ASP.NET"]},
    ".NET": {"aliases": ["dotnet", "dot net", ".net core", "dotnet core", "asp.net core"],
             "related": ["C#", "ASP.NET", "Azure"]},
    "Go": {"aliases": ["golang", "go lang"], "related": ["Rust", "Docker", "Kubernetes"]},
    "Rust": {"aliases": ["rustlang", "rust lang"], "related": ["Go", "C++", "Systems Programming"]},
    "PHP": {"aliases": ["php7", "php8"], "related": ["Laravel", "WordPress", "MySQL"]},
    "Laravel": {"aliases": ["laravel php"], "parent": "PHP", "related": ["PHP", "Symfony"]},
    "Ruby": {"aliases": ["ruby lang", "ruby programming"], "related": ["Ruby on Rails"]},
    "Ruby on Rails": {"aliases": ["ror", "rails"], "parent": "Ruby",
                      "related": ["Ruby", "Django", "Laravel"]},
    "Swift": {"aliases": ["swiftlang"], "related": ["iOS Development", "SwiftUI"]},
    "Dart": {"aliases": ["dartlang"], "related": ["Flutter"]},
    "Flutter": {"aliases": ["flutter sdk", "flutter framework"], "parent": "Dart",
                "related": ["Dart", "React Native", "Android Development"]},
    "React Native": {"aliases": ["reactnative", "react-native", "rn"],
                     "parent": "React",
                     "related": ["React", "Flutter", "Mobile Development"]},
    "SQL": {"aliases": ["structured query language", "sql queries", "t-sql", "pl/sql", "plsql"],
            "related": ["MySQL", "PostgreSQL", "Database Design"]},
    "MySQL": {"aliases": ["my sql"], "parent": "SQL", "related": ["PostgreSQL", "SQL", "PHP"]},
    "PostgreSQL": {"aliases": ["postgres", "postgre sql", "psql"], "parent": "SQL",
                   "related": ["MySQL", "SQL", "Supabase"]},
    "MongoDB": {"aliases": ["mongo", "mongodb atlas"], "related": ["Mongoose", "NoSQL", "Node.js"]},
    "Redis": {"aliases": ["redis cache"], "related": ["Caching", "Memcached"]},
    "Machine Learning": {
        "aliases": ["ml", "machine-learning", "applied ml"],
        "related": ["Deep Learning", "Python", "TensorFlow", "PyTorch"],
    },
    "Deep Learning": {"aliases": ["dl", "deep-learning", "dnn"],
                      "parent": "Machine Learning",
                      "related": ["Neural Networks", "TensorFlow", "PyTorch"]},
    "TensorFlow": {"aliases": ["tf", "tensor flow"], "parent": "Python",
                   "related": ["PyTorch", "Keras", "Deep Learning"]},
    "PyTorch": {"aliases": ["torch", "pytorch lightning"], "parent": "Python",
                "related": ["TensorFlow", "Deep Learning"]},
    "Scikit-learn": {"aliases": ["sklearn", "scikit learn"], "parent": "Python",
                     "related": ["pandas", "Machine Learning"]},
    "Pandas": {"aliases": ["panda", "pd"], "parent": "Python", "related": ["NumPy", "Data Analysis"]},
    "NumPy": {"aliases": ["numpy", "np"], "parent": "Python", "related": ["pandas", "SciPy"]},
    "NLP": {"aliases": ["natural language processing", "nlp", "llm", "large language models"],
            "parent": "Machine Learning",
            "related": ["Deep Learning", "Hugging Face", "GenAI"]},
    "Computer Vision": {"aliases": ["cv", "image processing", "opencv"],
                        "parent": "Deep Learning", "related": ["OpenCV", "YOLO"]},
    "Power BI": {"aliases": ["powerbi", "power bi desktop", "dax"], "related": ["Tableau", "Excel", "SQL"]},
    "Tableau": {"aliases": ["tableau desktop"], "related": ["Power BI", "Data Visualization"]},
    "Excel": {"aliases": ["microsoft excel", "ms excel", "spreadsheets", "vlookup", "pivot tables"],
              "related": ["Google Sheets", "Power BI"]},
    "AWS": {"aliases": ["amazon web services", "amazon aws", "ec2", "s3", "lambda"],
            "related": ["Azure", "GCP", "DevOps"]},
    "Azure": {"aliases": ["microsoft azure"], "related": ["AWS", "GCP", ".NET"]},
    "GCP": {"aliases": ["google cloud", "google cloud platform"], "related": ["AWS", "Azure"]},
    "Docker": {"aliases": ["docker containers", "dockerfile", "docker compose"],
               "related": ["Kubernetes", "DevOps", "CI/CD"]},
    "Kubernetes": {"aliases": ["k8s", "kube"], "parent": "Docker",
                   "related": ["Docker", "Helm", "DevOps"]},
    "Git": {"aliases": ["git version control"], "related": ["GitHub", "GitLab"]},
    "GitHub": {"aliases": [], "parent": "Git", "related": ["Git", "CI/CD"]},
    "CI/CD": {"aliases": ["cicd", "ci cd", "continuous integration", "continuous deployment"],
              "related": ["Jenkins", "GitHub Actions", "DevOps"]},
    "Jenkins": {"aliases": ["jenkins pipeline"], "related": ["CI/CD", "DevOps"]},
    "Terraform": {"aliases": ["iac", "infrastructure as code"], "related": ["AWS", "DevOps", "Ansible"]},
    "Linux": {"aliases": ["ubuntu", "debian", "unix"],
              "related": ["DevOps", "Bash"]},
    "Figma": {"aliases": ["figma design"], "related": ["UI/UX", "Adobe XD"]},
    "JIRA": {"aliases": ["jira software", "atlassian jira"], "related": ["Agile", "Scrum"]},
    "SAP": {"aliases": ["sap erp", "sap hana"], "related": ["ERP", "Oracle"]},
    "Data Structures": {"aliases": ["data structure", "dsa", "ds"],
                        "related": ["Algorithms", "LeetCode", "Competitive Programming"]},
    "Algorithms": {"aliases": ["algorithm", "algo"],
                   "related": ["Data Structures", "Dynamic Programming", "LeetCode"]},
    "REST APIs": {"aliases": ["rest", "restful", "restful api", "rest apis"],
                 "related": ["Postman", "GraphQL", "API Design"]},
    "GraphQL": {"aliases": ["graphql", "graph ql", "apollo"], "related": ["REST API", "Apollo"]},
    "Firebase": {"aliases": ["firestore", "firebase auth"], "related": ["Supabase", "Backend"]},
    "Selenium": {"aliases": ["selenium webdriver"], "related": ["Testing", "Cypress", "Playwright"]},
    "Agile": {"aliases": ["agile methodology", "sprint"],
              "related": ["Scrum", "Jira"]},
    "UI/UX": {"aliases": ["ui ux", "ui/ux design", "ux design", "user experience", "user interface"],
              "related": ["Figma", "Wireframing"]},
}


def _auto_variants(name):
    """Generate dot/space/hyphen/compact spelling variants of a skill name."""
    variants = set()
    low = name.lower().strip()
    variants.add(low)
    # "Node.js" -> "nodejs", "node js"
    no_dots = low.replace(".", "")
    variants.add(no_dots)
    variants.add(low.replace(".", " ").strip())
    variants.add(re.sub(r"\s+", " ", low.replace(".", " ")).strip())
    # "React Native" -> "reactnative"; "Machine Learning" -> "machinelearning"
    compact = re.sub(r"[\s.\-]+", "", low)
    variants.add(compact)
    # hyphen forms
    variants.add(re.sub(r"\s+", "-", low))
    # drop trailing version-y words
    variants.add(re.sub(r"\s+(development|programming|framework|design)$", "", low).strip())
    return {v for v in variants if v and len(v) >= 1}


def build():
    skills = []
    with open(CSV_PATH, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = row["skill"].strip()
            category = row["category"].strip()
            aliases = {a.strip().lower() for a in row.get("aliases", "").split("|") if a.strip()}
            aliases |= _auto_variants(name)

            entry = {"name": name, "category": category,
                     "aliases": sorted(aliases)}
            extra = ENRICH.get(name)
            if extra:
                entry["aliases"] = sorted(aliases | {a.lower() for a in extra.get("aliases", [])})
                if extra.get("parent"):
                    entry["parent"] = extra["parent"]
                if extra.get("related"):
                    entry["related"] = extra["related"]
            skills.append(entry)

    # Add skills missing from the CSV entirely
    for name, category, aliases, parent, related in NEW_SKILLS:
        entry = {"name": name, "category": category,
                 "aliases": sorted({a.lower() for a in aliases} | _auto_variants(name))}
        if parent:
            entry["parent"] = parent
        if related:
            entry["related"] = related
        skills.append(entry)

    # sanity: alias -> skill must be unique (first writer wins, deterministic).
    # A new duplicate is a BUILD FAILURE: the 2026-11-16 refresh shipped
    # library names (django, flask, ...) as Python aliases and broke
    # test_aliases_unique. Only KNOWN_DUPES pass through with a warning.
    seen = {}
    dupes = []
    for s in skills:
        for a in s["aliases"]:
            if a in seen and seen[a] != s["name"]:
                dupes.append((a, seen[a], s["name"]))
            else:
                seen.setdefault(a, s["name"])
    bad_dupes = [d for d in dupes if d[0] not in KNOWN_DUPES]
    if dupes:
        print(f"WARNING: {len(dupes)} duplicate aliases (first wins):")
        for a, first, second in dupes[:10]:
            print(f"  '{a}': {first} (kept) vs {second} (dropped)")
    if bad_dupes:
        print(f"ERROR: {len(bad_dupes)} new duplicate aliases — "
              "fix the CSV/ENRICH source.")
        raise SystemExit(1)

    taxonomy = {
        "version": "1.0.0",
        "source": "Curated in-house by the STUDY-BUDDY team from data/skills_db.csv "
                  "with hand-written alias/hierarchy enrichment. See docs/data/ATTRIBUTION.md.",
        "skill_count": len(skills),
        "skills": skills,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(taxonomy, f, indent=2, ensure_ascii=False)
    print(f"Wrote {OUT_PATH}: {len(skills)} skills, {len(seen)} unique aliases")


if __name__ == "__main__":
    build()
