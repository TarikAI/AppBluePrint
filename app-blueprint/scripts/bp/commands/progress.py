"""commands.progress: sync, status, owner, claim, mark, verify, guard, hook, moved from blueprint.py 1.3.0 (contract 1.5).

Domain modules (status, data, verify) are imported inside the handlers (contract 1.2). The 2.0 flags of these
commands (7.2) are declared here and reach their implementation through hooks (1.4); a flag whose module is not built
answers not-built:

    status --feature F          status_card (features)      status --features            features
    status --milestone M/--tasks   roadmap                  status --unknowns            unknowns
    verify --feature F [--criterion C] [--repeat N] [--by ID] [--json]   verify_feature (records)
"""
import argparse
import json
import os
import sys
from pathlib import Path

from bp import registry
from bp.version import TOOL_VERSION
from bp.core import BlueprintError, append_log, bp_dir, die, emit, find_root, git_head, norm, now_iso
from bp.globs import pattern_instance_rx
from bp.learn import record_incident
from bp.plan import GLYPH, LABEL, STATES, load_plan, load_state, owner_of, planned_file, save_state
from bp.registry import command_built
from bp.validate import validate_plan
from bp.views.context import blocked_by, build_context, status_json
from bp.views.render import render_all

# status flags of 2.0 -> the module that implements them (through its status_lines or status_card hook)
STATUS_FLAGS = {"feature": "features", "features": "features", "milestone": "roadmap", "tasks": "roadmap",
                "unknowns": "unknowns"}


def not_built(what, module):
    """The 7.3 not-built error for a 2.0 feature whose module is missing from this build."""
    return BlueprintError("not-built", f"{what} is not built in tool {TOOL_VERSION}; it arrives with "
                          f"{registry.ARRIVES.get(module, 'a later workstream')} (app-blueprint 2.0)",
                          next="update the skill, then run: python <skill>/scripts/blueprint.py init --refresh-tool")


def register(name, p):
    """Arguments of sync, status, owner, claim, mark, verify, guard and hook (cli.main calls this per subcommand)."""
    if name == "sync":
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--no-render", action="store_true")
        p.add_argument("--strict", action="store_true", help="exit 1 if there are unplanned files or missing modify targets (for CI)")
        p.set_defaults(fn=cmd_sync)
    elif name == "status":
        p.add_argument("--owner")
        p.add_argument("--phase")
        p.add_argument("--next", action="store_true", help="list files ready to build now")
        p.add_argument("--json", action="store_true")
        p.add_argument("--features", action="store_true", help="feature counts by status")
        p.add_argument("--feature", metavar="F", help="the card of one feature: files, criteria, evaluation")
        p.add_argument("--milestone", metavar="M", help="progress of one milestone")
        p.add_argument("--tasks", action="store_true", help="tasks and their status")
        p.add_argument("--unknowns", action="store_true", help="open questions and what they block")
        p.set_defaults(fn=cmd_status)
    elif name == "owner":
        p.add_argument("path")
        p.set_defaults(fn=cmd_owner)
    elif name == "claim":
        p.add_argument("path")
        p.add_argument("--by", required=True)
        p.add_argument("--note")
        p.add_argument("--force", action="store_true")
        p.set_defaults(fn=cmd_claim)
    elif name == "mark":
        p.add_argument("path")
        p.add_argument("state", choices=["built", "blocked", "planned"])
        p.add_argument("--by", required=True)
        p.add_argument("--note")
        p.add_argument("--force", action="store_true")
        p.set_defaults(fn=cmd_mark)
    elif name == "verify":
        p.add_argument("--phase")
        p.add_argument("--owner")
        p.add_argument("--run", action="store_true")
        p.add_argument("--timeout", type=int, default=900)
        p.add_argument("--stale", action="store_true", help="only files whose last passing proof no longer holds")
        p.add_argument("--feature", metavar="F", help="check a feature's criteria (with --run, records the results)")
        p.add_argument("--criterion", metavar="C", help="with --feature: only this criterion")
        p.add_argument("--repeat", type=int, metavar="N", help="with --feature: run each check N times; all must pass")
        p.add_argument("--by", metavar="ID", help="with --feature: the roster id running the checks")
        p.add_argument("--json", action="store_true", help="with --feature: print the records as one JSON object")
        p.set_defaults(fn=cmd_verify)
    elif name == "guard":
        p.add_argument("path")
        p.add_argument("--by", help="owner id (default: $BLUEPRINT_OWNER)")
        p.set_defaults(fn=cmd_guard)
    elif name == "hook":
        p.add_argument("when", choices=["pre", "post"])
        p.set_defaults(fn=cmd_hook)


def _guard_signal(root, msg):
    """The loop signal for a guard_decision refusal (contract 9.4)."""
    code = ("guard-unplanned" if " is not in the blueprint" in msg else
            "guard-wrong-owner" if " is owned by " in msg else None)
    if code:
        registry.signal(root, "guard", code)


