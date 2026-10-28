"""Render config regression (audit SEC-03, 2026-10-28).

render.yaml once declared envVars twice; in YAML the last block wins,
which silently dropped the PYTHON_VERSION pin — the exact 3.10-vs-3.12
mismatch that once crashed every deploy. These tests parse render.yaml
with a duplicate-key-strict loader so the failure can never return
silently, and assert the pin and the database wiring are present.
"""

import yaml

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that fails on duplicate mapping keys."""


def _no_duplicates(loader, node):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise yaml.YAMLError(f"duplicate key in render.yaml: {key!r}")
        seen.add(key)
    return loader.construct_mapping(node, deep=True)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


def _load():
    with open(ROOT / "render.yaml", encoding="utf-8") as f:
        return yaml.load(f, Loader=_StrictLoader)


def test_render_yaml_has_no_duplicate_keys():
    """The SEC-03 failure mode: duplicate keys must fail loudly."""
    _load()  # raises yaml.YAMLError on any duplicate key


def test_render_yaml_single_envvars_with_python_pin():
    cfg = _load()
    services = cfg["services"]
    assert len(services) == 1
    env_vars = services[0]["envVars"]
    keys = [e["key"] for e in env_vars]
    assert len(keys) == len(set(keys)), f"duplicate envVars: {keys}"
    by_key = {e["key"]: e for e in env_vars}
    assert by_key["PYTHON_VERSION"]["value"] == "3.12.0"
    assert "SECRET_KEY" in by_key
    assert "DATABASE_URL" in by_key


def test_render_yaml_uses_gunicorn_conf():
    """Single source of truth (audit fix, 2026-10-28): worker/timeout
    flags live only in gunicorn.conf.py. An inline --workers/--timeout
    here would silently disagree with the conf file again."""
    cfg = _load()
    start = cfg["services"][0]["startCommand"]
    assert start == "gunicorn -c gunicorn.conf.py unified_app:app", start


def test_dockerfile_uses_gunicorn_conf():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "gunicorn.conf.py" in text
    # Quoted flags would only appear in the JSON CMD array (the
    # HEALTHCHECK's --timeout=10s is unrelated and must not trip this).
    assert '"--workers"' not in text and '"--timeout"' not in text


def test_render_yaml_database_block_present():
    """The acra-db block is the live wiring until the Neon cutover
    documented in docs/ops/neon-database-decision.md is verified.
    Removing it without that cutover breaks production deploys."""
    cfg = _load()
    dbs = {d["name"] for d in cfg.get("databases", [])}
    assert "acra-db" in dbs
