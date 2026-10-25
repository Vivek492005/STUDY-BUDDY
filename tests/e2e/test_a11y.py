"""TEST-08: accessibility checks via Playwright (no axe-core dependency).

Checks on key pages (public + authenticated):
  1. <html lang> is set
  2. document has a non-empty <title>
  3. every <img> has an alt attribute
  4. every form control has a programmatic label
     (label[for], wrapping <label>, aria-label, or aria-labelledby)
  5. every <button> has an accessible name
  6. exactly one <h1> per page
  7. body text/background contrast ratio >= 4.5 (WCAG AA)

Known gaps are pinned in KNOWN_GAPS: the test FAILS if any NEW
violation appears, so regressions are caught while the current baseline
stays green. Fixing the pinned gaps = delete the entry.

Baseline measured 2026-10-19 (local, Chromium headless):
  * contrast 15.7-19.3 on all checked pages (excellent)
  * known gaps (both fixed 2026-10-25, entries deleted): /resume file
    input had no programmatic label (templates/resume_index.html
    #fileInput); dashboard goal title input was placeholder-only
    (templates/history.html)
axe-core via npx is documented as a follow-up in docs/testing/a11y.md.
"""
import re
import uuid

import pytest

pytest.importorskip("playwright")

# (page path, control identifier) pairs with a documented a11y gap.
# Format: (path, kind, identifier)
# Empty since the 2026-10-25 a11y sweep (commit 4b8ea3d): both pinned gaps
# were fixed there — /resume #fileInput gained aria-label="Upload your
# resume", and the dashboard goal title field gained a programmatic label.
# Per this file's contract, fixing a gap = delete its entry; an empty set
# keeps test_known_gaps_still_documented green and the suite still fails
# on any NEW unlabeled control.
KNOWN_GAPS = set()

PAGES_PUBLIC = ["/", "/login", "/register", "/resume"]
PAGES_AUTH = ["/dashboard", "/tracker"]

MIN_CONTRAST = 4.5


def _is_labeled(el_info):
    return el_info["labeled"]


def _collect(page):
    return page.evaluate("""() => {
        const inputs = Array.from(document.querySelectorAll('input')).filter(
            el => el.type !== 'hidden').map(el => {
                const id = el.id;
                const labeled = !!(el.closest('label')
                    || (id && document.querySelector(`label[for="${CSS.escape(id)}"]`))
                    || el.getAttribute('aria-label')
                    || el.getAttribute('aria-labelledby'));
                return {name: el.name || el.type, id: id, labeled};
            });
        const buttons = Array.from(document.querySelectorAll('button')).map(el => ({
            name: (el.innerText || '').trim().slice(0, 40) || el.getAttribute('aria-label') || '(unnamed)',
            named: !!((el.innerText || '').trim() || el.getAttribute('aria-label')
                      || el.getAttribute('aria-labelledby')),
        }));
        const cs = getComputedStyle(document.body);
        return {
            lang: document.documentElement.getAttribute('lang'),
            title: document.title,
            imgs_no_alt: Array.from(document.images).filter(i => !i.hasAttribute('alt')).length,
            h1_count: document.querySelectorAll('h1').length,
            inputs, buttons,
            body_fg: cs.color, body_bg: cs.backgroundColor,
        };
    }""")


def _parse_rgb(s):
    m = re.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", s or "")
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def _luminance(rgb):
    def chan(c):
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def _contrast(fg, bg):
    l1, l2 = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


def _check_page(page, base, path):
    page.goto(f"{base}{path}")
    data = _collect(page)
    violations = []

    if not data["lang"]:
        violations.append("missing <html lang>")
    if not (data["title"] or "").strip():
        violations.append("missing <title>")
    if data["imgs_no_alt"]:
        violations.append(f"{data['imgs_no_alt']} <img> without alt")
    if data["h1_count"] != 1:
        violations.append(f"expected 1 <h1>, found {data['h1_count']}")

    for inp in data["inputs"]:
        if not inp["labeled"] and (path, "input", inp["name"]) not in KNOWN_GAPS:
            violations.append(f"unlabeled input: name={inp['name']!r} id={inp['id']!r}")
    for btn in data["buttons"]:
        if not btn["named"]:
            violations.append(f"unnamed <button>: {btn['name']!r}")

    fg = _parse_rgb(data["body_fg"])
    bg = _parse_rgb(data["body_bg"])
    ratio = _contrast(fg, bg) if fg and bg else None
    if ratio is None:
        violations.append("could not compute body contrast")
    elif ratio < MIN_CONTRAST:
        violations.append(f"body contrast {ratio:.2f} < {MIN_CONTRAST}")

    return violations, ratio


def _signup_and_login(page, base):
    email = f"a11y-{uuid.uuid4().hex[:8]}@example.com"
    page.goto(f"{base}/register")
    page.fill("#name", "A11y User")
    page.fill("#email", email)
    page.fill("#password", "<redacted>")
    page.check('input[name="age_confirm"]')
    page.click('button[type="submit"]')
    page.wait_for_url("**/dashboard**", timeout=15000)


def test_a11y_public_pages(page, live_server):
    failures = {}
    contrasts = {}
    for path in PAGES_PUBLIC:
        violations, ratio = _check_page(page, live_server, path)
        contrasts[path] = round(ratio, 2) if ratio else None
        if violations:
            failures[path] = violations
    print(f"\n[a11y] body contrast ratios: {contrasts}")
    assert not failures, f"a11y violations: {failures}"


def test_a11y_authenticated_pages(page, live_server):
    _signup_and_login(page, live_server)
    failures = {}
    for path in PAGES_AUTH:
        violations, _ = _check_page(page, live_server, path)
        if violations:
            failures[path] = violations
    assert not failures, f"a11y violations: {failures}"


def test_known_gaps_still_documented(page, live_server):
    """Every KNOWN_GAPS entry must still be a real gap (no stale entries)."""
    authed = {p for p, _, _ in KNOWN_GAPS if p in PAGES_AUTH}
    if authed:
        _signup_and_login(page, live_server)
    for path, kind, identifier in KNOWN_GAPS:
        page.goto(f"{live_server}{path}")
        data = _collect(page)
        if kind == "input":
            matches = [i for i in data["inputs"] if i["name"] == identifier]
            assert matches, f"known gap vanished from page: {(path, kind, identifier)}"
            assert not any(i["labeled"] for i in matches), \
                f"known gap was fixed but not removed from KNOWN_GAPS: {(path, kind, identifier)}"
