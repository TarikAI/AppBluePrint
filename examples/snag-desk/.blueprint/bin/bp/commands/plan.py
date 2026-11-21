"""commands.plan: init, validate, render, contracts, export, moved from blueprint.py 1.3.0 (contract 1.5).

init --refresh-tool differs from 1.3.0 as contract 1.5 row b says: the 1.7 copy order, the upgrade message and the
"already the project's copy" early return.
"""
import csv
import json
import os
import sys
from pathlib import Path

from bp.version import TOOL_VERSION
from bp.core import (BlueprintError, append_log, bp_dir, die, emit, ensure_gitignore, find_root, norm, read_json,
                     state_lock, write_json, write_state)
from bp.learn import _copy_version, _file_tool_version
from bp.migrate import plan_version, upgrade_state
from bp.plan import SECTIONS_V2, SKELETON, load_plan, owner_of
from bp.registry import command_built
from bp.validate import record_friction, validate_findings, validate_plan
from bp.views.render import render_all
from bp.export import agenticteam_tasks, shq
from bp.install import copy_tool


def register(name, p):
    """Arguments of init, validate, render, contracts and export (cli.main calls this per subcommand, contract 1.4)."""
    if name == "init":
        p.add_argument("--name")
        p.add_argument("--preset", help="stack preset id, e.g. rust-agent-engine")
        p.add_argument("--refresh-tool", action="store_true", help="only re-copy the script and map template")
        p.set_defaults(fn=cmd_init)
    elif name == "validate":
        p.add_argument("--team", help="AgenticTeam team.json: check owner ids and write access")
        p.add_argument("--json", action="store_true", help="print the findings and counts as one JSON object")
        p.set_defaults(fn=cmd_validate)
    elif name == "render":
        p.add_argument("--check", action="store_true", help="fail if the views are out of date")
        p.add_argument("--force", action="store_true", help="render even if validate has errors")
        p.set_defaults(fn=cmd_render)
    elif name == "contracts":
        p.add_argument("--write", action="store_true")
        p.add_argument("--module", action="append", metavar="ID", help="only this module (repeatable); default: every module with a path")
        p.set_defaults(fn=cmd_contracts)
    elif name == "export":
        p.add_argument("format", choices=["codeowners", "csv", "json", "agenticteam"])
        p.add_argument("--team", help="AgenticTeam team.json, to check write access")
        p.add_argument("--json", action="store_true", help="agenticteam: print task objects instead of commands")
        p.set_defaults(fn=cmd_export)


def cmd_validate(args):
    root = find_root(args.root)
    plan = load_plan(root)
    team = None
    if args.team:
        team = read_json(Path(args.team))
        if team is None:
            die(f"team file not found: {args.team}")
    found = validate_findings(plan, team, root=root)
    errors = [f for f in found if f["level"] == "error"]
    warnings = [f for f in found if f["level"] == "warning"]
    if errors:
        record_friction(root, errors)
    if getattr(args, "json", False):  # contract 7.2: the findings and the counts
        counts = {"files": len(plan["files"]), "patterns": len(plan["patterns"]), "modules": len(plan["modules"]),
                  "phases": len(plan["phases"]), "owners": len(plan["owners"]), "roster": len(plan["roster"]),
                  "resources": len(plan["resources"])}  # owners: owner rules; roster: roster entries (review N9)
        counts.update({k: len(plan.get(k) or []) for k in SECTIONS_V2})
        # 7.1: "ok" is true because the command ran (a failure envelope carries a 7.3 error); "valid" says whether the
        # plan has no errors, and the exit status is 1 when it has
        emit(args, "validate", {"valid": not errors, "errors": errors, "warnings": warnings, "counts": counts}, [])
        sys.exit(1 if errors else 0)
    for w in warnings:
        print("warning:", w["message"])
    for e in errors:
        print("error:", e["message"])
    n_files, n_pat = len(plan["files"]), len(plan["patterns"])
    if errors:
        print(f"\n{len(errors)} error(s), {len(warnings)} warning(s). The blueprint is not ready.")
        sys.exit(1)
    res = f", {len(plan['resources'])} data resources" if plan["resources"] else ""
    new = "".join(f", {len(plan[k])} {k}" for k in ("features", "milestones", "tasks", "unknowns") if plan.get(k))
    print(f"OK: {n_files} files, {n_pat} patterns, {len(plan['modules'])} modules, "
          f"{len(plan['phases'])} phases, {len(plan['roster'])} owners{res}{new}. {len(warnings)} warning(s).")


