"""Ultimate Lab Phase 1: feature rooms, flagship paths, intent search.

Data-only module: no Flask app import, no database access. The routes in
``studybuddy/ultimate_routes.py`` render these structures and own all
persistence (``LabPathProgress`` rows, XP hooks).

Honesty rules for this data:
- Room overviews are one sentence each, derived only from the feature's
  own tool names. No invented capabilities, counts, or outcomes.
- Path "estimated time" is never shown in minutes: a path shows its step
  count only. Step counts are real (len(steps)).
- Finish actions: ``tracker`` writes a real tracker wishlist row via the
  existing tracker validation (role required, company optional for an
  explicit wishlist add). ``milestone`` writes nothing beyond the path's
  own completion state and links onward.
"""

import re


def slugify_feature(feature):
    """'JD-Matched ATS Scoring' -> 'jd-matched-ats-scoring'."""
    slug = re.sub(r"[^a-z0-9]+", "-", feature.lower()).strip("-")
    return slug or "lab"


# ---------------------------------------------------------------------------
# Room outputs: where a room's outputs go next (shared with ultimate_routes,
# which keeps _FEATURE_NEXT_ACTIONS as an alias of this map).
# ---------------------------------------------------------------------------

ROOM_OUTPUTS = {
    "Job Tracker": [("tracker", "Open job tracker", "/tracker")],
    "Opportunity Radar": [("tracker", "Open job tracker", "/tracker")],
    "Cover Letter + Cold Outreach": [("tracker", "Open job tracker", "/tracker")],
    "JD-Matched ATS Scoring": [("tracker", "Open job tracker", "/tracker")],
    "GitHub Integration": [("portfolio", "Open portfolio builder", "/portfolio")],
    "Portfolio Builder": [("portfolio", "Open portfolio builder", "/portfolio")],
    "Resume Versions": [("portfolio", "Open portfolio builder", "/portfolio")],
    "STAR Builder": [("portfolio", "Open portfolio builder", "/portfolio")],
    "Certifications": [("portfolio", "Open portfolio builder", "/portfolio")],
    "Mock Interviews": [("mentor", "Find a mentor", "/mentors")],
    "Communication Coach": [("mentor", "Find a mentor", "/mentors")],
    "Career Assessment": [("mentor", "Find a mentor", "/mentors")],
    "Peer Mocks": [("mentor", "Find a mentor", "/mentors")],
}

# (feature, overview) — overviews paraphrase the feature's tool names only.
_ROOM_OVERVIEWS = {
    "JD-Matched ATS Scoring": (
        "Check how a resume reads against a job description: a screening "
        "checklist, a keyword action plan, and a competitiveness read."),
    "AI Bullet Rewriter": (
        "Turn flat bullets into STAR stories, stronger verbs, and "
        "metric-led lines."),
    "Resume Versions": (
        "Compare, tailor, and track resume versions against a job "
        "description."),
    "Cover Letter + Cold Outreach": (
        "Pick recipients, draft follow-ups, and score outreach before "
        "you send it."),
    "LinkedIn Grader": (
        "Check search visibility, plan content, and close gaps against "
        "a job description."),
    "GitHub Integration": (
        "Grade a README, turn commits into bullets, and choose what "
        "to pin."),
    "Career Assessment": (
        "Map fit, build a roadmap, and measure the delta from where "
        "you are."),
    "Salary Explorer": (
        "Evaluate an offer, practise negotiation, and check salary bands."),
    "Portfolio Builder": (
        "Shape a case study, check links, and preview the recruiter view."),
    "Mock Interviews": (
        "Prep answers by voice, research a company, and track redos."),
    "DSA Practice": (
        "Master patterns, simulate an online assessment, and space "
        "repetitions."),
    "Communication Coach": (
        "Check video presence, practise the HR round, and score clarity."),
    "STAR Builder": (
        "Bank stories, predict follow-ups, and rehearse the 30-second "
        "pitch."),
    "Learning Roadmaps": (
        "Follow an AI track, hit project milestones, and plan the week."),
    "Skill Quizzes": (
        "Sit a cert-style exam, drill weak spots, and check the "
        "leaderboard."),
    "Certifications": (
        "Simulate exam day, weigh ROI, and plan renewal."),
    "PDF Q&A": (
        "Ask across documents, verify citations, and turn answers into "
        "notes."),
    "arXiv Import": (
        "Bulk-import papers, find related work, and queue reading."),
    "Citation Formatter": (
        "Format in-text citations, match journal styles, and find "
        "duplicates."),
    "Literature Review Generator": (
        "Cluster papers, highlight gaps, and set the review length."),
    "Gap Finder": (
        "Cross-check trends, test feasibility, and shape a proposal."),
    "Flashcards": (
        "Export to Anki, sprint before an exam, and loop weak cards."),
    "Trending Research": (
        "A personalised feed, paper-to-project ideas, and a weekly "
        "digest."),
    "Opportunity Radar": (
        "Deadline alerts, fit scores, and one-click tracker adds."),
    "Job Tracker": (
        "Pipeline analytics, follow-up nudges, and offer comparison."),
    "Momentum": (
        "Streak freezes, squad leaderboards, and reward tiers."),
    "Peer Mocks": (
        "Match partners, score with a rubric, and book recurring slots."),
    "Retrospectives": (
        "Pattern alerts, experiment tracking, and a win library."),
    "Mentors": (
        "Async Q&A digests, prep packets, and alumni outcomes."),
    "Career Health Review": (
        "A monthly report, peer benchmarks, and action nudges."),
}


