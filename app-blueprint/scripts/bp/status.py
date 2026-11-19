"""status: moved from blueprint.py 1.3.0 (contract 1.5)."""
import os
import subprocess

from bp.core import append_log, die, file_sha, norm, now_iso
from bp.globs import glob_match, pattern_instance_rx
from bp.plan import DEFAULT_IGNORE, STATES, item_verify, load_state, owner_of, phase_index, planned_file, save_state


def folder_rule_segments(rule):
    """The folder names of a plain folder rule ('src/legacy/' -> ['src', 'legacy']); None for any other rule."""
    r = rule.strip().replace("\\", "/")
    if not r.endswith("/") or any(c in r for c in "*?["):
        return None
    return [x for x in r.strip("/").split("/") if x] or None


def repo_files(root, plan, ignored_tracked=None):
    """Files to compare with the plan. An ignored path is kept only if it is an individually planned file, or it
    matches a pattern glob that names, as literal folder segments, every folder of the plain folder rule(s) that
    drop it. If ignored_tracked is a list, tracked files that DEFAULT_IGNORE dropped are appended to it."""
    ignore = DEFAULT_IGNORE + list(plan.get("ignore", []))
    planned = {norm(f.get("path", "")) for f in plan["files"]}
    pats = [(pattern_instance_rx(p["glob"]), norm(p["glob"]).lstrip("/").split("/"))
            for p in plan["patterns"] if p.get("glob")]

    def named_by(rules, gsegs):
        for g in rules:
            segs = folder_rule_segments(g)
            if segs is None or not all(x in gsegs for x in segs):
                return False
        return True

    def kept(path, rules):
        if path in planned:
            return True
        hits = [g for g in rules if glob_match(g, path)]
        return not hits or any(rx.match(path) and named_by(hits, gsegs) for rx, gsegs in pats)

    def planned_inside(rel):
        return any(f.startswith(rel + "/") for f in planned)

    def prune(rel):
        hits = [g for g in ignore if glob_match(g, rel + "/x")]
        return bool(hits) and not planned_inside(rel) and not any(named_by(hits, gsegs) for _, gsegs in pats)

    files = None
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                             cwd=root, capture_output=True, timeout=60)
        if out.returncode == 0:
            files = [norm(p) for p in out.stdout.decode("utf-8", "replace").split("\0") if p]
            if ignored_tracked is not None:
                tr = subprocess.run(["git", "ls-files", "--cached", "-z"], cwd=root, capture_output=True, timeout=60)
                if tr.returncode == 0:
                    tracked = {norm(p) for p in tr.stdout.decode("utf-8", "replace").split("\0") if p}
                    ignored_tracked.extend(sorted(p for p in tracked if (root / p).is_file() and not kept(p, DEFAULT_IGNORE)
                                                  and not p.startswith((".blueprint/", ".git/"))))
    except (OSError, subprocess.SubprocessError):
        files = None
    if files is None:
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = norm(os.path.relpath(dirpath, root))
            rel_dir = "" if rel_dir == "." else rel_dir
            dirnames[:] = [d for d in dirnames if not prune((rel_dir + "/" if rel_dir else "") + d)]
            for fn in filenames:
                files.append((rel_dir + "/" if rel_dir else "") + fn)
    return sorted(p for p in files if (root / p).is_file() and kept(p, ignore))


def effective_status(root, plan, state, f):
    rec = state["files"].get(f["path"], {})
    present = (root / f["path"]).is_file()
    if f.get("action") == "delete":
        return ("verified" if not present else (rec.get("state") if rec.get("state") in ("claimed", "blocked") else "planned")), present
    if rec.get("state") == "blocked":
        return "blocked", present
    if not present:
        return ("claimed" if rec.get("state") == "claimed" else "planned"), present
    ver = rec.get("verified") or {}
    if ver.get("ok") and ver.get("sha") and ver["sha"] == file_sha(root / f["path"]):
        return "verified", present
    if rec.get("state") in ("built", "verified"):
        return "built", present
    return "ondisk", present


