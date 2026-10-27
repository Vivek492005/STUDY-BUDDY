"""Ultimate Lab blueprint: serves all 91 ultimate tools via JSON.

Endpoints (registered by register_ultimate_bp):
  GET  /api/ultimate/tools          -> metadata for every tool (id, feature, name, description, fields)
  POST /api/ultimate/run/<tool_id>  -> run a tool; JSON body = {field: value}; returns {ok, result} or {ok:false, error}
  GET  /ultimate/lab               -> the interactive dashboard page

SEC-01 (Phase 0): every endpoint requires a logged-in user. The two JSON
APIs return 401 for anonymous callers; the HTML Lab page uses the standard
@login_required redirect. The run endpoint is rate-limited and rejects
oversized bodies.

Monolith-split pattern (study_library.py precedent): this module is a
real Flask blueprint and MUST NOT import unified_app at top level.
Models are imported lazily inside functions.

Lab Phase 0: all 91 tools dispatch through the typed allowlist in
studybuddy/labs_registry.py. The legacy dict-dispatch branch
(_coerce_value / _load_fn / importlib) is deleted; ultimate_reg_a/b/c
remain the metadata source (id, feature, name, description, fields)
for the tools list and form labels.
"""
import hashlib
import json
import re
from datetime import date, datetime
from typing import get_origin

from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import current_user, login_required

ultimate_bp = Blueprint("ultimate", __name__)

from studybuddy.lab_paths import (PATHS, ROOM_OUTPUTS, build_rooms,
                                  intent_search, path_for_id, room_for_slug,
                                  step_for_id)
from studybuddy.ultimate_reg_a import TOOLS_A
from studybuddy.ultimate_reg_b import TOOLS_B
from studybuddy.ultimate_reg_c import TOOLS_C

ALL_TOOLS = TOOLS_A + TOOLS_B + TOOLS_C
_TOOL_INDEX = {t["id"]: t for t in ALL_TOOLS}

# Roadmap SEC-01: hard cap on tool-run request bodies (Flask's global
# MAX_CONTENT_LENGTH is 16MB; tool payloads never legitimately need that).
_MAX_RUN_BODY = 64 * 1024  # 64 KiB

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


# ---------------------------------------------------------------------------
# Structured-form schemas (P1 Labs UX, Lab Phase 0: all tools typed).
#
# The form schema is ONE generic derivation from each tool's pydantic
# input model in labs_registry (field names, types, required-ness,
# defaults, constraints), with the shape hints already documented in the
# legacy label text ("JSON with {a, b, c}", "JSON array of {...}")
# parsed into keys + an example skeleton for object/json fields.
# The schema is descriptive only: it never changes what run_tool accepts.
# A form submission is exactly the payload the old raw-JSON textarea
# produced after JSON.parse — only the user no longer writes the JSON
# by hand.
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
        _as_legacy_bool,
        _as_legacy_date,
        _as_legacy_number,
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
        elif validator is _as_legacy_number:
            kind = "number"
        elif validator is _as_legacy_bool:
            kind = "boolean"
        elif validator is _as_legacy_date:
            kind = "string"
        elif ann is bool:
            kind = "boolean"
        elif ann is int:
            kind = "integer"
        elif ann is float:
            kind = "number"
        elif ann is date:
            kind = "string"
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
        extra = finfo.json_schema_extra
        if isinstance(extra, dict) and extra.get("options"):
            # Generated select fields: membership is NOT validated (legacy
            # behaviour); the options only drive the dropdown renderer.
            entry["kind"] = "enum"
            entry["options"] = [str(o) for o in extra["options"]]
        fields.append(entry)
    return fields


