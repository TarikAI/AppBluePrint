"""views.render: moved from blueprint.py 1.3.0 (contract 1.5)."""
import json

from bp.version import TOOL_VERSION
from bp.core import bp_dir, find_asset, write_text_if_changed
from bp.plan import GLYPH, LABEL, STATES
from bp.data import data_view
from bp.status import compute
from bp.views.structure import render_structure
from bp.views.roster import render_roster


def find_template():
    return find_asset("map.template.html")


def render_map(plan, state, comp):
    tpl = find_template()
    if not tpl:
        return None
    data = {"tool": TOOL_VERSION, "app": plan["app"], "stack": plan["stack"], "approach": plan.get("approach"),
            "modules": plan["modules"], "phases": plan["phases"], "roster": plan["roster"], "owners": plan["owners"],
            "decisions": plan["decisions"], "rows": comp["rows"], "patterns": comp["patterns"], "counts": comp["counts"],
            "unplanned": state.get("unplanned", []), "missingModify": state.get("missingModify", []),
            "syncedAt": state.get("syncedAt"), "head": state.get("head"), "states": list(STATES), "glyph": GLYPH, "label": LABEL,
            "data": data_view(plan, state)}
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = tpl.read_text(encoding="utf-8")
    mm = find_asset("mindmap.js")
    mm_js = mm.read_text(encoding="utf-8").replace("</script", "<\\/script") if mm else "function mindMap(host){host.textContent='Mind map unavailable: run init --refresh-tool.';return {expandAll:function(){},collapseAll:function(){},zoom:function(){},fit:function(){}};}"
    html = html.replace("/*__MINDMAP_JS__*/", mm_js)
    return html.replace("__APP_NAME__", _html_escape(plan["app"].get("name", "App"))).replace("__BLUEPRINT_DATA__", payload)


def _html_escape(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def render_all(root, plan, state, quiet=False, check=False):
    comp = compute(root, plan, state)
    outputs = {"STRUCTURE.md": render_structure(plan, state, comp), "ROSTER.md": render_roster(plan, state, comp)}
    m = render_map(plan, state, comp)
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
    if m is None and not quiet:
        print("map.template.html not found next to the script; map.html skipped. Run init --refresh-tool.")
    return stale
