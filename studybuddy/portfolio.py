"""Portfolio one-page builder (Wave 1 feature #12).

Helpers for validating portfolio payloads and generating unique public slugs.
All user content is length-bounded; URLs are restricted to http(s).
"""

import re
import unicodedata

MAX_NAME = 80
MAX_HEADLINE = 140
MAX_BIO = 600
MAX_SKILLS = 30
MAX_SKILL_LEN = 40
# Raised 12 -> 50 in Phase 3 so the GitHub import can fill a portfolio
# without the editor refusing to save it back.
MAX_PROJECTS = 50
MAX_PROJECT_TITLE = 80
MAX_PROJECT_DESC = 400
MAX_LINKS = 8
MAX_LINK_LABEL = 40
# Phase 3 sections (all plain text, HTML stripped, 50 items each).
MAX_SECTION_ITEMS = 50
MAX_CERT_TITLE = 80
MAX_CERT_ISSUER = 80
MAX_CERT_YEAR = 20
MAX_EXP_FIELD = 80
MAX_EXP_PERIOD = 40
MAX_ACH_TITLE = 120
MAX_ACH_DESC = 400
MAX_CONTACT_LABEL = 40
MAX_CONTACT_VALUE = 200
PORTFOLIO_THEMES = ("classic", "dark", "minimal", "bold")


def slugify(text):
    """Turn a display name into a URL-safe slug base."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return text[:40] or "portfolio"


def _clean_url(url):
    """Return a safe http(s) URL or None."""
    url = (url or "").strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return None
    if len(url) > 500 or any(c.isspace() for c in url):
        return None
    return url


def _strip_text(value):
    """Plain-text only: strip HTML tags and surrounding whitespace."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"<[^>]*>", "", value).strip()


def _clean_section_rows(raw, section):
    """Validate one Phase 3 list section. Returns (rows, error)."""
    if not isinstance(raw, list):
        return None, f"{section} must be a list."
    if len(raw) > MAX_SECTION_ITEMS:
        return None, f"At most {MAX_SECTION_ITEMS} {section} allowed."
    rows = []
    for item in raw:
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            return None, f"Each {section} item must be an object."
        if section == "certifications":
            title = _strip_text(item.get("title"))[:MAX_CERT_TITLE]
            issuer = _strip_text(item.get("issuer"))[:MAX_CERT_ISSUER]
            year = _strip_text(item.get("year"))[:MAX_CERT_YEAR]
            if not title and not issuer and not year:
                continue
            if not title:
                return None, "Each certification needs a title."
            rows.append({"title": title, "issuer": issuer, "year": year})
        elif section == "experience":
            role = _strip_text(item.get("role") or item.get("title"))
            role = role[:MAX_EXP_FIELD]
            org = _strip_text(item.get("organization"))[:MAX_EXP_FIELD]
            period = _strip_text(item.get("period"))[:MAX_EXP_PERIOD]
            desc = _strip_text(item.get("description"))[:MAX_PROJECT_DESC]
            if not role and not org and not period and not desc:
                continue
            if not role and not org:
                return None, "Each experience item needs a role or organization."
            rows.append({"role": role, "organization": org,
                         "period": period, "description": desc})
        elif section == "education":
            inst = _strip_text(item.get("institution"))[:MAX_EXP_FIELD]
            degree = _strip_text(item.get("degree") or item.get("title"))
            degree = degree[:MAX_EXP_FIELD]
            period = _strip_text(item.get("period"))[:MAX_EXP_PERIOD]
            if not inst and not degree and not period:
                continue
            if not inst and not degree:
                return None, "Each education item needs an institution or degree."
            rows.append({"institution": inst, "degree": degree,
                         "period": period})
        elif section == "achievements":
            title = _strip_text(item.get("title"))[:MAX_ACH_TITLE]
            desc = _strip_text(item.get("description"))[:MAX_ACH_DESC]
            if not title and not desc:
                continue
            if not title:
                return None, "Each achievement needs a title."
            rows.append({"title": title, "description": desc})
        elif section == "contact":
            label = _strip_text(item.get("label"))[:MAX_CONTACT_LABEL]
            value = _strip_text(item.get("value"))[:MAX_CONTACT_VALUE]
            if not label and not value:
                continue
            if not label or not value:
                return None, "Each contact item needs both a label and a value."
            rows.append({"label": label, "value": value})
    return rows, None


