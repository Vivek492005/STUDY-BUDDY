<div align="center">

<img src="static/img/study-buddy-logo.png" alt="STUDY-BUDDY logo" width="180"/>

# STUDY-BUDDY 🦉

**The AI-powered career & research intelligence platform for students — from resume to offer letter, and from paper to publication.**

Free & open-source: ATS resume scoring · JD matching · mock interviews · aptitude tests · DSA practice · research-paper Q&A

[![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.x-black?logo=flask)](https://flask.palletsprojects.com/)
[![Live Demo](https://img.shields.io/badge/Live-Demo-brightgreen?logo=render)](https://study-buddy-3m16.onrender.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/Vivek492005/STUDY-BUDDY/actions/workflows/ci.yml/badge.svg)](https://github.com/Vivek492005/STUDY-BUDDY/actions/workflows/ci.yml)

[Live Demo](https://study-buddy-3m16.onrender.com) · [▶ Try it — no signup](https://study-buddy-3m16.onrender.com/try) · [Features](#-features) · [Setup](#-installation--setup) · [Contributing](#-contributing)

</div>

---

<div align="center">
  <img src="docs/assets/demo.gif" alt="STUDY-BUDDY demo — uploading a resume on the no-signup try page and getting a full readiness report" width="880">
  <br>
  <em>The no-signup demo: upload a resume, get a full readiness report in seconds.</em>
</div>

---

## 📖 Overview

**STUDY-BUDDY** is an all-in-one platform that helps students navigate their entire career journey: discovering the right career path, building a standout resume, preparing for interviews, finding opportunities, learning new skills, conducting research, and staying motivated — all in one place.

Built as a Flask monolith, it pairs **Google Gemini** (with automatic fallback to Groq and OpenRouter) with **classic retrieval (TF-IDF/BM25)** and large curated content banks (hundreds of interview questions, aptitude drills, DSA problems, and learning resources) so students get AI-powered guidance backed by real substance — not empty chat. Semantic embeddings via sentence-transformers are supported where installed; production currently uses the keyword/retrieval path.

### Why STUDY-BUDDY?

- **One profile, every tool** — resume, applications, practice scores, learning progress and research library all connected
- **Evidence over hype** — research answers carry citations, rejection advice is labeled as hypothesis, health scores show their data
- **Humane by design** — streaks with forgiveness shields, opt-out gamification, no pay-to-win, no dark patterns
- **Free & student-first** — every core feature works without payment
- **Honest by default** — scores show the evidence behind them, synthetic benchmarks are labeled synthetic, and no fake users, resumes, or traction are ever presented as real

### 🆕 What's new (October 2026)

- **v1.0.0 released** (19 October 2026) — the platform is publicly launched; the sections below describe what is live today.
- **Resume scorer rebuilt** (25 October 2026) — the old bucketed score saturated, so different strong resumes landed on identical totals. Scoring is now evidence-based and continuous: section completeness, skill coverage, distinct action verbs, quantified achievements (percentages, money, scale — actually detected now), bullet strength, contact completeness, experience years with **education dates excluded**, and formatting consistency. Every component names its evidence in the breakdown. It remains a **heuristic estimate, not a real ATS prediction** — real applicant-tracking systems do not publish 0–100 scores.
- **Content banks grown** — interviews **1,455 questions / 46 companies**, quizzes **660 / 22 skills**, aptitude **815**, Striver A2Z **495 problems**, certifications **110**, roadmaps **30 skills / 20 roles / 439 resources**.
- **Landing numbers can't go stale** — homepage stats are now computed live from the content banks instead of hard-coded.
- **Security hardened** — Content-Security-Policy is enforced (nonce-based scripts, no third-party script CDNs), and all 91 Ultimate Lab tools ship guided forms instead of raw-JSON-only input.
- **Research SRS** — flashcards use SM-2-lite spaced repetition with a due queue; the PDF Study Library listing bug is fixed.

---

## 🚀 Live Demo

👉 **https://study-buddy-3m16.onrender.com**

> Hosted on Render's free tier — the first load may take ~50 seconds while the service wakes up (cold start). Open the link once a minute before sharing it.

**No signup?** Try the instant resume demo: **[study-buddy-3m16.onrender.com/try](https://study-buddy-3m16.onrender.com/try)** — upload a PDF/DOCX resume, get a full readiness report, nothing stored.

---

## ⚡ Quick Start

**macOS / Linux:**

```bash
git clone https://github.com/Vivek492005/STUDY-BUDDY.git
cd STUDY-BUDDY
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # Python 3.12 required (see render.yaml's PYTHON_VERSION pin)
cp .env.example .env              # then fill in your keys (never commit .env)
python unified_app.py        # → http://localhost:5000
```

**Windows (PowerShell):**

```powershell
git clone https://github.com/Vivek492005/STUDY-BUDDY.git
cd STUDY-BUDDY
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt   # Python 3.12 required (see render.yaml's PYTHON_VERSION pin)
Copy-Item .env.example .env       # then fill in your keys (never commit .env)
python unified_app.py        # → http://localhost:5000
```

> If PowerShell refuses to run the activation script, allow local
> scripts once with `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`,
> then re-run `.venv\Scripts\Activate.ps1`.

Core features work with no API keys. Full configuration (env vars, production, deployment) is in [Installation & Setup](#-installation--setup).

---

## ✨ Features

### 📄 Resume & Career Intelligence

| Feature | What it does |
|---|---|
| AI Resume Analysis | Upload your resume for evidence-based ATS-style scoring (heuristic, not a real ATS prediction) with section-by-section feedback, bullet-level evidence, and a fix checklist with a live projected score |
| JD-Matched ATS Scoring | Paste a job description to see exactly how well your resume matches it, with gap analysis |
| Bullet Impact Rewriter | Rewrite weak resume bullets into quantified, impact-driven statements (AI-polished alternatives) |
| Cover Letter Generator | Generate tailored cover letters from your resume + the target job posting |
| LinkedIn Profile Grader | Grade your LinkedIn profile with actionable fixes for headline, about, and experience sections |
| GitHub / LeetCode Integration | Pull your real coding profile data into your career dashboard |
| Portfolio Builder | Build and publish a personal portfolio site from your profile data |
| Career Discovery Assessment | Answer a guided assessment to discover career clusters that fit your interests and strengths |
| Application Tracker | Kanban pipeline for every application — wishlist → applied → screening → interview → offer / rejected — linked to the exact resume version used |

### 🎯 Interview & Placement Prep

| Feature | What it does |
|---|---|
| Mock Interviews | **1,455-question** bank across **46 companies** — role- and company-specific mocks with rubric-scored feedback, weak-area re-drills, search, and filters |
| Communication Coach | Delivery coaching: pace, filler words (104-entry filler library), clarity, structure, and confidence signals |
| DSA Practice | **495 Striver A2Z problems** with a progress tracker, 64 in-app lessons, a hint ladder, worked reflections, and a mastery map |
| Aptitude Tests | **815 questions** in timed test mode — quantitative, logical, and verbal reasoning — plus Learn mode, company mocks, and a Mistake Book |
| STAR Story Builder | Build behavioral interview stories with **44 examples** across **11 competencies** |

### 📚 Learning Hub

| Feature | What it does |
|---|---|
| Skill Roadmaps | **30 skills and 20 roles** with **439 free resources** — courses, docs, and practice — plus a skill-gap form and a stage-jump rail |
| Skill Quizzes | **660 quiz questions** across **22 skills** to test and certify your knowledge |
| Certification Guides | **110 certifications** with guidance on what to take, when, and how to prepare (including free options, marked Free) |

### 🔬 Research Intelligence

| Feature | What it does |
|---|---|
| Source-Grounded PDF Q&A | Ask questions across your PDFs — every answer points back to supporting passages with page/chunk provenance; honest abstention when evidence is absent |
| arXiv / DOI Import | Import papers directly from arXiv or DOI with metadata, plus an interactive citation graph |
| Citation Formatter | One-click citations in **IEEE, APA 7th, MLA 9th, Vancouver, Chicago**, and **BibTeX** export |
| Literature Review Generator | Synthesize multiple papers into a structured, evidence-linked literature review |
| Research-Gap Finder | Surface recurring limitations, conflicts, under-studied populations, and method gaps — labeled as hypotheses until reviewed |
| Flashcards & Paper Quizzes | Auto-generated from your documents: TF-IDF key terms, definition cards, cloze deletions, and MCQs with server-side grading — plus SM-2-lite spaced repetition with a due queue |

### 🔥 Engagement

| Feature | What it does |
|---|---|
| XP & Streaks | Earn XP only for meaningful career actions (never raw screen time); daily streaks keep momentum visible |
| Forgiveness Shields | Miss a day? Two streak shields per month auto-repair it — progress without guilt trips |
| Weekly Leagues | Small opt-in cohorts competing on consistency, not prestige — winnable resets, no pay-to-win |
| Weekly Quests | Scheduled challenges (e.g. "complete 3 quizzes") with bonus XP on completion |
| Opt-Out | Turn off all gamification any time — the platform respects your choice |

### 👥 Community

| Feature | What it does |
|---|---|
| Peer Mock Interviews | Create a room, share the link, and practice with a friend — question cards from the 1,455-question bank, timers, role swap, and reciprocal 5-dimension rubric feedback |
| Mentor & Alumni Office Hours | Book group sessions with mentors across domains — sample profiles ship with the app; real mentors apply and are marked verified only after review. Structured question templates keep conversations useful, with post-session action notes |

### 📊 Review & Analytics

| Feature | What it does |
|---|---|
| Rejection Retrospectives | Funnel analytics show where applications stall; a guided wizard turns each rejection into observed patterns → hypotheses → one controlled experiment, with an experiment log |
| Career Health Review | A monthly, explainable review across **5 transparent dimensions** — direction, proof, opportunity activity, preparation, wellbeing — each with trend, confidence, evidence, and your smallest next action. No opaque "employability score." |

### 📡 Opportunity Radar

| Feature | What it does |
|---|---|
| Auto-Refreshing Board | **124 open opportunities** (128 total in the current data file) — hackathons from the Devpost API, internships from SimplifyJobs — with a freshness indicator; expired auto-entries are pruned, duplicates removed |
| Smart Merging | Curated entries are never touched; expired auto-entries are pruned, duplicates removed |
| Filters & Bookmarks | Filter by type (hackathon / internship / competition), bookmark what matters |

### ⚡ Ultimate Lab

| Feature | What it does |
|---|---|
| 91 Power Tools | Every feature's deep "ultimate" enhancements (streak freeze, squad challenges, peer mocks, rejection-pattern alerts, mentor prep packets, monthly health review…) runnable from one dashboard at `/ultimate/lab` — with guided forms, so 38 tools need no hand-written JSON at all |
| Tool API | `GET /api/ultimate/tools` lists all tools; `POST /api/ultimate/run/<tool_id>` runs one with a JSON body |

### 🔑 Sign-in & Extras

| Feature | What it does |
|---|---|
| Google Sign-in | One-click OAuth login alongside email/password (email verified via Google) |
| India Salary Explorer | Explore salary ranges by role, experience, and city, grounded in the curated salary dataset |
| Security | Content-Security-Policy enforced (nonce-based scripts, no third-party script CDNs); secrets only via environment variables, never in the repo; uploads are parsed in a sandboxed subprocess and deleted after parsing |

---

## 📸 Screenshots

| Landing | No-signup demo |
|---|---|
| ![Landing page](docs/assets/screenshots/landing.png) | ![Try page](docs/assets/screenshots/try.png) |
| **Dashboard** | **Skill quizzes** |
| ![Dashboard](docs/assets/screenshots/dashboard.png) | ![Skill quizzes](docs/assets/screenshots/quiz.png) |

---

## 🛠 Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, Flask 3.x, Flask-SQLAlchemy, Flask-Login |
| Database | SQLite (dev) / PostgreSQL (prod via `DATABASE_URL`) |
| AI | Google Gemini API with Groq/OpenRouter fallback; TF-IDF/BM25 retrieval (sentence-transformers optional — keyword fallback in production) |
| Frontend | Jinja2 templates, kinetic editorial + Bento CSS design systems, vanilla JS |
| Server | Gunicorn |
| Testing | pytest (9,786 collected cases, measured 2026-10-28) |
| Deployment | Render (free tier) |

---

## 📁 Project Structure

```
acra/
├── unified_app.py          # Flask app: models, routes, app factory (~11,900 lines)
├── studybuddy/             # Feature modules (Flask blueprints, LLM gateway, data loaders, pure logic)
│   ├── momentum.py         # XP / streaks / leagues / quests engine
│   ├── community.py        # Peer mocks + mentor office hours
│   ├── health_review.py    # Monthly career health aggregation
│   ├── flashcards.py       # Flashcard generation (TF-IDF, cloze, MCQ)
│   ├── gap_finder.py       # Research-gap analysis
│   ├── lit_review.py       # Literature review generator
│   ├── arxiv_import.py     # arXiv / DOI import + citation graph
│   ├── citation_fmt.py     # IEEE / APA / MLA / Vancouver / Chicago / BibTeX
│   ├── pdf_qa.py           # Source-grounded PDF question answering
│   ├── opportunity_sources.py  # Devpost + SimplifyJobs auto-ingest
│   ├── learning_roadmaps.py    # Skill roadmaps + resources
│   ├── skill_quizzes.py    # Quiz engine
│   ├── mock_interview.py   # Interview question bank + rubric scoring
│   ├── dsa_practice.py     # DSA problems + hints
│   ├── ai_gateway.py       # Gemini → Groq → OpenRouter fallback chain
│   ├── ultimate_reg_*.py   # Ultimate Lab tool registry (91 tools)
│   ├── ultimate_routes.py  # /ultimate/lab dashboard + /api/ultimate API
│   ├── resume_insights.py  # Bullet evidence + fix checklist for resume results
│   ├── parsing.py          # Sandboxed upload parsing (subprocess + timeouts)
│   └── ...                 # 15+ more feature modules
├── templates/              # Jinja2 templates (kinetic editorial + Bento UI)
├── static/
│   ├── css/bento.css       # Design system
│   └── img/                # STUDY-BUDDY owl logo + favicons
├── tests/                  # pytest suite (9,786 collected cases, measured 2026-10-28)
└── data/                   # JSON content banks (questions, resources)
```

---

## ⚙️ Installation & Setup

### Prerequisites

- Python 3.12+
- A [Gemini API key](https://aistudio.google.com/) (optional — core features work without it; AI-enhanced paths need it)

### Steps

```bash
# 1. Clone the repository
git clone https://github.com/Vivek492005/STUDY-BUDDY.git
cd STUDY-BUDDY

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables
cp .env.example .env   # then edit .env
```

On Windows, use PowerShell equivalents for steps 2 and 4:

```powershell
# 2. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\Activate.ps1

# 3. Install dependencies (same as above)
pip install -r requirements.txt

# 4. Configure environment variables
Copy-Item .env.example .env   # then edit .env in Notepad or your editor
```

To set an environment variable for the current PowerShell session
(instead of editing `.env`), use `$env:` syntax, e.g.
`$env:GEMINI_API_KEY = "your-key"` before running the app.

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | ✅ | Flask session secret — set a long random value in production |
| `DATABASE_URL` | ⚠️ prod | Managed PostgreSQL URL; falls back to local SQLite. Which managed database production uses (Render free Postgres vs Neon) is tracked in `docs/ops/neon-database-decision.md` — confirm there before re-pointing production. |
| `GEMINI_API_KEY` | optional | Enables Gemini-powered feedback, rewriting, and Q&A enhancements |

### Run

```bash
# Development
python unified_app.py

# Production (as deployed; worker/timeout settings live only in gunicorn.conf.py)
gunicorn -c gunicorn.conf.py unified_app:app
```

> Gunicorn is Unix-only. On Windows, run the development server with
> `python unified_app.py` (or serve the app with waitress:
> `pip install waitress`, then `waitress-serve --port=5000 unified_app:app`).

Open **http://localhost:5000** and create an account to get started.

---

## 🧪 Testing

```bash
pytest tests/ -q
```

- **9,786 collected test cases** (measured 2026-10-28; parametrized cases counted individually), covering every feature module: happy paths, validation, auth isolation, CSRF, rate limits, and error/edge cases
- Run the suite twice before any release; the project standard is **100% green, 0 failures**
- `CONTRIBUTING.md` is authoritative for the full setup (venv, `requirements-dev.txt`, sandbox env, ruff, benchmarks) — the one-liner above assumes that setup is done

---

## ☁️ Deployment

The live site runs on **Render's free tier**:

1. Connect the GitHub repo to Render as a **Web Service** (or use the committed `render.yaml`, which is authoritative)
2. Build command: `pip install --require-hashes -r requirements.txt`
3. Start command: `gunicorn -c gunicorn.conf.py unified_app:app` (worker/timeout settings live only in `gunicorn.conf.py`)
4. Set `SECRET_KEY`, `DATABASE_URL` (managed database — see `docs/ops/neon-database-decision.md` for the Render-vs-Neon status), and `GEMINI_API_KEY` in the dashboard
5. Every push to `main` auto-deploys

---

## 🗺 Roadmap

- [x] v1.0.0 — publicly launched (October 2026)
- [x] Career core (resume, JD match, tracker)
- [x] Placement prep (interviews, DSA, aptitude, STAR)
- [x] Learning hub (roadmaps, quizzes, certifications)
- [x] Research intelligence (PDF Q&A, citations, lit review, gaps, flashcards)
- [x] Engagement (XP, streaks, leagues, quests)
- [x] Community (peer mocks, mentor office hours)
- [x] Review & analytics (retrospectives, health review)
- [x] Opportunity radar auto-refresh
- [ ] Mobile PWA polish & offline packs
- [ ] Vernacular (Hindi + regional) language support — the i18n scaffolding exists, but real translations are still needed; no switcher is wired until they exist
- [ ] Real-resume benchmark gold set — collected opt-in only, never fabricated

---

## 🤝 Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide.

Quick version:
1. Fork the repo and create a feature branch (`git checkout -b feat/my-feature`)
2. Add tests — the suite must stay 100% green
3. Run `ruff check --no-cache` — must be clean
4. Open a PR using the template

### Benchmarks

We measure, we don't guess. See [docs/benchmarks.md](docs/benchmarks.md):

| Benchmark | Metric | Score |
|-----------|--------|-------|
| Resume parsing | Field accuracy (name/email/phone) | 1.00* |
| Resume parsing | Skill F1 | 1.00* |
| RAG retrieval | Recall@1 / MRR | 1.00* (8-question gold set) |
| RAG answers | Answer accuracy / Citation validity / Abstention | 0.96 / 0.96 / 0.80† |

\* *On synthetic gold sets. Real-world documents score lower — help us expand
the gold sets with challenging examples!*
† *On the larger 50-question + 5 abstention-case synthetic set (October 2026,
`scripts/benchmark_rag.py`). Misses are kept as findings — e.g. a stopword
weakness in the tokenizer — rather than tuned away. The 1.00 RAG rows above
come from the small 8-question gold set and are the less honest headline,
which is why this row exists.*

Run them yourself:
```bash
python benchmarks/run.py --quick
python benchmarks/rag_eval.py --quick
python scripts/validate_data.py
```

---

## 📄 License

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.

---

## 👤 Author

**Vivek Bartwal** — [GitHub @Vivek492005](https://github.com/Vivek492005)

Built for students, by a student. 🦉

⭐ If STUDY-BUDDY helped your placement prep, a star helps other students find it.
