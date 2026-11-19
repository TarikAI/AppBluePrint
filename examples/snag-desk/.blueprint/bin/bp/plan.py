"""plan: moved from blueprint.py 1.3.0 (contract 1.5)."""
import json

from bp.version import SCHEMA_VERSION
from bp.core import bp_dir, die, norm, read_json, write_json
from bp.globs import glob_match


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


def check_plan_shapes(plan):
    """Exit 1 with every shape problem at once, so a malformed plan never reaches code that assumes the shapes.
    A null section or field is treated as missing, not as an error."""
    if not isinstance(plan, dict):
        die(f"blueprint.json must be an object (got {json_kind(plan)})\n"
            "Fix it, then run: python .blueprint/bin/blueprint.py validate")
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
    if bad:
        die("\n".join("blueprint.json: " + b for b in bad)
            + "\nFix it, then run: python .blueprint/bin/blueprint.py validate")


def normalize_plan(plan):
    """After the shape check: null means missing, and keys the code reads directly always exist (validate reports
    the empty ones)."""
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


def load_plan(root):
    path = bp_dir(root) / "blueprint.json"
    if not path.exists():
        die(f"No blueprint at {path}. Run: blueprint.py init --name \"App name\"")
    try:
        plan = read_json(path)
    except json.JSONDecodeError as e:
        die(f"blueprint.json is not valid JSON: {e}")
    check_plan_shapes(plan)
    normalize_plan(plan)
    for key, default in (("stack", []), ("modules", []), ("phases", []), ("files", []),
                         ("patterns", []), ("owners", []), ("roster", []), ("decisions", []),
                         ("ignore", []), ("approach", {}), ("app", {}), ("stores", []), ("resources", [])):
        plan.setdefault(key, default)
    for f in plan["files"]:
        f["path"] = norm(f.get("path", ""))
    return plan


def load_state(root):
    return read_json(bp_dir(root) / "state.json", {"files": {}, "patterns": {}, "unplanned": [],
                                                   "syncedAt": None, "head": None})


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
}
