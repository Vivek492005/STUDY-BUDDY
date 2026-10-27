"""Career-discovery assessment (Wave 1 feature #7).

A guided 28-question quiz (interests / strengths / work-style / subjects).
Deterministic, server-side scoring: every answer option carries transparent
weights per career cluster; cluster match % = earned / max-possible.

Gemini (wired in unified_app.py) writes personalized "why you fit" blurbs
when available; the rule-based explanations below are the guaranteed
fallback.
"""

CLUSTERS = {
    "swe": {
        "name": "Software Development",
        "tagline": "Build the apps and systems the world runs on.",
        "skills": ["Python or Java", "Data structures & algorithms", "Git & collaboration", "System design basics"],
        "tools": ["JD-matched ATS scoring", "AI bullet rewriter", "GitHub analyzer", "LeetCode stats"],
        "tool_links": ["/resume#jd-match", "/resume#bullet-rewriter", "/profile", "/profile"],
    },
    "data_ai": {
        "name": "Data Science & AI/ML",
        "tagline": "Turn data into predictions and intelligent products.",
        "skills": ["Python", "Statistics & probability", "Machine learning (scikit-learn)", "SQL"],
        "tools": ["JD-matched ATS scoring", "LeetCode stats", "Application tracker"],
        "tool_links": ["/resume#jd-match", "/profile", "/tracker"],
    },
    "design": {
        "name": "UI/UX Design",
        "tagline": "Design experiences people love to use.",
        "skills": ["Figma", "UX research", "Visual hierarchy", "Prototyping"],
        "tools": ["LinkedIn grader", "Cover-letter generator"],
        "tool_links": ["/resume#linkedin-grader", "/resume#cover-generator"],
    },
    "security": {
        "name": "Cybersecurity",
        "tagline": "Defend systems and hunt vulnerabilities.",
        "skills": ["Networking basics", "Linux", "OWASP Top 10", "Python scripting"],
        "tools": ["GitHub analyzer", "LeetCode stats"],
        "tool_links": ["/profile", "/profile"],
    },
    "cloud": {
        "name": "Cloud & DevOps",
        "tagline": "Ship reliably and scale infrastructure.",
        "skills": ["Linux", "Docker", "CI/CD pipelines", "AWS basics"],
        "tools": ["GitHub analyzer", "JD-matched ATS scoring"],
        "tool_links": ["/profile", "/resume#jd-match"],
    },
    "pm": {
        "name": "Product Management",
        "tagline": "Decide what gets built and why.",
        "skills": ["Analytics", "Communication", "Roadmapping", "SQL basics"],
        "tools": ["Cover-letter generator", "LinkedIn grader", "Application tracker"],
        "tool_links": ["/resume#cover-generator", "/resume#linkedin-grader", "/tracker"],
    },
    "marketing": {
        "name": "Digital Marketing & Content",
        "tagline": "Tell stories that move people to act.",
        "skills": ["Copywriting", "SEO basics", "Analytics", "Content strategy"],
        "tools": ["Cover-letter generator", "LinkedIn grader"],
        "tool_links": ["/resume#cover-generator", "/resume#linkedin-grader"],
    },
    "analytics": {
        "name": "Business & Data Analytics",
        "tagline": "Answer business questions with data.",
        "skills": ["SQL", "Spreadsheets", "Data visualization", "Statistics"],
        "tools": ["JD-matched ATS scoring", "LinkedIn grader"],
        "tool_links": ["/resume#jd-match", "/resume#linkedin-grader"],
    },
    # ---- Phase 2 clusters (no overlap with the original eight) ----
    "devops": {
        "name": "DevOps & Platform Engineering",
        "tagline": "Automate releases and keep services running.",
        "skills": ["Linux", "Docker & Kubernetes", "CI/CD pipelines", "Infrastructure as code"],
        "tools": ["GitHub analyzer", "Application tracker"],
        "tool_links": ["/profile", "/tracker"],
    },
    "mobile": {
        "name": "Mobile Development",
        "tagline": "Build the apps people carry everywhere.",
        "skills": ["Kotlin or Swift", "Flutter or React Native", "Mobile UI patterns", "App store releases"],
        "tools": ["GitHub analyzer", "Application tracker"],
        "tool_links": ["/profile", "/tracker"],
    },
    "qa_testing": {
        "name": "QA & Test Engineering",
        "tagline": "Break software on purpose so users never have to.",
        "skills": ["Test planning", "Automation (Selenium/Playwright)", "API testing", "Bug reporting"],
        "tools": ["GitHub analyzer", "Application tracker"],
        "tool_links": ["/profile", "/tracker"],
    },
    "embedded": {
        "name": "Embedded Systems & IoT",
        "tagline": "Write software that lives inside hardware.",
        "skills": ["C/C++", "Microcontrollers", "Operating systems basics", "Sensors & protocols"],
        "tools": ["GitHub analyzer", "Application tracker"],
        "tool_links": ["/profile", "/tracker"],
    },
    "game_dev": {
        "name": "Game Development",
        "tagline": "Build worlds, mechanics, and play.",
        "skills": ["C++/C#", "Unity or Unreal", "Game mechanics", "Computer graphics basics"],
        "tools": ["GitHub analyzer", "Application tracker"],
        "tool_links": ["/profile", "/tracker"],
    },
    "data_engineering": {
        "name": "Data Engineering",
        "tagline": "Build the pipelines data science runs on.",
        "skills": ["SQL", "Python", "ETL pipelines", "Data warehousing basics"],
        "tools": ["JD-matched ATS scoring", "Application tracker"],
        "tool_links": ["/resume#jd-match", "/tracker"],
    },
}

