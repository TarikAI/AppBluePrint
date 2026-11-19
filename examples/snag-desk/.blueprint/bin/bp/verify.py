"""verify: the part of 1.x cmd_verify that groups files by verify command, runs a command and records file proofs.

Moved from blueprint.py 1.3.0 (contract 1.5) with the 1.x behaviour: the command's output goes to the terminal; a
timeout is a failure; an OSError from starting the shell propagates (1.x recorded it as an incident).
"""
import subprocess
import time

from bp.core import now_iso
from bp.plan import item_verify, owner_of
from bp.status import HASHER, proof_inputs


def verify_groups(plan, phase=None, owner=None, only=None):
    """{verify command: [planned paths]} in plan order, skipping deletes; only, if given, limits the paths."""
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
    return groups


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