def build_rooms(tools):
    """Group the tool grid into rooms, one per feature.

    ``tools`` is the ``ALL_TOOLS`` list (dicts with id/feature/name/
    description). Workflow order is the grid order within the feature.
    Returns a list of room dicts in first-appearance feature order.
    """
    rooms = []
    by_feature = {}
    for tool in tools:
        by_feature.setdefault(tool["feature"], []).append(tool)
    for feature, feature_tools in by_feature.items():
        rooms.append({
            "slug": slugify_feature(feature),
            "feature": feature,
            "overview": _ROOM_OVERVIEWS.get(feature, ""),
            "tools": feature_tools,
            "outputs": [{"kind": k, "label": label, "href": href}
                        for k, label, href in ROOM_OUTPUTS.get(feature, [])],
        })
    return rooms


def room_for_slug(rooms, slug):
    """Return the room dict for a slug, or None."""
    for room in rooms:
        if room["slug"] == slug:
            return room
    return None


# ---------------------------------------------------------------------------
# Flagship paths.
#
# Step kinds:
# - "tool": completed by running the named Lab tool; the step endpoint
#   requires a LabRun row owned by the user for exactly that tool. XP 10.
# - "page": completed by the user marking it done after visiting the
#   linked page. No XP (a checkbox is not a verifiable artifact).
# Path completion (all steps done) awards 25 XP once per path.
# ---------------------------------------------------------------------------

