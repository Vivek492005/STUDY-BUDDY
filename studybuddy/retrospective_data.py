"""Content banks for Wave 5 #29 — Rejection Retrospectives.

Two banks:
- ``HYPOTHESIS_STARTERS``: ~40 hypothesis starters across 10 categories.
  Every starter is phrased as a hypothesis ("Maybe…", "Possibly…") — never
  as a fact. Trust rule: these are thinking prompts, not conclusions.
- ``EXPERIMENT_TEMPLATES``: ~30 controlled-experiment templates. Each one
  names a single controlled change plus a measurable target, so the user
  can test a hypothesis instead of guessing.

Pure data module: no imports from the app, safe to import from tests and
templates alike.
"""

# (category_key, human-readable label) — shared by both banks.
RETRO_CATEGORIES = (
    ("resume_relevance", "Resume relevance"),
    ("ats_keywords", "ATS keywords"),
    ("portfolio_evidence", "Portfolio evidence"),
    ("volume_timing", "Application volume & timing"),
    ("role_fit", "Role fit"),
    ("outreach_networking", "Outreach & networking"),
    ("interview_prep", "Interview preparation"),
    ("salary_expectations", "Salary expectations"),
    ("location_constraints", "Location constraints"),
    ("followup", "Follow-up & persistence"),
)

CATEGORY_LABELS = dict(RETRO_CATEGORIES)

# Phase 4 reason taxonomy: 15 common rejection causes. Each entry is
# (reason_id, human label, stage_hint) where stage_hint is the pipeline
# stage where this cause most often shows up (or None when it spans all
# stages). The wizard's reason field accepts one of these ids OR free text
# alongside — free text is never replaced by the taxonomy.
RETRO_REASONS = (
    ("skill-gap-dsa", "DSA / coding gap", "oa"),
    ("skill-gap-system-design", "System design gap", "interview"),
    ("resume-weak", "Resume did not show fit", "applied"),
    ("jd-mismatch", "Role / JD mismatch", "applied"),
    ("oa-timeout", "Online assessment ran out of time", "oa"),
    ("interview-communication", "Communication in interviews", "interview"),
    ("interview-depth", "Answers lacked depth", "interview"),
    ("behavioral-fit", "Behavioral / culture fit", "interview"),
    ("salary-mismatch", "Salary expectations mismatch", "offer"),
    ("location", "Location constraints", "offer"),
    ("experience-shortfall", "Experience shortfall", "applied"),
    ("referral-absent", "No referral / weak outreach", "applied"),
    ("preparation-time", "Not enough preparation time", None),
    ("company-freeze", "Hiring freeze / role closed", None),
    ("unknown", "Unknown / no feedback given", None),
)

RETRO_REASON_IDS = frozenset(r[0] for r in RETRO_REASONS)
REASON_LABELS = {r[0]: r[1] for r in RETRO_REASONS}


def reason_label(reason):
    """Human label for a stored reason id; free text passes through."""
    return REASON_LABELS.get(reason or "", reason or "unspecified")

