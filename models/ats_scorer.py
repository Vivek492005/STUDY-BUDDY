"""
ATS Scorer - Calculates ATS (Applicant Tracking System) resume score.

Evaluates resume quality based on multiple criteria.

NOTE: This is a *heuristic* resume-quality estimate. Real applicant-tracking
systems do not publish 0-100 scores, so the number below is a checklist-style
quality signal, not a prediction of any real ATS outcome. What this scorer
does promise: the score is computed from THIS resume's text, and changing
the resume changes the score in the direction the evidence points
(verified by differential tests in tests/test_resume_scorer_real.py).

Scoring model (2026-10-25 rebuild, grounded in how Resume Worded /
Jobscan / Rezi-style tools describe their checks: parsing + section
structure + keyword coverage + measurable impact + action-verb strength
+ bullet quality + formatting consistency):
  - Every component uses a smooth curve (diminishing returns), not
    coarse buckets. The old bucketed version saturated: two different
    strong resumes both hit every cap and scored identically, which is
    what users correctly reported as "fake" results.
  - Quantified achievements are detected on the text AS GIVEN. An
    earlier version counted metrics on clean_text() output, but cleaning
    stripped % and $ — so the metrics component was silently 0 for
    every resume. clean_text() now preserves % $ ₹ € £.
  - Experience years must come from extract_experience_years() called
    with sections=, so education dates are not counted as work years.
"""

import math
import re

# Phone patterns: Indian 10-digit mobile (with optional +91/91/0 handling via
# the 5+5 or 10-digit grouping) plus US/international 3-3-4 grouping.
PHONE_RE = re.compile(
    r'(?:\+?91[-.\s]?)?[6-9]\d{4}[-.\s]?\d{5}'
    r'|(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}'
)

#: Strong action-verb roots. Surface forms are matched with common
#: inflections (ed/ing/es/s) plus the irregulars below; a verb counts
#: once no matter how often it repeats (distinct verbs only).
_VERB_ROOTS = [
    'develop', 'design', 'implement', 'manage', 'create', 'build', 'lead',
    'launch', 'improve', 'optimize', 'reduce', 'increase', 'achieve',
    'deliver', 'coordinate', 'analyze', 'establish', 'maintain', 'automate',
    'integrate', 'resolve', 'collaborate', 'mentor', 'present', 'publish',
    'engineer', 'drive', 'boost', 'streamline', 'transform', 'accelerate',
    'expand', 'secure', 'grow', 'ship', 'negotiate', 'scale', 'pioneer',
    'overhaul', 'spearhead', 'revamp', 'own', 'define',
]
_VERB_IRREGULARS = {'led', 'built', 'grew', 'won', 'drove', 'wrote', 'taught'}


def _verb_forms():
    forms = set(_VERB_IRREGULARS)
    for root in _VERB_ROOTS:
        forms.add(root)
        forms.add(root + 'ed')
        forms.add(root + 'ing')
        forms.add(root + 'es')
        forms.add(root + 's')
        if root.endswith('e'):
            forms.add(root[:-1] + 'ing')
    return forms


_VERB_FORMS = _verb_forms()


def _find_verbs(text_lower):
    """Distinct strong action verbs present (word-boundary matched)."""
    found = set()
    for form in _VERB_FORMS:
        if re.search(r'\b' + re.escape(form) + r'\b', text_lower):
            found.add(form)
    # Collapse inflections back to roots for the distinct count: a resume
    # using "developed" and "developing" has one distinct verb, not two.
    roots = set()
    for form in found:
        if form in _VERB_IRREGULARS:
            roots.add(form)
            continue
        root = form
        for suffix in ('ing', 'ed', 'es', 's'):
            if root.endswith(suffix) and root[:-len(suffix)] in _VERB_ROOTS:
                root = root[:-len(suffix)]
                break
        else:
            # e-dropped gerund: "driving" -> "drive"
            if form.endswith('ing') and (form[:-3] + 'e') in _VERB_ROOTS:
                root = form[:-3] + 'e'
        roots.add(root)
    return roots