def form_schema(tool):
    """Structured-form schema for one tool (descriptive only).

    Returns {"source": "schema"|"none", "fields": [...]}. Every grid tool
    has a pydantic input model (Lab Phase 0); "none" is the fallback
    for a tool with no handler at all.
    """
    from studybuddy.labs_registry import TOOL_HANDLERS

    handler = TOOL_HANDLERS.get(tool["id"])
    if handler is None:
        return {"source": "none", "fields": []}
    legacy_labels = {
        f["name"]: f.get("label") or ""
        for f in tool.get("fields", [])
    }
    return {"source": "schema",
            "fields": _pydantic_form_fields(handler.input_model,
                                            legacy_labels)}


def run_tool(tool_id, payload):
    """Execute one registered tool. Returns (result_dict, error_str).

    Lab Phase 0: every tool dispatches through the typed allowlist
    (pydantic-validated, no importlib/getattr). Unknown ids are rejected
    by run_typed_tool itself.
    """
    from studybuddy.labs_registry import run_typed_tool
    return run_typed_tool(tool_id, payload)


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


def _lab_user_key():
    """Rate-limit key: the logged-in user, else the remote IP."""
    if current_user.is_authenticated:
        return "lab-user:%s" % current_user.get_id()
    return "lab-ip:%s" % (request.remote_addr or "unknown")


def _lab_family_key():
    """Per-tool-family rate-limit key (feature of the requested tool).

    One heavy session in a single family can no longer lock the whole
    Lab: each family has its own bucket, and the global bucket bounds
    total volume. Unknown tool ids share an "unknown" bucket.
    """
    tool_id = (request.view_args or {}).get("tool_id", "")
    feature = _TOOL_INDEX.get(tool_id, {}).get("feature", "unknown")
    return "%s:family:%s" % (_lab_user_key(), feature)


@ultimate_bp.route("/api/ultimate/tools", methods=["GET"])
def _ultimate_tools():
    denied = _deny_anonymous()
    if denied:
        return denied
    disabled = _labs_disabled()
    if disabled:
        return disabled
    return jsonify({"ok": True, "count": len(ALL_TOOLS), "tools": public_tools()})


@ultimate_bp.route("/api/ultimate/run/<path:tool_id>", methods=["POST"])
def _ultimate_run(tool_id):
    denied = _deny_anonymous()
    if denied:
        return denied
    disabled = _labs_disabled()
    if disabled:
        return disabled
    if request.content_length and request.content_length > _MAX_RUN_BODY:
        return jsonify({"ok": False,
                        "error": "Request body too large (max 64 KiB)."}), 413
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"ok": False, "error": "Send a JSON body."}), 400
    result, error = run_tool(tool_id, payload)
    if error:
        return jsonify({"ok": False, "error": error}), 400
    labels = _tool_labels(tool_id)
    row = _record_run(current_user.id, tool_id, payload, result, labels)
    body = {"ok": True, "result": result, "labels": labels,
            "next_actions": _tool_next_actions(tool_id)}
    if row is not None:
        body["run_id"] = row.id
    return jsonify(body)


@ultimate_bp.route("/ultimate/lab", methods=["GET"])
@login_required
def _ultimate_lab():
    # Dynamic counts (Lab Phase 0): never hardcoded — the old template
    # said "31 features" when the registry has 30.
    features = {t["feature"] for t in ALL_TOOLS}
    done_by_path, _runs = _path_progress(current_user.id)
    lab_paths = []
    for path in PATHS:
        done = done_by_path.get(path["id"], set())
        lab_paths.append({"id": path["id"], "title": path["title"],
                          "tagline": path["tagline"],
                          "done": len(done), "total": len(path["steps"]),
                          "completed": len(done) >= len(path["steps"])})
    lab_rooms = [{"slug": room["slug"], "feature": room["feature"],
                  "tool_count": len(room["tools"])}
                 for room in build_rooms(ALL_TOOLS)]
    return render_template("ultimate_lab.html",
                           lab_tool_count=len(ALL_TOOLS),
                           lab_feature_count=len(features),
                           lab_paths=lab_paths, lab_rooms=lab_rooms)


# ---------------------------------------------------------------------------
# Lab Phase 0: honest labels, next actions, history (My Lab).
# ---------------------------------------------------------------------------