# Per-cluster handoffs for the result page. roadmap_skill / quiz_skill refer
# to real roadmap skill ids and quiz slugs; None means no matching page
# exists, so the frontend must render no link (never a dead link).
CLUSTER_HANDOFFS = {
    "swe": {"roadmap_skill": "python", "quiz_skill": "dsa"},
    "data_ai": {"roadmap_skill": "machine-learning", "quiz_skill": "machine-learning"},
    "design": {"roadmap_skill": None, "quiz_skill": None},
    "security": {"roadmap_skill": "cybersecurity-basics", "quiz_skill": "linux"},
    "cloud": {"roadmap_skill": "cloud-aws", "quiz_skill": "linux"},
    "pm": {"roadmap_skill": None, "quiz_skill": None},
    "marketing": {"roadmap_skill": None, "quiz_skill": None},
    "analytics": {"roadmap_skill": "data-analysis", "quiz_skill": "data-analysis"},
    "devops": {"roadmap_skill": "devops-cicd", "quiz_skill": "linux"},
    "mobile": {"roadmap_skill": None, "quiz_skill": None},
    "qa_testing": {"roadmap_skill": None, "quiz_skill": None},
    "embedded": {"roadmap_skill": "cpp", "quiz_skill": "cpp"},
    "game_dev": {"roadmap_skill": "cpp", "quiz_skill": "cpp"},
    "data_engineering": {"roadmap_skill": "sql", "quiz_skill": "sql"},
}