def _write_refusal(args, root, plan, path, by):
    """The refuse("write") message of a 2.0 module (protected paths, 4.8), or None. A hook that crashes refuses: the
    guard fails closed, and the crash is recorded as an incident."""
    try:
        msg = registry.refusal("write", root, plan, path, by)
    except Exception as exc:  # noqa: BLE001 - fail closed, never let a broken hook allow a write
        record_incident(args, exc)
        return f"{path}: the guard could not check this write (incident recorded), so it refuses it. Run doctor."
    if msg:
        registry.signal(root, "guard", "guard-protected")
    return msg


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
    from bp.status import next_ready, summarize
    root = find_root(args.root)
    plan = load_plan(root)
    state = load_state(root)
    for flag, module in STATUS_FLAGS.items():  # 2.0 flags need their module (7.2); read with getattr (1.4)
        if getattr(args, flag, None) and not registry.exists(module):
            raise not_built(f"status --{flag}", module)
    ctx = build_context(root, plan, state)
    comp = ctx["comp"]
    rows = comp["rows"]
    if args.owner:
        rows = [r for r in rows if r["owner"] == args.owner]
    if args.phase:
        rows = [r for r in rows if r["phase"] == args.phase]
    if args.json:
        out = status_json(ctx, {"rows": rows, "patterns": comp["patterns"], "unplanned": state.get("unplanned", []),
                                "syncedAt": state.get("syncedAt")})
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return
    if getattr(args, "feature", None):  # the feature card only (status_card, features)
        card = registry.hook("status_card")
        if card is None:
            raise not_built("status --feature", "features")
        for line in card(ctx, args.feature):
            print(line)
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
        blocked = set(ctx["blocked"].get("phases") or ())
        ready = next_ready(plan, comp["rows"], args.owner, blocked_phases=blocked)
        print("\n  Ready to build next:")
        for ph in plan["phases"]:  # 2.6: files of a phase blocked by an open unknown are not offered
            if ph.get("id") in blocked and any(r["phase"] == ph["id"] and r["status"] in ("planned", "claimed", "ondisk")
                                               for r in comp["rows"]):
                print(f"    (phase {ph['id']} skipped: blocked by {', '.join(blocked_by(ctx, 'phase', ph['id'])) or 'an open unknown'})")
        for r in ready[:20]:
            print(f"    {r['path']}  ({r['owner']}, {r['phase']})")
        if not ready:
            print("    nothing: everything in the current phase is claimed, built or waiting on a dependency")
    n_stale = sum(1 for r in rows if r.get("stale"))
    if n_stale and plan.get("_from") != 1:  # contract 4.7; a version 1 plan keeps 1.3.0's output (W0 rule)
        print(f"\n  ↻ {n_stale} file(s) need re-verifying (inputs changed): verify --stale --run")
    for _, lines in registry.collect("status_lines", ctx, args):  # 2.0 hook lines, after the 1.x output
        for line in lines or []:
            print(line)
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
    if not getattr(args, "force", False):  # 2.0: refuse("claim") hooks, e.g. a phase blocked by an unknown (2.6)
        msg = registry.refusal("claim", root, plan, path, args.by)
        if msg:
            raise BlueprintError("blocked-by-unknown", msg, entry=f"file {path}", code=3)
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


def _verify_feature(args, root, plan):
    """verify --feature F (7.2) through the verify_feature hook of records. Fail closed: exit 0 only when the hook says
    0 and every record it returns has result pass."""
    fn = registry.hook("verify_feature")
    if fn is None:
        raise not_built("verify --feature", "records")
    records, code = fn(args, build_context(root, plan, load_state(root)))
    records = list(records or [])
    if getattr(args, "json", False):
        emit(args, "verify", {"records": records}, [])
    ok = code == 0 and type(code) is int and all(isinstance(r, dict) and r.get("result") == "pass" for r in records)
    sys.exit(0 if ok else 1)


def cmd_verify(args):
    from bp.verify import record_file_proofs, run_check, verify_groups
    feature = getattr(args, "feature", None)
    with_feature = {"--criterion": getattr(args, "criterion", None), "--repeat": getattr(args, "repeat", None),
                    "--by": getattr(args, "by", None), "--json": getattr(args, "json", False) or None}
    used = [k for k, v in with_feature.items() if v is not None]
    if used and not feature:
        raise BlueprintError("usage", f"{', '.join(used)} go{'es' if len(used) == 1 else ''} with --feature F",
                             next="blueprint.py verify --feature F --run", code=64)
    if getattr(args, "repeat", None) is not None and args.repeat < 1:
        raise BlueprintError("usage", "--repeat must be 1 or more", next="blueprint.py verify --feature F --run --repeat 3",
                             code=64)
    root = find_root(args.root)
    plan = load_plan(root)
    if feature:
        _verify_feature(args, root, plan)
        return
    only = None
    if getattr(args, "stale", False):  # files whose last proof passed but no longer holds (contract 4.7)
        from bp.status import compute
        only = {r["path"] for r in compute(root, plan, load_state(root))["rows"] if r["stale"]}
    groups = verify_groups(plan, phase=args.phase, owner=args.owner, only=only)
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
        if not ok:
            registry.signal(root, "verify", f"verify-{result['result']}")  # 9.4
        record_file_proofs(root, plan, state, cmd, paths, result)
        append_log(root, {"event": "verify", "cmd": cmd, "ok": ok, "files": len(paths)})
    save_state(root, state)
    render_all(root, plan, state, quiet=True)
    sys.exit(1 if failed else 0)


def cmd_guard(args):
    from bp.status import guard_decision
    root = find_root(args.root)
    plan = load_plan(root)
    by = args.by or os.environ.get("BLUEPRINT_OWNER")
    code, msg = guard_decision(root, plan, args.path, by)
    if code:
        _guard_signal(root, msg)
    else:  # 2.0: refuse("write") hooks after the 1.x guard allows (contract 1.4, 4.8)
        msg = _write_refusal(args, root, plan, norm(args.path), by)
        code = 2 if msg else 0
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
        by = os.environ.get("BLUEPRINT_OWNER")
        code, msg = guard_decision(root, plan, rel, by)
        if mode in ("block", "ask"):
            if code:
                _guard_signal(root, msg)
            else:  # 2.0: refuse("write") hooks after the 1.x guard allows (contract 1.4, 4.8)
                msg = _write_refusal(args, root, plan, rel, by)
                code = 2 if msg else 0
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
