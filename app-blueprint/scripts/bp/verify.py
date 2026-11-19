"""verify: the part of 1.x cmd_verify that groups files by verify command, runs a command and records file proofs.

Moved from blueprint.py 1.3.0 (contract 1.5) with the 1.x behaviour: the command's output goes to the terminal; a
timeout is a failure; an OSError from starting the shell propagates (1.x recorded it as an incident).
"""
import subprocess
import time

from bp.core import file_sha, now_iso
from bp.plan import item_verify, owner_of


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
    """Record the outcome on every present file of the group, as 1.x did (sha only when it passed)."""
    ok = result["result"] == "pass"
    for p in paths:
        fp = root / p
        rec = state["files"].setdefault(p, {})
        if fp.is_file():
            rec["verified"] = {"ok": ok, "cmd": cmd, "at": now_iso(), "sha": file_sha(fp) if ok else None}