def _lab_models():
    """Lazy model imports (monolith-split rule: no top-level unified_app)."""
    from unified_app import (
        LabPathProgress,
        LabRun,
        LabSave,
        LabToolFavorite,
        db,
    )
    return LabRun, LabSave, LabToolFavorite, LabPathProgress, db


_HEURISTIC_WORDS = ("score", "grade", "evaluat", "rating", "rank", "odds",
                    "readiness")


def _tool_labels(tool_id):
    """Honest labels for a tool: basis + data source (Lab Phase 0).

    Basis rule (documented): LLM-assisted when the typed handler's meta
    says the backend calls an LLM; heuristic when the tool name promises
    an estimate (score/grade/evaluate/...); otherwise rule-based.
    Data source comes from the handler meta (curated JSON / live HTTP
    fetch / user input), never guessed per tool.
    """
    from studybuddy.labs_registry import TOOL_HANDLERS
    handler = TOOL_HANDLERS.get(tool_id)
    if handler is not None and handler.meta.uses_llm:
        basis = "LLM-assisted"
    else:
        name = ""
        if handler is not None:
            name = handler.meta.name
        else:
            name = _TOOL_INDEX.get(tool_id, {}).get("name", "")
        basis = ("heuristic"
                 if any(w in name.lower() for w in _HEURISTIC_WORDS)
                 else "rule-based")
    data_source = (handler.meta.data_source if handler is not None
                   else "user input (rule-based)")
    return {"basis": basis, "data_source": data_source}


# Next-action handoffs live in lab_paths.ROOM_OUTPUTS (shared with rooms);
# this alias keeps the Phase 0 name working for _tool_next_actions.
_FEATURE_NEXT_ACTIONS = ROOM_OUTPUTS


def _tool_next_actions(tool_id):
    """Next actions for a run result: save first, then feature handoffs."""
    feature = _TOOL_INDEX.get(tool_id, {}).get("feature", "")
    actions = [{"kind": "save", "label": "Save output to My Lab"}]
    for kind, label, href in _FEATURE_NEXT_ACTIONS.get(feature, []):
        actions.append({"kind": kind, "label": label, "href": href})
    return actions


def _input_hash(payload):
    canon = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _record_run(user_id, tool_id, payload, result, labels):
    """Persist a successful run + award Lab XP. Never raises: a history
    failure must never break the run response itself."""
    row = None
    try:
        LabRun, _LabSave, _LabFav, _LabPath, db = _lab_models()
        tool = _TOOL_INDEX.get(tool_id, {})
        out_text = json.dumps(result, default=str)
        if len(out_text) > 200000:
            out_text = json.dumps({"_truncated": True,
                                   "preview": out_text[:4000]})
        row = LabRun(user_id=user_id, tool_id=tool_id,
                     feature=tool.get("feature", ""),
                     input_hash=_input_hash(payload),
                     input_json=json.dumps(payload, sort_keys=True,
                                           default=str),
                     output_json=out_text, basis=labels["basis"])
        db.session.add(row)
        db.session.commit()
    except Exception:
        try:
            from unified_app import db
            db.session.rollback()
        except Exception:
            pass
        return None
    try:
        from studybuddy.momentum import career_xp_hook
        career_xp_hook(user_id, "lab_run", ref_id="labrun:%d" % row.id)
    except Exception:
        pass  # XP never breaks history
    return row


def _own_run_or_404(run_id):
    """The user's own LabRun row, or (None, 404 response)."""
    LabRun, _LabSave, _LabFav, _LabPath, _db = _lab_models()
    row = LabRun.query.filter_by(id=run_id,
                                 user_id=current_user.id).first()
    if row is None:
        return None, jsonify({"ok": False, "error": "Run not found."}), 404
    return row, None, None