# Each entry: {"id": str, "category": str, "text": str}.
# "id" is stable (h1..h40) so templates/wizard can reference starters.
HYPOTHESIS_STARTERS = [
    # ---- Resume relevance (h1-h4) ----
    {"id": "h1", "category": "resume_relevance",
     "text": "Maybe my resume led with years of experience instead of the outcomes this role cares about."},
    {"id": "h2", "category": "resume_relevance",
     "text": "Maybe the first five seconds of my resume didn't match the job title closely enough."},
    {"id": "h3", "category": "resume_relevance",
     "text": "Maybe my bullets described responsibilities when this hiring manager was looking for results and numbers."},
    {"id": "h4", "category": "resume_relevance",
     "text": "Maybe my resume was too long for this role's level, and the relevant parts were buried on page two."},

    # ---- ATS keywords (h5-h8) ----
    {"id": "h5", "category": "ats_keywords",
     "text": "Maybe my resume used different words than the job description for the same skills (e.g. \"data viz\" vs \"data visualization\")."},
    {"id": "h6", "category": "ats_keywords",
     "text": "Maybe I listed tools but never the exact stack named in the posting, so an automated screen filtered me out."},
    {"id": "h7", "category": "ats_keywords",
     "text": "Maybe my resume layout (columns, icons, graphics) confused the parser and key sections were never read."},
    {"id": "h8", "category": "ats_keywords",
     "text": "Maybe the job description emphasized certifications or degree wording my resume doesn't mirror."},

    # ---- Portfolio evidence (h9-h12) ----
    {"id": "h9", "category": "portfolio_evidence",
     "text": "Maybe I claimed a skill but linked no proof — and this role's reviewers weight working demos heavily."},
    {"id": "h10", "category": "portfolio_evidence",
     "text": "Maybe my portfolio projects looked like tutorials rather than original work with real trade-offs."},
    {"id": "h11", "category": "portfolio_evidence",
     "text": "Maybe my GitHub or profile links were broken, outdated, or pointed at private repos nobody could open."},
    {"id": "h12", "category": "portfolio_evidence",
     "text": "Maybe I had relevant project work but it was buried below older, less relevant entries."},

    # ---- Application volume & timing (h13-h16) ----
    {"id": "h13", "category": "volume_timing",
     "text": "Maybe I applied after the first screening wave — late applications compete with an already-shortlisted pile."},
    {"id": "h14", "category": "volume_timing",
     "text": "Maybe I applied to too many roles at once and none of the applications got a tailored pass."},
    {"id": "h15", "category": "volume_timing",
     "text": "Maybe I applied during a hiring freeze window or end-of-quarter slowdown for this company."},
    {"id": "h16", "category": "volume_timing",
     "text": "Maybe I rushed the application and skipped the optional sections (cover note, work samples) that reviewers skim first."},

    # ---- Role fit (h17-h20) ----
    {"id": "h17", "category": "role_fit",
     "text": "Maybe I was overqualified on paper and they worried I'd leave in six months."},
    {"id": "h18", "category": "role_fit",
     "text": "Maybe the role needed a different core strength (e.g. frontend depth vs my backend background)."},
    {"id": "h19", "category": "role_fit",
     "text": "Maybe my background is non-traditional for this industry and the screening defaulted to familiar profiles."},
    {"id": "h20", "category": "role_fit",
     "text": "Maybe this posting was really a \"nice-to-have\" hire and the bar was calibrated for an internal candidate."},

    # ---- Outreach & networking (h21-h24) ----
    {"id": "h21", "category": "outreach_networking",
     "text": "Maybe my application entered a stack of hundreds while referred candidates were reviewed in a separate queue."},
    {"id": "h22", "category": "outreach_networking",
     "text": "Maybe I never reached the hiring manager, and nobody inside could vouch for my work."},
    {"id": "h23", "category": "outreach_networking",
     "text": "Maybe my cold outreach was too generic — it didn't reference anything specific about the team or their work."},
    {"id": "h24", "category": "outreach_networking",
     "text": "Maybe I reached out too late, after the shortlist was already decided."},

    # ---- Interview preparation (h25-h28) ----
    {"id": "h25", "category": "interview_prep",
     "text": "Maybe my answers were correct but rambling — I may not have used a clear structure like STAR under pressure."},
    {"id": "h26", "category": "interview_prep",
     "text": "Maybe I under-prepared the company's product and recent work, and it showed in the \"why us\" answers."},
    {"id": "h27", "category": "interview_prep",
     "text": "Maybe the online assessment tested patterns I hadn't practiced recently (timed coding, aptitude, case)."},
    {"id": "h28", "category": "interview_prep",
     "text": "Maybe I did fine on content but signaled low energy or nerves, which interviewers may have read as low interest."},

    # ---- Salary expectations (h29-h32) ----
    {"id": "h29", "category": "salary_expectations",
     "text": "Maybe my stated expected CTC was above this company's band for the role level."},
    {"id": "h30", "category": "salary_expectations",
     "text": "Maybe I revealed my current compensation too early and it anchored their offer below what they planned."},
    {"id": "h31", "category": "salary_expectations",
     "text": "Maybe I signaled inflexibility on compensation before they were sold on my value."},
    {"id": "h32", "category": "salary_expectations",
     "text": "Maybe the role's budget was cut mid-process and nobody senior enough to override it was in the loop."},

    # ---- Location constraints (h33-h36) ----
    {"id": "h33", "category": "location_constraints",
     "text": "Maybe the role was effectively location-locked and my city wasn't what they needed, despite the \"hybrid\" label."},
    {"id": "h34", "category": "location_constraints",
     "text": "Maybe my willingness to relocate wasn't visible anywhere in my application."},
    {"id": "h35", "category": "location_constraints",
     "text": "Maybe time-zone overlap mattered more than the posting suggested for this team's collaboration style."},
    {"id": "h36", "category": "location_constraints",
     "text": "Maybe they had equally qualified local candidates and relocation support wasn't in the budget."},

    # ---- Follow-up & persistence (h37-h40) ----
    {"id": "h37", "category": "followup",
     "text": "Maybe I never followed up after the interview, and they read silence as disinterest."},
    {"id": "h38", "category": "followup",
     "text": "Maybe my follow-up came too soon or read as pushy, and it colored their impression."},
    {"id": "h39", "category": "followup",
     "text": "Maybe I thanked them but never sent the work sample or reference they asked for in time."},
    {"id": "h40", "category": "followup",
     "text": "Maybe the process simply stalled and my one follow-up wasn't enough to restart it."},
]

