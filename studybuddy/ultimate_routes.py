"""Ultimate Lab API dispatcher: serves all 91 ultimate tools via JSON.

Endpoints (registered by register_ultimate_routes):
  GET  /api/ultimate/tools          -> metadata for every tool (id, feature, name, description, fields)
  POST /api/ultimate/run/<tool_id>  -> run a tool; JSON body = {field: value}; returns {ok, result} or {ok:false, error}
  GET  /ultimate/lab               -> the interactive dashboard page

SEC-01 (Phase 0): every endpoint requires a logged-in user. The two JSON
APIs return 401 for anonymous callers; the HTML Lab page uses the standard
@login_required redirect. The run endpoint is rate-limited and rejects
oversized bodies.
"""
import importlib
import json
import re
from datetime import date, datetime
from typing import get_origin

from flask import jsonify, render_template, request
from flask_login import current_user, login_required

from studybuddy.ultimate_reg_a import TOOLS_A
from studybuddy.ultimate_reg_b import TOOLS_B
from studybuddy.ultimate_reg_c import TOOLS_C

ALL_TOOLS = TOOLS_A + TOOLS_B + TOOLS_C
_TOOL_INDEX = {t["id"]: t for t in ALL_TOOLS}

# Roadmap SEC-01: hard cap on tool-run request bodies (Flask's global
# MAX_CONTENT_LENGTH is 16MB; tool payloads never legitimately need that).
_MAX_RUN_BODY = 64 * 1024  # 64 KiB

# Fields that arrive as YYYY-MM-DD strings but must be date objects.
_DATE_FIELDS = {"last_active", "date", "start_date", "end_date"}


def _jsonable(obj):
    """Recursively convert to JSON-safe types."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def _coerce_value(field, raw):
    """Apply field-type coercion: JSON parse, number cast, checkbox bool, dates."""
    ftype = field.get("type")
    name = field["name"]
    if raw is None or raw == "":
        return None
    if field.get("json"):
        if isinstance(raw, (dict, list)):
            return raw
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            raise ValueError(f"Field '{name}' must be valid JSON.")
    if ftype == "number":
        try:
            return int(raw) if "." not in str(raw) else float(raw)
        except (ValueError, TypeError):
            raise ValueError(f"Field '{name}' must be a number.")
    if ftype == "checkbox":
        return raw in (True, "true", "on", "1", 1)
    if name in _DATE_FIELDS and isinstance(raw, str):
        try:
            return date.fromisoformat(raw.strip()[:10])
        except ValueError:
            raise ValueError(f"Field '{name}' must be YYYY-MM-DD.")
    # "one per line" textareas -> list of strings
    if ftype == "textarea" and "one per line" in field.get("label", "").lower():
        if isinstance(raw, str):
            return [ln.strip() for ln in raw.splitlines() if ln.strip()]
    # comma-separated
    if ftype == "text" and "comma" in field.get("label", "").lower():
        if isinstance(raw, str):
            return [p.strip() for p in raw.split(",") if p.strip()]
    return raw


def _load_fn(tool):
    mod = importlib.import_module(tool["module"])
    fn = getattr(mod, tool["function"])
    return fn


# ---------------------------------------------------------------------------
# Structured-form schemas (P1 Labs UX).
#
# The Lab page used to render every tool from its legacy field defs, and
# every field marked json=True was a bare textarea with a "must be valid
# JSON" hint — 54 of 91 tools. The form schema below is ONE generic
# derivation, computed server-side from the sources of truth that already
# exist:
#   * migrated tools -> the pydantic input model in labs_registry
#     (field names, types, required-ness, defaults, constraints);
#   * legacy tools   -> their registry field defs, with the shape hints
#     already documented in the label text ("JSON with {a, b, c}",
#     "JSON array of {...}") parsed into keys + an example skeleton.
# The schema is descriptive only: it never changes what run_tool accepts.
# Payload keys are identical to the legacy/pydantic field names (verified
# equal for all 20 overlapping migrated tools), and both dispatchers
# already accept parsed values (lists/dicts) for JSON fields, so a form
# submission is exactly the payload the old raw-JSON textarea produced
# after JSON.parse — only the user no longer writes the JSON by hand.
# ---------------------------------------------------------------------------

_BRACE_RE = re.compile(r"\{([^{}]*)\}")


def _humanize(name):
    return name.replace("_", " ").strip().capitalize()


def _brace_keys(label):
    """Key names from a label's '{a, b, c}' shape note, or []."""
    m = _BRACE_RE.search(label or "")
    if not m:
        return []
    keys = []
    for part in m.group(1).split(","):
        key = part.split(":")[0].strip().strip("'\"")
        if key and key not in keys:
            keys.append(key)
    return keys


