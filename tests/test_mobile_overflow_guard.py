"""Audit Stage 3 (UX-01): shared mobile-overflow guard + 390px fixes.

Static locks for the guard added to templates/base.html (inherited by
every base-extending page) and to every standalone full-page template,
plus the roadmaps 3-column grid fix found by the 390px evaluation.
The live-browser evidence lives in
hidden_work/overflow-390px-20261028.md (before/after JSON reports
beside it); these tests lock the guard against accidental removal.
"""

import glob
import os
import re

TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "templates")

GUARD_RULES = [
    "img, table, video, iframe, embed, object{max-width:100%}",
    "pre{max-width:100%;overflow-x:auto}",
    "html, body{overflow-x:clip}",
]


def _read(name):
    with open(os.path.join(TEMPLATES, name), encoding="utf-8") as f:
        return f.read()


def test_guard_present_in_base_template():
    base = _read("base.html")
    for rule in GUARD_RULES:
        assert rule in base


def test_guard_present_in_every_standalone_page():
    missing = []
    for path in glob.glob(os.path.join(TEMPLATES, "*.html")):
        text = open(path, encoding="utf-8").read()
        if "<html" not in text or '{% extends "base.html" %}' in text:
            continue  # base-extending templates inherit the guard
        if not all(rule in text for rule in GUARD_RULES):
            missing.append(os.path.basename(path))
    assert missing == []


def test_no_hardcoded_html_lang():
    hardcoded = []
    for path in glob.glob(os.path.join(TEMPLATES, "*.html")):
        text = open(path, encoding="utf-8").read()
        if re.search(r'<html[^>]*lang="en"', text):
            hardcoded.append(os.path.basename(path))
    assert hardcoded == []


def test_roadmaps_skill_grid_collapses_on_narrow_screens():
    roadmaps = _read("roadmaps.html")
    assert "gap-skills-grid" in roadmaps
    assert "@media (max-width:640px){.gap-skills-grid{grid-template-columns:1fr}}" in roadmaps
    # The old hardcoded 3-column inline style must be gone from the grid.
    assert 'style="grid-template-columns:repeat(3,1fr);"' not in roadmaps
