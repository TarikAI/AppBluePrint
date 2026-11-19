"""commands.progress: sync, status, owner, claim, mark, verify, guard, hook, moved from blueprint.py 1.3.0 (contract 1.5).

Domain modules (status, data, verify) are imported inside the handlers (contract 1.2).
"""
import argparse
import json
import os
import sys
from pathlib import Path

from bp.core import append_log, bp_dir, die, find_root, git_head, norm, now_iso
from bp.globs import pattern_instance_rx
from bp.plan import GLYPH, LABEL, STATES, load_plan, load_state, owner_of, planned_file, save_state
from bp.registry import command_built
from bp.validate import validate_plan
from bp.views.render import render_all


def cmd_sync(args):
    from bp.data import scan_data
    from bp.status import compute, repo_files
    root = find_root(args.root)
    plan = load_plan(root)
    state = load_state(root)
    ignored_tracked = []
    disk = repo_files(root, plan, ignored_tracked)
    disk_set = set(disk)
    planned = {f["path"] for f in plan["files"]}
    module_readmes = {norm(m.get("path", "")).rstrip("/") + "/README.md" for m in plan["modules"] if m.get("path")}
    pat_rx = [(p.get("id"), pattern_instance_rx(p.get("glob", ""))) for p in plan["patterns"] if p.get("glob")]
    patterns = {pid: {"instances": []} for pid, _ in pat_rx}
    unplanned = []
    for p in disk:
        if p in planned or p in module_readmes:
            continue
        hit = False
        for pid, rx in pat_rx:
            if rx.match(p):
                patterns[pid]["instances"].append(p)
                hit = True
                break
        if not hit:
            unplanned.append(p)
    missing_modify = [f["path"] for f in plan["files"] if f.get("action") == "modify" and f["path"] not in disk_set]
    state["patterns"] = patterns
    state["unplanned"] = unplanned
    state["ignoredTracked"] = {"count": len(ignored_tracked), "paths": ignored_tracked[:20]}
    state["missingModify"] = missing_modify
    state["syncedAt"] = now_iso()
    state["head"] = git_head(root)
    data_issues = []
    if plan["stores"] or plan["resources"]:
        state["data"] = scan_data(root, plan, state, disk)
        data_issues = state["data"]["issues"]
    save_state(root, state)
    comp = compute(root, plan, state)
    c = comp["counts"]
    append_log(root, {"event": "sync", "head": state["head"], "counts": c, "unplanned": len(unplanned), "dataIssues": len(data_issues)})
    if not args.quiet:
        print(f"Synced {len(disk)} files on disk against {len(plan['files'])} planned files and {len(plan['patterns'])} patterns.")
        print("  " + "  ".join(f"{GLYPH[s]} {LABEL[s]} {c[s]}" for s in STATES))
        if unplanned:
            print(f"  Unplanned files (drift): {len(unplanned)}")
            for p in unplanned[:15]:
                print("    +", p)
            if len(unplanned) > 15:
                print(f"    ... and {len(unplanned) - 15} more (see map.html, Drift)")
        if ignored_tracked:
            print(f"  Ignored by default rules: {len(ignored_tracked)} tracked file(s) (e.g. {', '.join(ignored_tracked[:3])}). "
                  "Plan them or cover them with a pattern to track them.")
        if missing_modify:
            print(f"  Planned to modify but missing: {len(missing_modify)}")
            for p in missing_modify[:10]:
                print("    !", p)
        if state.get("data"):
            dt = state["data"]
            print(f"  Data: {len(dt['tables'])} table(s) in schema files, {len(plan['resources'])} declared resource(s), "
                  f"{len(dt['observed'])} file(s) touching data, {len(data_issues)} issue(s).")
            for i in data_issues[:12]:
                print(f"    ~ [{i['type']}] {i['detail']}")
            if len(data_issues) > 12:
                print(f"    ... and {len(data_issues) - 12} more (see map.html, Data)")
    if not args.no_render:
        errors, _ = validate_plan(plan)
        if errors:
            if not args.quiet:
                print(f"  Views not refreshed: the blueprint has {len(errors)} validation error(s). Run validate.")
        else:
            render_all(root, plan, state, quiet=True)
    serious = [i for i in data_issues if i["type"] != "declared-not-found"]
    if getattr(args, "strict", False) and (unplanned or missing_modify or serious):
        sys.exit(1)