def _run_public(row, saved_ids=None, include_io=False):
    d = {"id": row.id, "tool_id": row.tool_id, "feature": row.feature,
         "basis": row.basis,
         "created_at": row.created_at.isoformat() if row.created_at else None,
         "saved": row.id in (saved_ids or set())}
    if include_io:
        try:
            d["input"] = json.loads(row.input_json)
        except (ValueError, TypeError):
            d["input"] = {}
        try:
            d["output"] = json.loads(row.output_json)
        except (ValueError, TypeError):
            d["output"] = {}
    else:
        d["output_preview"] = (row.output_json or "")[:500]
    return d


@ultimate_bp.route("/api/ultimate/history", methods=["GET"])
def _ultimate_history():
    denied = _deny_anonymous()
    if denied:
        return denied
    disabled = _labs_disabled()
    if disabled:
        return disabled
    try:
        limit = int(request.args.get("limit", "20"))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))
    LabRun, LabSave, _LabFav, _LabPath, _db = _lab_models()
    saved_ids = {s.run_id for s in
                 LabSave.query.filter_by(user_id=current_user.id).all()}
    rows = (LabRun.query.filter_by(user_id=current_user.id)
            .order_by(LabRun.id.desc()).limit(limit).all())
    return jsonify({"ok": True, "runs": [_run_public(r, saved_ids)
                                         for r in rows]})


@ultimate_bp.route("/api/ultimate/runs/<int:run_id>", methods=["GET"])
def _ultimate_run_detail(run_id):
    denied = _deny_anonymous()
    if denied:
        return denied
    disabled = _labs_disabled()
    if disabled:
        return disabled
    row, resp, code = _own_run_or_404(run_id)
    if row is None:
        return resp, code
    _LabRun, LabSave, _LabFav, _LabPath, _db = _lab_models()
    saved = LabSave.query.filter_by(user_id=current_user.id,
                                    run_id=run_id).first() is not None
    return jsonify({"ok": True, "run": _run_public(
        row, {run_id} if saved else set(), include_io=True)})


@ultimate_bp.route("/api/ultimate/runs/<int:run_id>/save", methods=["POST"])
def _ultimate_save_run(run_id):
    denied = _deny_anonymous()
    if denied:
        return denied
    disabled = _labs_disabled()
    if disabled:
        return disabled
    row, resp, code = _own_run_or_404(run_id)
    if row is None:
        return resp, code
    _LabRun, LabSave, _LabFav, _LabPath, db = _lab_models()
    if LabSave.query.filter_by(user_id=current_user.id,
                               run_id=run_id).first() is None:
        try:
            db.session.add(LabSave(user_id=current_user.id, run_id=run_id))
            db.session.commit()
        except Exception:
            db.session.rollback()  # already saved (unique race) -> ok
    return jsonify({"ok": True, "saved": True})


@ultimate_bp.route("/api/ultimate/runs/<int:run_id>/save", methods=["DELETE"])
def _ultimate_unsave_run(run_id):
    denied = _deny_anonymous()
    if denied:
        return denied
    disabled = _labs_disabled()
    if disabled:
        return disabled
    row, resp, code = _own_run_or_404(run_id)
    if row is None:
        return resp, code
    _LabRun, LabSave, _LabFav, _LabPath, db = _lab_models()
    LabSave.query.filter_by(user_id=current_user.id,
                            run_id=run_id).delete()
    db.session.commit()
    return jsonify({"ok": True, "saved": False})


@ultimate_bp.route("/api/ultimate/runs/<int:run_id>", methods=["DELETE"])
def _ultimate_delete_run(run_id):
    denied = _deny_anonymous()
    if denied:
        return denied
    disabled = _labs_disabled()
    if disabled:
        return disabled
    row, resp, code = _own_run_or_404(run_id)
    if row is None:
        return resp, code
    _LabRun, _LabSave, _LabFav, LabPathProgress, db = _lab_models()
    # Path steps keep their completion; only the run link is cleared.
    (LabPathProgress.query.filter_by(run_id=run_id)
     .update({"run_id": None}))
    db.session.delete(row)
    db.session.commit()
    return jsonify({"ok": True, "deleted": True})