def _example_for(kind, keys):
    """Insertable JSON skeleton for a guided editor, or None."""
    if kind == "object" and keys:
        return json.dumps({k: "" for k in keys}, indent=2)
    if kind == "json" and keys:
        return json.dumps([{k: "" for k in keys}], indent=2)
    return None


def _constraint(metadata, cls, attr):
    for m in metadata:
        if isinstance(m, cls):
            return getattr(m, attr, None)
    return None


def _pydantic_form_fields(model, legacy_labels):
    """One schema field per pydantic model field."""
    from annotated_types import Ge, Le, MaxLen, MinLen
    from pydantic import BeforeValidator

    from studybuddy.labs_registry import (
        _as_json_list,
        _as_json_object,
        _as_json_value,
    )

    fields = []
    for name, finfo in model.model_fields.items():
        ann = finfo.annotation
        origin = get_origin(ann)
        validator = None
        for m in finfo.metadata:
            if isinstance(m, BeforeValidator):
                validator = m.func
                break
        if validator is _as_json_object:
            kind = "object"
        elif validator is _as_json_list:
            kind = "lines"
        elif validator is _as_json_value:
            kind = "json"
        elif ann is bool:
            kind = "boolean"
        elif ann is int:
            kind = "integer"
        elif ann is float:
            kind = "number"
        elif ann is str:
            maxlen = _constraint(finfo.metadata, MaxLen, "max_length")
            kind = "text" if (maxlen is None or maxlen > 500) else "string"
        elif ann is list or origin is list:
            kind = "lines"
        elif ann is dict or origin is dict:
            kind = "object"
        else:
            kind = "json"
        entry = {
            "name": name,
            "label": legacy_labels.get(name) or _humanize(name),
            "kind": kind,
            "required": bool(finfo.is_required()),
        }
        if kind in ("integer", "number"):
            ge = _constraint(finfo.metadata, Ge, "ge")
            le = _constraint(finfo.metadata, Le, "le")
            if ge is not None:
                entry["min"] = ge
            if le is not None:
                entry["max"] = le
        if kind in ("string", "text"):
            minlen = _constraint(finfo.metadata, MinLen, "min_length")
            maxlen = _constraint(finfo.metadata, MaxLen, "max_length")
            if minlen is not None:
                entry["min_length"] = minlen
            if maxlen is not None:
                entry["max_length"] = maxlen
        default = finfo.default
        if isinstance(default, (str, int, float, bool)):
            entry["default"] = default
        if kind in ("object", "json"):
            keys = _brace_keys(legacy_labels.get(name, ""))
            if keys:
                entry["keys"] = keys
                example = _example_for(kind, keys)
                if example is not None:
                    entry["example"] = example
        fields.append(entry)
    return fields


def _legacy_form_fields(tool):
    """Convert legacy registry field defs to the same schema shape."""
    out = []
    for f in tool.get("fields", []):
        name = f["name"]
        label = f.get("label") or _humanize(name)
        entry = {"name": name, "label": label,
                 "required": bool(f.get("required"))}
        if f.get("json"):
            keys = _brace_keys(label)
            if "array" in label.lower():
                entry["kind"] = "json"
                entry["hint"] = "JSON array"
                if keys:
                    entry["keys"] = keys
                    entry["example"] = json.dumps(
                        [{k: "" for k in keys}], indent=2)
            elif keys:
                entry["kind"] = "object"
                entry["keys"] = keys
                entry["example"] = json.dumps(
                    {k: "" for k in keys}, indent=2)
            else:
                entry["kind"] = "json"
                entry["hint"] = "JSON format"
        elif f.get("type") == "textarea":
            entry["kind"] = "text"
        elif f.get("type") == "number":
            entry["kind"] = "number"
        elif f.get("type") == "select":
            entry["kind"] = "enum"
            entry["options"] = [
                str(o.get("value", o.get("label")) if isinstance(o, dict)
                    else o)
                for o in (f.get("options") or [])
            ]
        elif f.get("type") == "checkbox":
            entry["kind"] = "boolean"
        else:
            entry["kind"] = "string"
        out.append(entry)
    return out