def validate_portfolio_payload(payload):
    """Validate a portfolio save payload.

    Returns (data, error). data is the cleaned dict ready for storage;
    error is a human-readable message or None.
    """
    if not isinstance(payload, dict):
        return None, "Invalid portfolio data."
    display_name = (payload.get("display_name") or "").strip()
    if not display_name:
        return None, "Display name is required."
    if len(display_name) > MAX_NAME:
        return None, f"Display name must be {MAX_NAME} characters or fewer."
    headline = (payload.get("headline") or "").strip()
    if len(headline) > MAX_HEADLINE:
        return None, f"Headline must be {MAX_HEADLINE} characters or fewer."
    bio = (payload.get("bio") or "").strip()
    if len(bio) > MAX_BIO:
        return None, f"Bio must be {MAX_BIO} characters or fewer."

    raw_skills = payload.get("skills") or []
    if not isinstance(raw_skills, list):
        return None, "Skills must be a list."
    skills = []
    for s in raw_skills:
        s = (s or "").strip()
        if not s:
            continue
        if len(s) > MAX_SKILL_LEN:
            return None, f"Each skill must be {MAX_SKILL_LEN} characters or fewer."
        if s not in skills:
            skills.append(s)
    if len(skills) > MAX_SKILLS:
        return None, f"At most {MAX_SKILLS} skills allowed."

    raw_projects = payload.get("projects") or []
    if not isinstance(raw_projects, list):
        return None, "Projects must be a list."
    if len(raw_projects) > MAX_PROJECTS:
        return None, f"At most {MAX_PROJECTS} projects allowed."
    projects = []
    for p in raw_projects:
        if not isinstance(p, dict):
            return None, "Each project must be an object."
        title = (p.get("title") or "").strip()
        desc = (p.get("description") or "").strip()
        link = _clean_url(p.get("link"))
        if not title and not desc and not link:
            continue  # skip empty rows
        if not title:
            return None, "Each project needs a title."
        if len(title) > MAX_PROJECT_TITLE:
            return None, f"Project titles must be {MAX_PROJECT_TITLE} characters or fewer."
        if len(desc) > MAX_PROJECT_DESC:
            return None, f"Project descriptions must be {MAX_PROJECT_DESC} characters or fewer."
        projects.append({"title": title, "description": desc, "link": link})

    raw_links = payload.get("links") or []
    if not isinstance(raw_links, list):
        return None, "Links must be a list."
    if len(raw_links) > MAX_LINKS:
        return None, f"At most {MAX_LINKS} links allowed."
    links = []
    for ln in raw_links:
        if not isinstance(ln, dict):
            return None, "Each link must be an object."
        label = (ln.get("label") or "").strip()
        url = _clean_url(ln.get("url"))
        if not label and not url:
            continue
        if not label or not url:
            return None, "Each link needs both a label and a valid https:// URL."
        if len(label) > MAX_LINK_LABEL:
            return None, f"Link labels must be {MAX_LINK_LABEL} characters or fewer."
        links.append({"label": label, "url": url})

    # Phase 3 sections + theme (additive: absent keys default to empty).
    sections = {}
    for section in ("certifications", "experience", "education",
                    "achievements", "contact"):
        rows, error = _clean_section_rows(payload.get(section) or [], section)
        if error:
            return None, error
        sections[section] = rows
    theme = (payload.get("theme") or "classic")
    if not isinstance(theme, str) or theme not in PORTFOLIO_THEMES:
        return None, ("Theme must be one of: " +
                      ", ".join(PORTFOLIO_THEMES) + ".")

    return {
        "display_name": display_name,
        "headline": headline,
        "bio": bio,
        "skills": skills,
        "projects": projects,
        "links": links,
        "show_github": bool(payload.get("show_github")),
        "show_leetcode": bool(payload.get("show_leetcode")),
        "is_published": bool(payload.get("is_published")),
        "theme": theme,
        **sections,
    }, None


