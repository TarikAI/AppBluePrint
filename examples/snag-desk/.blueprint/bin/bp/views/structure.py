"""views.structure: STRUCTURE.md, moved from blueprint.py 1.3.0 (contract 1.5), with the 2.0 tags of 8.3."""
import io
import re

from bp import registry
from bp.core import norm
from bp.plan import GLYPH, LABEL, STATES
from bp.data import data_view


def build_tree(rows, patterns, modules):
    tree = {}
    for r in rows:
        node = tree
        parts = r["path"].split("/")
        for d in parts[:-1]:
            node = node.setdefault(d + "/", {})
        node[parts[-1]] = r
    for p in patterns:
        node = tree
        parts = norm(p["glob"]).split("/")
        for d in parts[:-1]:
            node = node.setdefault(d + "/", {})
        node[parts[-1]] = dict(p, kind="pattern")
    return tree


def tree_lines(tree, module_by_path, prefix="", base="", tags=None):
    """The tree as text lines. tags (2.0, from the file_tags hooks): {path or glob: [tag]}, shown as " [tag]" after
    the file or pattern line; none given, the lines are 1.3.0's."""
    lines = []
    keys = sorted(tree, key=lambda k: (not k.endswith("/"), k.lower()))
    for i, k in enumerate(keys):
        last = i == len(keys) - 1
        branch = "└── " if last else "├── "
        v = tree[k]
        if k.endswith("/"):
            path = base + k
            mod = module_by_path.get(path.rstrip("/"))
            note = f"  # {mod['purpose']} [{mod['id']}]" if mod else ""
            lines.append(prefix + branch + k + note)
            lines += tree_lines(v, module_by_path, prefix + ("    " if last else "│   "), path, tags)
        elif v.get("kind") == "pattern":
            n = len(v.get("instances") or [])
            lines.append(f"{prefix}{branch}{k}  ⟨pattern {v['id']}: {v['purpose']}; {n} on disk⟩ ({v['owner']}, {v['phase']})"
                         + _tag_text(tags, v.get("glob")))
        else:
            act = "" if v["action"] == "create" else f" [{v['action']}]"
            star = " ★" if v["contract"] else ""
            lines.append(f"{prefix}{branch}{k}{act}{star}  {GLYPH[v['status']]} {v['purpose']} ({v['owner']}, {v['phase']})"
                         + _tag_text(tags, v.get("path")))
    return lines


def _tag_text(tags, key):
    found = (tags or {}).get(key) or []
    return "".join(f" [{t}]" for t in found)


def file_tags(ctx):
    """{path or glob: [tag]}: every file_tags hook's result, concatenated in DOMAIN order (contract 1.4, 8.3)."""
    out = {}
    for _, part in registry.collect("file_tags", ctx):
        for key, tags in (part or {}).items():
            out.setdefault(key, []).extend(str(t) for t in tags)
    return out


_LINE_BREAKS = re.compile("[\x0d\x0a\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]+")


def one_line(value):
    """Plan or state text as status prints it: each run of line-break characters (CR, LF, VT, FF, FS, GS, RS, NEL,
    U+2028, U+2029) becomes one space, and every other character, tabs and repeated spaces included, is printed as
    1.3.0 printed it (review of #16, M1). An invalid plan can hold a multi-line id, name or path; status never lets
    one break a line of its own output."""
    return _LINE_BREAKS.sub(" ", str(value))


def md_cell(s):
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")