#: Quantified achievements: percentages, money (₹ $ € £), multipliers
#: ("3x"), and numbers attached to a scale noun ("40 hospitals" counts
#: as scale; a bare year like "2021" does not).
_SCALE_NOUNS = (
    r'users?|clients?|customers?|projects?|team members?|members?|'
    r'employees?|stores?|patients?|students?|accounts?|products?|'
    r'features?|releases?|tickets?|leads?|offices|countries?|factories|'
    r'hospitals|schools|partners?|vendors?|markets?|regions?'
)
_METRIC_RE = re.compile(
    r'\d+(?:\.\d+)?%'
    r'|[$₹€£][\d,]+(?:\.\d+)?'
    r'|\d+(?:\.\d+)?x\b'
    r'|\d+\s*(?:' + _SCALE_NOUNS + r')\b',
    re.IGNORECASE)


def _find_metrics(text):
    """Unique quantified values, whitespace/case normalized."""
    raw = _METRIC_RE.findall(text)
    return {re.sub(r'\s+', ' ', m.strip().lower()) for m in raw}


#: Vague openers Resume Worded-style tools flag ("responsible for" etc.).
_WEAK_PHRASES = [
    'responsible for', 'duties included', 'helped with', 'worked on',
    'in charge of', 'assisted with', 'tasked with', 'various tasks',
    'etc.', 'and so on', 'day to day',
]


def _count_weak_phrases(text_lower):
    return sum(text_lower.count(p) for p in _WEAK_PHRASES)


def _curve(x, scale):
    """Diminishing-returns curve: 0 at 0, approaches 1 as x grows."""
    return 1.0 - math.exp(-x / scale) if x > 0 else 0.0


def _word_points(word_count, gaming_detected):
    """Word-count component (max 6). Peaks at a standard 1-2 page resume;
    very short or very long documents taper off. Withheld entirely when
    copy-paste repetition is detected (gaming guard)."""
    if gaming_detected:
        return 0.0
    w = word_count
    if w <= 100:
        return 6.0 * (w / 100.0) * 0.5
    if w <= 300:
        return 3.0 + 3.0 * (w - 100) / 200.0
    if w <= 900:
        return 6.0
    if w <= 1500:
        return 6.0 - 2.0 * (w - 900) / 600.0
    return max(2.0, 4.0 - 2.0 * (w - 1500) / 1500.0)


def _bullet_strength(experience_text, text_lower):
    """Fraction of experience lines that are 'strong': they start with an
    action verb AND (carry a metric OR run 8+ words). Returns
    (score_0_to_4, total_lines, strong_lines). No experience section ->
    (0, 0, 0) with the detail saying so (adding the section is itself
    the fix, and the fix checklist says that)."""
    if not experience_text:
        return 0.0, 0, 0
    lines = [ln.strip() for ln in experience_text.split('\n') if ln.strip()]
    if not lines:
        return 0.0, 0, 0
    strong = 0
    for ln in lines:
        first = re.split(r'\s+', ln.strip(), maxsplit=1)[0].strip('•-*').lower()
        first = re.sub(r'[^a-z]', '', first)
        starts_with_verb = first in _VERB_FORMS
        has_metric = bool(_METRIC_RE.search(ln))
        if starts_with_verb and (has_metric or len(ln.split()) >= 8):
            strong += 1
    return 4.0 * (strong / len(lines)), len(lines), strong


def _date_consistency(experience_text):
    """2 points when experience dates mostly use one format (YYYY-YYYY
    vs Mon YYYY); 1 neutral point when no dates are present (nothing
    inconsistent proven); 0-2 scaled by the dominant format's share."""
    if not experience_text:
        return 1.0, 'no dates found'
    year_ranges = len(re.findall(r'\b\d{4}\s*[-–]\s*\d{4}\b', experience_text))
    month_dates = len(re.findall(
        r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*\d{4}\b',
        experience_text, re.IGNORECASE))
    total = year_ranges + month_dates
    if total == 0:
        return 1.0, 'no dates found'
    share = max(year_ranges, month_dates) / total
    fmt = 'YYYY-YYYY' if year_ranges >= month_dates else 'Mon YYYY'
    return round(2.0 * share, 1), f'{total} dates, mostly {fmt}'


