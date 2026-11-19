"""plan: loading and saving the plan and state, constants, indexes and ownership (contract 1.3, 1.5, 3.3).

load_plan reads any supported schema version and returns a version 2 plan: version 1 files are upgraded in memory
(bp.migrate) and marked "_from": 1. The new sections and fields are type-checked at load against V2_FIELDS, so code
behind load_plan never sees a wrongly typed value (contract 3.3).
"""
import json
import sys

from bp.version import SCHEMA_VERSION
from bp.core import BlueprintError, bp_dir, die, norm, read_json, write_json
from bp.globs import glob_match
from bp.migrate import upgrade_plan, upgrade_state


ACTIONS = ("create", "modify", "delete")


ROSTER_KINDS = ("agent", "human")


STATES = ("planned", "claimed", "ondisk", "built", "verified", "blocked")


GLYPH = {"planned": "○", "claimed": "◔", "ondisk": "◑", "built": "●", "verified": "✔", "blocked": "▲"}


LABEL = {"planned": "planned", "claimed": "claimed", "ondisk": "on disk", "built": "built",
         "verified": "verified", "blocked": "blocked"}


DEFAULT_IGNORE = [".git/", "node_modules/", "target/", "dist/", "build/", ".next/", ".turbo/",
                  ".venv/", "venv/", "__pycache__/", ".pytest_cache/", "coverage/", ".DS_Store",
                  ".idea/", ".vscode/", ".blueprint/", "/.agentic-team/", "/.claude/", "/.codex/"]


WRITE_ACCESS = {"workspace-write"}