def cmd_render(args):
    from bp.status import load_state
    root = find_root(args.root)
    plan = load_plan(root)
    errors, _ = validate_plan(plan, root=root)
    if errors and not args.force:
        die(f"The blueprint has {len(errors)} error(s); run validate first (or render --force for a draft view).")
    state = load_state(root)
    if args.check:
        stale = render_all(root, plan, state, check=True)
        if stale:
            print("Out of date: " + ", ".join(stale) + ". Run: blueprint.py render")
            sys.exit(1)
        print("Views are current.")
        return
    render_all(root, plan, state, fail_on_conflict=True)  # only render fails on a view conflict (8)


CONTRACT_TEMPLATE = """# {name}

<!-- Folder contract from the app blueprint. The owner keeps it true as the code lands. -->

## Overview
{purpose}

Owner: {owner}. Layer: {layer}. Depends on: {deps}.

## Interface
{interface}

## Invariants
- TBD by {owner}: rules every caller and change must keep.

## Side effects
- TBD by {owner}: database writes, network calls, files, events.

## Verification
- Run: `{verify}`
"""


def cmd_contracts(args):
    from bp.status import compute, load_state
    root = find_root(args.root)
    plan = load_plan(root)
    comp = compute(root, plan, load_state(root))
    made = 0
    wanted = list(args.module or [])
    unknown = [w for w in wanted if w not in {m.get("id") for m in plan["modules"]}]
    if unknown:
        die(f"unknown module id(s): {', '.join(unknown)}. Known: {', '.join(str(m.get('id')) for m in plan['modules'])}", 64)
    for m in plan["modules"]:
        if wanted and m.get("id") not in wanted:
            continue
        if not m.get("path"):
            if wanted:
                print(f"module {m.get('id')} has no path, so it gets no contract")
            continue
        path = root / norm(m["path"]) / "README.md"
        mod_rows = [r for r in comp["rows"] if r["module"] == m["id"]]
        produces = sorted({p for r in mod_rows for p in r["produces"]})
        owner = owner_of(plan, norm(m["path"]).rstrip("/") + "/README.md")[0] or "(unowned)"
        text = CONTRACT_TEMPLATE.format(
            name=m["id"], purpose=m.get("purpose", ""), owner=owner, layer=m.get("layer", "-"),
            deps=", ".join(m.get("dependsOn", [])) or "nothing",
            interface="\n".join(f"- {p}" for p in produces) or "- (no produces listed in the blueprint yet)",
            verify=m.get("verify") or "set a verify command on the module")
        if path.exists():
            print(f"keep  {norm(os.path.relpath(path, root))} (exists)")
            continue
        if args.write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            made += 1
            print(f"wrote {norm(os.path.relpath(path, root))}")
        else:
            print(f"would write {norm(os.path.relpath(path, root))}")
            made += 1
    if args.write:
        append_log(root, {"event": "contracts", "written": made})
        if made:
            print("Run the project's own tests now: a new README can break tooling that treats every entry in a folder "
                  "as an item. Remove the README for that module if it does.")
    elif made:
        print("Add --write to create the missing ones. Existing READMEs are never overwritten.")


def cmd_export(args):
    from bp.data import data_view
    from bp.status import compute, load_state
    root = find_root(args.root)
    plan = load_plan(root)
    state = load_state(root)
    comp = compute(root, plan, state)
    fmt = args.format
    if fmt == "codeowners":
        gh = {r["id"]: r.get("github") for r in plan["roster"]}
        print("# Generated from .blueprint/blueprint.json owner rules (last match wins).")
        for rule in plan["owners"]:
            handle = gh.get(rule["owner"])
            pat = norm(rule["pattern"])  # forward slashes: GitHub reads a backslash as an escape
            pat = pat if pat.startswith(("/", "*")) else "/" + pat
            if handle:
                print(f"{pat} {handle}")
            else:
                print(f"# {pat} {rule['owner']}  (agent or no GitHub handle in roster)")
    elif fmt == "csv":
        wr = csv.writer(sys.stdout, lineterminator="\n")
        wr.writerow(["kind", "path", "action", "module", "phase", "owner", "status", "contract", "purpose", "verify"])
        for r in comp["rows"]:
            wr.writerow(["file", r["path"], r["action"], r["module"], r["phase"], r["owner"], r["status"], r["contract"], r["purpose"], r["verify"]])
        for p in comp["patterns"]:
            wr.writerow(["pattern", p["glob"], "create", p["module"], p["phase"], p["owner"], f"{len(p['instances'])} on disk", False, p["purpose"], p["verify"]])
    elif fmt == "json":
        from bp.views.context import build_context, status_json
        base = {"app": plan["app"], "phases": plan["phases"], "roster": plan["roster"], "rows": comp["rows"],
                "patterns": comp["patterns"], "unplanned": state.get("unplanned", []), "syncedAt": state.get("syncedAt"),
                "data": data_view(plan, state)}
        print(json.dumps(status_json(build_context(root, plan, state), base), indent=2, ensure_ascii=False))
    elif fmt == "agenticteam":
        team = read_json(Path(args.team)) if args.team else None
        tasks = agenticteam_tasks(plan, comp, team)
        bad = [t for t in tasks if t.get("writable") is False]
        if bad:
            for t in bad:
                print(f"error: {t['owner']} cannot write files in AgenticTeam (access is not workspace-write): {t['id']}", file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json.dumps(tasks, indent=2, ensure_ascii=False))
            return
        cli = "python .agentic-team/bin/agentic_team.py"
        print("# AgenticTeam build tasks generated from the app blueprint. Review, then run in order.")
        for t in tasks:
            parts = [cli, "add-task", "--project .", "--id", t["id"], "--title", shq(t["title"]), "--owner", t["owner"], "--stage", t["stage"],
                     "--action", t["action"], "--target", t["target"]]
            parts += [x for p in t["paths"] for x in ("--path", shq(p))]
            parts += [x for d in t["dependsOn"] for x in ("--depends-on", d)]
            parts += [x for i in t["inputs"] for x in ("--input", i)]
            parts += [x for a in t["acceptance"] for x in ("--acceptance", shq(a))]
            parts += [x for c in t["checks"] for x in ("--check", shq(c))]
            parts += [x for v in t.get("verifies", []) for x in ("--verifies", v)]
            print(" ".join(parts))
    else:
        die(f"unknown format {fmt}", 64)


