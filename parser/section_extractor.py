"""
Section Extractor - Identifies and extracts resume sections.
"""
import re
from datetime import datetime

# Common section headers in resumes
SECTION_PATTERNS = {
    'summary': r'(?i)(?:summary|objective|about\s*me|profile|professional\s*summary|career\s*objective|personal\s*statement|executive\s*profile|career\s*summary|professional\s*profile)',
    'education': r'(?i)(?:education|academic|qualification|degree|university|college|school|certification|certifications)',
    'experience': r'(?i)(?:experience|work\s*experience|employment|work\s*history|professional\s*experience|career\s*history|internship|work\s*history)',
    'skills': r'(?i)(?:skills|technical\s*skills|core\s*competencies|competencies|technologies|tech\s*stack|proficiencies|areas\s*of\s*expertise)',
    'projects': r'(?i)(?:projects|personal\s*projects|academic\s*projects|key\s*projects|portfolio|project\s*experience|selected\s*projects|technical\s*projects)',
    'awards': r'(?i)(?:awards|achievements|honors|accomplishments|recognition)',
    'languages': r'(?i)(?:languages|language\s*proficiency)',
    'interests': r'(?i)(?:interests|hobbies|extracurricular|activities)',
    'references': r'(?i)(?:references|referees)',
    'publications': r'(?i)(?:publications|papers|research)',
    'volunteer': r'(?i)(?:volunteer|volunteering|community\s*service)',
}


def _looks_like_header(clean_line):
    """True when a line plausibly IS a section header, not content.

    The old rule (pattern match + under 50 chars) let content lines
    hijack section state: "B.Com, Delhi University, 2022-2025" matches
    the education pattern, so it was treated as a second EDUCATION
    header — and because the real education content was still empty at
    that point, the whole education section was never saved. Same for
    "College fest website..." (contains "college") wiping PROJECTS.
    Headers are short, few words, and never carry years.
    """
    if len(clean_line) >= 50:
        return False
    if len(clean_line.split()) > 4:
        return False
    if re.search(r'\b(?:19|20)\d{2}\b', clean_line):
        return False
    return True


def extract_sections(text):
    """
    Extract different sections from resume text.

    Args:
        text: Cleaned resume text

    Returns:
        dict: Dictionary with section names as keys and content as values
    """
    if not text:
        return {}

    sections = {}
    lines = text.split('\n')
    current_section = 'header'
    current_content = []

    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            continue

        # Check if this line is a section header (case-insensitive)
        found_section = None
        # Remove numbers/icons for better matching
        clean_line = re.sub(r'^[^\w\s]+|^\d+[\.\)\s-]*', '', line_stripped).strip()

        for section_name, pattern in SECTION_PATTERNS.items():
            if re.search(pattern, clean_line):
                # Headers are short, few words, year-free (see
                # _looks_like_header); content lines naming a university
                # or college must not hijack section state.
                if _looks_like_header(clean_line):
                    found_section = section_name
                    break

        if found_section:
            # Save previous section
            if current_content:
                sections[current_section] = '\n'.join(current_content).strip()
            current_section = found_section
            current_content = []
        else:
            current_content.append(line_stripped)

    # Save last section
    if current_content:
        sections[current_section] = '\n'.join(current_content).strip()

    # --- Heuristic Fallback for Summary ---
    # If no formal summary section was found, check if the "header" section
    # contains a large block of text starting with professional keywords
    if 'summary' not in sections and 'header' in sections:
        header_lines = sections['header'].split('\n')
        for i, line in enumerate(header_lines):
            # If we find a line that looks like the start of a summary
            # (long sentence, professional keywords, but no explicit header)
            if len(line) > 100 or re.search(r'(?i)(?:professional|experience|seeking|passionate|specializing)', line):
                # The rest of the header might be the summary
                sections['summary'] = '\n'.join(header_lines[i:]).strip()
                break

    return sections