def json_kind(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    return {str: "string", list: "list", dict: "object"}.get(type(v), type(v).__name__)


LIST_SECTIONS = ("stack", "modules", "phases", "files", "patterns", "owners", "roster", "decisions", "stores", "resources")


ID_SECTIONS = ("modules", "phases", "patterns", "roster", "stores", "resources")  # sections whose ids are keys


STRING_FIELDS = ("path", "module", "phase", "owner", "pattern", "glob", "action", "kind", "store", "definedIn",
                 "name", "purpose", "verify", "layer", "instance", "role", "harness", "github", "lane", "goal", "exit",
                 "part", "choice", "docs", "verified", "version", "decision", "why", "source", "host", "note",
                 "resource", "access", "summary", "preset", "layout", "notes")


STRING_LIST_FIELDS = ("dependsOn", "consumes", "produces", "aliases", "examples", "schema", "ac", "platforms")


REQUIRED_KEYS = {"modules": ("id", "purpose"), "phases": ("id",), "roster": ("id",), "patterns": ("id", "glob"),
                 "owners": ("pattern", "owner"), "stores": ("id",), "resources": ("id",), "files": ("path", "action")}


def check_entry(label, item, bad, ids=False):
    """Type-check one object's known fields; append a message per problem to bad."""
    for fld in (("id",) if ids else ()) + STRING_FIELDS:
        if item.get(fld) is not None and not isinstance(item[fld], str):
            bad.append(f"{label}.{fld} must be a string (got {json_kind(item[fld])})")
    for fld in STRING_LIST_FIELDS + ("uses",):
        if item.get(fld) is None and fld not in item:
            continue
        val = item.get(fld)
        if not isinstance(val, list):
            bad.append(f"{label}.{fld} must be a list (got {json_kind(val)})")
            continue
        want, what = (dict, "an object") if fld == "uses" else (str, "a string")
        for j, x in enumerate(val):
            if not isinstance(x, want):
                bad.append(f"{label}.{fld}[{j}] must be {what} (got {json_kind(x)})")
            elif fld == "uses":
                check_entry(f"{label}.uses[{j}]", x, bad)


SHAPE_FOOTER = "\nFix it, then run: python .blueprint/bin/blueprint.py validate"


def check_plan_shapes(plan):
    """Exit 1 with every shape problem at once, so a malformed plan never reaches code that assumes the shapes.
    A null section or field is treated as missing, not as an error. (The 1.3.0 checks; load_plan adds the 2.0 ones.)"""
    if not isinstance(plan, dict):
        die(f"blueprint.json must be an object (got {json_kind(plan)})" + SHAPE_FOOTER)
    bad = _shape_problems_1x(plan)
    if bad:
        die("\n".join("blueprint.json: " + b for b in bad) + SHAPE_FOOTER)


def _shape_problems_1x(plan):
    """The problems check_plan_shapes reports, as a list (plan is a dict)."""
    bad = []
    for key in LIST_SECTIONS:
        v = plan.get(key)
        if v is None:
            continue
        if not isinstance(v, list):
            bad.append(f"{key} must be a list (got {json_kind(v)})")
            continue
        for i, item in enumerate(v):
            if not isinstance(item, dict):
                bad.append(f"{key}[{i}] must be an object (got {json_kind(item)})")
            else:
                check_entry(f"{key}[{i}]", item, bad, ids=key in ID_SECTIONS)
    ig = plan.get("ignore")
    if ig is not None:
        if not isinstance(ig, list):
            bad.append(f"ignore must be a list (got {json_kind(ig)})")
        else:
            for i, item in enumerate(ig):
                if not isinstance(item, str):
                    bad.append(f"ignore[{i}] must be a string (got {json_kind(item)})")
    for key in ("app", "approach"):
        v = plan.get(key)
        if v is None:
            continue
        if not isinstance(v, dict):
            bad.append(f"{key} must be an object (got {json_kind(v)})")
        else:
            check_entry(key, v, bad)
    return bad


SECTIONS_V2 = ("milestones", "features", "tasks", "unknowns")

# Field types of the sections and fields added in schema 2 (contract 2.3 to 2.7), per section. A type is "string",
# "bool", "strings" (a list of strings), or (kind, table) for nested values: ("object", table) or ("objects", table)
# for a list of objects. Fields not listed are not checked here (validate checks values; unknown keys are kept).
CRITERION_FIELDS = {"id": "string", "scenario": "string", "action": "string", "expected": "string", "mustNot": "string",
                    "level": "string", "verify": "string", "verifyLevel": "string", "test": "string", "ac": "string",
                    "from": "string"}
V2_FIELDS = {
    "milestones": {"id": "string", "name": "string", "goal": "string", "due": "string", "exit": "string"},
    "features": {"id": "string", "title": "string", "statement": "string", "kind": "string", "owner": "string",
                 "milestone": "string", "level": "string", "criteria": ("objects", CRITERION_FIELDS),
                 "dependsOn": "strings", "draft": "bool",
                 "source": ("object", {"study": "string", "behaviours": "strings"}), "notes": "string"},
    "tasks": {"id": "string", "title": "string", "owner": "string", "milestone": "string", "ownerAction": "bool",
              "dependsOn": "strings", "doneWhen": "string", "notes": "string"},
    "unknowns": {"id": "string", "question": "string", "severity": "string", "status": "string", "blocks": "strings",
                 "evidenceFor": "strings", "evidenceAgainst": "strings",
                 "experiment": ("object", {"plan": "string", "timebox": "string", "exit": "string", "owner": "string"}),
                 "decision": "string", "from": "string", "notes": "string"},
    # fields added to 1.2 sections (2.7); the 1.3.0 checks above still cover their other fields
    "files": {"features": "strings", "infrastructure": "bool"},
    "patterns": {"features": "strings", "infrastructure": "bool"},
    "phases": {"rollback": "string"},
    "decisions": {"unknown": "string", "alternatives": "strings", "date": "string"},
}


def _check_fields(label, item, table, bad):
    """Type-check the fields of one object against a V2_FIELDS table. A null field is missing, not an error."""
    for fld, typ in table.items():
        val = item.get(fld)
        if val is None:
            continue
        where = f"{label}.{fld}"
        if typ == "string":
            if not isinstance(val, str):
                bad.append(f"{where} must be a string (got {json_kind(val)})")
        elif typ == "bool":
            if not isinstance(val, bool):
                bad.append(f"{where} must be a boolean (got {json_kind(val)})")
        elif typ == "strings":
            if not isinstance(val, list):
                bad.append(f"{where} must be a list (got {json_kind(val)})")
            else:
                bad.extend(f"{where}[{j}] must be a string (got {json_kind(x)})"
                           for j, x in enumerate(val) if not isinstance(x, str))
        elif typ[0] == "object":
            if not isinstance(val, dict):
                bad.append(f"{where} must be an object (got {json_kind(val)})")
            else:
                _check_fields(where, val, typ[1], bad)
        else:  # ("objects", table)
            if not isinstance(val, list):
                bad.append(f"{where} must be a list (got {json_kind(val)})")
                continue
            for j, x in enumerate(val):
                if not isinstance(x, dict):
                    bad.append(f"{where}[{j}] must be an object (got {json_kind(x)})")
                else:
                    _check_fields(f"{where}[{j}]", x, typ[1], bad)


def _shape_problems_2x(plan):
    """The 2.0 load checks (contract 3.3): the top-level version, then the new sections and fields (V2_FIELDS)."""
    bad = []
    v = plan.get("version")
    if v is None:
        bad.append("version is required")
    elif type(v) is not int:
        bad.append(f"version must be a whole number (got {json_kind(v)})")
    elif v < 1:
        bad.append(f"version must be 1 or 2 (got {v})")  # a version above 2 is schema-too-new (upgrade_plan)
    for key, table in V2_FIELDS.items():
        sec = plan.get(key)
        if sec is None:
            continue
        if not isinstance(sec, list):
            if key in SECTIONS_V2:  # a 1.2 section of the wrong type is already a 1.3.0 problem
                bad.append(f"{key} must be a list (got {json_kind(sec)})")
            continue
        for i, item in enumerate(sec):
            if isinstance(item, dict):
                _check_fields(f"{key}[{i}]", item, table, bad)
            elif key in SECTIONS_V2:
                bad.append(f"{key}[{i}] must be an object (got {json_kind(item)})")
    return bad


def _drop_nulls(d):
    for k in [k for k, v in d.items() if v is None]:
        del d[k]


def normalize_plan(plan):
    """After the shape check: null means missing, and keys the code reads directly always exist (validate reports
    the empty ones). In the 2.0 sections null also means missing, at every level V2_FIELDS describes."""
    for key in [k for k, v in plan.items() if v is None]:
        del plan[key]
    for key in LIST_SECTIONS:
        for item in plan.get(key) or []:
            for k in [k for k, v in item.items() if v is None]:
                del item[k]
            for k in REQUIRED_KEYS.get(key, ()):
                item.setdefault(k, "")
            for u in item.get("uses") or []:
                for k in [k for k, v in u.items() if v is None]:
                    del u[k]
                u.setdefault("resource", "")
    for key in ("app", "approach"):
        if isinstance(plan.get(key), dict):
            for k in [k for k, v in plan[key].items() if v is None]:
                del plan[key][k]
    for key in SECTIONS_V2:
        for item in plan.get(key) or []:
            if not isinstance(item, dict):
                continue
            _drop_nulls(item)
            for fld, typ in V2_FIELDS[key].items():
                val = item.get(fld)
                if isinstance(typ, tuple) and typ[0] == "object" and isinstance(val, dict):
                    _drop_nulls(val)
                elif isinstance(typ, tuple) and isinstance(val, list):
                    for x in val:
                        if isinstance(x, dict):
                            _drop_nulls(x)


FIX_NEXT = "fix .blueprint/blueprint.json, then run: python .blueprint/bin/blueprint.py validate"


def _load_error(error, msg, next):  # noqa: A002
    """A plan load error: the 1.x text on stderr (exactly what die() printed), then a BlueprintError (exit 1) that
    already counts as printed, so --json callers still get the 7.1 failure envelope and guard can fail closed."""
    print(msg, file=sys.stderr)
    e = BlueprintError(error, msg, next=next)
    e.printed = True
    raise e


def load_raw_plan(root):
    """blueprint.json as parsed JSON, untouched (no checks, no upgrade). Writers of the plan start from this (3.3)."""
    path = bp_dir(root) / "blueprint.json"
    if not path.exists():
        _load_error("no-blueprint", f"No blueprint at {path}. Run: blueprint.py init --name \"App name\"",
                    'blueprint.py init --name "App name"')
    try:
        return read_json(path)
    except json.JSONDecodeError as e:
        _load_error("bad-json", f"blueprint.json is not valid JSON: {e}", FIX_NEXT)
    except UnicodeDecodeError as e:  # review R2-M1: unreadable is a load error too (bad-json), never a crash
        _load_error("bad-json", f"blueprint.json is not UTF-8 text: {e}", FIX_NEXT)
    except RecursionError:
        _load_error("bad-json", "blueprint.json is nested too deeply to read", FIX_NEXT)
    except OSError as e:
        _load_error("bad-json", f"blueprint.json cannot be read: {e.strerror or e}", FIX_NEXT)


def load_plan(root):
    """The plan at schema version 2, whatever supported version the file has (contract 3.3). Shape problems exit 1
    with every message at once; a version other than 1 or 2 raises schema-too-new. Never writes blueprint.json."""
    raw = load_raw_plan(root)
    if not isinstance(raw, dict):
        _load_error("bad-shape", f"blueprint.json must be an object (got {json_kind(raw)})" + SHAPE_FOOTER, FIX_NEXT)
    bad = _shape_problems_1x(raw) + _shape_problems_2x(raw)
    if bad:
        _load_error("bad-shape", "\n".join("blueprint.json: " + b for b in bad) + SHAPE_FOOTER, FIX_NEXT)
    plan = upgrade_plan(raw)
    plan.pop("_from", None)  # an in-memory marker only: a "_from" in the file is ignored (dropped from the plan)
    if raw["version"] == 1:
        plan["_from"] = 1
    normalize_plan(plan)
    for key, default in (("stack", []), ("modules", []), ("phases", []), ("files", []),
                         ("patterns", []), ("owners", []), ("roster", []), ("decisions", []),
                         ("ignore", []), ("approach", {}), ("app", {}), ("stores", []), ("resources", []),
                         ("milestones", []), ("features", []), ("tasks", []), ("unknowns", [])):
        plan.setdefault(key, default)
    for f in plan["files"]:
        f["path"] = norm(f.get("path", ""))
    return plan


def load_state(root):
    """state.json at stateVersion 2 (a 1.x state is upgraded in memory; the next save writes it, contract 3.2)."""
    return upgrade_state(read_json(bp_dir(root) / "state.json", {"files": {}, "patterns": {}, "unplanned": [],
                                                                  "syncedAt": None, "head": None}))


def save_state(root, state):
    write_json(bp_dir(root) / "state.json", state)


def owner_of(plan, path):
    """Return (owner, rule_index, all_matching_indexes) using last-match-wins."""
    matches = [i for i, r in enumerate(plan["owners"]) if glob_match(r.get("pattern", ""), path)]
    if not matches:
        return None, None, []
    i = matches[-1]
    return plan["owners"][i].get("owner"), i, matches


def phase_index(plan):
    return {p.get("id"): i for i, p in enumerate(plan["phases"])}


def module_index(plan):
    return {m.get("id"): m for m in plan["modules"]}


def item_verify(plan, item):
    if item.get("verify"):
        return item["verify"]
    mod = module_index(plan).get(item.get("module"))
    return (mod or {}).get("verify")


def planned_file(plan, path):
    path = norm(path)
    for f in plan["files"]:
        if f["path"] == path:
            return f
    return None


SKELETON = {
    "version": SCHEMA_VERSION,
    "app": {"name": "", "summary": "", "platforms": [], "preset": ""},
    "stack": [],
    "approach": {"layout": "", "notes": ""},
    "modules": [],
    "phases": [{"id": "P1", "name": "Walking skeleton", "goal": "The thinnest version that builds, runs and is tested end to end.", "exit": ""}],
    "files": [],
    "patterns": [],
    "owners": [],
    "roster": [],
    "decisions": [],
    "ignore": [],
    "milestones": [],
    "features": [],
    "tasks": [],
    "unknowns": [],
}


def section_index(plan, name):
    """{id: entry} for the entries of a list section that are objects with a string id, in plan order (first wins)."""
    out = {}
    for item in plan.get(name) or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"] not in out:
            out[item["id"]] = item
    return out


def features_of(item):
    """The feature ids a file or pattern entry serves (its features[] strings), or []."""
    val = item.get("features") if isinstance(item, dict) else None
    return [x for x in val if isinstance(x, str)] if isinstance(val, list) else []