# Each option maps to {cluster_id: points (0-2)}.
QUESTIONS = [
    # ---- Interests ----
    {"id": "q1", "dimension": "interests", "text": "Which weekend project sounds most fun?",
     "options": [
         {"text": "Building an app or website", "weights": {"swe": 2, "cloud": 1}},
         {"text": "Analyzing a dataset for hidden patterns", "weights": {"data_ai": 2, "analytics": 1}},
         {"text": "Redesigning an app's interface", "weights": {"design": 2, "pm": 1}},
         {"text": "Finding vulnerabilities in a system", "weights": {"security": 2, "swe": 1}},
     ]},
    {"id": "q2", "dimension": "interests", "text": "What kind of problems do you enjoy solving?",
     "options": [
         {"text": "Logic puzzles and algorithms", "weights": {"swe": 2, "data_ai": 1}},
         {"text": "Understanding why people behave a certain way", "weights": {"pm": 2, "marketing": 1, "design": 1}},
         {"text": "Making systems faster and more reliable", "weights": {"cloud": 2, "swe": 1}},
         {"text": "Spotting risks everyone else missed", "weights": {"security": 2, "analytics": 1}},
     ]},
    {"id": "q3", "dimension": "interests", "text": "Pick a headline you'd love about your work:",
     "options": [
         {"text": "\u201cApp used by a million people\u201d", "weights": {"swe": 2, "pm": 1}},
         {"text": "\u201cModel predicts with 99% accuracy\u201d", "weights": {"data_ai": 2, "analytics": 1}},
         {"text": "\u201cDesign wins a global award\u201d", "weights": {"design": 2, "marketing": 1}},
         {"text": "\u201cZero breaches, third year running\u201d", "weights": {"security": 2, "cloud": 1}},
     ]},
    {"id": "q4", "dimension": "interests", "text": "Which content do you binge?",
     "options": [
         {"text": "Coding tutorials and build videos", "weights": {"swe": 2}},
         {"text": "Data stories and visualizations", "weights": {"analytics": 2, "data_ai": 1}},
         {"text": "Design breakdowns", "weights": {"design": 2}},
         {"text": "Startup and product teardowns", "weights": {"pm": 2, "marketing": 1}},
     ]},
    # ---- Strengths ----
    {"id": "q5", "dimension": "strengths", "text": "What are you naturally good at?",
     "options": [
         {"text": "Breaking big problems into steps", "weights": {"swe": 2, "pm": 1}},
         {"text": "Math and statistics", "weights": {"data_ai": 2, "analytics": 2}},
         {"text": "Visual taste and aesthetics", "weights": {"design": 2, "marketing": 1}},
         {"text": "Being thorough and detail-oriented", "weights": {"security": 2, "cloud": 1}},
     ]},
    {"id": "q6", "dimension": "strengths", "text": "Friends ask you for help with\u2026",
     "options": [
         {"text": "Fixing their tech issues", "weights": {"cloud": 2, "swe": 1}},
         {"text": "Making sense of numbers", "weights": {"analytics": 2, "data_ai": 1}},
         {"text": "Making things look good", "weights": {"design": 2, "marketing": 1}},
         {"text": "Planning and organizing", "weights": {"pm": 2, "security": 1}},
     ]},
    {"id": "q7", "dimension": "strengths", "text": "Your superpower in group projects:",
     "options": [
         {"text": "Writing the core logic", "weights": {"swe": 2}},
         {"text": "Doing the research and analysis", "weights": {"analytics": 2, "data_ai": 1}},
         {"text": "Designing the product or pitch", "weights": {"design": 2, "marketing": 1}},
         {"text": "Keeping everyone on track", "weights": {"pm": 2, "cloud": 1}},
     ]},
    {"id": "q8", "dimension": "strengths", "text": "Which statement fits you best?",
     "options": [
         {"text": "\u201cI think in systems and flows\u201d", "weights": {"cloud": 2, "swe": 1}},
         {"text": "\u201cI question assumptions with data\u201d", "weights": {"data_ai": 2, "analytics": 1}},
         {"text": "\u201cI notice what others overlook visually\u201d", "weights": {"design": 2}},
         {"text": "\u201cI think about what could go wrong\u201d", "weights": {"security": 2, "pm": 1}},
     ]},
    # ---- Work style ----
    {"id": "q9", "dimension": "workstyle", "text": "Your ideal workday:",
     "options": [
         {"text": "Deep focus, headphones on, building", "weights": {"swe": 2, "security": 1}},
         {"text": "A mix of analysis and discussions", "weights": {"analytics": 2, "data_ai": 1, "pm": 1}},
         {"text": "Collaborative whiteboard sessions", "weights": {"design": 2, "pm": 2}},
         {"text": "On-call, solving live incidents", "weights": {"cloud": 2, "security": 1}},
     ]},
    {"id": "q10", "dimension": "workstyle", "text": "You prefer feedback that is\u2026",
     "options": [
         {"text": "Binary: it works or it doesn't", "weights": {"swe": 2, "cloud": 1}},
         {"text": "Driven by numbers", "weights": {"data_ai": 2, "analytics": 2}},
         {"text": "Qualitative and human", "weights": {"design": 2, "marketing": 1}},
         {"text": "Strategic and big-picture", "weights": {"pm": 2, "security": 1}},
     ]},
    {"id": "q11", "dimension": "workstyle", "text": "Pick a work setting:",
     "options": [
         {"text": "Solo builder with clear specs", "weights": {"swe": 2}},
         {"text": "Small team running fast experiments", "weights": {"pm": 2, "data_ai": 1}},
         {"text": "Studio-like creative team", "weights": {"design": 2, "marketing": 2}},
         {"text": "Mission-critical ops team", "weights": {"security": 2, "cloud": 2}},
     ]},
    {"id": "q12", "dimension": "workstyle", "text": "How do you feel about ambiguity?",
     "options": [
         {"text": "Love it \u2014 I'll define the problem", "weights": {"pm": 2, "design": 1}},
         {"text": "Fine, if I have data to explore", "weights": {"data_ai": 2, "analytics": 1}},
         {"text": "Prefer clear requirements", "weights": {"swe": 2, "security": 1}},
         {"text": "Thrive in controlled chaos", "weights": {"cloud": 2, "marketing": 1}},
     ]},
    # ---- Subjects ----
    {"id": "q13", "dimension": "subjects", "text": "Favorite subject in school or college:",
     "options": [
         {"text": "Computer science / programming", "weights": {"swe": 2, "cloud": 1}},
         {"text": "Mathematics / statistics", "weights": {"data_ai": 2, "analytics": 2}},
         {"text": "Art / design", "weights": {"design": 2, "marketing": 1}},
         {"text": "Business / economics", "weights": {"pm": 2, "analytics": 1, "marketing": 1}},
     ]},
    {"id": "q14", "dimension": "subjects", "text": "Which elective would you pick?",
     "options": [
         {"text": "Advanced algorithms", "weights": {"swe": 2, "security": 1}},
         {"text": "Machine learning", "weights": {"data_ai": 2, "analytics": 1}},
         {"text": "Human-computer interaction", "weights": {"design": 2, "pm": 1}},
         {"text": "Network security", "weights": {"security": 2, "cloud": 1}},
     ]},
    {"id": "q15", "dimension": "subjects", "text": "A hackathon theme that excites you:",
     "options": [
         {"text": "Build anything in 24 hours", "weights": {"swe": 2, "cloud": 1}},
         {"text": "Predict-a-thon", "weights": {"data_ai": 2, "analytics": 1}},
         {"text": "Design jam", "weights": {"design": 2, "marketing": 1}},
         {"text": "Capture the flag", "weights": {"security": 2}},
     ]},
    {"id": "q16", "dimension": "subjects", "text": "Which book title would you pick up?",
     "options": [
         {"text": "\u201cClean Code\u201d", "weights": {"swe": 2}},
         {"text": "\u201cStorytelling with Data\u201d", "weights": {"analytics": 2, "data_ai": 1, "marketing": 1}},
         {"text": "\u201cDon't Make Me Think\u201d", "weights": {"design": 2, "pm": 1}},
         {"text": "\u201cThe Phoenix Project\u201d", "weights": {"cloud": 2, "pm": 1}},
     ]},
    # ---- Phase 2: specialist paths (every cluster keeps >= 2 questions) ----
    {"id": "q17", "dimension": "interests", "text": "Which side project would you start this month?",
     "options": [
         {"text": "A mobile app for your campus", "weights": {"mobile": 2, "swe": 1}},
         {"text": "A CI/CD pipeline for a class project", "weights": {"devops": 2, "cloud": 1}},
         {"text": "Breaking a demo app on purpose to find bugs", "weights": {"qa_testing": 2, "security": 1}},
         {"text": "A tiny game prototype", "weights": {"game_dev": 2, "design": 1}},
     ]},
    {"id": "q18", "dimension": "interests", "text": "Which system would you love to build?",
     "options": [
         {"text": "A data pipeline that cleans and moves data daily", "weights": {"data_engineering": 2, "data_ai": 1}},
         {"text": "Firmware for a small robot or sensor", "weights": {"embedded": 2, "swe": 1}},
         {"text": "A one-click deploy system for a team", "weights": {"devops": 2, "cloud": 1}},
         {"text": "An automated test suite for an app", "weights": {"qa_testing": 2, "mobile": 1}},
     ]},
    {"id": "q19", "dimension": "strengths", "text": "What kind of detail work do you enjoy?",
     "options": [
         {"text": "Debugging hardware that refuses to boot", "weights": {"embedded": 2}},
         {"text": "Finding edge cases nobody thought of", "weights": {"qa_testing": 2, "security": 1}},
         {"text": "Making builds and releases boringly reliable", "weights": {"devops": 2, "cloud": 1}},
         {"text": "Tuning game mechanics until they feel right", "weights": {"game_dev": 2, "design": 1}},
     ]},
    {"id": "q20", "dimension": "strengths", "text": "Which skill would you happily practise for a month?",
     "options": [
         {"text": "SQL and building data pipelines", "weights": {"data_engineering": 2, "analytics": 1}},
         {"text": "Polishing a mobile app's screens and flow", "weights": {"mobile": 2, "design": 1}},
         {"text": "Infrastructure as code and containers", "weights": {"devops": 2}},
         {"text": "Low-level C/C++ programming", "weights": {"embedded": 2, "swe": 1}},
     ]},
    {"id": "q21", "dimension": "workstyle", "text": "Pick a team ritual you'd look forward to:",
     "options": [
         {"text": "Release-train reviews and incident post-mortems", "weights": {"devops": 2}},
         {"text": "Testing the app on ten different phones", "weights": {"mobile": 2, "qa_testing": 1}},
         {"text": "Hardware lab days with boards and sensors", "weights": {"embedded": 2}},
         {"text": "Playtesting sessions with real players", "weights": {"game_dev": 2, "design": 1}},
     ]},
    {"id": "q22", "dimension": "workstyle", "text": "Your idea of a satisfying workday:",
     "options": [
         {"text": "Writing test plans and watching them go green", "weights": {"qa_testing": 2}},
         {"text": "A pipeline that ran clean overnight", "weights": {"data_engineering": 2}},
         {"text": "Shipping an app update users notice", "weights": {"mobile": 2}},
         {"text": "A game level that finally plays well", "weights": {"game_dev": 2, "swe": 1}},
     ]},
    {"id": "q23", "dimension": "subjects", "text": "Which elective would you pick next semester?",
     "options": [
         {"text": "Operating systems", "weights": {"embedded": 2, "devops": 1}},
         {"text": "Databases and data engineering", "weights": {"data_engineering": 2, "analytics": 1}},
         {"text": "Mobile application development", "weights": {"mobile": 2}},
         {"text": "Game design and development", "weights": {"game_dev": 2}},
     ]},
    {"id": "q24", "dimension": "subjects", "text": "A workshop you'd sign up for this weekend:",
     "options": [
         {"text": "Robotics and IoT with microcontrollers", "weights": {"embedded": 2}},
         {"text": "Software testing and automation", "weights": {"qa_testing": 2}},
         {"text": "Cloud infrastructure and deployment", "weights": {"devops": 2, "cloud": 1}},
         {"text": "Computer graphics and game engines", "weights": {"game_dev": 2, "design": 1}},
     ]},
    {"id": "q25", "dimension": "interests", "text": "Which rabbit hole would you fall into?",
     "options": [
         {"text": "Game devlogs and how games are made", "weights": {"game_dev": 2}},
         {"text": "Homelab and self-hosting projects", "weights": {"devops": 2, "cloud": 1}},
         {"text": "App teardowns: why great apps feel great", "weights": {"mobile": 2, "design": 1}},
         {"text": "Data engineering blogs and pipeline design", "weights": {"data_engineering": 2}},
     ]},
    {"id": "q26", "dimension": "strengths", "text": "What patience do you have that others don't?",
     "options": [
         {"text": "Re-running the same checks until everything passes", "weights": {"qa_testing": 2}},
         {"text": "Thinking across hardware and software together", "weights": {"embedded": 2}},
         {"text": "Keeping services up while everyone else sleeps", "weights": {"devops": 2}},
         {"text": "Telling a story through play, not words", "weights": {"game_dev": 2, "marketing": 1}},
     ]},
    {"id": "q27", "dimension": "workstyle", "text": "Which launch day excites you most?",
     "options": [
         {"text": "Your app hitting the Play Store / App Store", "weights": {"mobile": 2}},
         {"text": "A deploy pipeline going green on the first try", "weights": {"devops": 2, "cloud": 1}},
         {"text": "A device you programmed booting up perfectly", "weights": {"embedded": 2}},
         {"text": "Players loving a level you designed", "weights": {"game_dev": 2}},
     ]},
    {"id": "q28", "dimension": "subjects", "text": "Your dream internship team:",
     "options": [
         {"text": "A mobile product team", "weights": {"mobile": 2}},
         {"text": "A platform / infrastructure team", "weights": {"devops": 2}},
         {"text": "A data platform team", "weights": {"data_engineering": 2, "data_ai": 1}},
         {"text": "A QA and automation team", "weights": {"qa_testing": 2}},
     ]},
]