def form_schema(tool):
    """Structured-form schema for one tool (descriptive only).

    Returns {"source": "schema"|"legacy"|"none", "fields": [...]}.
    "schema" tools derive from the pydantic input model; "legacy" tools
    from their registry field defs; "none" tools take no inputs.
    """
    from studybuddy.labs_registry import TOOL_HANDLERS

    handler = TOOL_HANDLERS.get(tool["id"])
    if handler is not None:
        legacy_labels = {
            f["name"]: f.get("label") or ""
            for f in tool.get("fields", [])
        }
        return {"source": "schema",
                "fields": _pydantic_form_fields(handler.input_model,
                                                legacy_labels)}
    fields = _legacy_form_fields(tool)
    return {"source": "legacy" if fields else "none", "fields": fields}


def run_tool(tool_id, payload):
    """Execute one registered tool. Returns (result_dict, error_str)."""
    # ARC-05: migrated tools dispatch through the typed allowlist first
    # (pydantic-validated, no importlib/getattr). Legacy dict-dispatch below.
    from studybuddy.labs_registry import is_migrated, run_typed_tool
    if is_migrated(tool_id):
        return run_typed_tool(tool_id, payload)
    tool = _TOOL_INDEX.get(tool_id)
    if not tool:
        return None, f"Unknown tool '{tool_id}'."
    if not isinstance(payload, dict):
        return None, "Request body must be a JSON object."
    kwargs = {}
    try:
        for field in tool.get("fields", []):
            name = field["name"]
            raw = payload.get(name)
            if raw is None or raw == "":
                if field.get("required"):
                    return None, f"Field '{field.get('label', name)}' is required."
                continue
            kwargs[name] = _coerce_value(field, raw)
    except ValueError as e:
        return None, str(e)
    try:
        fn = _load_fn(tool)
        result = fn(**kwargs)
    except (ValueError, TypeError) as e:
        return None, str(e) or "Invalid input."
    except Exception as e:  # never leak tracebacks
        return None, f"Tool failed: {type(e).__name__}."
    return _jsonable(result), None


def public_tools():
    """Metadata for the frontend (no function refs).

    Each tool additionally carries its structured-form schema ("form"):
    an additive key — the tool count and every legacy field are unchanged.
    """
    out = []
    for t in ALL_TOOLS:
        d = {k: t[k] for k in ("id", "feature", "name", "description", "fields")}
        d["form"] = form_schema(t)
        out.append(d)
    return out


def _deny_anonymous():
    """401 JSON for anonymous API callers (SEC-01). None when logged in."""
    if not current_user.is_authenticated:
        return jsonify({"ok": False,
                        "error": "Login required to use the Ultimate Lab."}), 401
    return None


def _labs_disabled():
    """403 JSON when FEATURE_LABS=0 (ARC-12)."""
    from studybuddy.app_config import config
    if not config.feature_labs:
        return jsonify({"ok": False,
                        "error": "The Ultimate Lab is disabled."}), 403
    return None


def register_ultimate_routes(app, limiter=None):
    run_limit = app.config.get("ULTIMATE_RUN_LIMIT", "30 per hour")

    @app.route("/api/ultimate/tools", methods=["GET"])
    def _ultimate_tools():
        denied = _deny_anonymous()
        if denied:
            return denied
        disabled = _labs_disabled()
        if disabled:
            return disabled
        return jsonify({"ok": True, "count": len(ALL_TOOLS), "tools": public_tools()})

    @app.route("/api/ultimate/run/<path:tool_id>", methods=["POST"])
    def _ultimate_run(tool_id):
        denied = _deny_anonymous()
        if denied:
            return denied
        if request.content_length and request.content_length > _MAX_RUN_BODY:
            return jsonify({"ok": False,
                            "error": "Request body too large (max 64 KiB)."}), 413
        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify({"ok": False, "error": "Send a JSON body."}), 400
        result, error = run_tool(tool_id, payload)
        if error:
            return jsonify({"ok": False, "error": error}), 400
        return jsonify({"ok": True, "result": result})

    if limiter is not None:
        # Rate-limit the (already auth-gated) run endpoint.
        app.view_functions["_ultimate_run"] = limiter.limit(run_limit)(
            app.view_functions["_ultimate_run"])

    @app.route("/ultimate/lab", methods=["GET"])
    @login_required
    def _ultimate_lab():
        return render_template("ultimate_lab.html")