def unique_slug(base, taken):
    """Return a unique slug given a base and a set of taken slugs."""
    base = slugify(base)
    if base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


# ================= Feature 9 Ultimate Enhancements =================
# 9.1 Case-study generator | 9.2 Live demo checker | 9.3 Recruiter preview

import re as _re4


def _clean4(text):
    return _re4.sub(r"<[^>]*>", "", text or "")


def generate_case_study(project):
    """Turn a project dict into a structured case study.

    project: {title, description, tech (list), role, link, demo_link}.
    Returns {title, problem, approach, outcome, tech, prompts} — prompts fill
    gaps where the user gave too little detail.
    """
    if not isinstance(project, dict):
        raise ValueError("project must be a dict.")
    title = _clean4(project.get("title") or "").strip()[:120]
    if not title:
        raise ValueError("Project title is required.")
    desc = _clean4(project.get("description") or "").strip()
    tech = [_clean4(t).strip() for t in (project.get("tech") or [])
            if isinstance(t, str) and _clean4(t).strip()][:12]
    role = _clean4(project.get("role") or "").strip()[:120]

    # Heuristic split of description into problem/approach/outcome.
    sentences = [_s.strip() for _s in _re4.split(r"(?<=[.!?])\s+", desc) if _s.strip()]
    problem, approach, outcome = "", "", ""
    if sentences:
        # Outcome: sentences with numbers.
        numbered = [s for s in sentences if _re4.search(r"\d", s)]
        plain = [s for s in sentences if s not in numbered]
        outcome = " ".join(numbered[:2])
        if len(plain) >= 2:
            problem, approach = plain[0], " ".join(plain[1:3])
        elif plain:
            problem = plain[0]

    prompts = []
    if not problem:
        prompts.append("What problem does this solve? Who has this problem?")
    if not approach:
        prompts.append("How did you build it? Key technical decisions?")
    if not outcome:
        prompts.append("What was the result? Add numbers: users, speed, scale.")
    if not tech:
        prompts.append("List the tech stack — recruiters filter by it.")
    if not project.get("demo_link"):
        prompts.append("Add a live demo link — it 10x's recruiter engagement.")

    body = []
    if problem:
        body.append(f"**Problem:** {problem}")
    if approach:
        body.append(f"**Approach:** {approach}")
    if outcome:
        body.append(f"**Outcome:** {outcome}")
    if role:
        body.append(f"**My role:** {role}")
    if tech:
        body.append(f"**Stack:** {', '.join(tech)}")

    completeness = round((5 - len(prompts)) / 5 * 100)
    return {"title": title, "sections": body, "markdown": "\n\n".join(body),
            "prompts": prompts, "completeness_pct": max(0, completeness),
            "verdict": ("Publish-ready case study." if not prompts
                        else f"{len(prompts)} gap(s) to fill before publishing.")}


