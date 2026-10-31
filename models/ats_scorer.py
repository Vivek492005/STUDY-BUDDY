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

Anti-gaming hardening (2026-10-31, Eval 3 follow-through — an external
eval found a keyword-stuffed junk resume scoring 63 vs 72 for a clean
resume, far too narrow a gap):
  (a) Repetition caps / diminishing returns. Content tokens repeated
      beyond a free allowance feed a stuffing penalty (up to 8 content
      points); near-duplicate experience bullets collapse to one, so
      22 copies of a template earn ~1/22 of the bullet points; and a
      skill mentioned more than a handful of times has its evidence
      weight decayed quadratically toward zero.
  (b) Context-aware skill detection. A skill earns full weight only
      with supporting context: a line outside the skills section where
      the skill sits next to an action verb or a quantified metric
      (usage evidence). Lines naming more than a handful of distinct
      skills are treated as disguised lists and evidence none of them.
      Bare comma-separated list mentions earn only a small flat credit
      (capped), so stuffing dozens of keywords into SKILLS cannot
      saturate the component.
Legitimate resumes are unaffected: honest skill mentions stay under
the repetition allowances, real bullets are not near-duplicates, and
skills embedded in achievement bullets keep full weight.
"""

import math
import re
from collections import Counter

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


#: Stopwords excluded from the repetition analysis: function words an
#: honest resume repeats freely must never look like stuffing.
_REP_STOPWORDS = frozenset({
    'the', 'and', 'for', 'with', 'from', 'that', 'this', 'have', 'been',
    'were', 'are', 'was', 'has', 'had', 'will', 'would', 'can', 'could',
    'should', 'may', 'might', 'must', 'shall', 'not', 'but', 'all',
    'any', 'each', 'such', 'than', 'then', 'there', 'their', 'they',
    'them', 'its', 'our', 'your', 'his', 'her', 'who', 'whom', 'which',
    'what', 'when', 'where', 'how', 'why', 'into', 'over', 'under',
    'between', 'through', 'during', 'about', 'against', 'among', 'per',
    'a', 'an', 'of', 'to', 'in', 'on', 'at', 'by', 'as', 'is', 'it',
    'or', 'if', 'so', 'up', 'out', 'off',
})
_REP_TOKEN_RE = re.compile(r'[a-z]{3,}')

#: A content token may repeat this often for free (an honest resume
#: repeats "experience", "team", "project" a handful of times); only
#: repeats beyond the allowance feed the stuffing penalty.
_REP_FREE_OCCURRENCES = 6
_REP_PENALTY_RATE = 0.2
_REP_PENALTY_CAP = 8.0


def _repetition_penalty(text_lower):
    """Keyword-stuffing penalty (Eval-3 anti-gaming, part a).

    Counts non-stopword tokens repeated beyond _REP_FREE_OCCURRENCES.
    Returns (penalty_points, excess_repeats, worst_tokens). Stuffed
    resumes repeat the same keywords dozens of times to pad word
    count and fake keyword coverage; honest resumes stay near zero.
    """
    counts = Counter(_REP_TOKEN_RE.findall(text_lower))
    excess = 0
    worst = []
    for tok, cnt in counts.most_common():
        if tok in _REP_STOPWORDS:
            continue
        if cnt > _REP_FREE_OCCURRENCES:
            excess += cnt - _REP_FREE_OCCURRENCES
            worst.append((tok, cnt))
    penalty = min(_REP_PENALTY_CAP, _REP_PENALTY_RATE * excess)
    return penalty, excess, worst[:5]


#: Two experience lines are near-duplicates at this token Jaccard
#: similarity (template filler differs only in the verb/number).
_DUP_JACCARD_THRESHOLD = 0.8


def _dedup_lines(lines):
    """Collapse near-duplicate lines (Eval-3 anti-gaming, part a).

    Returns (unique_lines, dup_ratio). Greedy: a line is a duplicate
    when its token Jaccard similarity with any kept line reaches the
    threshold. Empty-token lines are never duplicates of each other.
    """
    unique = []
    uniq_token_sets = []
    for ln in lines:
        toks = set(_REP_TOKEN_RE.findall(ln.lower()))
        is_dup = False
        if toks:
            for kept in uniq_token_sets:
                inter = len(toks & kept)
                union = len(toks | kept)
                if inter and union and inter / union >= _DUP_JACCARD_THRESHOLD:
                    is_dup = True
                    break
        if not is_dup:
            unique.append(ln)
            uniq_token_sets.append(toks)
    dup_ratio = 1.0 - len(unique) / len(lines) if lines else 0.0
    return unique, dup_ratio


#: Canonical skill name -> alias surface forms, built lazily from the
#: extractor's alias table so "py"/"js" mentions count toward
#: "Python"/"JavaScript" repetition and support analysis.
_SKILL_ALIASES_BY_CANON = None


def _skill_alias_map():
    global _SKILL_ALIASES_BY_CANON
    if _SKILL_ALIASES_BY_CANON is None:
        from models.skill_extractor import SKILL_ALIASES
        by_canon = {}
        for alias, canon in SKILL_ALIASES.items():
            by_canon.setdefault(canon, []).append(alias)
        _SKILL_ALIASES_BY_CANON = by_canon
    return _SKILL_ALIASES_BY_CANON


def _skill_forms(skill_name):
    """Matchable surface forms for a skill: canonical name + aliases."""
    forms = [skill_name.lower()]
    for alias in _skill_alias_map().get(skill_name, []):
        if alias not in forms:
            forms.append(alias)
    return forms


def _form_pattern(form):
    return re.compile(r'(?<![a-zA-Z])' + re.escape(form) + r'(?![a-zA-Z])')


#: Compiled (form, pattern) pairs per skill name. The skills DB is
#: static, so patterns are compiled once and reused across score
#: calls (the stdlib re cache only holds 512 patterns — fewer than
#: the DB's form count — so per-call compilation would recompile
#: hundreds of patterns every time).
_SKILL_PATTERN_CACHE = {}


def _skill_patterns(name):
    cached = _SKILL_PATTERN_CACHE.get(name)
    if cached is None:
        cached = [(form, _form_pattern(form)) for form in _skill_forms(name)]
        _SKILL_PATTERN_CACHE[name] = cached
    return cached


#: A line naming more than this many distinct skills is a disguised
#: list, not usage: it evidences none of them.
_MAX_SKILLS_PER_SUPPORT_LINE = 5
#: Occurrences of one skill beyond this decay quadratically toward
#: zero (repeated occurrences of the same term contribute
#: progressively less — Eval-3 anti-gaming, part a).
_SKILL_OCC_CAP = 8


def _analyze_skill_support(skills, sections):
    """Context-aware skill evidence (Eval-3 anti-gaming, part b).

    Returns (supported, n_unsupported, cats_supported):
      supported: {skill_name: weight} for skills with >= 1 supporting
        line. weight = repetition-decayed evidence (1.0 for a normally
        mentioned skill, decaying toward 0 past _SKILL_OCC_CAP total
        occurrences).
      n_unsupported: skills found only as bare list mentions.
      cats_supported: categories with >= 1 strongly-evidenced skill
        (supporting line and repetition factor >= 0.5).
    A supporting line lives outside the skills section, names at most
    _MAX_SKILLS_PER_SUPPORT_LINE distinct skills, and places the skill
    next to an action verb or a quantified metric (usage evidence).
    """
    patterns = {}   # skill name -> [(form, compiled pattern)]
    categories = {}  # skill name -> category
    for s in skills or []:
        name = (s.get('name') or '').strip() if isinstance(s, dict) else str(s).strip()
        if not name or name in patterns:
            continue
        patterns[name] = _skill_patterns(name)
        categories[name] = s.get('category') if isinstance(s, dict) else None

    # Candidate pool: every section except the skills list itself.
    pool_lines = []
    for sec_name, sec_text in (sections or {}).items():
        if sec_name == 'skills':
            continue
        pool_lines.extend(
            ln.strip() for ln in (sec_text or '').split('\n') if ln.strip())

    def line_skills(ln_lower):
        """Distinct skills confirmed present in this line (cheap
        substring prefilter before the word-boundary regex)."""
        hits = set()
        for name, form_pats in patterns.items():
            for form, pat in form_pats:
                if form in ln_lower and pat.search(ln_lower):
                    hits.add(name)
                    break
        return hits

    occurrences = {name: 0 for name in patterns}
    supporting = {name: 0 for name in patterns}
    for ln in pool_lines:
        ln_lower = ln.lower()
        hits = line_skills(ln_lower)
        for name in hits:
            occurrences[name] += sum(len(pat.findall(ln_lower))
                                     for _, pat in patterns[name])
        if not hits or len(hits) > _MAX_SKILLS_PER_SUPPORT_LINE:
            continue  # disguised list: evidences nothing
        ln_tokens = set(_REP_TOKEN_RE.findall(ln_lower))
        has_verb = bool(ln_tokens & _VERB_FORMS)
        has_metric = bool(_METRIC_RE.search(ln))
        if has_verb or has_metric:
            for name in hits:
                supporting[name] += 1

    # Skills-section-only occurrences still count toward repetition.
    skills_text = (sections or {}).get('skills', '').lower()
    for name, form_pats in patterns.items():
        occurrences[name] += sum(len(pat.findall(skills_text))
                                 for _, pat in form_pats)

    supported = {}
    cats_supported = set()
    for name in patterns:
        if supporting[name] < 1:
            continue
        occ = occurrences[name]
        rep_factor = 1.0 if occ <= _SKILL_OCC_CAP else (_SKILL_OCC_CAP / occ) ** 2
        supported[name] = rep_factor
        if rep_factor >= 0.5 and categories.get(name):
            cats_supported.add(categories[name])
    n_unsupported = sum(1 for name in patterns if supporting[name] < 1)
    return supported, n_unsupported, cats_supported


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
    (score_0_to_4, total_lines, strong_lines, dup_ratio). Near-duplicate
    bullets (Eval-3 anti-gaming) collapse first: only unique lines can
    be strong, and the score is spread over ALL original lines, so 22
    copies of one template earn ~1/22 of the points instead of 22/22.
    No experience section -> (0, 0, 0, 0) with the detail saying so
    (adding the section is itself the fix, and the fix checklist says
    that)."""
    if not experience_text:
        return 0.0, 0, 0, 0.0
    lines = [ln.strip() for ln in experience_text.split('\n') if ln.strip()]
    if not lines:
        return 0.0, 0, 0, 0.0
    unique, dup_ratio = _dedup_lines(lines)
    strong = 0
    for ln in unique:
        first = re.split(r'\s+', ln.strip(), maxsplit=1)[0].strip('•-*').lower()
        first = re.sub(r'[^a-z]', '', first)
        starts_with_verb = first in _VERB_FORMS
        has_metric = bool(_METRIC_RE.search(ln))
        if starts_with_verb and (has_metric or len(ln.split()) >= 8):
            strong += 1
    return 4.0 * (strong / len(lines)), len(lines), strong, dup_ratio


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

    # 2. Skills & Keywords (25 points): CONTEXT-AWARE (Eval-3
    # anti-gaming). A skill earns full weight only with supporting
    # context — mentioned outside the skills section next to an action
    # verb or a quantified metric. The main curve (20 pts) runs over
    # repetition-decayed supported evidence plus evidenced-category
    # diversity; bare comma-separated list mentions earn only a small
    # flat credit (5 pts, capped), so stuffing dozens of keywords into
    # SKILLS can never saturate the component. No cliffs.
    supported, n_unsupported, cats_supported = _analyze_skill_support(
        skills, sections)
    num_skills = len(skills)
    supported_effective = sum(supported.values()) + 0.5 * len(cats_supported)
    skill_score = (20.0 * _curve(supported_effective, 2.0)
                   + min(5.0, 0.35 * n_unsupported))
    skill_score = max(0.0, min(skill_score, 25.0))
    scores['skills'] = {
        'score': round(skill_score, 1),
        'max': 25,
        'label': 'Skills & Keywords',
        'details': (f"{num_skills} skills found: {len(supported)} with supporting "
                    f"context across {len(cats_supported)} evidenced categories, "
                    f"{n_unsupported} list-only (flat credit, capped)"),
    }

    # 3. Content Quality (20 points): word curve (6) + distinct action
    # verbs (5) + quantified achievements (5) + bullet strength (4),
    # minus a weak-phrase penalty (up to 2) and a keyword-repetition
    # stuffing penalty (up to 8, Eval-3 anti-gaming).
    word_count = len(resume_text.split())
    _text_lower = resume_text.lower()
    verbs_found = _find_verbs(_text_lower)
    metrics = _find_metrics(resume_text)
    bullet_score, bullet_total, bullet_strong, bullet_dup = _bullet_strength(
        sections.get('experience', ''), _text_lower)
    weak_count = _count_weak_phrases(_text_lower)
    weak_penalty = min(2.0, 0.5 * weak_count)
    rep_penalty, rep_excess, rep_worst = _repetition_penalty(_text_lower)
    penalty = weak_penalty + rep_penalty
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
        _content_details += f"; {weak_count} vague phrase(s) (-{weak_penalty:.1f})"
    if rep_excess:
        _worst_str = ', '.join(f"{t} x{c}" for t, c in rep_worst)
        _content_details += (f"; keyword repetition: {rep_excess} excess repeats "
                             f"(-{rep_penalty:.1f}; e.g. {_worst_str})")
    if bullet_dup > 0.5:
        _content_details += (f"; {bullet_dup:.0%} of experience lines are "
                             "near-duplicates (template filler discounted)")
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
    # year estimate exists but the section does. Anti-gaming (Eval-3):
    # claimed years are discounted when the experience section is
    # mostly near-duplicate template bullets — mass-duplicated filler
    # cannot evidence years of work. (bullet_dup comes from the
    # content component's dedup analysis above.)
    if experience_years and experience_years > 0:
        exp_score = 15.0 * _curve(experience_years, 3)
        exp_detail = f"~{experience_years} years of experience detected (education dates excluded)"
        if bullet_dup > 0.5:
            _exp_factor = max(0.2, 1.0 - 0.8 * bullet_dup)
            exp_score *= _exp_factor
            exp_detail += (f"; duplicated-bullet discount x{_exp_factor:.2f} "
                           f"({bullet_dup:.0%} near-duplicate lines)")
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