# Each entry: {"id": str, "category": str, "hypothesis": str,
#              "change": str, "target": str, "duration": str}.
# "id" is stable (e1..e30). The "change" is always ONE controlled change;
# the "target" is always measurable.
EXPERIMENT_TEMPLATES = [
    # ---- Resume relevance (e1-e3) ----
    {"id": "e1", "category": "resume_relevance",
     "hypothesis": "Maybe my resume leads with experience instead of outcomes.",
     "change": "Rewrite the top 3 bullets of my resume to start with a quantified result.",
     "target": "Get a response (screen/OA/interview) on at least 2 of the next 10 applications.",
     "duration": "3 weeks"},
    {"id": "e2", "category": "resume_relevance",
     "hypothesis": "Maybe the first screen doesn't match the job title closely enough.",
     "change": "Add a one-line headline under my name that mirrors the target job title and 2 core skills.",
     "target": "Compare response rate of the next 10 applications vs my last 10.",
     "duration": "3 weeks"},
    {"id": "e3", "category": "resume_relevance",
     "hypothesis": "Maybe relevant content is buried on page two.",
     "change": "Cut my resume to one page, moving the most relevant role/project to the top third.",
     "target": "Track time-to-first-response; aim for any response within 10 days on the next 8 applications.",
     "duration": "4 weeks"},

    # ---- ATS keywords (e4-e6) ----
    {"id": "e4", "category": "ats_keywords",
     "hypothesis": "Maybe my wording doesn't match the job description's keywords.",
     "change": "For the next 10 applications, copy the posting's exact skill terms into my resume's skills section.",
     "target": "Measure screen-through rate on these 10 vs the previous 10.",
     "duration": "3 weeks"},
    {"id": "e5", "category": "ats_keywords",
     "hypothesis": "Maybe my resume layout confuses applicant-tracking parsers.",
     "change": "Create a single-column, text-only resume version and use it for the next 10 applications.",
     "target": "Count responses (screen/OA/interview) within 2 weeks per application, vs the fancy-layout baseline.",
     "duration": "4 weeks"},
    {"id": "e6", "category": "ats_keywords",
     "hypothesis": "Maybe the posting emphasizes credentials my resume doesn't mirror.",
     "change": "Add an explicit \"Relevant coursework / certifications\" line matching the posting's language.",
     "target": "Response rate on the next 8 applications in this industry.",
     "duration": "4 weeks"},

    # ---- Portfolio evidence (e7-e9) ----
    {"id": "e7", "category": "portfolio_evidence",
     "hypothesis": "Maybe reviewers weight working demos and I link none.",
     "change": "Add a live demo + code link to the top project on my resume and portfolio page.",
     "target": "Ask 3 reviewers/friends to rate credibility before and after; target +2 points average.",
     "duration": "2 weeks"},
    {"id": "e8", "category": "portfolio_evidence",
     "hypothesis": "Maybe my projects look like tutorials rather than original work.",
     "change": "Rewrite one project description to name a real trade-off I made and why.",
     "target": "Use the rewritten description in the next 5 applications; track interview invites.",
     "duration": "3 weeks"},
    {"id": "e9", "category": "portfolio_evidence",
     "hypothesis": "Maybe my links are broken or point at private repos.",
     "change": "Click every link on my resume; fix or remove anything that 404s or requires login.",
     "target": "Zero broken links verified this week; note response rate on the next 10 applications.",
     "duration": "1 week"},

    # ---- Application volume & timing (e10-e12) ----
    {"id": "e10", "category": "volume_timing",
     "hypothesis": "Maybe I applied late, after the first screening wave.",
     "change": "Apply within 48 hours of a posting going live for the next 10 applications.",
     "target": "Response rate on these 10 vs my historical late-application rate.",
     "duration": "4 weeks"},
    {"id": "e11", "category": "volume_timing",
     "hypothesis": "Maybe high volume means none of my applications get a tailored pass.",
     "change": "Cap at 3 applications per week, each with a customized resume + note, for one month.",
     "target": "Response rate on the 12 tailored applications vs my spray-and-pray baseline.",
     "duration": "4 weeks"},
    {"id": "e12", "category": "volume_timing",
     "hypothesis": "Maybe I skip the optional sections reviewers skim first.",
     "change": "Write a 3-line tailored note for every application for the next 10.",
     "target": "Response rate on these 10 vs the 10 before.",
     "duration": "3 weeks"},

    # ---- Role fit (e13-e15) ----
    {"id": "e13", "category": "role_fit",
     "hypothesis": "Maybe I'm applying to roles whose core strength isn't mine.",
     "change": "Only apply to roles where at least 70% of the requirements match my proven experience, for one month.",
     "target": "Response rate on fit-filtered applications vs my previous average.",
     "duration": "4 weeks"},
    {"id": "e14", "category": "role_fit",
     "hypothesis": "Maybe I look overqualified and they fear I'll leave.",
     "change": "For senior-titled postings, tailor the resume to the role level and add a one-line \"why this role\" note.",
     "target": "Screen-through rate on the next 6 role-level-matched applications.",
     "duration": "4 weeks"},
    {"id": "e15", "category": "role_fit",
     "hypothesis": "Maybe non-traditional backgrounds get filtered by default.",
     "change": "Add a one-line \"career narrative\" at the top of my resume connecting past work to this role.",
     "target": "Response rate on the next 8 applications in the new industry.",
     "duration": "4 weeks"},

    # ---- Outreach & networking (e16-e18) ----
    {"id": "e16", "category": "outreach_networking",
     "hypothesis": "Maybe referred candidates are reviewed in a separate queue.",
     "change": "Get one warm introduction or employee referral per application for the next 8.",
     "target": "Response rate on referred applications vs cold applications.",
     "duration": "4 weeks"},
    {"id": "e17", "category": "outreach_networking",
     "hypothesis": "Maybe my cold outreach is too generic.",
     "change": "Send 10 personalized messages, each referencing something specific about the team or their recent work.",
     "target": "Reply rate on the 10 messages; aim for at least 3 conversations.",
     "duration": "2 weeks"},
    {"id": "e18", "category": "outreach_networking",
     "hypothesis": "Maybe I reach out too late, after the shortlist is set.",
     "change": "Message a team member within 24 hours of applying, for the next 8 applications.",
     "target": "Response rate on applications with same-day outreach vs without.",
     "duration": "4 weeks"},

    # ---- Interview preparation (e19-e21) ----
    {"id": "e19", "category": "interview_prep",
     "hypothesis": "Maybe my answers are correct but unstructured under pressure.",
     "change": "Practice 5 behavioral answers in strict STAR format before each of the next 3 interviews.",
     "target": "Self-score structure 1–10 after each interview; target average ≥ 7.",
     "duration": "3 weeks"},
    {"id": "e20", "category": "interview_prep",
     "hypothesis": "Maybe I under-prepare the company's product and it shows.",
     "change": "Spend 30 minutes researching the product + write 3 specific questions before each interview.",
     "target": "Interviewer engagement score (self-rated 1–10) across the next 4 interviews.",
     "duration": "3 weeks"},
    {"id": "e21", "category": "interview_prep",
     "hypothesis": "Maybe timed assessments test patterns I haven't practiced.",
     "change": "Do 30 minutes of timed practice (coding or aptitude) daily for 2 weeks before the next OA.",
     "target": "OA pass rate on the next 3 assessments vs my previous attempts.",
     "duration": "4 weeks"},

    # ---- Salary expectations (e22-e24) ----
    {"id": "e22", "category": "salary_expectations",
     "hypothesis": "Maybe my stated expected CTC is above the band.",
     "change": "Research the band (levels.fyi, peers) and state a range anchored to it for the next 5 screens.",
     "target": "Screen-to-next-round rate on these 5 vs previous screens.",
     "duration": "4 weeks"},
    {"id": "e23", "category": "salary_expectations",
     "hypothesis": "Maybe I reveal current compensation too early and it anchors low.",
     "change": "Deflect early comp questions with a researched range until an offer discussion, for the next 5 processes.",
     "target": "Whether comp comes up before round 2 (count), and final offer vs previous baseline.",
     "duration": "6 weeks"},
    {"id": "e24", "category": "salary_expectations",
     "hypothesis": "Maybe I signal inflexibility before they're sold on my value.",
     "change": "Delay all comp talk until after a technical round passes, for the next 4 processes.",
     "target": "Advance-past-screen rate on these 4 vs before.",
     "duration": "4 weeks"},

    # ---- Location constraints (e25-e27) ----
    {"id": "e25", "category": "location_constraints",
     "hypothesis": "Maybe the role is effectively location-locked.",
     "change": "For one month, apply only to roles explicitly marked remote or in my city.",
     "target": "Response rate on location-matched applications vs before.",
     "duration": "4 weeks"},
    {"id": "e26", "category": "location_constraints",
     "hypothesis": "Maybe my willingness to relocate isn't visible.",
     "change": "Add an explicit \"Open to relocate to X\" line on my resume and LinkedIn for the next 10 applications.",
     "target": "Response rate on these 10 vs the 10 before.",
     "duration": "3 weeks"},
    {"id": "e27", "category": "location_constraints",
     "hypothesis": "Maybe time-zone overlap matters more than the posting suggests.",
     "change": "Mention my overlap hours explicitly in the application note for the next 8 remote roles.",
     "target": "Screen-through rate on these 8 vs previous remote applications.",
     "duration": "4 weeks"},

    # ---- Follow-up & persistence (e28-e30) ----
    {"id": "e28", "category": "followup",
     "hypothesis": "Maybe silence after the interview reads as disinterest.",
     "change": "Send a thank-you note within 24 hours after each of the next 4 interviews.",
     "target": "Next-round rate on these 4 vs previous interviews.",
     "duration": "4 weeks"},
    {"id": "e29", "category": "followup",
     "hypothesis": "Maybe my follow-ups are mistimed.",
     "change": "Follow up exactly once, 5 business days after the last contact, for the next 6 stalled processes.",
     "target": "Revival rate (any reply) on these 6 vs my previous ad-hoc follow-ups.",
     "duration": "4 weeks"},
    {"id": "e30", "category": "followup",
     "hypothesis": "Maybe I thank them but never deliver what they asked for in time.",
     "change": "Send any requested work sample or reference within 24 hours, for the next 3 requests.",
     "target": "Whether the process advanced within a week, for each of the 3.",
     "duration": "4 weeks"},
]


def category_label(key):
    """Human-readable label for a category key (fallback: the key)."""
    return CATEGORY_LABELS.get(key, key)


def starters_by_category():
    """{category_key: [starters]} preserving category order."""
    grouped = {key: [] for key, _ in RETRO_CATEGORIES}
    for s in HYPOTHESIS_STARTERS:
        grouped.setdefault(s["category"], []).append(s)
    return grouped


def templates_by_category():
    """{category_key: [templates]} preserving category order."""
    grouped = {key: [] for key, _ in RETRO_CATEGORIES}
    for t in EXPERIMENT_TEMPLATES:
        grouped.setdefault(t["category"], []).append(t)
    return grouped