QUESTION_IDS = [q["id"] for q in QUESTIONS]
TOP_N_CLUSTERS = 5


def _max_scores():
    """Max possible points per cluster across all questions."""
    maxima = {cid: 0 for cid in CLUSTERS}
    for q in QUESTIONS:
        best = {}
        for opt in q["options"]:
            for cid, pts in opt["weights"].items():
                best[cid] = max(best.get(cid, 0), pts)
        for cid, pts in best.items():
            maxima[cid] = maxima.get(cid, 0) + pts
    return maxima


MAX_SCORES = _max_scores()


def score_assessment(answers):
    """Score {question_id: option_index} answers. Returns ranked cluster results.

    Each result: {id, name, tagline, match (0-100), earned, possible,
    top_signals: [question texts where this cluster won points]}.
    """
    earned = {cid: 0 for cid in CLUSTERS}
    signals = {cid: [] for cid in CLUSTERS}
    for q in QUESTIONS:
        idx = answers.get(q["id"])
        if not isinstance(idx, int) or not 0 <= idx < len(q["options"]):
            continue
        opt = q["options"][idx]
        for cid, pts in opt["weights"].items():
            if pts > 0:
                earned[cid] = earned.get(cid, 0) + pts
                signals[cid].append(q["text"])
    results = []
    for cid, meta in CLUSTERS.items():
        possible = MAX_SCORES.get(cid, 1) or 1
        match = round(100.0 * earned.get(cid, 0) / possible, 1)
        results.append({
            "id": cid,
            "name": meta["name"],
            "tagline": meta["tagline"],
            "match": match,
            "earned": earned.get(cid, 0),
            "possible": possible,
            "skills": meta["skills"],
            "tools": meta["tools"],
            "tool_links": meta["tool_links"],
            "top_signals": signals[cid][:3],
            "why": "",  # filled by rule-based fallback or Gemini
        })
    results.sort(key=lambda r: (-r["match"], r["name"]))
    return results[:TOP_N_CLUSTERS]