@ultimate_bp.route("/api/ultimate/history", methods=["DELETE"])
def _ultimate_clear_history():
    denied = _deny_anonymous()
    if denied:
        return denied
    disabled = _labs_disabled()
    if disabled:
        return disabled
    LabRun, LabSave, _LabFav, LabPathProgress, db = _lab_models()
    own_ids = [r.id for r in
               LabRun.query.filter_by(user_id=current_user.id).all()]
    if own_ids:
        (LabPathProgress.query.filter(
            LabPathProgress.run_id.in_(own_ids)).update({"run_id": None}))
        LabSave.query.filter_by(user_id=current_user.id).delete()
        LabRun.query.filter_by(user_id=current_user.id).delete()
        db.session.commit()
    return jsonify({"ok": True, "deleted": len(own_ids)})


@ultimate_bp.route("/api/ultimate/runs/<int:run_id>/rerun", methods=["POST"])
def _ultimate_rerun(run_id):
    denied = _deny_anonymous()
    if denied:
        return denied
    disabled = _labs_disabled()
    if disabled:
        return disabled
    row, resp, code = _own_run_or_404(run_id)
    if row is None:
        return resp, code
    try:
        payload = json.loads(row.input_json)
    except (ValueError, TypeError):
        payload = {}
    result, error = run_tool(row.tool_id, payload)
    if error:
        return jsonify({"ok": False, "error": error}), 400
    labels = _tool_labels(row.tool_id)
    new_row = _record_run(current_user.id, row.tool_id, payload, result,
                          labels)
    body = {"ok": True, "result": result, "labels": labels,
            "next_actions": _tool_next_actions(row.tool_id)}
    if new_row is not None:
        body["run_id"] = new_row.id
    return jsonify(body)


@ultimate_bp.route("/api/ultimate/favorites", methods=["GET"])
def _ultimate_favorites():
    denied = _deny_anonymous()
    if denied:
        return denied
    disabled = _labs_disabled()
    if disabled:
        return disabled
    _LabRun, _LabSave, LabToolFavorite, _LabPath, _db = _lab_models()
    favs = LabToolFavorite.query.filter_by(
        user_id=current_user.id).order_by(LabToolFavorite.id.desc()).all()
    out = []
    for fav in favs:
        tool = _TOOL_INDEX.get(fav.tool_id)
        if tool is None:
            continue
        out.append({"tool_id": fav.tool_id, "feature": tool.get("feature", ""),
                    "name": tool.get("name", fav.tool_id)})
    return jsonify({"ok": True, "favorites": out})


@ultimate_bp.route("/api/ultimate/favorites/<path:tool_id>", methods=["POST"])
def _ultimate_add_favorite(tool_id):
    denied = _deny_anonymous()
    if denied:
        return denied
    disabled = _labs_disabled()
    if disabled:
        return disabled
    if tool_id not in _TOOL_INDEX:
        return jsonify({"ok": False,
                        "error": f"Unknown tool '{tool_id}'."}), 400
    _LabRun, _LabSave, LabToolFavorite, _LabPath, db = _lab_models()
    if LabToolFavorite.query.filter_by(user_id=current_user.id,
                                       tool_id=tool_id).first() is None:
        try:
            db.session.add(LabToolFavorite(user_id=current_user.id,
                                           tool_id=tool_id))
            db.session.commit()
        except Exception:
            db.session.rollback()  # already favourited (unique race)
    return jsonify({"ok": True, "favorite": True})


@ultimate_bp.route("/api/ultimate/favorites/<path:tool_id>", methods=["DELETE"])
def _ultimate_remove_favorite(tool_id):
    denied = _deny_anonymous()
    if denied:
        return denied
    disabled = _labs_disabled()
    if disabled:
        return disabled
    _LabRun, _LabSave, LabToolFavorite, _LabPath, db = _lab_models()
    LabToolFavorite.query.filter_by(user_id=current_user.id,
                                    tool_id=tool_id).delete()
    db.session.commit()
    return jsonify({"ok": True, "favorite": False})