def cmd_status(args):
    from bp.status import compute, next_ready, summarize
    root = find_root(args.root)
    plan = load_plan(root)
    state = load_state(root)
    comp = compute(root, plan, state)
    rows = comp["rows"]
    if args.owner:
        rows = [r for r in rows if r["owner"] == args.owner]
    if args.phase:
        rows = [r for r in rows if r["phase"] == args.phase]
    if args.json:
        print(json.dumps({"rows": rows, "patterns": comp["patterns"], "unplanned": state.get("unplanned", []),
                          "syncedAt": state.get("syncedAt")}, indent=2, ensure_ascii=False))
        return
    total = len(rows)
    done = sum(1 for r in rows if r["status"] == "verified")
    built = sum(1 for r in rows if r["status"] in ("built", "verified"))
    print(f"{plan['app'].get('name')}: {total} planned files"
          + (f" for {args.owner}" if args.owner else "") + (f" in {args.phase}" if args.phase else ""))
    print(f"  verified {done}/{total}, built or better {built}/{total}. Last sync: {state.get('syncedAt') or 'never'}")
    print("\n  By phase:")
    for ph in plan["phases"]:
        d = summarize([r for r in rows if r["phase"] == ph["id"]], "phase").get(ph["id"])
        if d:
            n = sum(d.values())
            print(f"    {ph['id']:<4} {ph.get('name', ''):<28} " + " ".join(f"{GLYPH[s]}{d[s]}" for s in STATES if d[s]) + f"  ({d['verified']}/{n} verified)")
    if not args.owner:
        print("\n  By owner:")
        for o, d in sorted(summarize(rows, "owner").items()):
            n = sum(d.values())
            print(f"    {o:<28} " + " ".join(f"{GLYPH[s]}{d[s]}" for s in STATES if d[s]) + f"  ({d['verified']}/{n} verified)")
    blocked = [r for r in rows if r["status"] == "blocked"]
    if blocked:
        print("\n  Blocked:")
        for r in blocked:
            print(f"    ▲ {r['path']} ({r['owner']}): {r.get('note') or ''}")
    if args.next:
        ready = next_ready(plan, comp["rows"], args.owner)
        print("\n  Ready to build next:")
        for r in ready[:20]:
            print(f"    {r['path']}  ({r['owner']}, {r['phase']})")
        if not ready:
            print("    nothing: everything in the current phase is claimed, built or waiting on a dependency")
    if plan.get("_from") == 1:  # contract 3.3; the migrate hint only once the command exists (1.5 row d)
        print("\nSchema version 1: " + ("run migrate to use milestones, features, tasks and unknowns."
                                        if command_built("migrate") else "it keeps working."))


def cmd_owner(args):
    root = find_root(args.root)
    plan = load_plan(root)
    path = norm(args.path)
    owner, rule, matches = owner_of(plan, path)
    f = planned_file(plan, path)
    pat = next((p for p in plan["patterns"] if pattern_instance_rx(p.get("glob", "")).match(path)), None)
    where = "planned file" if f else (f"instance of pattern {pat['id']}" if pat else "NOT in the blueprint")
    if owner is None:
        print(f"{path}: no owner ({where})")
        sys.exit(1)
    r = plan["owners"][rule]
    print(f"{path}: {owner} (rule {rule + 1}: {r['pattern']}{', override' if r.get('override') else ''}; {where})")
    if len(matches) > 1:
        print("  also matched by: " + ", ".join(f"rule {m + 1} {plan['owners'][m]['pattern']} -> {plan['owners'][m]['owner']}" for m in matches[:-1]))


