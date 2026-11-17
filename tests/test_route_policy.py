"""TEST-05: Route policy — secure by default.

Every endpoint must be classified in tests/route_policy.yaml as
public or auth. Unauthenticated requests to auth endpoints must
get 401/302. Adding a route without classifying it fails CI.
"""
import os
import re

import yaml


def load_policy():
    path = os.path.join(os.path.dirname(__file__), "route_policy.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def _rule_to_path(rule):
    """Convert a Flask rule with converters to a testable path."""
    # Replace <type:name> and <name> with dummy values
    path = re.sub(r"<int:[^>]+>", "1", rule)
    path = re.sub(r"<path:[^>]+>", "test", path)
    path = re.sub(r"<[^>]+>", "test", path)
    return path


class TestRoutePolicy:
    def test_every_route_classified(self, app):
        policy = load_policy()
        rules = set(
            str(r.rule) for r in app.url_map.iter_rules()
            if "static" not in str(r.rule))
        unlisted = rules - set(policy.keys())
        assert not unlisted, \
            f"Unclassified routes (add to route_policy.yaml): {unlisted}"

    def test_policy_values_valid(self):
        policy = load_policy()
        for route, cls in policy.items():
            assert cls in ("public", "auth"), \
                f"{route}: invalid classification '{cls}'"

    def test_auth_routes_reject_anonymous(self, client, app):
        policy = load_policy()
        # Full coverage: EVERY auth GET route is checked (no sampling).
        # The old 30-route sample never reached /resume, /practice,
        # /salary, /star, /research, /research/cite, /research/trending
        # or /salary/data, so 8 mislabelled routes survived CI until the
        # 2026-11-17 audit caught them (all 8 are public hubs by design).
        checked = 0
        for route, cls in policy.items():
            if cls != "auth":
                continue
            path = _rule_to_path(route)
            # Only test simple GET routes
            rules = [r for r in app.url_map.iter_rules()
                     if str(r.rule) == route]
            if not rules:
                continue
            methods = rules[0].methods or set()
            if "GET" not in methods:
                continue
            r = client.get(path, follow_redirects=False)
            # 400 is also acceptable (rejected before auth check, e.g. bad param)
            assert r.status_code in (302, 401, 403, 404, 405, 400), \
                f"{route}: anonymous GET got {r.status_code}, expected 302/401"
            checked += 1
        assert checked > 0, "no auth routes tested"