PATHS = [
    {
        "id": "resume-offer",
        "title": "Resume → Offer",
        "tagline": "From readiness score to a tracked target role.",
        "finish_kind": "tracker",
        "finish_label": "Add a target role to your tracker",
        "finish_detail": (
            "Give the role you are targeting (company optional for a "
            "wishlist row) and it lands in your job tracker."),
        "finish_links": [{"label": "Open job tracker", "href": "/tracker"}],
        "steps": [
            {"id": "r1", "title": "Check your readiness score",
             "kind": "page", "href": "/resume",
             "detail": "Open your resume page and note the readiness score — "
                       "the next steps work from it."},
            {"id": "r2", "title": "Run the screening checklist",
             "kind": "tool", "tool_id": "jd/prescreen",
             "detail": "Run the illustrative screening checklist on your resume "
                       "and a job description."},
            {"id": "r3", "title": "Convert one bullet to STAR",
             "kind": "tool", "tool_id": "bullets/star-convert",
             "detail": "Take your flattest bullet and rewrite it as a STAR story."},
            {"id": "r4", "title": "Get your keyword action plan",
             "kind": "tool", "tool_id": "jd/keyword-plan",
             "detail": "Find the keywords your resume is missing for this role."},
            {"id": "r5", "title": "Compare two resume versions",
             "kind": "tool", "tool_id": "versions/diff",
             "detail": "Diff your tailored version against your base resume."},
            {"id": "r6", "title": "Check your competitiveness",
             "kind": "tool", "tool_id": "jd/offer-odds",
             "detail": "Get a competitiveness read before you apply."},
            {"id": "r7", "title": "Prep one mock answer",
             "kind": "tool", "tool_id": "mock/voice-prep",
             "detail": "Rehearse one answer by voice before the real round."},
        ],
    },
    {
        "id": "placement-sprint",
        "title": "30-Day Placement Sprint",
        "tagline": "Four weeks: resume, applications, interviews, offers.",
        "finish_kind": "milestone",
        "finish_label": "Finish the sprint",
        "finish_detail": (
            "No new row is created here — finishing records the sprint "
            "as complete and points you at your tracker and portfolio."),
        "finish_links": [{"label": "Open job tracker", "href": "/tracker"},
                         {"label": "Open portfolio builder",
                          "href": "/portfolio"}],
        "steps": [
            {"id": "s1", "title": "Week 1 — Strengthen your verbs",
             "kind": "tool", "tool_id": "bullets/verb-power",
             "detail": "Upgrade the verbs across one resume section."},
            {"id": "s2", "title": "Week 1 — Check LinkedIn search visibility",
             "kind": "tool", "tool_id": "linkedin/search-check",
             "detail": "See whether recruiters can find your profile."},
            {"id": "s3", "title": "Week 1 — Grade your README",
             "kind": "tool", "tool_id": "github/readme-grade",
             "detail": "Grade the README of your strongest project."},
            {"id": "s4", "title": "Week 2 — Score an outreach draft",
             "kind": "tool", "tool_id": "outreach/score",
             "detail": "Score a cold email before you send it."},
            {"id": "s5", "title": "Week 2 — Score an opportunity's fit",
             "kind": "tool", "tool_id": "opportunities/fit-score",
             "detail": "Check one opportunity against your profile."},
            {"id": "s6", "title": "Week 2 — Add target roles to the tracker",
             "kind": "page", "href": "/tracker",
             "detail": "Add the roles you are targeting this month."},
            {"id": "s7", "title": "Week 3 — Master one DSA pattern",
             "kind": "tool", "tool_id": "dsa/pattern-mastery",
             "detail": "Work one pattern until you can explain it cold."},
            {"id": "s8", "title": "Week 3 — Drill your weakest skill",
             "kind": "tool", "tool_id": "quizzes/weakness-drill",
             "detail": "Let the quiz find your weakest skill, then drill it."},
            {"id": "s9", "title": "Week 3 — Score your answer clarity",
             "kind": "tool", "tool_id": "coach/clarity-score",
             "detail": "Score one spoken answer for clarity."},
            {"id": "s10", "title": "Week 4 — Check salary bands",
             "kind": "tool", "tool_id": "salary/bands",
             "detail": "Look up the band for your target role and city."},
            {"id": "s11", "title": "Week 4 — Compare offers side by side",
             "kind": "tool", "tool_id": "tracker/compare-offers",
             "detail": "Compare two offers on more than salary."},
            {"id": "s12", "title": "Week 4 — Write your first retrospective",
             "kind": "page", "href": "/retrospectives",
             "detail": "After an interview, write what worked and what did not."},
        ],
    },
    {
        "id": "interview-ready",
        "title": "Interview Ready",
        "tagline": "Assessment to mock round, in eight steps.",
        "finish_kind": "milestone",
        "finish_label": "Finish interview prep",
        "finish_detail": (
            "No new row is created here — finishing records the path as "
            "complete and points you at mentors and retrospectives."),
        "finish_links": [{"label": "Find a mentor", "href": "/mentors"},
                         {"label": "Open retrospectives",
                          "href": "/retrospectives"}],
        "steps": [
            {"id": "i1", "title": "Take the career assessment",
             "kind": "page", "href": "/assessment",
             "detail": "Start from an honest assessment of where you stand."},
            {"id": "i2", "title": "Find your skill gaps",
             "kind": "tool", "tool_id": "assessment/delta",
             "detail": "Measure the delta between you and the role."},
            {"id": "i3", "title": "Drill your weakest skill",
             "kind": "tool", "tool_id": "quizzes/weakness-drill",
             "detail": "Close the weakest gap first."},
            {"id": "i4", "title": "Master one DSA pattern",
             "kind": "tool", "tool_id": "dsa/pattern-mastery",
             "detail": "One pattern, explained cold, beats ten skimmed."},
            {"id": "i5", "title": "Bank one STAR story",
             "kind": "tool", "tool_id": "star/story-bank",
             "detail": "Bank a story you can reuse across interviews."},
            {"id": "i6", "title": "Research a target company",
             "kind": "tool", "tool_id": "mock/company-intel",
             "detail": "Go in knowing the company's interview themes."},
            {"id": "i7", "title": "Practise the HR round",
             "kind": "tool", "tool_id": "coach/hr-round",
             "detail": "Rehearse the round most candidates wing."},
            {"id": "i8", "title": "Score a peer mock with the rubric",
             "kind": "tool", "tool_id": "peermocks/score-rubric",
             "detail": "Scoring someone else sharpens your own answers."},
        ],
    },
]


def path_for_id(path_id):
    """Return the path dict for an id, or None."""
    for path in PATHS:
        if path["id"] == path_id:
            return path
    return None


def step_for_id(path, step_id):
    """Return the step dict within a path, or None."""
    for step in path["steps"]:
        if step["id"] == step_id:
            return step
    return None


# ---------------------------------------------------------------------------
# Intent search: curated phrase -> tool ids, then substring fallback.
# Phrases are matched case-insensitively; a query matches a phrase when
# either contains the other (so "resume" finds "improve my resume").
# ---------------------------------------------------------------------------