# ---------------------------------------------------------------------------
# Lab Phase 1: feature rooms, flagship paths, intent search.
# ---------------------------------------------------------------------------

def _path_progress(user_id):
    """(done_by_path, run_by_step) for one user from LabPathProgress."""
    _LabRun, _LabSave, _LabFav, LabPathProgress, _db = _lab_models()
    done = {}
    runs = {}
    for row in LabPathProgress.query.filter_by(user_id=user_id).all():
        done.setdefault(row.path, set()).add(row.step)
        if row.run_id:
            runs[(row.path, row.step)] = row.run_id
    return done, runs


def _path_public(path, done_steps, run_map):
    """Path dict for JSON: steps with done flags, honest step counts."""
    steps = []
    for step in path["steps"]:
        item = dict(step)
        item["done"] = step["id"] in done_steps
        if (path["id"], step["id"]) in run_map:
            item["run_id"] = run_map[(path["id"], step["id"])]
        steps.append(item)
    return {"id": path["id"], "title": path["title"],
            "tagline": path["tagline"],
            "finish_kind": path["finish_kind"],
            "finish_label": path["finish_label"],
            "finish_detail": path["finish_detail"],
            "finish_links": path["finish_links"], "steps": steps,
            "done": len(done_steps), "total": len(path["steps"]),
            "completed": len(done_steps) >= len(path["steps"])}


@ultimate_bp.route("/ultimate/rooms/<slug>", methods=["GET"])
@login_required
def _ultimate_room(slug):
    # Mirrors the hub: @login_required (302 anon), no FEATURE_LABS gate
    # on the page itself — the tool-run APIs enforce the flag.
    room = room_for_slug(build_rooms(ALL_TOOLS), slug)
    if room is None:
        abort(404)
    return render_template("ultimate_room.html", room=room)


@ultimate_bp.route("/ultimate/paths/<path_id>", methods=["GET"])
@login_required
def _ultimate_path_page(path_id):
    # Mirrors the hub: @login_required (302 anon), no FEATURE_LABS gate
    # on the page itself — the step/complete APIs enforce the flag.
    path = path_for_id(path_id)
    if path is None:
        abort(404)
    done_by_path, run_map = _path_progress(current_user.id)
    done = done_by_path.get(path_id, set())
    # "Finished" (completion XP claimed) is distinct from "all steps
    # done": the finish action is claimed separately via POST complete.
    # The ledger key mirrors career_xp_hook's dedupe construction.
    from unified_app import XPLedger
    ledger_key = "%d:lab_path_complete:ref:labpath:%d:%s:complete" % (
        current_user.id, current_user.id, path_id)
    finished = XPLedger.query.filter_by(
        user_id=current_user.id, dedupe_key=ledger_key).first() is not None
    return render_template("ultimate_path.html",
                           path=_path_public(path, done, run_map),
                           finished=finished)


@ultimate_bp.route("/api/ultimate/paths", methods=["GET"])
def _ultimate_paths():
    denied = _deny_anonymous()
    if denied:
        return denied
    disabled = _labs_disabled()
    if disabled:
        return disabled
    done_by_path, _runs = _path_progress(current_user.id)
    out = []
    for path in PATHS:
        done = done_by_path.get(path["id"], set())
        # Steps included so the hub can offer "count this run toward a
        # path step" without a second fetch per path.
        steps = [{"id": s["id"], "title": s["title"], "kind": s["kind"],
                  "tool_id": s.get("tool_id"), "done": s["id"] in done}
                 for s in path["steps"]]
        out.append({"id": path["id"], "title": path["title"],
                    "tagline": path["tagline"],
                    "finish_kind": path["finish_kind"],
                    "steps": steps,
                    "done": len(done), "total": len(path["steps"]),
                    "completed": len(done) >= len(path["steps"])})
    return jsonify({"ok": True, "paths": out})