def rule_based_why(cluster):
    """Deterministic 'why you fit' blurb from the user's own answers."""
    name = cluster["name"]
    signals = cluster.get("top_signals") or []
    if signals:
        quoted = "; ".join(f"you leaned toward building/creating in \u201c{s}\u201d" for s in signals[:2])
        return (f"Your answers point to {name}: {quoted} — a pattern that matches how {name}s "
                "think and work.")
    return (f"Your overall answer pattern aligns best with {name} — explore its "
            "starter skills below to confirm the fit.")


def validate_assessment_inputs(payload):
    """Validate submitted answers. Returns (answers, error)."""
    raw = payload.get("answers")
    if not isinstance(raw, dict):
        return None, "Please answer all questions before submitting."
    answers = {}
    for q in QUESTIONS:
        if q["id"] not in raw:
            return None, "Please answer every question (missing: {}).".format(q["id"])
        try:
            idx = int(raw[q["id"]])
        except (TypeError, ValueError):
            return None, "Invalid answer for question {}.".format(q["id"])
        if not 0 <= idx < len(q["options"]):
            return None, "Invalid answer for question {}.".format(q["id"])
        answers[q["id"]] = idx
    return answers, None


def public_questions():
    """Questions safe to embed in the page (no scoring internals beyond weights)."""
    return [
        {"id": q["id"], "dimension": q["dimension"], "text": q["text"],
         "options": [o["text"] for o in q["options"]]}
        for q in QUESTIONS
    ]