def cmd_init(args):
    root = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    d = bp_dir(root)
    plan_path = d / "blueprint.json"
    if args.refresh_tool:
        if not plan_path.exists():
            die("No blueprint here to refresh.")
        bin_dir = d / "bin"
        # Single-file is decided by the launcher's content, not copy_layout(): an upgrade stopped after step 2
        # leaves the 1.x file beside a new bp/, and that is still an upgrade from the single-file tool.
        launcher = bin_dir / "blueprint.py"
        text = launcher.read_text(encoding="utf-8", errors="replace") if launcher.is_file() else ""
        single = bool(text) and "from bp.api import" not in text
        old = (_file_tool_version(launcher) if single else None) or _copy_version(bin_dir) or "unknown"
        if not copy_tool(root):
            return  # run from the project's own copy: copy_tool said what to run instead
        try:  # under the lock, so it never races a first lock adding the same lines
            with state_lock(root):
                ensure_gitignore(root)  # state.lock and *.tmp are never project changes
        except BlueprintError:  # busy or no lock here: the tool is refreshed; the lines are still worth adding
            ensure_gitignore(root)
        if single:
            print(f"Upgraded .blueprint/bin from the single-file tool {old} to the package tool {TOOL_VERSION}.")
        else:
            print(f"Refreshed .blueprint/bin to tool {TOOL_VERSION}.")
        try:
            raw = read_json(plan_path)
        except (OSError, ValueError):
            raw = None
        if plan_version(raw) == 1:  # contract 1.7; the migrate hint only once the command exists (1.5 row d)
            hint = (" To use milestones, features, tasks and unknowns, run: python .blueprint/bin/blueprint.py migrate"
                    if command_built("migrate") else "")
            print("Your blueprint is schema version 1. It keeps working." + hint)
        return
    if plan_path.exists():
        die(f"{plan_path} already exists. Edit it, or use --refresh-tool to update the copied script.", 1)
    d.mkdir(parents=True, exist_ok=True)
    plan = json.loads(json.dumps(SKELETON))
    plan["app"]["name"] = args.name or root.name
    plan["app"]["preset"] = args.preset or ""
    write_json(plan_path, plan)
    with state_lock(root):
        ensure_gitignore(root)  # state.lock and *.tmp are never project changes
        write_state(root, upgrade_state({"files": {}, "patterns": {}, "unplanned": [], "syncedAt": None, "head": None}))
    (d / "log.jsonl").touch()
    copy_tool(root)
    readme = d / "README.md"
    if not readme.exists():
        readme.write_text(
            "# .blueprint\n\n"
            "- `blueprint.json`: the plan. Agents propose changes; a person approves them.\n"
            "- `state.json`, `log.jsonl`: progress. Written only by `bin/blueprint.py`.\n"
            "- `STRUCTURE.md`, `ROSTER.md`, `map.html`: generated views. Do not edit.\n\n"
            "Common commands (run from the repo root):\n\n"
            "```\npython .blueprint/bin/blueprint.py validate\npython .blueprint/bin/blueprint.py sync\n"
            "python .blueprint/bin/blueprint.py status --next\npython .blueprint/bin/blueprint.py owner <path>\n```\n",
            encoding="utf-8")
    append_log(root, {"event": "init", "name": plan["app"]["name"], "preset": plan["app"]["preset"], "tool": TOOL_VERSION})
    print(f"Created .blueprint/ in {root}. Next: fill blueprint.json, then run validate and render.")