@ultimate_bp.route("/api/ultimate/paths/<path_id>", methods=["GET"])
def _ultimate_path_detail(path_id):
    denied = _deny_anonymous()
    if denied:
        return denied
    disabled = _labs_disabled()
    if disabled:
        return disabled
    path = path_for_id(path_id)
    if path is None:
        return jsonify({"ok": False, "error": "Unknown path."}), 404
    done_by_path, run_map = _path_progress(current_user.id)
    done = done_by_path.get(path_id, set())
    return jsonify({"ok": True,
                    "path": _path_public(path, done, run_map)})


@ultimate_bp.route("/api/ultimate/paths/<path_id>/steps/<step_id>/complete",
                   methods=["POST"])
def _ultimate_path_step_complete(path_id, step_id):
    denied = _deny_anonymous()
    if denied:
        return denied
    disabled = _labs_disabled()
    if disabled:
        return disabled
    path = path_for_id(path_id)
    if path is None:
        return jsonify({"ok": False, "error": "Unknown path."}), 404
    step = step_for_id(path, step_id)
    if step is None:
        return jsonify({"ok": False, "error": "Unknown step."}), 404
    payload = request.get_json(silent=True) or {}
    run_id = None
    if step["kind"] == "tool":
        # Tool steps are verified, not checkboxed: the run must belong
        # to this user and be for exactly this step's tool.
        try:
            run_id = int(payload.get("run_id"))
        except (TypeError, ValueError):
            return jsonify({"ok": False,
                            "error": "A run id is required for tool "
                                     "steps."}), 400
        row, resp, code = _own_run_or_404(run_id)
        if row is None:
            return resp, code
        if row.tool_id != step["tool_id"]:
            return jsonify({"ok": False,
                            "error": "That run is for a different "
                                     "tool."}), 400
    _LabRun, _LabSave, _LabFav, LabPathProgress, db = _lab_models()
    existing = LabPathProgress.query.filter_by(
        user_id=current_user.id, path=path_id, step=step_id).first()
    if existing is not None:
        return jsonify({"ok": True, "already": True, "xp_awarded": False,
                        "step": step_id})
    db.session.add(LabPathProgress(user_id=current_user.id, path=path_id,
                                   step=step_id, run_id=run_id))
    db.session.commit()
    xp_awarded = False
    if step["kind"] == "tool":
        # Step XP (10) only for verified tool runs; page steps earn
        # progress, not XP — a checkbox is not a verifiable artifact.
        from studybuddy.momentum import career_xp_hook
        hook = career_xp_hook(
            current_user.id, "lab_path_step",
            ref_id="labpath:%d:%s:%s" % (current_user.id, path_id, step_id))
        xp_awarded = bool(hook.get("ok")) and not hook.get("duplicate") \
            and not hook.get("skipped")
    return jsonify({"ok": True, "already": False, "xp_awarded": xp_awarded,
                    "step": step_id})


@ultimate_bp.route("/api/ultimate/paths/<path_id>/complete",
                   methods=["POST"])