def extract_education_details(education_text):
    """
    Extract education details from education section.
    
    Returns:
        list: List of education entries with degree, institution, year
    """
    if not education_text:
        return []

    entries = []

    # Common degree patterns
    degree_patterns = [
        r'(?i)(B\.?(?:Tech|E|Sc|A|Com|S)|Bachelor(?:\'s)?)',
        r'(?i)(M\.?(?:Tech|E|Sc|A|Com|S|BA)|Master(?:\'s)?)',
        r'(?i)(Ph\.?D\.?|Doctorate)',
        r'(?i)(Diploma|Associate)',
        r'(?i)(B\.?C\.?A|M\.?C\.?A)',
        r'(?i)(B\.?B\.?A|M\.?B\.?A)',
        r'(?i)(12th|10th|HSC|SSC|High School)',
    ]

    # Year pattern
    year_pattern = r'((?:19|20)\d{2})'

    lines = education_text.split('\n')
    for line in lines:
        entry = {'raw': line}

        # Find degree
        for dp in degree_patterns:
            match = re.search(dp, line)
            if match:
                entry['degree'] = match.group(1)
                break

        # Find year
        years = re.findall(year_pattern, line)
        if years:
            entry['year'] = years[-1]  # Take the last year (graduation year)

        if 'degree' in entry or 'year' in entry:
            entries.append(entry)

    return entries


_MONTHS = {m: i + 1 for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun',
     'jul', 'aug', 'sep', 'oct', 'nov', 'dec'])}


def _parse_month_year(s):
    """'Jan 2020' -> (2020, 1); None when unparseable."""
    m = re.match(r'([A-Za-z]+)\.?\s*(\d{4})', (s or '').strip())
    if not m:
        return None
    month = _MONTHS.get(m.group(1).lower()[:3])
    if month is None:
        return None
    return (int(m.group(2)), month)


def extract_experience_years(text, sections=None):
    """
    Estimate total years of experience from resume text.

    Args:
        text: Resume text (cleaned).
        sections: Optional extract_sections() dict. When given, the
            education section is EXCLUDED from the scope: an earlier
            version scanned the whole document, so a fresher whose only
            dates were "B.Com, 2022-2025" was credited with 3 years of
            work experience. Direct "N years of experience" mentions in
            the summary still count (they live outside education).

    Year ranges are summed (overlapping jobs double-count slightly —
    documented estimate, not payroll math). Month ranges now compute
    real spans instead of counting ranges (a Jan 2020–Dec 2023 job used
    to count as 1 year).

    Returns:
        float: Estimated years of experience
    """
    if not text:
        return 0

    scope = text
    if sections:
        scope = '\n'.join(v for k, v in sections.items() if k != 'education')
        if not scope.strip():
            return 0

    # Direct mention of experience
    patterns = [
        r'(\d+)\+?\s*(?:years?|yrs?)\s*(?:of\s*)?(?:experience|exp)',
        r'(?:experience|exp)\s*(?:of\s*)?(\d+)\+?\s*(?:years?|yrs?)',
    ]

    for pattern in patterns:
        match = re.search(pattern, scope, re.IGNORECASE)
        if match:
            return float(match.group(1))

    # Month date ranges -> real spans in years
    date_ranges = re.findall(
        r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*\d{4})\s*[-–to]+\s*((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*\d{4}|Present|Current)',
        scope, re.IGNORECASE
    )

    if date_ranges:
        total = 0.0
        for start, end in date_ranges:
            parsed_start = _parse_month_year(start)
            if parsed_start is None:
                continue
            if end.lower() in ('present', 'current'):
                end_ym = (datetime.now().year, datetime.now().month)
            else:
                parsed_end = _parse_month_year(end)
                if parsed_end is None:
                    continue
                end_ym = parsed_end
            months = (end_ym[0] - parsed_start[0]) * 12 + (end_ym[1] - parsed_start[1])
            total += max(0, months) / 12.0
        return round(total, 1) if total > 0 else 1

    # Year ranges
    year_ranges = re.findall(r'(\d{4})\s*[-–to]+\s*(\d{4}|Present|Current)', scope, re.IGNORECASE)
    if year_ranges:
        total = 0
        for start, end in year_ranges:
            start_yr = int(start)
            end_yr = datetime.now().year if end.lower() in ('present', 'current') else int(end)
            total += max(0, end_yr - start_yr)
        return total if total > 0 else 1

    return 0
