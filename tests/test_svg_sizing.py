"""Regression tests: no unsized SVG icons (the giant-clipboard class of bug).

Every <svg> rendered in templates or JS-injected HTML must either carry
width=/height= attributes or sit inside a wrapper class that CSS constrains.
A bare <svg> with only a viewBox defaults to 300x150px and blows up layouts.
"""
import glob
import re
from html.parser import HTMLParser

import pytest

TEMPLATES = sorted(glob.glob("templates/*.html"))
JS_FILES = sorted(glob.glob("static/js/*.js"))
CSS_FILES = sorted(glob.glob("static/css/*.css"))

# Wrapper classes verified (by test_sized_wrappers_have_css_rules) to have a
# CSS rule sizing svg inside them.
SIZED_WRAPPERS = {
    "b-brand",        # .b-brand svg { width:30px; height:30px }
    "card-icon",      # .card-icon svg { width:1.4em; height:1.4em }
    "feature-icon",   # .feature-icon svg { width:2rem/2.1rem }
    "upload-icon",    # .upload-icon svg { width:2rem/2.1rem }
    "empty-icon",     # .empty-icon svg { width:2.1rem }
    "career-icon",    # .career-icon svg { width:1.4em }
    "feedback-icon",  # .feedback-icon svg { width:1.4em }
    "step-icon",      # .step-icon svg { width:1.4em }
    "sb-side-nav",    # .sb-side-nav a svg { width:19px; height:19px }
    "sb-side-foot",   # .sb-side-foot a svg { width:19px; height:19px }
    "file-icon",      # .file-icon svg
    "brand-icon",     # .brand-icon svg (resume_style)
    "score-ring",     # .score-ring svg { width:200px; height:200px }
    "avatar",         # .message .avatar svg { width:18px; height:18px }
    "b-ring",         # .b-ring { width:210px; height:210px }
    "research-wave",  # .research-wave { width:100%; height:200px }
    "b-dropbtn",      # .b-dropbtn svg { width:12px; height:12px } (nav chevrons)
}


class _SvgAudit(HTMLParser):
    """Collect <svg> tags that are not provably sized."""

    def __init__(self, css_text):
        super().__init__(convert_charrefs=False)
        self.css_text = css_text
        self.stack = []  # open elements: list of (tag, class_tokens, id)
        self.problems = []
        self.in_link_icon = False

    def _record(self, tag, attrs, line):
        attrs = dict(attrs)
        if "width" in attrs or "height" in attrs:
            return
        own_classes = set((attrs.get("class") or "").split())
        if own_classes & SIZED_WRAPPERS:
            return
        for _t, classes, anc_id in reversed(self.stack):
            if classes & SIZED_WRAPPERS:
                return
            if anc_id and re.search(
                    r"#" + re.escape(anc_id) + r"[^\{]*\{[^}]*width", self.css_text):
                return
        el_id = attrs.get("id")
        if el_id and re.search(
                r"#" + re.escape(el_id) + r"[^\{]*\{[^}]*width", self.css_text):
            return
        self.problems.append((line, "<svg>"))

    def handle_starttag(self, tag, attrs):
        if tag == "link" and dict(attrs).get("rel") == "icon":
            self.in_link_icon = True
            return
        if tag == "svg":
            if not self.in_link_icon:
                self._record(tag, attrs, self.getpos()[0])
            self.stack.append((tag, set(), None))  # keep nesting balanced
            return
        d = dict(attrs)
        self.stack.append((tag, set((d.get("class") or "").split()), d.get("id")))

    def handle_startendtag(self, tag, attrs):
        if tag == "svg":
            self._record(tag, attrs, self.getpos()[0])
        # self-closing: no stack push

    def handle_endtag(self, tag):
        if tag == "link":
            self.in_link_icon = False
        # pop the nearest matching open tag
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def _css_text():
    return "\n".join(open(f, encoding="utf-8").read() for f in CSS_FILES)


def _svg_problems(source, css):
    parser = _SvgAudit(css)
    try:
        parser.feed(source)
    except Exception:
        # A parse error must not hide real problems: fall back to regex scan
        # that flags every bare svg.
        for m in re.finditer(r"<svg\b[^>]*>", source):
            tag = m.group(0)
            if "width=" not in tag and "height=" not in tag:
                parser.problems.append(
                    (source[: m.start()].count("\n") + 1, tag[:60]))
    return parser.problems


def test_sized_wrappers_have_css_rules():
    css = _css_text()
    missing = []
    for cls in sorted(SIZED_WRAPPERS):
        if not re.search(r"\." + re.escape(cls) + r"[^\{]*\{[^}]*width", css, re.S):
            missing.append(cls)
    assert not missing, "sized wrappers lacking a width rule in CSS: %s" % missing


@pytest.mark.parametrize("path", TEMPLATES)
def test_templates_have_no_unsized_svg(path):
    css = _css_text()
    source = open(path, encoding="utf-8").read()
    problems = _svg_problems(source, css)
    assert not problems, "%s has unsized <svg> at lines: %s" % (
        path, [ln for ln, _ in problems])


@pytest.mark.parametrize("path", JS_FILES)
def test_js_html_has_no_unsized_svg(path):
    # JS string literals holding svg markup: scan the raw text for svg tags
    # inside template strings and require width/height or a sized wrapper
    # class nearby.
    css = _css_text()
    source = open(path, encoding="utf-8").read()
    problems = []
    for m in re.finditer(r"<svg\b[^>]*>", source):
        tag = m.group(0)
        if "width=" in tag or "height=" in tag:
            continue
        line_no = source[: m.start()].count("\n") + 1
        window = source[max(0, m.start() - 300): m.start()]
        tokens = set()
        for cm in re.finditer(r'class="([^"]*)"', window):
            tokens |= set(cm.group(1).split())
        for cm in re.finditer(r"class:\\?'([^']*)'", window):
            tokens |= set(cm.group(1).split())
        if tokens & SIZED_WRAPPERS:
            continue
        idm = re.search(r'id="([^"]*)"', tag)
        if idm and re.search(r"#" + re.escape(idm.group(1)) + r"[^\{]*\{[^}]*width", css):
            continue
        # createElementNS-built svg trees (study_import.js) are sized via
        # #graph-svg CSS; the literal '<svg' never appears for them.
        problems.append((line_no, tag[:60]))
    assert not problems, "%s has unsized <svg> at: %s" % (path, problems)


def test_dashboard_score_breakdown_icon_is_wrapped():
    """The exact reported bug: Score Breakdown's clipboard svg must be sized."""
    src = open("templates/dashboard.html", encoding="utf-8").read()
    m = re.search(r"Score Breakdown</h3>", src)
    assert m, "Score Breakdown heading missing"
    before = src[max(0, m.start() - 400): m.start()]
    assert 'class="card-icon"' in before, "Score Breakdown svg is not wrapped in .card-icon"
