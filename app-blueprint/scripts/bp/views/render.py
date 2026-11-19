"""views.render: map.html and render_all(), moved from blueprint.py 1.3.0 (contract 1.5), with the 2.0 plug-in points.

render_all builds the context once (views.context) and writes STRUCTURE.md, ROSTER.md, every view the VIEWS modules
return (ROADMAP.md, FEATURES.md, contract 8) and map.html. The map gets the map_payload hooks' keys in its data object
and every tab-*.js asset inlined at the template's /*__TABS_JS__*/ placeholder, in sorted order (8.4).
"""
import json
import re
import sys

from bp.version import TOOL_VERSION
from bp import registry
from bp.core import TOOL_HOME, BlueprintError, bp_dir, find_asset, write_text_if_changed
from bp.plan import GLYPH, LABEL, STATES, save_state
from bp.data import data_view
from bp.status import compute
from bp.views.context import build_context, merged
from bp.views.structure import render_structure
from bp.views.roster import render_roster

BASE_VIEWS = ("STRUCTURE.md", "ROSTER.md", "map.html")
RESERVED_VIEWS = BASE_VIEWS + ("README.md",)  # init writes README.md for people to edit
_VIEW_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.md$")


def find_template():
    return find_asset("map.template.html")


def tab_scripts():
    """The text of every tab-*.js asset, sorted by name: beside the launcher first (a copy), then the skill's assets."""
    found = {}
    for folder in (TOOL_HOME, TOOL_HOME.parent / "assets"):
        if folder.is_dir():
            for p in sorted(folder.glob("tab-*.js")):
                found.setdefault(p.name, p)
    return [found[n].read_text(encoding="utf-8") for n in sorted(found)]


def _script(text):
    return text.replace("</script", "<\\/script")


def render_map(plan, state, comp, ctx=None):
    tpl = find_template()
    if not tpl:
        return None
    data = {"tool": TOOL_VERSION, "app": plan["app"], "stack": plan["stack"], "approach": plan.get("approach"),
            "modules": plan["modules"], "phases": plan["phases"], "roster": plan["roster"], "owners": plan["owners"],
            "decisions": plan["decisions"], "rows": comp["rows"], "patterns": comp["patterns"], "counts": comp["counts"],
            "unplanned": state.get("unplanned", []), "missingModify": state.get("missingModify", []),
            "syncedAt": state.get("syncedAt"), "head": state.get("head"), "states": list(STATES), "glyph": GLYPH, "label": LABEL,
            "data": data_view(plan, state)}
    if ctx is not None:  # 2.0: keys from the map_payload hooks (contract 8.5); a collision is a bug
        data = merged("map_payload", ctx, data)
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = tpl.read_text(encoding="utf-8")
    mm = find_asset("mindmap.js")
    mm_js = _script(mm.read_text(encoding="utf-8")) if mm else "function mindMap(host){host.textContent='Mind map unavailable: run init --refresh-tool.';return {expandAll:function(){},collapseAll:function(){},zoom:function(){},fit:function(){}};}"
    html = html.replace("/*__MINDMAP_JS__*/", mm_js)
    html = html.replace("/*__TABS_JS__*/", "\n;\n".join(_script(t) for t in tab_scripts()))
    return html.replace("__APP_NAME__", _html_escape(plan["app"].get("name", "App"))).replace("__BLUEPRINT_DATA__", payload)


def _html_escape(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _hook_views(ctx):
    """{filename: (module, text)} from every views hook. A name must be a plain .md file name in .blueprint/, not a
    base view, not README.md (init's, edited by hand) and not one another module returns (else RuntimeError: a bug
    in a hook)."""
    out = {}
    for name, views in registry.collect("views", ctx):
        for fname, text in (views or {}).items():
            if not isinstance(fname, str) or not _VIEW_NAME.match(fname) or fname in RESERVED_VIEWS or fname in out:
                raise RuntimeError(f"{name}.views returned the view name {fname!r}: a .md file name in .blueprint/, "
                                   "not a base view, not README.md and not one another module returns")
            out[fname] = (name, str(text))
    return out


def render_all(root, plan, state, quiet=False, check=False):
    """Write (or with check=True, compare) every view that should exist for this plan; returns the stale names.

    The views hooks' files are recorded in state.json as "generatedViews": the union of the old and new names is
    saved before any view is written, and the exact list after the deletions, so a crash in between leaves a superset
    that the next render cleans up (review R2-M4). A recorded name that no module returns any more should not exist:
    render deletes it, render --check reports it. Base views are never deleted; a file render never wrote is never
    touched, and a view whose name is such a file is not written: render names it and the module, renders the other
    views, and fails (view-conflict, exit 1). A plan with no hook views writes no key."""
    ctx = build_context(root, plan, state)
    comp = ctx["comp"]
    outputs = {"STRUCTURE.md": render_structure(plan, state, comp, ctx), "ROSTER.md": render_roster(plan, state, comp, ctx)}
    current = state.get("generatedViews") if "generatedViews" in state else []
    recorded = [n for n in current if isinstance(n, str) and _VIEW_NAME.match(n) and n not in RESERVED_VIEWS]         if isinstance(current, list) else []
    hook = _hook_views(ctx)
    conflicts = {n: mod for n, (mod, _) in hook.items() if n not in recorded and (bp_dir(root) / n).exists()}
    views = {n: text for n, (_, text) in hook.items() if n not in conflicts}
    outputs.update(views)
    if not check:
        union = sorted(set(recorded) | set(views))
        if union != current:
            state["generatedViews"] = union
            save_state(root, state)
    m = render_map(plan, state, comp, ctx)
    if m is not None:
        outputs["map.html"] = m
    stale = []
    for name, text in outputs.items():
        path = bp_dir(root) / name
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(name)
        elif write_text_if_changed(path, text) and not quiet:
            print(f"wrote .blueprint/{name}")
    for name in recorded:
        path = bp_dir(root) / name
        if name in outputs or not path.exists():
            continue
        if check:
            stale.append(name)
        else:
            path.unlink()
            if not quiet:
                print(f"removed .blueprint/{name} (nothing to show)")
    if not check and sorted(views) != state.get("generatedViews", []):
        state["generatedViews"] = sorted(views)
        save_state(root, state)
    for name, mod in sorted(conflicts.items()):
        print(f"render: .blueprint/{name} exists and was not written by blueprint.py, but the view module {mod} writes "
              "that name; move or rename it, then run render again.", file=sys.stderr)
        stale.append(name)
    if m is None and not quiet:
        print("map.template.html not found next to the script; map.html skipped. Run init --refresh-tool.")
    if conflicts and not check:
        raise BlueprintError("view-conflict", f"{len(conflicts)} view(s) not written: " + ", ".join(sorted(conflicts)),
                             next="move or rename the file(s) named above, then run render")
    return stale