def check_demo_links(projects, timeout=8):
    """Verify project demo/repo links are alive. Returns per-link status.

    SEC-02: URLs go through the SSRF-safe fetcher — private IPs, metadata
    endpoints and non-HTTP schemes are blocked before any connection opens.

    projects: list of {title, link, demo_link}. Never raises on network
    failure — a dead link is a result, not an exception.
    """
    from studybuddy.safe_fetch import SSRFError, safe_fetch, validate_url
    if not isinstance(projects, (list, tuple)):
        raise ValueError("projects must be a list.")
    results = []
    for p in projects:
        if not isinstance(p, dict):
            continue
        title = _clean4(p.get("title") or "Untitled")[:80]
        for key in ("demo_link", "link"):
            url = (p.get(key) or "").strip()
            if not url:
                continue
            if not _re4.match(r"^https?://", url, _re4.I):
                results.append({"title": title, "kind": key, "url": url[:120],
                                "alive": False, "status": "invalid URL"})
                continue
            # SSRF policy gate first — blocked URLs never touch the network.
            try:
                validate_url(url)
            except SSRFError as e:
                results.append({"title": title, "kind": key, "url": url[:120],
                                "alive": False,
                                "status": f"blocked (SSRF policy: {e})"})
                continue
            try:
                code, _body, _hdrs = safe_fetch(
                    url, method="HEAD", timeout=timeout, max_bytes=65536)
                alive = 200 <= code < 400
                results.append({"title": title, "kind": key, "url": url[:120],
                                "alive": alive, "status": f"HTTP {code}"})
            except SSRFError:
                # HEAD may be blocked; try GET as fallback once.
                try:
                    code, _body, _hdrs = safe_fetch(
                        url, method="GET", timeout=timeout, max_bytes=65536)
                    alive = 200 <= code < 400
                    results.append({"title": title, "kind": key,
                                    "url": url[:120],
                                    "alive": alive,
                                    "status": f"HTTP {code} (GET)"})
                except SSRFError as e2:
                    results.append({"title": title, "kind": key,
                                    "url": url[:120], "alive": False,
                                    "status": f"unreachable ({e2})"})
    dead = [r for r in results if not r["alive"]]
    return {"links": results, "checked": len(results),
            "alive_count": len(results) - len(dead),
            "dead": dead,
            "verdict": ("All links alive." if results and not dead else
                        f"{len(dead)}/{len(results)} links dead — fix before sharing."
                        if results else "No links to check.")}


def recruiter_preview(portfolio):
    """30-second scan mode: what a hiring manager actually absorbs.

    portfolio: {display_name, headline, skills (list), projects (list of {title,
    description, demo_link})}. Returns {scan: [...], score, fixes}.
    """
    if not isinstance(portfolio, dict):
        raise ValueError("portfolio must be a dict.")
    name = _clean4(portfolio.get("display_name") or "").strip()
    headline = _clean4(portfolio.get("headline") or "").strip()
    skills = [s for s in (portfolio.get("skills") or []) if isinstance(s, str)][:20]
    projects = [p for p in (portfolio.get("projects") or []) if isinstance(p, dict)][:10]

    scan = []
    score = 0
    fixes = []
    # 0-5s: name + headline.
    if name and headline:
        scan.append(f"0-5s: {name} — {headline[:80]}")
        score += 20
    else:
        fixes.append("Add a headline — recruiters decide in 5 seconds.")
        scan.append("0-5s: (no headline — recruiter bounces)")
    # 5-15s: skills scan.
    if len(skills) >= 5:
        scan.append(f"5-15s: Skills spotted: {', '.join(skills[:8])}")
        score += 20
    else:
        fixes.append(f"Only {len(skills)} skills listed — aim for 8+.")
        scan.append(f"5-15s: Skills look thin ({len(skills)} listed)")
    # 15-30s: top project.
    if projects:
        top = projects[0]
        t = _clean4(top.get("title") or "Untitled")[:60]
        has_demo = bool((top.get("demo_link") or "").strip())
        has_num = bool(_re4.search(r"\d", _clean4(top.get("description") or "")))
        scan.append(f"15-30s: Top project '{t}'" +
                    (" [has demo]" if has_demo else " [no demo]") +
                    (" [quantified]" if has_num else " [no numbers]"))
        score += 20
        if has_demo:
            score += 10
        else:
            fixes.append("Top project has no demo link — add one.")
        if has_num:
            score += 10
        else:
            fixes.append("Top project has no numbers — quantify impact.")
    else:
        fixes.append("No projects — a portfolio without projects is just a bio.")
        scan.append("15-30s: (no projects — recruiter leaves)")
    # Bonus: project count.
    if len(projects) >= 3:
        score += 10
        scan.append(f"Bonus: {len(projects)} projects — good depth.")
    score = min(100, score)
    return {"scan": scan, "score": score, "fixes": fixes[:4],
            "grade": ("A — 30-second win" if score >= 80 else
                      "B — solid" if score >= 60 else
                      "C — leaks attention" if score >= 40 else
                      "D — recruiter bounced")}
