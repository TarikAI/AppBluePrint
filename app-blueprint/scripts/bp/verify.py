"""verify: the part of 1.x cmd_verify that groups files by verify command, runs a command and records file proofs.

Moved from blueprint.py 1.3.0 (contract 1.5) with the 1.x behaviour: the command's output goes to the terminal; a
timeout is a failure; an OSError from starting the shell propagates (1.x recorded it as an incident).
"""
import subprocess
import time

from bp.core import now_iso
from bp.plan import item_verify, owner_of
from bp.status import HASHER, proof_inputs


def verify_groups(plan, phase=None, owner=None, only=None, state=None):
    """{verify command: [paths]} in plan order, skipping deletes; only, if given, limits the paths.

    The planned files first. Then, for a version 2 plan and when state is given, each pattern's instances recorded in
    state (by the last sync) under the pattern's verify command (its own, else its module's), filtered by the
    pattern's phase and the instance's owner, so a pattern instance can be proven like a planned file (option B of
    the core review). An instance that is also a planned file is proven as that file. A version 1 plan keeps the 1.x
    groups exactly: planned files only."""
    groups = {}
    for f in plan["files"]:
        if f.get("action") == "delete" or (phase and f.get("phase") != phase):
            continue
        if owner and owner_of(plan, f["path"])[0] != owner:
            continue
        if only is not None and f["path"] not in only:
            continue
        cmd = item_verify(plan, f)
        if cmd:
            groups.setdefault(cmd, []).append(f["path"])
    if state is None or plan.get("_from") == 1:
        return groups
    planned = {f["path"] for f in plan["files"]}
    recorded = state.get("patterns") if isinstance(state.get("patterns"), dict) else {}
    for p in plan["patterns"]:
        if not isinstance(p.get("id"), str) or (phase and p.get("phase") != phase):
            continue
        cmd = item_verify(plan, p)
        entry = recorded.get(p["id"])
        instances = entry.get("instances") if isinstance(entry, dict) else None
        if not cmd or not isinstance(instances, list):
            continue
        for inst in instances:
            if not isinstance(inst, str) or inst in planned or (only is not None and inst not in only):
                continue
            if owner and owner_of(plan, inst)[0] != owner:
                continue
            paths = groups.setdefault(cmd, [])
            if inst not in paths:
                paths.append(inst)
    return groups


def stale_instances(root, plan, state):
    """The recorded pattern instances (version 2 plans) whose last proof passed but no longer holds (contract 4.7),
    as verify --stale needs them beside the stale planned files."""
    from bp.status import proof_current
    if plan.get("_from") == 1:
        return set()
    out = set()
    for paths in verify_groups(plan, state=state).values():
        for p in paths:
            rec = state["files"].get(p) if isinstance(state.get("files"), dict) else None
            ver = rec.get("verified") if isinstance(rec, dict) else None
            if isinstance(ver, dict) and ver.get("ok") is True and (root / p).is_file() \
                    and not proof_current(root, plan, ver, paths=[p]):
                out.add(p)
    return out


def run_check(cmd, cwd, timeout):
    """Run one verify command through the shell. result is pass, fail or timeout."""
    started = time.monotonic()
    try:
        res = subprocess.run(cmd, shell=True, cwd=cwd, timeout=timeout)
        result, code = ("pass" if res.returncode == 0 else "fail"), res.returncode
    except subprocess.TimeoutExpired:
        result, code = "timeout", None
    return {"result": result, "exit": code, "durationMs": int((time.monotonic() - started) * 1000)}


def record_file_proofs(root, plan, state, cmd, paths, result):
    """Record the outcome on every present file of the group (contract 4.5). A pass writes the proof: sha (sha1 of the
    normalised content, so a 1.x tool can still read it) and inputs (the file and its dependency closure, digested).
    Anything else (fail, timeout, error) writes ok false with the result; only a pass ever counts."""
    ok = result["result"] == "pass"
    for p in paths:
        fp = root / p
        rec = state["files"].setdefault(p, {})
        if not fp.is_file():
            continue
        if ok:
            rec["verified"] = {"ok": True, "cmd": cmd, "at": now_iso(), "sha": HASHER.sha1(fp),
                               "inputs": proof_inputs(root, plan, [p])}
        else:
            rec["verified"] = {"ok": False, "cmd": cmd, "at": now_iso(), "sha": None, "result": result["result"]}