def cmd_claim(args):
    from bp.status import require_owner, set_file_state
    root = find_root(args.root)
    plan = load_plan(root)
    path = norm(args.path)
    if not planned_file(plan, path) and not any(pattern_instance_rx(p.get("glob", "")).match(path) for p in plan["patterns"]):
        die(f"{path} is not in the blueprint. Propose a blueprint change first.", 3)
    require_owner(plan, path, args.by, args.force)
    set_file_state(root, path, "claimed", args.by, args.note)
    print(f"{args.by} claimed {path}")


def cmd_mark(args):
    from bp.status import require_owner, set_file_state
    root = find_root(args.root)
    plan = load_plan(root)
    path = norm(args.path)
    f = planned_file(plan, path)
    if not f and not any(pattern_instance_rx(p.get("glob", "")).match(path) for p in plan["patterns"]):
        die(f"{path} is not in the blueprint.", 3)
    require_owner(plan, path, args.by, args.force)
    if args.state == "built" and (f or {}).get("action") != "delete" and not (root / path).is_file():
        die(f"{path} does not exist on disk, so it cannot be built yet.", 3)
    if args.state == "blocked" and not args.note:
        die("Say why it is blocked: --note \"waiting on ...\"", 64)
    set_file_state(root, path, args.state, args.by, args.note)
    print(f"{path}: {args.state} (by {args.by})")


def cmd_verify(args):
    from bp.verify import record_file_proofs, run_check, verify_groups
    root = find_root(args.root)
    plan = load_plan(root)
    groups = verify_groups(plan, phase=args.phase, owner=args.owner)
    if not groups:
        print("No verify commands in scope.")
        return
    if not args.run:
        print("Verify commands (add --run to execute them; each passing command marks its present files verified):")
        for cmd, paths in groups.items():
            print(f"  {cmd}    # {len(paths)} file(s)")
        return
    state = load_state(root)
    failed = 0
    for cmd, paths in groups.items():
        print(f"$ {cmd}")
        result = run_check(cmd, root, args.timeout)
        ok = result["result"] == "pass"
        if result["result"] == "timeout":
            print(f"  timed out after {args.timeout}s")
        print(f"  {'passed' if ok else 'FAILED'} for {len(paths)} file(s)")
        failed += 0 if ok else 1
        record_file_proofs(root, plan, state, cmd, paths, result)
        append_log(root, {"event": "verify", "cmd": cmd, "ok": ok, "files": len(paths)})
    save_state(root, state)
    render_all(root, plan, state, quiet=True)
    sys.exit(1 if failed else 0)


def cmd_guard(args):
    from bp.status import guard_decision
    root = find_root(args.root)
    plan = load_plan(root)
    code, msg = guard_decision(root, plan, args.path, args.by or os.environ.get("BLUEPRINT_OWNER"))
    if msg:
        print(msg, file=sys.stderr)
    sys.exit(code)


def cmd_hook(args):
    """Read a Claude Code hook payload on stdin. pre: block unplanned or foreign writes. post: sync quietly."""
    from bp.status import guard_decision
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)
    tool_input = payload.get("tool_input") or {}
    raw = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
    root = find_root(args.root or payload.get("cwd"))
    if not raw or not (bp_dir(root) / "blueprint.json").exists():
        sys.exit(0)
    try:
        rel = norm(os.path.relpath(Path(raw).resolve(), root))
    except ValueError:
        sys.exit(0)
    if rel.startswith("../"):
        sys.exit(0)
    plan = load_plan(root)
    if args.when == "pre":
        # BLUEPRINT_GUARD: block (default) stops the write and tells the agent why;
        # ask lets the person approve it; off disables the guard.
        mode = os.environ.get("BLUEPRINT_GUARD", "block")
        code, msg = guard_decision(root, plan, rel, os.environ.get("BLUEPRINT_OWNER"))
        if code and mode == "block":
            print(msg, file=sys.stderr)
            sys.exit(2)
        if code and mode == "ask":
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                                     "permissionDecisionReason": msg}}))
        sys.exit(0)
    a = argparse.Namespace(root=str(root), quiet=True, no_render=False)
    cmd_sync(a)
    sys.exit(0)