def calculate_ats_score(resume_text, sections, skills, experience_years):
    """
    Calculate ATS score for a resume based on multiple criteria.

    Args:
        resume_text: Full cleaned resume text
        sections: Dict of extracted sections
        skills: List of extracted skill dicts
        experience_years: Estimated years of experience (call
            extract_experience_years with sections= so education dates
            are excluded)

    Returns:
        dict: ATS score breakdown with total score and category scores
    """
    scores = {}

    # De-duplicate skills (case-insensitive) so repeated listings can't inflate
    # the skills score.
    _seen_skill_names = set()
    _uniq_skills = []
    for _s in skills or []:
        _nm = (_s.get('name') or '').strip().lower() if isinstance(_s, dict) else str(_s).strip().lower()
        if _nm and _nm not in _seen_skill_names:
            _seen_skill_names.add(_nm)
            _uniq_skills.append(_s)
    skills = _uniq_skills

    # Repetition / gaming guard: pathological copy-paste repetition (very low
    # unique-word ratio) zeroes the word-count component and is disclosed.
    _words_lc = resume_text.lower().split()
    _gaming_detected = bool(_words_lc) and len(_words_lc) > 100 and \
        (len(set(_words_lc)) / len(_words_lc)) < 0.2

    # 1. Section Completeness (20 points): 4 per essential section,
    # 1 per bonus section (up to 4). Names the missing sections.
    essential_sections = ['summary', 'education', 'experience', 'skills']
    optional_sections = ['projects', 'awards', 'publications', 'volunteer', 'languages']
    essential_found = [s for s in essential_sections if s in sections]
    optional_found = [s for s in optional_sections if s in sections]
    section_score = len(essential_found) * 4.0 + min(len(optional_found), 4) * 1.0
    missing = [s for s in essential_sections if s not in sections]
    scores['sections'] = {
        'score': round(section_score, 1),
        'max': 20,
        'label': 'Section Completeness',
        'details': (f"{len(essential_found)}/{len(essential_sections)} essential sections"
                    + (f" (missing: {', '.join(missing)})" if missing else "")
                    + f", {len(optional_found)} bonus sections"),
    }

    # 2. Skills & Keywords (25 points): smooth curve over skill count
    # plus category diversity — no cliffs, no hard cap below 25.
    num_skills = len(skills)
    categories = {s.get('category') for s in skills
                  if isinstance(s, dict) and s.get('category')}
    effective = num_skills + 0.5 * len(categories)
    skill_score = 25.0 * _curve(effective, 10)
    scores['skills'] = {
        'score': round(skill_score, 1),
        'max': 25,
        'label': 'Skills & Keywords',
        'details': f"{num_skills} skills found across {len(categories)} categories",
    }

    # 3. Content Quality (20 points): word curve (6) + distinct action
    # verbs (5) + quantified achievements (5) + bullet strength (4),
    # minus a weak-phrase penalty (up to 2).
    word_count = len(resume_text.split())
    _text_lower = resume_text.lower()
    verbs_found = _find_verbs(_text_lower)
    metrics = _find_metrics(resume_text)
    bullet_score, bullet_total, bullet_strong = _bullet_strength(
        sections.get('experience', ''), _text_lower)
    weak_count = _count_weak_phrases(_text_lower)
    penalty = min(2.0, 0.5 * weak_count)
    content_score = (
        _word_points(word_count, _gaming_detected)
        + 5.0 * _curve(len(verbs_found), 4)
        + 5.0 * _curve(len(metrics), 3)
        + bullet_score
        - penalty
    )
    content_score = max(0.0, min(content_score, 20.0))
    _content_details = (f"{word_count} words, {len(verbs_found)} distinct action verbs, "
                        f"{len(metrics)} quantified achievements, "
                        f"{bullet_strong}/{bullet_total} strong experience lines")
    if metrics:
        _content_details += f" (e.g. {', '.join(sorted(metrics)[:3])})"
    if weak_count:
        _content_details += f"; {weak_count} vague phrase(s) (-{penalty:.1f})"
    if _gaming_detected:
        _content_details += " — repetitive text detected, word-count points withheld"
    scores['content'] = {
        'score': round(content_score, 1),
        'max': 20,
        'label': 'Content Quality',
        'details': _content_details,
    }

    # 4. Contact Information (10 points): email 3, phone 3, LinkedIn 2,
    # GitHub 1, portfolio/website/blog 1 (GitHub and portfolio are
    # separate points now — a GitHub-only resume differs from a
    # portfolio-only one).
    has_email = bool(re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', resume_text))
    has_phone = bool(PHONE_RE.search(resume_text))
    has_linkedin = bool(re.search(r'linkedin', resume_text, re.IGNORECASE))
    has_github = bool(re.search(r'github', resume_text, re.IGNORECASE))
    has_portfolio = bool(re.search(r'portfolio|website|blog', resume_text, re.IGNORECASE))
    contact_score = (3 if has_email else 0) + (3 if has_phone else 0) \
        + (2 if has_linkedin else 0) + (1 if has_github else 0) \
        + (1 if has_portfolio else 0)
    scores['contact'] = {
        'score': round(contact_score, 1),
        'max': 10,
        'label': 'Contact Information',
        'details': f"Email: {'✓' if has_email else '✗'}, Phone: {'✓' if has_phone else '✗'}, LinkedIn: {'✓' if has_linkedin else '✗'}, GitHub: {'✓' if has_github else '✗'}, Portfolio: {'✓' if has_portfolio else '✗'}",
    }

    # 5. Experience (15 points): smooth years curve; fallbacks when no
    # year estimate exists but the section does.
    if experience_years and experience_years > 0:
        exp_score = 15.0 * _curve(experience_years, 3)
        exp_detail = f"~{experience_years} years of experience detected (education dates excluded)"
    elif 'experience' in sections:
        exp_score = 3.0
        exp_detail = "experience section present, no year estimate"
    elif 'project' in resume_text.lower() or 'projects' in sections:
        exp_score = 2.0
        exp_detail = "no work experience found; projects present"
    else:
        exp_score = 0.0
        exp_detail = "no work experience found"
    scores['experience'] = {
        'score': round(exp_score, 1),
        'max': 15,
        'label': 'Experience',
        'details': exp_detail,
    }

    # 6. Formatting & Readability (10 points): line count (2) + few
    # special characters (1.5) + section structure (2.5) + word count
    # in range (2) + date-format consistency (2).
    format_score = 0.0
    lines = resume_text.split('\n')
    non_empty_lines = [l for l in lines if l.strip()]
    if 20 <= len(non_empty_lines) <= 120:
        format_score += 2.0
    elif 10 < len(non_empty_lines) < 20 or 120 < len(non_empty_lines) <= 200:
        format_score += 1.0
    special_chars = len(re.findall(r'[★●◆▪►•]', resume_text))
    if special_chars < 20:
        format_score += 1.5
    if len(sections) >= 3:
        format_score += 2.5
    elif len(sections) >= 2:
        format_score += 1.5
    if 400 <= word_count <= 1000:
        format_score += 2.0
    elif 200 <= word_count <= 1500:
        format_score += 1.0
    date_score, date_detail = _date_consistency(sections.get('experience', ''))
    format_score += date_score
    scores['formatting'] = {
        'score': round(min(format_score, 10), 1),
        'max': 10,
        'label': 'Formatting & Readability',
        'details': f"{len(non_empty_lines)} lines, {len(sections)} sections identified; dates: {date_detail}",
    }

    # Calculate total
    total = sum(s['score'] for s in scores.values())
    max_total = sum(s['max'] for s in scores.values())

    # Determine grade
    percentage = (total / max_total) * 100
    if percentage >= 85:
        grade = 'A'
        grade_label = 'Excellent'
    elif percentage >= 70:
        grade = 'B'
        grade_label = 'Good'
    elif percentage >= 55:
        grade = 'C'
        grade_label = 'Average'
    elif percentage >= 40:
        grade = 'D'
        grade_label = 'Below Average'
    else:
        grade = 'F'
        grade_label = 'Needs Improvement'

    return {
        'total_score': round(total, 1),
        'max_score': max_total,
        'percentage': round(percentage, 1),
        'grade': grade,
        'grade_label': grade_label,
        'breakdown': scores,
        'gaming_detected': _gaming_detected,
        'method_note': ('Heuristic resume-quality estimate, not a real ATS score. '
                        'Real applicant-tracking systems do not publish 0-100 scores; '
                        'treat this as a checklist-style quality signal. Scores are '
                        'computed from this resume\'s own text: sections, skills, '
                        'action verbs, quantified achievements, bullet strength, '
                        'contact completeness, experience years (education dates '
                        'excluded), and formatting consistency.'),
    }