# ================= Feature 7 Ultimate Enhancements =================
# 7.1 AI-role fit track | 7.2 Roadmap export | 7.3 Re-take delta

import re as _re2


def _clean2(text):
    return _re2.sub(r"<[^>]*>", "", text or "")


# 2026's new AI-native roles with the skills they demand.
_AI_ROLES = [
    {"id": "genai_engineer", "name": "GenAI Engineer",
     "tagline": "Ship production LLM features: RAG, agents, evals.",
     "skills": ["python", "llm", "rag", "langchain", "prompt engineering",
                "api", "vector db", "evaluation"],
     "demand": "Very high — 234% YoY growth in GenAI skill demand (2026)."},
    {"id": "ai_pm", "name": "AI Product Manager",
     "tagline": "Own AI features end-to-end: problem, prompt, metrics.",
     "skills": ["product thinking", "llm", "analytics", "communication",
                "sql", "user research", "prompt engineering"],
     "demand": "High — every SaaS company is hiring AI PMs."},
    {"id": "ai_ops", "name": "AI Operations / LLMOps",
     "tagline": "Keep AI systems reliable: monitoring, cost, safety.",
     "skills": ["python", "monitoring", "cloud", "evaluation", "api",
                "data pipeline", "cost optimization"],
     "demand": "Growing — production AI needs operators."},
    {"id": "ai_workflow", "name": "AI Workflow Engineer",
     "tagline": "Automate business processes with AI agents.",
     "skills": ["automation", "api", "prompt engineering", "python",
                "no-code tools", "process design"],
     "demand": "High — non-tech teams are the biggest buyers."},
    {"id": "ai_governance", "name": "AI Governance Specialist",
     "tagline": "Make AI safe and compliant: policy, audits, risk.",
     "skills": ["communication", "policy", "risk assessment", "analytics",
                "documentation", "ethics"],
     "demand": "Emerging — regulation is creating this role."},
]


