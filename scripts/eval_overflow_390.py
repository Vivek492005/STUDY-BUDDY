#!/usr/bin/env python3
"""390px overflow evaluation (audit UX-01, Stage 3).

Loads public hub pages at a 390px viewport in headless Chromium and
reports, per page: HTTP status, document scrollWidth vs viewport
width, and the elements (tag + class/id) whose boxes extend past the
viewport. Used to evaluate the global overflow guard in
templates/base.html — run once before the guard and once after, and
compare the two JSON reports.

Requires a running server (the app's dev server is fine):

    PORT=5001 .venv/bin/python unified_app.py &
    .venv/bin/python scripts/eval_overflow_390.py \
        --base http://127.0.0.1:5001 \
        --out hidden_work/overflow-390px-after.json

Needs Playwright Chromium (see tests/e2e/ setup). Pages that do not
return 200 are recorded and skipped, not failed — this is an
evaluation report, not a pass/fail gate.
"""

from __future__ import annotations

import argparse
import json

PAGES = [
    "/",
    "/dsa",
    "/dsa/a2z",
    "/aptitude",
    "/try",
    "/mentors",
    "/salary",
    "/roadmaps",
    "/quizzes",
    "/practice",
    "/learn",
    "/research",
    "/ultimate/lab",
    "/opportunities",
    "/resume",
    "/interview",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:5001")
    parser.add_argument("--out", required=True)
    parser.add_argument("--width", type=int, default=390)
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    report = {"viewport_width": args.width, "pages": []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": args.width,
                                          "height": 844})
        for path in PAGES:
            url = args.base + path
            try:
                resp = page.goto(url, wait_until="networkidle",
                                 timeout=60000)
                status = resp.status if resp else None
            except Exception as exc:  # noqa: BLE001 — report, don't crash
                report["pages"].append({"path": path, "error": str(exc)})
                continue
            if status != 200:
                report["pages"].append({"path": path, "status": status})
                continue
            page.wait_for_timeout(1000)  # let JS layout settle
            data = page.evaluate("""() => {
                const vw = window.innerWidth;
                const offenders = [];
                for (const el of document.querySelectorAll('body *')) {
                    const r = el.getBoundingClientRect();
                    if (r.right > vw + 1 && r.width > 0) {
                        offenders.push({
                            tag: el.tagName.toLowerCase(),
                            cls: (el.className && el.className.baseVal !== undefined)
                                ? String(el.className) : (el.className || ''),
                            id: el.id || '',
                            right: Math.round(r.right),
                            width: Math.round(r.width),
                        });
                        if (offenders.length >= 10) break;
                    }
                }
                return {
                    scrollWidth: document.documentElement.scrollWidth,
                    bodyScrollWidth: document.body ? document.body.scrollWidth : null,
                    offenders: offenders,
                };
            }""")
            data["path"] = path
            data["status"] = status
            data["overflows"] = data["scrollWidth"] > args.width + 1
            report["pages"].append(data)
        browser.close()

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    n = len(report["pages"])
    bad = [p["path"] for p in report["pages"] if p.get("overflows")]
    print(f"{n} pages evaluated, {len(bad)} overflowing: {bad}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