def compute(root, plan, state):
    """Everything the views need, in one dict."""
    rows = []
    for f in plan["files"]:
        st, present = effective_status(root, plan, state, f)
        owner, rule, _ = owner_of(plan, f["path"])
        rec = state["files"].get(f["path"], {})
        rows.append({"path": f["path"], "kind": "file", "action": f.get("action"), "module": f.get("module"),
                     "phase": f.get("phase"), "purpose": f.get("purpose", ""), "owner": owner, "rule": rule,
                     "contract": bool(f.get("contract")), "status": st, "present": present,
                     "consumes": f.get("consumes", []), "produces": f.get("produces", []),
                     "dependsOn": [norm(d) for d in f.get("dependsOn", [])], "verify": item_verify(plan, f),
                     "ac": f.get("ac", []), "by": rec.get("by"), "at": rec.get("at"), "note": rec.get("note"),
                     "uses": [u for u in f.get("uses", []) if isinstance(u, dict)],
                     "observed": ((state.get("data") or {}).get("observed") or {}).get(f["path"], {})})
    pats = []
    for p in plan["patterns"]:
        owner, rule, _ = owner_of(plan, p.get("glob", ""))
        inst = state.get("patterns", {}).get(p.get("id"), {}).get("instances", [])
        pats.append({"id": p.get("id"), "glob": p.get("glob"), "module": p.get("module"), "phase": p.get("phase"),
                     "purpose": p.get("purpose", ""), "instance": p.get("instance", ""), "owner": owner, "rule": rule,
                     "examples": p.get("examples", []), "instances": inst, "verify": item_verify(plan, p)})
    counts = {s: 0 for s in STATES}
    for r in rows:
        counts[r["status"]] += 1
    return {"rows": rows, "patterns": pats, "counts": counts}


def summarize(rows, key):
    out = {}
    for r in rows:
        k = r.get(key) or "(none)"
        d = out.setdefault(k, {s: 0 for s in STATES})
        d[r["status"]] += 1
    return out


def next_ready(plan, rows, owner=None):
    pidx = phase_index(plan)
    by_path = {r["path"]: r for r in rows}
    open_rows = [r for r in rows if r["status"] in ("planned", "claimed", "ondisk")]
    if not open_rows:
        return []
    current = min(pidx.get(r["phase"], 99) for r in open_rows)
    ready = []
    for r in open_rows:
        if pidx.get(r["phase"], 99) != current or (owner and r["owner"] != owner):
            continue
        deps = [by_path.get(d) for d in r["dependsOn"]]
        if all(d is None or d["status"] in ("built", "verified") for d in deps):
            ready.append(r)
    return sorted(ready, key=lambda r: r["path"])


def require_owner(plan, path, by, force):
    owner, _, _ = owner_of(plan, path)
    if owner != by and not force:
        die(f"{path} is owned by {owner}, not {by}. Ask its owner, or use --force if you are the lead reassigning it.", 3)


def set_file_state(root, path, new_state, by, note=None):
    state = load_state(root)
    rec = state["files"].setdefault(path, {})
    rec.update({"state": new_state, "by": by, "at": now_iso()})
    if note is not None:
        rec["note"] = note
    if new_state == "planned":
        rec.pop("verified", None)
    save_state(root, state)
    append_log(root, {"event": new_state, "path": path, "by": by, "note": note})


def guard_decision(root, plan, path, by):
    path = norm(path)
    if path.startswith(".blueprint/"):
        return 0, ""
    if planned_file(plan, path) is None and not any(pattern_instance_rx(p.get("glob", "")).match(path) for p in plan["patterns"]):
        if any(glob_match(g, path) for g in DEFAULT_IGNORE + list(plan.get("ignore", []))):
            return 0, ""
        return 2, (f"{path} is not in the blueprint (.blueprint/blueprint.json). Do not create unplanned files: "
                   f"propose a blueprint change (file entry with purpose, module, phase, owner) and get it approved.")
    if by:
        owner, _, _ = owner_of(plan, path)
        if owner != by:
            return 2, f"{path} is owned by {owner}, not {by}. Write only files you own; ask {owner} or the lead."
    return 0, ""