def _ultimate_path_complete(path_id):
    denied = _deny_anonymous()
    if denied:
        return denied
    disabled = _labs_disabled()
    if disabled:
        return disabled
    path = path_for_id(path_id)
    if path is None:
        return jsonify({"ok": False, "error": "Unknown path."}), 404
    done_by_path, _runs = _path_progress(current_user.id)
    done = done_by_path.get(path_id, set())
    missing = [s["id"] for s in path["steps"] if s["id"] not in done]
    if missing:
        return jsonify({"ok": False, "error": "Finish every step first.",
                        "missing": missing}), 400
    payload = request.get_json(silent=True) or {}
    role = (payload.get("role") or "").strip()
    company = (payload.get("company") or "").strip()
    tracker_data = None
    if path["finish_kind"] == "tracker" and role:
        # Wishlist subset of POST /tracker/add's validation (that
        # validator is nested inside create_app, so it cannot be
        # imported — these rules mirror it: role required, company
        # optional for an explicit wishlist row, both max 200 chars).
        # Validated BEFORE the completion XP hook so a bad finish
        # cannot award XP.
        if len(role) > 200:
            return jsonify({"ok": False,
                            "error": "Role is too long (max 200 "
                                     "characters)."}), 400
        if len(company) > 200:
            return jsonify({"ok": False,
                            "error": "Company name is too long (max 200 "
                                     "characters)."}), 400
        tracker_data = {"company": company, "role": role}
    from studybuddy.momentum import career_xp_hook
    hook = career_xp_hook(
        current_user.id, "lab_path_complete",
        ref_id="labpath:%d:%s:complete" % (current_user.id, path_id))
    already = bool(hook.get("duplicate"))
    xp_awarded = bool(hook.get("ok")) and not already \
        and not hook.get("skipped")
    tracker_created = False
    application_id = None
    if tracker_data is not None and not already:
        from unified_app import JobApplication, db
        row = JobApplication(user_id=current_user.id, stage="wishlist",
                             **tracker_data)
        db.session.add(row)
        db.session.commit()
        tracker_created = True
        application_id = row.id
        # Same hook as POST /tracker/add (ref = the application id).
        career_xp_hook(current_user.id, "tracker_add", ref_id=row.id)
    return jsonify({"ok": True, "completed": True, "already": already,
                    "xp_awarded": xp_awarded,
                    "tracker_created": tracker_created,
                    "application_id": application_id,
                    "finish_links": path["finish_links"]})


@ultimate_bp.route("/api/ultimate/search", methods=["GET"])
def _ultimate_search():
    denied = _deny_anonymous()
    if denied:
        return denied
    disabled = _labs_disabled()
    if disabled:
        return disabled
    query = request.args.get("q", "")

    def _slim(tool):
        return {"id": tool["id"], "feature": tool["feature"],
                "name": tool["name"],
                "description": tool.get("description", "")}

    intent, matches = intent_search(query, ALL_TOOLS)
    return jsonify({"ok": True, "q": query,
                    "intent": [_slim(t) for t in intent],
                    "matches": [_slim(t) for t in matches]})


def register_ultimate_bp(app, limiter=None):
    """Register the Ultimate Lab blueprint (monolith-split pattern).

    Rate limits (Lab Phase 0, documented defaults): the run endpoint
    keeps its per-family bound of 30/hour — the same bound the old
    global bucket gave any single family — and gains a global bound of
    120/hour so one family's heavy session cannot lock the whole Lab.
    Both bounds are app.config keys (ULTIMATE_RUN_LIMIT,
    ULTIMATE_RUN_GLOBAL_LIMIT); exact numbers are revisited after two
    weeks of real run data.
    """
    app.register_blueprint(ultimate_bp)
    if limiter is not None:
        family_limit = app.config.get("ULTIMATE_RUN_LIMIT", "30 per hour")
        global_limit = app.config.get("ULTIMATE_RUN_GLOBAL_LIMIT",
                                      "120 per hour")
        view = app.view_functions["ultimate._ultimate_run"]
        view = limiter.limit(global_limit, key_func=_lab_user_key)(view)
        view = limiter.limit(family_limit, key_func=_lab_family_key)(view)
        app.view_functions["ultimate._ultimate_run"] = view
        # Mutating My Lab endpoints (save/delete/favourite/rerun): a
        # plain per-user bound. Reads stay unlimited, like /tools.
        for _ep in ("ultimate._ultimate_save_run",
                    "ultimate._ultimate_unsave_run",
                    "ultimate._ultimate_delete_run",
                    "ultimate._ultimate_clear_history",
                    "ultimate._ultimate_rerun",
                    "ultimate._ultimate_add_favorite",
                    "ultimate._ultimate_remove_favorite",
                    "ultimate._ultimate_path_step_complete",
                    "ultimate._ultimate_path_complete"):
            if _ep in app.view_functions:
                app.view_functions[_ep] = limiter.limit(
                    "60 per hour", key_func=_lab_user_key)(
                        app.view_functions[_ep])