def ai_role_fit(user_skills):
    """Score the user against 2026's AI-native roles.

    user_skills: list of skill name strings. Returns roles ranked by fit
    with {name, tagline, fit (0-100), matched, missing, demand}.
    """
    if not isinstance(user_skills, (list, tuple)):
        raise ValueError("user_skills must be a list of skill names.")
    mine = set()
    for s in user_skills:
        if isinstance(s, str):
            cleaned = _clean2(s).strip().lower()
            if cleaned:
                mine.add(cleaned)
    out = []
    for role in _AI_ROLES:
        matched = [sk for sk in role["skills"] if sk in mine]
        # Fuzzy: partial word overlap counts half.
        fuzzy = [sk for sk in role["skills"]
                 if sk not in matched
                 and any(w in mine or mine and any(w in m for m in mine)
                         for w in sk.split())]
        fit = round((len(matched) + 0.5 * len(fuzzy)) / len(role["skills"]) * 100, 1)
        missing = [sk for sk in role["skills"]
                   if sk not in matched and sk not in fuzzy][:5]
        out.append({"id": role["id"], "name": role["name"],
                    "tagline": role["tagline"], "fit": min(100.0, fit),
                    "matched": matched, "missing": missing,
                    "demand": role["demand"]})
    out.sort(key=lambda r: -r["fit"])
    return {"roles": out, "best_bet": out[0]["name"] if out else None,
            "guidance": "Pick ONE role. Close its top-3 missing skills in 90 days."}