def text_cell(s):
    """A plain table cell for a version 2 plan: one line; '&' and '<' as entities (as the module views write them, so no
    plan text becomes HTML); the backslash escaped before the pipe, so a value ending in one cannot escape the cell's
    closing pipe. A version 1 plan keeps 1.3.0's bytes (md_cell, or the raw value where 1.3.0 wrote it raw)."""
    text = one_line(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;")
    return text.replace("\\", "\\\\").replace("|", "\\|")


def code_cell(s):
    """The text of a code span in a version 2 table cell: one line, the backslash escaped before the pipe (a table cell
    is split on unescaped pipes even inside a code span), no entities (Markdown shows a code span's text as it is)."""
    return one_line(s if s is not None else "").replace("\\", "\\\\").replace("|", "\\|")


def code_span(s):
    """A code span for a version 2 table cell, backtick-safe: fenced with one backtick more than the longest run of
    backticks in the text, and padded with a space on each side when the text holds a backtick (or starts and ends
    with a space), so the span always ends where it should. An empty value is an empty cell."""
    text = code_cell(s)
    if not text:
        return ""
    runs = [len(r) for r in re.findall("`+", text)]
    fence = "`" * (max(runs) + 1 if runs else 1)
    pad = " " if runs or (len(text) > 1 and text.startswith(" ") and text.endswith(" ")) else ""
    return f"{fence}{pad}{text}{pad}{fence}"


class _Cells:
    """How the core's tables write plan text (item 6 and review of #17, F1). A version 1 plan keeps 1.3.0's bytes:
    md_cell where 1.3.0 used it, the raw value where it wrote it raw. A version 2 plan follows the one Markdown rule
    for every table cell: one line; backslash, then pipe, escaped; '<' and '&' as entities outside code spans; code
    spans backtick-safe (code_span).
    cell(s)   a plain cell              raw(s)       a plain cell 1.3.0 wrote raw
    span(s)   a code span (md_cell'd)   raw_span(s)  a code span 1.3.0 wrote raw
    spans(xs) a list of code spans in one cell"""

    def __init__(self, v1):
        self.v1 = v1
        if v1:
            self.cell, self.raw = md_cell, (lambda s: s)
            self.span = lambda s: f"`{md_cell(s)}`"
            self.raw_span = lambda s: f"`{s}`"
            self.spans = lambda xs: md_cell(", ".join("`" + x + "`" for x in xs))
        else:
            self.cell = self.raw = text_cell
            self.span = self.raw_span = code_span
            self.spans = lambda xs: ", ".join(code_span(x) for x in xs)


def cells(plan):
    """The cell writers (_Cells) for this plan's version."""
    return _Cells(plan.get("_from") == 1 or plan.get("version") == 1)


def render_structure(plan, state, comp, ctx=None):
    """STRUCTURE.md. With ctx (render_all), the file_tags hooks tag tree lines and, when the plan has features, a
    re-verify line follows the counts (contract 8.3); a plan without features renders exactly as in 1.3.0."""
    app = plan["app"]
    rows, pats = comp["rows"], comp["patterns"]
    T = cells(plan)
    tags = file_tags(ctx) if ctx is not None else None
    n_stale = sum(1 for r in rows if r.get("stale")) if ctx is not None and plan.get("features") else 0
    module_by_path = {norm(m.get("path", "")).rstrip("/"): m for m in plan["modules"] if m.get("path")}
    out = io.StringIO()
    w = out.write
    w(f"# {app.get('name')}: structure\n\n")
    w("<!-- Generated by blueprint.py render from .blueprint/blueprint.json. Do not edit. -->\n\n")
    if app.get("summary"):
        w(app["summary"] + "\n\n")
    c = comp["counts"]
    total = len(rows)
    w(f"**{total} planned files, {len(pats)} patterns.** Status: " + ", ".join(f"{GLYPH[s]} {LABEL[s]} {c[s]}" for s in STATES if c[s]) +
      f". Last sync: {state.get('syncedAt') or 'never'}" + (f" at `{state.get('head')}`" if state.get("head") else "") + ".\n\n")
    if n_stale:
        w(f"↻ {n_stale} file(s) need re-verifying (inputs changed): verify --stale --run\n\n")
    if plan["stack"]:
        w("## Stack\n\n| Part | Choice | Version | Docs | Checked |\n|---|---|---|---|---|\n")
        for s in plan["stack"]:
            w(f"| {T.cell(s.get('part'))} | {T.cell(s.get('choice'))} | {T.cell(s.get('version'))} | {T.cell(s.get('docs'))} | {T.cell(s.get('verified'))} |\n")
        w("\n")
    ap = plan.get("approach") or {}
    if ap:
        w("## Build approach\n\n")
        for k, v in ap.items():
            w(f"- **{k}:** {v}\n")
        w("\n")
    w("## Tree\n\n`★` shared contract · " + " · ".join(f"`{GLYPH[s]}` {LABEL[s]}" for s in STATES) + "\n\n```text\n")
    w(".\n" + "\n".join(tree_lines(build_tree(rows, pats, plan["modules"]), module_by_path, tags=tags)) + "\n```\n\n")
    if plan["modules"]:
        w("## Modules\n\n```mermaid\nflowchart LR\n")
        for m in plan["modules"]:
            layer = (" · " + m["layer"]) if m.get("layer") else ""
            w(f"  {re.sub(r'[^A-Za-z0-9_]', '_', m['id'])}[\"{m['id']}{layer}\"]\n")
        for m in plan["modules"]:
            for d in m.get("dependsOn", []):
                w(f"  {re.sub(r'[^A-Za-z0-9_]', '_', m['id'])} --> {re.sub(r'[^A-Za-z0-9_]', '_', d)}\n")
        w("```\n\n| Module | Path | Layer | Purpose | Depends on | Verify |\n|---|---|---|---|---|---|\n")
        for m in plan["modules"]:
            w(f"| {T.raw(m['id'])} | {T.span(m.get('path'))} | {T.cell(m.get('layer'))} | {T.cell(m.get('purpose'))} | {T.cell(', '.join(m.get('dependsOn', [])))} | {T.span(m.get('verify'))} |\n")
        w("\n")
    if plan["stores"] or plan["resources"]:
        dv = data_view(plan, state)
        w("## Data\n\nTables, environment variables and services the code connects to. Declared in the blueprint; checked by `sync` against schema files and code.\n\n")
        if dv["stores"]:
            w("| Store | Kind | Schema files | Purpose |\n|---|---|---|---|\n")
            for s in dv["stores"]:
                w(f"| {T.raw(s.get('id'))} | {T.raw(s.get('kind'))} | {T.spans(s.get('schema', []))} | {T.cell(s.get('purpose'))} |\n")
            w("\n")
        if dv["resources"]:
            w("| Resource | Kind | Store | Defined in | Owner | Read by | Written by |\n|---|---|---|---|---|---|---|\n")
            for r in dv["resources"]:
                w(f"| {T.raw(r['id'])} | {T.raw(r['kind'])} | {T.cell(r['store'])} | {T.raw_span(r['definedIn']) if r['definedIn'] else ''} | {T.cell(r['owner'])} | "
                  f"{T.spans(r['readers'])} | {T.spans(r['writers'])} |\n")
            w("\n```mermaid\nflowchart LR\n")
            mods_used = {}
            files_mod = {f["path"]: f.get("module") for f in plan["files"]}
            files_mod.update({p.get("glob"): p.get("module") for p in plan["patterns"]})
            for r in dv["resources"]:
                for x in r["readers"]:
                    mods_used.setdefault((files_mod.get(x), r["id"]), set()).add("read")
                for x in r["writers"]:
                    mods_used.setdefault((files_mod.get(x), r["id"]), set()).add("write")
            sid = lambda s: re.sub(r"[^A-Za-z0-9_]", "_", str(s))
            for r in dv["resources"]:
                w(f"  r_{sid(r['id'])}[(\"{r['id']}\")]\n")
            for (m, rid), acc in sorted(mods_used.items(), key=lambda x: (str(x[0][0]), x[0][1])):
                if m:
                    w(f"  m_{sid(m)}[\"{m}\"] -->|{'/'.join(sorted(acc))}| r_{sid(rid)}\n")
            w("```\n\n")
        if dv["issues"]:
            w(f"**Data drift: {len(dv['issues'])} issue(s)**\n\n")
            for i in dv["issues"][:100]:
                w(f"- [{i['type']}] {i['detail']}\n")
            w("\n")
        if dv.get("live"):
            lv = dv["live"]
            w(f"Live check ({lv['source']}, {lv['at']}): {len(lv['tables'])} tables; not in any migration: {', '.join(lv['liveOnly']) or 'none'}; "
              f"migrations not applied: {', '.join(lv['notApplied']) or 'none'}.\n\n")
    w("## Phases\n\n| Phase | Name | Goal | Exit | Files | Verified |\n|---|---|---|---|---|---|\n")
    for ph in plan["phases"]:
        pr = [r for r in rows if r["phase"] == ph["id"]]
        w(f"| {T.raw(ph['id'])} | {T.cell(ph.get('name'))} | {T.cell(ph.get('goal'))} | {T.cell(ph.get('exit'))} | {len(pr)} | {sum(1 for r in pr if r['status'] == 'verified')} |\n")
    w("\n")
    contracts = [r for r in rows if r["contract"]]
    if contracts:
        w("## Shared contracts\n\nThese files are depended on by several owners. Change them only through their owner.\n\n| File | Owner | Produces | Purpose |\n|---|---|---|---|\n")
        for r in contracts:
            w(f"| {T.raw_span(r['path'])} | {T.raw(r['owner'])} | {T.cell(', '.join(r['produces']))} | {T.cell(r['purpose'])} |\n")
        w("\n")
    if pats:
        w("## Repeated-file patterns\n\n| Pattern | Glob | One instance contains | Owner | Phase | On disk |\n|---|---|---|---|---|---|\n")
        for p in pats:
            w(f"| {T.raw(p['id'])} | {T.span(p['glob'])} | {T.cell(p['instance'])} | {T.raw(p['owner'])} | {T.raw(p['phase'])} | {len(p['instances'])} |\n")
        w("\n")
    w("## Files\n\n| File | Action | Module | Phase | Owner | Status | Purpose | Consumes | Produces | Verify |\n|---|---|---|---|---|---|---|---|---|---|\n")
    for r in rows:
        w(f"| {T.raw_span(r['path'])} | {T.raw(r['action'])} | {T.raw(r['module'])} | {T.raw(r['phase'])} | {T.raw(r['owner'])} | {GLYPH[r['status']]} {LABEL[r['status']]} | {T.cell(r['purpose'])} | {T.cell(', '.join(r['consumes']))} | {T.cell(', '.join(r['produces']))} | {T.span(r['verify'])} |\n")
    w("\n")
    if plan["decisions"]:
        w("## Decisions\n\n")
        for d in plan["decisions"]:
            w(f"- **{d.get('id')}:** {d.get('decision')}" + (f" Why: {d['why']}" if d.get("why") else "") + (f" ({d['source']})" if d.get("source") else "") + "\n")
        w("\n")
    un = state.get("unplanned") or []
    if un:
        w(f"## Drift: {len(un)} unplanned file(s)\n\nOn disk but not in the blueprint. Add each to the plan, cover it with a pattern, ignore it, or remove it.\n\n")
        for p in un[:200]:
            w(f"- `{p}`\n")
        if len(un) > 200:
            w(f"- ... and {len(un) - 200} more\n")
    return out.getvalue()
