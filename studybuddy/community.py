"""Wave 6 #27: Mentor & alumni office hours — pure logic module.

Connects students to lived experience through structured, bounded
conversations. This module holds NO Flask request handling and NO route
registration; it defines:

- PATHS: the career paths mentors are grouped under.
- QUESTION_TEMPLATES: structured question prompts the booking flow offers
  so students arrive with a real question instead of a blank box.
- seed_mentors(): idempotent seeder for 14 realistic Indian-context seed
  mentors (is_seed=True, verified=False). Seeds are ILLUSTRATIVE profiles
  (Phase 5 honesty rule): never verified, never bookable.
- Small rule helpers: booking eligibility, capacity refresh, admin check.

Anything that touches the database imports unified_app's models lazily
(inside the function) to avoid circular imports — the same convention
momentum.py uses.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PATHS = [
    ("engineering", "Engineering & Tech"),
    ("medicine", "Medicine & Healthcare"),
    ("law", "Law"),
    ("commerce", "Commerce & Finance"),
    ("humanities", "Humanities & Social Sciences"),
    ("design", "Design & Creative"),
    ("other", "Other Paths"),
]
PATH_KEYS = [key for key, _ in PATHS]
PATH_LABELS = dict(PATHS)


def path_label(key):
    """Display label for a path key; falls back to the raw key."""
    return PATH_LABELS.get(key or "", key or "")


# ---------------------------------------------------------------------------
# Structured question templates for bookings
# ---------------------------------------------------------------------------

QUESTION_TEMPLATES = [
    {
        "id": "first_90_days",
        "category": "Role reality",
        "text": "What did your first 90 days in this role actually look like?",
    },
    {
        "id": "college_redo",
        "category": "College choices",
        "text": "What would you do differently in college if you started over?",
    },
    {
        "id": "first_break_skill",
        "category": "Skills",
        "text": "What skill mattered most for getting your first break?",
    },
    {
        "id": "choosing_paths",
        "category": "Career decisions",
        "text": "How did you choose between two competing paths — like a job vs higher studies?",
    },
    {
        "id": "field_myth",
        "category": "Field myths",
        "text": "What's one myth about your field that you believed as a student?",
    },
    {
        "id": "bad_week",
        "category": "Role reality",
        "text": "What does a bad week look like in your role — and how do you get through it?",
    },
    {
        "id": "day_in_life",
        "category": "Role reality",
        "text": "Walk me through a typical day — including the parts nobody talks about.",
    },
    {
        "id": "entry_ladder",
        "category": "Getting in",
        "text": "What's the most realistic way for a fresher to break into your field right now?",
    },
    {
        "id": "prep_six_months",
        "category": "Preparation",
        "text": "If I have six months to prepare for your field, what should I focus on?",
    },
    {
        "id": "pivot",
        "category": "Career decisions",
        "text": "Have you ever pivoted or switched tracks? What made you do it?",
    },
    {
        "id": "networking",
        "category": "Getting in",
        "text": "How much did networking actually matter for you — and what worked in practice?",
    },
    {
        "id": "hard_lesson",
        "category": "Lessons",
        "text": "What's a lesson you learned the hard way that I should learn early?",
    },
]

QUESTION_TEMPLATE_IDS = frozenset(t["id"] for t in QUESTION_TEMPLATES)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

BOOKING_MAX_QUESTION_CHARS = 2000
APPLY_MAX_BIO_CHARS = 2000
APPLY_NAME_MAX_CHARS = 120
FEEDBACK_MIN_RATING = 1
FEEDBACK_MAX_RATING = 5
FEEDBACK_MAX_CHARS = 2000
NOTE_MAX_CHARS = 3000
REPORT_REASONS = [
    "spam",
    "harassment",
    "fake_profile",
    "inappropriate",
    "no_show",
    "other",
]
REPORT_MAX_CHARS = 2000

#: Cancellations allowed until this many minutes before the session starts.
CANCEL_CUTOFF_MINUTES = 60


# ---------------------------------------------------------------------------
# Admin check
# ---------------------------------------------------------------------------

def mentor_is_admin(email):
    """True when email appears in the STUDYBUDDY_ADMINS comma-separated list."""
    raw = os.environ.get("STUDYBUDDY_ADMINS", "")
    admins = {e.strip().lower() for e in raw.split(",") if e.strip()}
    return bool(email) and email.strip().lower() in admins


# ---------------------------------------------------------------------------
# Booking rules (pure helpers — callers pass the ORM rows)
# ---------------------------------------------------------------------------

def booking_eligibility(session_row, booking_count, existing_booking):
    """Return (ok, error_message) for a booking attempt.

    - session_row: OfficeHour row (must be status 'open')
    - booking_count: current number of non-cancelled bookings
    - existing_booking: the user's existing non-cancelled booking, if any
    """
    if session_row is None:
        return False, "This office hour does not exist."
    if session_row.status != "open":
        return False, "This office hour is %s." % session_row.status
    now = datetime.now(UTC)
    scheduled = session_row.scheduled_at
    if scheduled is not None:
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=UTC)
        if scheduled <= now:
            return False, "This session already started. Pick an upcoming one."
    if existing_booking is not None:
        return False, "You already have a seat booked for this session."
    if booking_count >= (session_row.capacity or 0):
        return False, "This session is full. Try another time."
    return True, ""


def can_cancel(session_row, now=None):
    """Cancellations are allowed until 60 minutes before the session."""
    now = now or datetime.now(UTC)
    if session_row.status not in ("open", "full"):
        return False, "This session is %s and cannot be cancelled." % session_row.status
    scheduled = session_row.scheduled_at
    if scheduled is not None:
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=UTC)
        delta_minutes = (scheduled - now).total_seconds() / 60
        if delta_minutes < CANCEL_CUTOFF_MINUTES:
            return False, ("Cancellations close 1 hour before the session. "
                           "Write to support if something urgent came up.")
    return True, ""


def refresh_session_status(session_row, active_booking_count):
    """Mark a session full/open from the active (non-cancelled) booking count.

    Returns the new status string.
    """
    if session_row.status in ("completed", "cancelled"):
        return session_row.status
    capacity = session_row.capacity or 0
    session_row.status = "full" if active_booking_count >= capacity else "open"
    return session_row.status


# ---------------------------------------------------------------------------
# Seeder
# ---------------------------------------------------------------------------

def seed_mentors():
    """Idempotent seeder: insert 14 Indian-context seed mentors once.

    Returns the number of rows inserted (0 when the table already has
    rows). Rows are created with is_seed=True and verified=False:
    seeds are ILLUSTRATIVE profiles (Phase 5), never verified and never
    bookable. Serializers additionally force verified=False for seeds,
    so old rows stored with verified=True still never present as such.
    """
    from unified_app import Mentor, db

    if Mentor.query.first() is not None:
        return 0
    seeds = [
        {
            "name": "Ananya Iyer",
            "headline": "Senior Software Engineer, Payments",
            "path": "engineering",
            "company": "Flipkart",
            "college": "IIT Madras",
            "bio": ("Backend engineer working on high-throughput payments "
                    "systems. Mentors juniors on DSA-for-interviews and "
                    "surviving your first production on-call."),
            "linkedin_url": "https://www.linkedin.com/in/example-ananya-iyer",
        },
        {
            "name": "Rohan Mehta",
            "headline": "Quantitative Analyst",
            "path": "commerce",
            "company": "Zerodha",
            "college": "SRCC, Delhi University",
            "bio": ("Commerce graduate turned quant. Happy to talk about "
                    "breaking into markets from a non-engineering background, "
                    "and what 'finance' actually means day to day."),
            "linkedin_url": "https://www.linkedin.com/in/example-rohan-mehta",
        },
        {
            "name": "Dr. Sneha Kulkarni",
            "headline": "Resident Doctor, General Medicine",
            "path": "medicine",
            "company": "AIIMS New Delhi",
            "college": "AIIMS New Delhi",
            "bio": ("MBBS + MD resident. Can demystify NEET PG prep, choosing "
                    "a specialisation, and the emotional side of clinical "
                    "years nobody warns you about."),
            "linkedin_url": "https://www.linkedin.com/in/example-sneha-kulkarni",
        },
        {
            "name": "Arjun Nair",
            "headline": "Corporate Lawyer",
            "path": "law",
            "company": "Cyril Amarchand Mangaldas",
            "college": "NLSIU Bengaluru",
            "bio": ("Litigation vs corporate — chose corporate after a summer "
                    "internship. Talks CLAT prep, mooting myths, and what "
                    "junior lawyers actually do in year one."),
            "linkedin_url": "https://www.linkedin.com/in/example-arjun-nair",
        },
        {
            "name": "Priya Sharma",
            "headline": "Product Designer",
            "path": "design",
            "company": "Swiggy",
            "college": "NID Ahmedabad",
            "bio": ("Product designer who switched from architecture. "
                    "Portfolio reviews, design interviews, and how to learn "
                    "Figma deeply without a design degree."),
            "linkedin_url": "https://www.linkedin.com/in/example-priya-sharma",
        },
        {
            "name": "Vikram Reddy",
            "headline": "Scientist, Propulsion Systems",
            "path": "engineering",
            "company": "ISRO",
            "college": "NIT Warangal",
            "bio": ("Works on propulsion test campaigns. Loves talking about "
                    "GATE vs direct research routes, and doing meaningful "
                    "engineering outside the private sector."),
            "linkedin_url": "https://www.linkedin.com/in/example-vikram-reddy",
        },
        {
            "name": "Kavya Menon",
            "headline": "Data Scientist",
            "path": "engineering",
            "company": "PhonePe",
            "college": "BITS Pilani",
            "bio": ("ML generalist: fraud models by day, Kaggle by night. "
                    "Covers the real math you need vs the hype, and how to "
                    "build a portfolio that recruiters open."),
            "linkedin_url": "https://www.linkedin.com/in/example-kavya-menon",
        },
        {
            "name": "Aditya Rao",
            "headline": "UPSC — IAS (2022 batch)",
            "path": "humanities",
            "company": "Government of India",
            "college": "Hindu College, Delhi University",
            "bio": ("History graduate, cleared UPSC in the second attempt. "
                    "Honest talk on preparation timelines, backup plans, and "
                    "whether the exam is worth the years it costs."),
            "linkedin_url": "https://www.linkedin.com/in/example-aditya-rao",
        },
        {
            "name": "Meera Joshi",
            "headline": "Chartered Accountant, Audit",
            "path": "commerce",
            "company": "Deloitte India",
            "college": "Christ University, Bengaluru",
            "bio": ("CA who cleared all levels in the first attempt. "
                    "Articleship survival, choosing between audit/tax/industry, "
                    "and the reality of Big-4 hours."),
            "linkedin_url": "https://www.linkedin.com/in/example-meera-joshi",
        },
        {
            "name": "Nikhil Bansal",
            "headline": "Founder, EdTech Startup",
            "path": "commerce",
            "company": "Self-employed",
            "college": "IIM Ahmedabad",
            "bio": ("Ex-consultant turned founder. Talks about validating "
                    "ideas on a student budget, when NOT to start up, and "
                    "what investors actually ask first-time founders."),
            "linkedin_url": "https://www.linkedin.com/in/example-nikhil-bansal",
        },
        {
            "name": "Dr. Farah Khan",
            "headline": "Clinical Psychologist",
            "path": "medicine",
            "company": "NIMHANS Bengaluru",
            "college": "NIMHANS Bengaluru",
            "bio": ("Works with adolescents and young adults. Can discuss "
                    "psychology career paths in India, the RCI registration "
                    "maze, and building a private practice."),
            "linkedin_url": "https://www.linkedin.com/in/example-farah-khan",
        },
        {
            "name": "Sahil Kapoor",
            "headline": "Journalist, Technology Desk",
            "path": "humanities",
            "company": "The Hindu",
            "college": "Jamia Millia Islamia",
            "bio": ("Covers tech policy and startups. Honest about media "
                    "salaries, breaking in without a journalism degree, and "
                    "how to build a writing portfolio that gets read."),
            "linkedin_url": "https://www.linkedin.com/in/example-sahil-kapoor",
        },
        {
            "name": "Divya Krishnan",
            "headline": "UX Researcher",
            "path": "design",
            "company": "Razorpay",
            "college": "IIT Guwahati (Design)",
            "bio": ("Researcher studying how India pays online. Covers "
                    "getting into UX research, running your first usability "
                    "study, and turning findings into design decisions."),
            "linkedin_url": "https://www.linkedin.com/in/example-divya-krishnan",
        },
        {
            "name": "Rahul Verma",
            "headline": "Judicial Services Aspirant Mentor",
            "path": "law",
            "company": "Independent",
            "college": "NLU Delhi",
            "bio": ("Practising advocate guiding judiciary aspirants. "
                    "Bare-acts-first prep strategy, answer-writing practice, "
                    "and balancing litigation income with exam prep."),
            "linkedin_url": "https://www.linkedin.com/in/example-rahul-verma",
        },
    ]

    for s in seeds:
        db.session.add(Mentor(
            name=s["name"],
            headline=s["headline"],
            path=s["path"],
            company=s["company"],
            college=s["college"],
            bio=s["bio"],
            linkedin_url=s["linkedin_url"],
            avatar_url=None,
            verified=False,
            is_seed=True,
        ))
    db.session.commit()
    log.info("seed_mentors: inserted %d seed mentors", len(seeds))
    return len(seeds)


def seed_office_hours():
    """Idempotent seeder: give the first four seed mentors one upcoming
    office hour each, so the feature is alive on first load.

    Returns the number of rows inserted (0 when any office hour exists).
    Sessions land 2-8 days out at 12:30 UTC with capacity 12.
    """
    from unified_app import Mentor, OfficeHour, db

    if OfficeHour.query.first() is not None:
        return 0
    mentors = (Mentor.query.filter_by(is_seed=True)
               .order_by(Mentor.id).limit(4).all())
    if len(mentors) < 4:
        return 0
    topics = [
        "Ask me anything: breaking into tech from a tier-2/3 college",
        "Placements decoded: resume, DSA and the interview loop",
        "Beyond the degree: picking your first role wisely",
        "Office hours: careers, higher studies and everything between",
    ]
    now = datetime.now(UTC)
    inserted = 0
    for i, mentor in enumerate(mentors):
        day = now + timedelta(days=2 + i * 2)
        scheduled = day.replace(hour=12, minute=30, second=0, microsecond=0)
        if scheduled <= now:
            scheduled = scheduled + timedelta(days=1)
        db.session.add(OfficeHour(
            mentor_id=mentor.id,
            topic=topics[i % len(topics)],
            path=mentor.path,
            scheduled_at=scheduled,
            duration_min=45,
            capacity=12,
            status="open",
        ))
        inserted += 1
    db.session.commit()
    log.info("seed_office_hours: inserted %d seed sessions", inserted)
    return inserted