def roadmap_export(cluster, weeks=26):
    """Turn an assessment result into a concrete weekly plan.

    cluster: dict with {name, skills (list)}. Returns {weeks: [{week, focus,
    tasks}], total_weeks}. Pure function — no AI needed.
    """
    if not isinstance(cluster, dict):
        raise ValueError("cluster must be an assessment result dict.")
    name = _clean2(cluster.get("name") or "your target role").strip()[:80] or "your target role"
    skills = [_clean2(s).strip() for s in (cluster.get("skills") or [])
              if isinstance(s, str) and _clean2(s).strip()][:10]
    if not skills:
        raise ValueError("The cluster has no skills to build a plan from.")
    try:
        weeks = int(weeks)
    except (TypeError, ValueError):
        weeks = 26
    weeks = max(4, min(52, weeks))

    phases = ["Foundations", "Build", "Ship", "Polish"]
    plan = []
    for w in range(1, weeks + 1):
        phase = phases[min(3, (w - 1) * 4 // weeks)]
        skill = skills[(w - 1) % len(skills)]
        if phase == "Foundations":
            tasks = [f"Learn {skill} fundamentals (3 hrs)", "Do 5 practice problems",
                     "Write notes in your own words"]
        elif phase == "Build":
            tasks = [f"Build a small project using {skill}", "Push to GitHub with README",
                     "Get feedback (peer or mentor)"]
        elif phase == "Ship":
            tasks = [f"Ship a portfolio-worthy {skill} project", "Write a LinkedIn post about it",
                     "Add it to your resume"]
        else:
            tasks = [f"Mock interview / quiz on {skill}", "Fill gaps from feedback",
                     "Apply to 5 roles using this skill"]
        plan.append({"week": w, "phase": phase, "focus": skill, "tasks": tasks})
    return {"role": name, "total_weeks": weeks, "weeks": plan,
            "milestones": [f"Week {weeks//4}: foundations done",
                           f"Week {weeks//2}: first shipped project",
                           f"Week {3*weeks//4}: portfolio complete",
                           f"Week {weeks}: interview-ready"]}


def assessment_delta(old_results, new_results):
    """Compare two assessment runs: where did you grow?

    Each results list: [{id, name, match}]. Returns {grew, shrank, stable,
    top_mover, summary}.
    """
    def _norm(results):
        out = {}
        for r in (results or []):
            if isinstance(r, dict) and r.get("id"):
                try:
                    out[r["id"]] = (r.get("name") or r["id"], float(r.get("match") or 0))
                except (TypeError, ValueError):
                    continue
        return out
    old, new = _norm(old_results), _norm(new_results)
    if not old or not new:
        raise ValueError("Both old and new results are needed for a delta.")
    grew, shrank, stable = [], [], []
    for cid, (name, nmatch) in new.items():
        if cid in old:
            delta = round(nmatch - old[cid][1], 1)
            entry = {"id": cid, "name": name, "old": old[cid][1],
                     "new": nmatch, "delta": delta}
            if delta >= 5:
                grew.append(entry)
            elif delta <= -5:
                shrank.append(entry)
            else:
                stable.append(entry)
    grew.sort(key=lambda x: -x["delta"])
    shrank.sort(key=lambda x: x["delta"])
    top = grew[0] if grew else None
    summary = ("You have grown most in {}.".format(top["name"]) if top else
               "No big movers — steady profile.")
    if shrank:
        summary += " Watch {} (down {} pts).".format(shrank[0]["name"], -shrank[0]["delta"])
    new_ids = [cid for cid in new if cid not in old]
    return {"grew": grew, "shrank": shrank, "stable": stable,
            "new_clusters": new_ids, "top_mover": top, "summary": summary}
