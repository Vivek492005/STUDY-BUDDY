"""SEO endpoints (P1, Eval 4): /robots.txt + /sitemap.xml.

Both were 404. The sitemap is built from the live url_map — public GET
pages with no URL variables — so new public pages appear automatically.
Login-gated pages must NOT be listed (a sitemap full of 302s to /login
is worse than a short one); the detector is pinned here via the
anonymous-200 check on every listed URL.
"""

import xml.etree.ElementTree as ET

# Public pages the sitemap must list (spot-check, not exhaustive — the
# anonymous-200 check below covers every listed URL).
EXPECTED_PUBLIC = [
    "/", "/learn", "/learn/roadmaps", "/learn/quizzes", "/learn/certs",
    "/dsa", "/dsa/a2z", "/research", "/research/cite",
    "/research/trending", "/privacy", "/terms", "/accessibility",
    "/try", "/opportunities", "/interview", "/aptitude", "/coach",
]

# Login-gated pages that must NOT be listed. NOTE: /resume renders 200
# for anonymous users (despite route_policy.yaml labelling it auth —
# that label is stale; the policy test only samples the first 30 auth
# routes, so it never checks /resume), so it is NOT gated here and
# belongs in the sitemap.
GATED = ["/dashboard", "/research/flashcards",
         "/research/review", "/ultimate/lab", "/tracker", "/mentors",
         "/profile"]


def _sitemap_locs(client):
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert "application/xml" in r.content_type
    root = ET.fromstring(r.get_data(as_text=True))
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    return [el.text for el in root.findall("sm:url/sm:loc", ns)]


def test_robots_txt(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "text/plain" in r.content_type
    body = r.get_data(as_text=True)
    assert "User-agent: *" in body
    assert "Disallow: /api/" in body
    assert "Disallow: /account/" in body
    assert "Sitemap: http://localhost/sitemap.xml" in body


def test_sitemap_lists_public_pages(client):
    locs = _sitemap_locs(client)
    paths = [loc.replace("http://localhost", "") for loc in locs]
    for p in EXPECTED_PUBLIC:
        assert p in paths, f"{p} missing from sitemap"
    for p in GATED:
        assert p not in paths, f"login-gated {p} must not be in sitemap"
    # No API, static, ops or parameterized URLs.
    assert not any("/api/" in p or "/static/" in p for p in paths)
    assert not any(p in ("/health", "/healthz", "/readyz", "/version",
                         "/robots.txt", "/sitemap.xml")
                    for p in paths)
    assert not any("<" in p for p in paths)


def test_sitemap_urls_all_render_anonymous(client):
    # Every listed URL must answer 200 for an anonymous visitor — the
    # pin that keeps login-gated pages (302 to /login) out of the
    # sitemap even if the decorator detector ever goes stale.
    for loc in _sitemap_locs(client):
        path = loc.replace("http://localhost", "")
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