INTENT_PHRASES = [
    ("improve my resume", ["bullets/star-convert", "jd/keyword-plan",
                           "jd/prescreen"]),
    ("ats", ["jd/prescreen", "jd/keyword-plan"]),
    ("beat ats", ["jd/prescreen", "jd/keyword-plan"]),
    ("cold email", ["outreach/recipients", "outreach/score"]),
    ("cold outreach", ["outreach/recipients", "outreach/score"]),
    ("cover letter", ["outreach/recipients"]),
    ("linkedin", ["linkedin/search-check", "linkedin/content-plan"]),
    ("github", ["github/readme-grade", "github/pin-optimizer"]),
    ("readme", ["github/readme-grade"]),
    ("salary", ["salary/evaluate", "salary/negotiate", "salary/bands"]),
    ("negotiate", ["salary/negotiate"]),
    ("offer", ["salary/evaluate", "tracker/compare-offers"]),
    ("mock interview", ["mock/voice-prep", "mock/company-intel",
                        "coach/hr-round"]),
    ("interview prep", ["mock/voice-prep", "mock/company-intel",
                        "coach/hr-round"]),
    ("dsa", ["dsa/pattern-mastery", "dsa/mock-oa"]),
    ("coding practice", ["dsa/pattern-mastery", "dsa/mock-oa"]),
    ("quiz", ["quizzes/weakness-drill", "quizzes/cert-exam-mode"]),
    ("test myself", ["quizzes/weakness-drill", "quizzes/cert-exam-mode"]),
    ("star story", ["star/story-bank", "star/30s-pitch"]),
    ("behavioral", ["star/story-bank", "star/30s-pitch"]),
    ("certification", ["certs/exam-day-sim", "certs/roi"]),
    ("cert exam", ["certs/exam-day-sim"]),
    ("research paper", ["litreview/cluster", "pdfqa/answer-across-docs"]),
    ("literature review", ["litreview/cluster", "litreview/gap-highlight"]),
    ("arxiv", ["arxiv/bulk-import"]),
    ("import papers", ["arxiv/bulk-import"]),
    ("citation", ["citations/in-text", "citations/journal-style"]),
    ("flashcard", ["flashcards/anki-export", "flashcards/exam-sprint"]),
    ("revise", ["flashcards/anki-export", "flashcards/exam-sprint"]),
    ("find jobs", ["opportunities/fit-score", "opportunities/deadline-alerts"]),
    ("opportunities", ["opportunities/fit-score",
                       "opportunities/deadline-alerts"]),
    ("track applications", ["tracker/pipeline-analytics",
                            "tracker/followup-nudges"]),
    ("streak", ["momentum/streak-freeze", "momentum/reward-tiers"]),
    ("motivation", ["momentum/streak-freeze", "momentum/reward-tiers"]),
    ("peer mock", ["peermocks/match-partners"]),
    ("practice with peers", ["peermocks/match-partners"]),
    ("mentor", ["mentors/prep-packet", "mentors/async-qa-digest"]),
    ("career change", ["assessment/ai-fit", "assessment/roadmap"]),
    ("what career", ["assessment/ai-fit", "assessment/roadmap"]),
    ("portfolio", ["portfolio/case-study", "portfolio/recruiter-preview"]),
    ("roadmap", ["roadmaps/agentic-ai-track", "roadmaps/weekly-planner"]),
    ("learn", ["roadmaps/agentic-ai-track", "roadmaps/weekly-planner"]),
    ("gap in research", ["gaps/feasibility", "gaps/to-proposal"]),
    ("trending", ["trending/personalized-feed", "trending/weekly-digest"]),
    ("what's new", ["trending/personalized-feed", "trending/weekly-digest"]),
    ("career health", ["health/monthly-report", "health/action-nudges"]),
    ("retrospective", ["retro/win-library", "retro/track-experiment"]),
    ("reflect", ["retro/win-library", "retro/track-experiment"]),
]


def intent_search(query, tools):
    """Return (intent_tools, substring_tools) for a query.

    Intent matches come first; substring matches over name/description/
    feature follow, excluding ids already in the intent list. Both lists
    are capped at 12 entries.
    """
    q = (query or "").strip().lower()
    if len(q) < 2:
        return [], []
    by_id = {t["id"]: t for t in tools}
    intent_ids = []
    for phrase, tool_ids in INTENT_PHRASES:
        if phrase in q or (len(q) >= 3 and q in phrase):
            for tool_id in tool_ids:
                if tool_id in by_id and tool_id not in intent_ids:
                    intent_ids.append(tool_id)
    substring_ids = []
    for tool in tools:
        if tool["id"] in intent_ids:
            continue
        hay = " ".join([tool["id"], tool["name"], tool["feature"],
                        tool.get("description", "")]).lower()
        if q in hay:
            substring_ids.append(tool["id"])
    return ([by_id[i] for i in intent_ids[:12]],
            [by_id[i] for i in substring_ids[:12]])
