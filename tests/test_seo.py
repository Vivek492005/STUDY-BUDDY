"""SEO endpoints (P1, Eval 4): /robots.txt + /sitemap.xml.

Both were 404. The sitemap is built from the live url_map — public GET
pages — plus public detail pages (DSA problems, A2Z lessons, certs,
roadmaps, aptitude practice topics) enumerated from the data loaders,
so new public pages appear automatically. Login-gated pages must NOT
be listed (a sitemap full of 302s to /login is worse than a short
one); the detector is pinned here via the anonymous-200 check on
every listed URL.
"""

import json
import re
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
    # Auth utility pages are not content and must not be listed.
    assert "/forgot-password" not in paths
    # OAuth utility routes (registered only when Google is configured)
    # and the JSON salary feed are not pages — never list them.
    assert "/salary/data" not in paths
    assert not any(p.startswith("/auth/") for p in paths)
    # No API, static, ops or parameterized URLs.
    assert not any("/api/" in p or "/static/" in p for p in paths)
    assert not any(p in ("/health", "/healthz", "/readyz", "/version",
                         "/robots.txt", "/sitemap.xml")
                    for p in paths)
    assert not any("<" in p for p in paths)


def test_sitemap_lists_detail_pages(client):
    # Week 0 SEO: parameterized public detail pages, enumerated from
    # the data loaders. Floors (not exact counts) so data growth does
    # not break the test; the anonymous-200 test below pins that every
    # listed URL actually renders.
    locs = _sitemap_locs(client)
    paths = [loc.replace("http://localhost", "") for loc in locs]
    assert len(paths) > 350, f"sitemap too small: {len(paths)} URLs"
    dsa = [p for p in paths
           if p.startswith("/dsa/") and "/a2z/" not in p]
    assert len(dsa) >= 132, f"DSA problems in sitemap: {len(dsa)}"
    a2z = [p for p in paths
           if p.startswith("/dsa/a2z/") and p.endswith("/learn")]
    assert len(a2z) >= 60, f"A2Z lessons in sitemap: {len(a2z)}"
    certs = [p for p in paths if p.startswith("/learn/certs/")]
    assert len(certs) >= 110, f"certs in sitemap: {len(certs)}"
    roadmaps = [p for p in paths if p.startswith("/learn/roadmaps/")]
    assert len(roadmaps) >= 30, f"roadmaps in sitemap: {len(roadmaps)}"
    apt = [p for p in paths if p.startswith("/aptitude/practice/")]
    assert len(apt) >= 40, f"aptitude topics in sitemap: {len(apt)}"
    for p in ["/dsa/dsa-arrays-01", "/dsa/a2z/a2z-0001/learn",
              "/learn/certs/aws-clf", "/learn/roadmaps/python",
              "/aptitude/practice/logical/analogies"]:
        assert p in paths, f"{p} missing from sitemap"
    # No duplicates.
    assert len(paths) == len(set(paths))


def test_sitemap_urls_all_render_anonymous(client):
    # Every listed URL must answer 200 for an anonymous visitor — the
    # pin that keeps login-gated pages (302 to /login) out of the
    # sitemap even if the decorator detector ever goes stale.
    for loc in _sitemap_locs(client):
        path = loc.replace("http://localhost", "")
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"


# ---------- Canonical, JSON-LD, OG, hero (Week 0 SEO) ----------

def _canonical(body):
    m = re.search(r'<link rel="canonical" href="([^"]+)">', body)
    assert m, "no canonical link on page"
    return m.group(1)


def test_canonical_on_hub_and_detail_pages(client):
    # Every page carries its own absolute URL as canonical, with the
    # query string stripped (request.base_url).
    cases = [
        ("/", "http://localhost/"),
        ("/learn", "http://localhost/learn"),
        ("/learn?foo=bar", "http://localhost/learn"),
        ("/dsa/dsa-arrays-01", "http://localhost/dsa/dsa-arrays-01"),
        ("/dsa/a2z/a2z-0001/learn",
         "http://localhost/dsa/a2z/a2z-0001/learn"),
        ("/aptitude/practice/logical/analogies",
         "http://localhost/aptitude/practice/logical/analogies"),
        ("/learn/certs/aws-clf", "http://localhost/learn/certs/aws-clf"),
        ("/learn/roadmaps/python",
         "http://localhost/learn/roadmaps/python"),
    ]
    for path, expected in cases:
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        assert _canonical(r.get_data(as_text=True)) == expected, path


def test_homepage_json_ld(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    m = re.search(
        r'<script type="application/ld\+json"[^>]*>(.*?)</script>',
        body, re.S)
    assert m, "no JSON-LD block on homepage"
    data = json.loads(m.group(1))
    graph = data.get("@graph", [])
    types = {node.get("@type") for node in graph}
    assert {"SoftwareApplication", "FAQPage",
            "Organization", "WebSite"} <= types, types
    app = next(n for n in graph if n["@type"] == "SoftwareApplication")
    assert app["offers"]["price"] == "0"
    faq = next(n for n in graph if n["@type"] == "FAQPage")
    assert len(faq["mainEntity"]) >= 4
    for q in faq["mainEntity"]:
        assert q["name"] and q["acceptedAnswer"]["text"]


def test_homepage_og_tags(client):
    r = client.get("/")
    body = r.get_data(as_text=True)
    assert ("STUDY-BUDDY — Free resume, interview &amp; DSA prep "
            "for B.Tech students") in body
    assert "1,455 interview questions and 495 DSA problems" in body
    assert 'name="twitter:card" content="summary_large_image"' in body


def test_homepage_h1_new_message(client):
    r = client.get("/")
    body = r.get_data(as_text=True)
    m = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S)
    assert m, "no H1 on homepage"
    h1 = m.group(0)
    for line in ["Free resume,", "Interview,", "DSA prep", "For B.Tech"]:
        assert line in h1, f"H1 line missing: {line}"
    assert ("Free resume, interview and DSA prep "
            "for B.Tech students.") in h1
    assert "Get hired" not in h1
    # Kinetic system preserved: 4 masked lines with staggered delays.
    assert h1.count('class="sb-mask"') == 4
    assert "--sb-delay:90ms" in h1 and "--sb-delay:270ms" in h1


def test_try_is_primary_hero_cta(client):
    r = client.get("/")
    body = r.get_data(as_text=True)
    primary = '<a class="sb-btn sb-btn--primary" href="/try">'
    secondary = '<a class="sb-btn sb-btn--outline" href="/resume">'
    assert primary in body, "/try must be the primary hero CTA"
    assert secondary in body, "/resume stays as secondary CTA"
    assert body.index(primary) < body.index(secondary)
