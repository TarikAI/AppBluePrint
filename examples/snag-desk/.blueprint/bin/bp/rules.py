"""rules: the 1.x structural rules of validate_plan (files, modules, phases, owners, team), moved from blueprint.py 1.3.0.

validate.validate_plan calls validate(), then data.validate_data(), then validate_team(): the 1.x order.
"""
import json
import re

from bp.version import SCHEMA_VERSION
from bp.core import norm
from bp.globs import pattern_instance_rx
from bp.plan import ACTIONS, ROSTER_KINDS, WRITE_ACCESS, item_verify, owner_of, phase_index


def validate(plan, E, W, vctx):
    """The body of 1.x validate_plan up to the data check, unchanged. E and W append an error or a warning."""
    if plan.get("version") != SCHEMA_VERSION:
        E(f"version must be {SCHEMA_VERSION}")
    if not (plan.get("app") or {}).get("name"):
        E("app.name is empty")

    def uniq(items, key, label):
        seen = set()
        for it in items:
            v = it.get(key)
            if not v:
                E(f"{label} without {key}: {json.dumps(it)[:80]}")
            elif v in seen:
                E(f"duplicate {label} {key}: {v}")
            seen.add(v)
        return seen

    mods = uniq(plan["modules"], "id", "module")
    phases = uniq(plan["phases"], "id", "phase")
    roster = uniq(plan["roster"], "id", "roster entry")
    uniq(plan["files"], "path", "file")
    uniq(plan["patterns"], "id", "pattern")
    pidx = phase_index(plan)
    if not plan["phases"]:
        E("no phases: phase 1 must be a walking skeleton")

    for s in plan["stack"]:
        for k in ("part", "choice", "docs", "verified"):
            if not s.get(k):
                E(f"stack entry {s.get('part') or s.get('choice') or '?'}: missing {k} (record the docs URL and the date you checked it)")

    for m in plan["modules"]:
        for d in m.get("dependsOn", []):
            if d not in mods:
                E(f"module {m.get('id')}: depends on unknown module {d}")
        if not m.get("purpose"):
            E(f"module {m.get('id')}: empty purpose")
    # module cycles
    graph = {m.get("id"): list(m.get("dependsOn", [])) for m in plan["modules"]}
    state = {}

    def visit(n, stack):
        state[n] = 1
        for d in graph.get(n, []):
            if state.get(d) == 1:
                E("module dependency cycle: " + " -> ".join(stack + [n, d]))
            elif not state.get(d):
                visit(d, stack + [n])
        state[n] = 2

    for n in graph:
        if not state.get(n):
            visit(n, [])

    for r in plan["roster"]:
        if r.get("kind") not in ROSTER_KINDS:
            E(f"roster {r.get('id')}: kind must be agent or human")

    planned_paths = {f["path"] for f in plan["files"]}
    pat_rx = [(p.get("id"), pattern_instance_rx(p.get("glob", ""))) for p in plan["patterns"] if p.get("glob")]

    def known_dep(d):
        d = norm(d)
        return d in planned_paths or any(rx.match(d) for _, rx in pat_rx) or d in {p.get("id") for p in plan["patterns"]}

    files_by_path = {f["path"]: f for f in plan["files"]}
    for f in plan["files"]:
        path = f["path"]
        if not path or path.startswith("/") or ".." in path.split("/") or re.match(r"^[A-Za-z]:", path):
            E(f"file {path!r}: use a repo-relative path")
        if f.get("action") not in ACTIONS:
            E(f"file {path}: action must be create, modify or delete")
        if f.get("module") not in mods:
            E(f"file {path}: unknown module {f.get('module')!r}")
        if f.get("phase") not in phases:
            E(f"file {path}: unknown phase {f.get('phase')!r}")
        if not f.get("purpose"):
            E(f"file {path}: empty purpose (one sentence: what this file is for)")
        if f.get("action") != "delete" and not item_verify(plan, f):
            E(f"file {path}: no verify command (set it on the file or its module)")
        for d in f.get("dependsOn", []):
            if not known_dep(d):
                E(f"file {path}: depends on {d}, which is not planned")
            dep = files_by_path.get(norm(d))
            if dep and dep.get("phase") in pidx and f.get("phase") in pidx and pidx[dep["phase"]] > pidx[f["phase"]]:
                E(f"file {path} ({f['phase']}) depends on {d} ({dep['phase']}), which lands in a later phase")

    for p in plan["patterns"]:
        pid = p.get("id")
        if not p.get("glob"):
            E(f"pattern {pid}: empty glob")
        if p.get("module") not in mods:
            E(f"pattern {pid}: unknown module {p.get('module')!r}")
        if p.get("phase") not in phases:
            E(f"pattern {pid}: unknown phase {p.get('phase')!r}")
        if not p.get("purpose") or not p.get("instance"):
            E(f"pattern {pid}: needs purpose and instance (what one instance contains)")
        if not item_verify(plan, p):
            E(f"pattern {pid}: no verify command (set it on the pattern or its module)")

    # produces / consumes
    produced = set()
    for f in plan["files"]:
        produced.update(f.get("produces", []))
    for f in plan["files"]:
        for c in f.get("consumes", []):
            if c not in produced:
                W(f"file {f['path']}: consumes {c!r}, which no planned file produces")

    # ownership
    rules = plan["owners"]
    for i, r in enumerate(rules):
        if not r.get("pattern") or not r.get("owner"):
            E(f"owner rule {i + 1}: needs pattern and owner")
        elif r["owner"] not in roster:
            E(f"owner rule {i + 1} ({r['pattern']}): owner {r['owner']!r} is not in the roster")
    wins = {i: 0 for i in range(len(rules))}
    hits = {i: 0 for i in range(len(rules))}
    items = [(f["path"], f["path"]) for f in plan["files"]] + [(p.get("glob", ""), "pattern " + str(p.get("id"))) for p in plan["patterns"]]
    # Which planned items each rule matches. A later rule whose matches all fall inside an earlier
    # rule's matches is a normal CODEOWNERS refinement (fallback first, specifics after). A later rule
    # that only partly overlaps an earlier rule of another owner makes ownership depend on rule order:
    # that is the overlap validate reports, unless the later rule says "override": true.
    covers = {i: set() for i in range(len(rules))}
    resolved = []
    for target, label in items:
        owner, win, matches = owner_of(plan, target)
        resolved.append((target, label, owner, win, matches))
        for m in matches:
            covers[m].add(label)
    reported = set()
    for target, label, owner, win, matches in resolved:
        for m in matches:
            hits[m] += 1
        if owner is None:
            E(f"{label}: no owner rule matches it")
            continue
        wins[win] += 1
        if rules[win].get("override"):
            continue
        for m in matches:
            if m == win or rules[m].get("owner") == owner or covers[win] <= covers[m] or (m, win) in reported:
                continue
            reported.add((m, win))
            extra = sorted(covers[win] - covers[m])[:2]
            E(f"owner rules {m + 1} ({rules[m]['pattern']} -> {rules[m]['owner']}) and {win + 1} "
              f"({rules[win]['pattern']} -> {owner}) partly overlap, e.g. on {label}; rule {win + 1} also takes "
              f"{', '.join(extra)}, which rule {m + 1} does not cover, so ownership depends on rule order. "
              f"Narrow one rule, or mark rule {win + 1} \"override\": true if that is intended.")
    for i, r in enumerate(rules):
        if hits[i] == 0:
            W(f"owner rule {i + 1} ({r.get('pattern')} -> {r.get('owner')}) matches no planned file or pattern")
        elif wins[i] == 0:
            W(f"owner rule {i + 1} ({r.get('pattern')} -> {r.get('owner')}) never wins: later rules override every match")

    # A bare folder-name rule ("agents/") matches that folder at any depth, like CODEOWNERS. Warn when it
    # covers planned items in the root folder and in a nested one, which is rarely what was meant.
    label_target = {label: target for target, label in items}
    for i, r in enumerate(rules):
        core = str(r.get("pattern") or "").strip().rstrip("/")
        if not core or "/" in core or "*" in core or "?" in core:
            continue
        root_hit, nested = False, None
        for label in sorted(covers[i]):
            parts = norm(label_target.get(label, "")).lstrip("/").split("/")
            for k, part in enumerate(parts[:-1]):
                if part == core:
                    if k == 0:
                        root_hit = True
                    elif nested is None:
                        nested = "/".join(parts)
        if root_hit and nested:
            W(f"owner rule {i + 1} ({r['pattern']} -> {r.get('owner')}) matches that folder name at any depth: "
              f"it also matches {nested}. Write /{core}/ for the root folder only.")



def validate_team(plan, team, E):
    """The 1.x AgenticTeam roster check (validate --team), unchanged; items is recomputed as 1.x built it."""
    items = [(f["path"], f["path"]) for f in plan["files"]] + [(p.get("glob", ""), "pattern " + str(p.get("id"))) for p in plan["patterns"]]
    if team is not None:
        agents = {a.get("id"): a for a in team.get("agents", [])}
        owners_used = {owner_of(plan, t)[0] for t, _ in items} - {None}
        for r in plan["roster"]:
            if r.get("kind") != "agent" or (r.get("harness") or "").lower() not in ("agenticteam", "agentic-team"):
                continue
            a = agents.get(r["id"])
            if not a:
                E(f"roster {r['id']}: not an agent in team.json")
            elif r["id"] in owners_used and a.get("access") not in WRITE_ACCESS:
                E(f"roster {r['id']}: owns files but its AgenticTeam access is {a.get('access')!r}, not workspace-write")
