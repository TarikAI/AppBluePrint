#!/usr/bin/env python3
"""check_owned.py: did this branch touch only the files its workstream owns?

The orchestrator runs it before merging a builder's branch (contract 12.1, decision 11):

    python maintainers/check_owned.py --workstream W1-R --base v2

It lists `git diff --name-only --diff-filter=ACMRDT --no-renames <base>...HEAD` (so a rename counts as the
delete of the old path and the add of the new one), loads the repository's own plan, .blueprint/blueprint.json,
through the launcher module (app-blueprint/scripts/blueprint.py, imported by path, no subprocess for the lookup),
and checks every changed path:

  - unplanned: the path is neither a planned file nor an instance of a planned pattern, or no owner rule
    matches it;
  - owned by another: the owner rules (last match wins) give it to a roster id other than --workstream.

The plan is read from the checked-out tree of --root, so a branch cannot widen its own area: the plan files
are owned by W0-C (the dogfood plan), and any other workstream touching them is reported like any other path.

Exit codes: 0 every changed path is the workstream's; 1 at least one path is unplanned or another's;
64 bad usage (unknown workstream id, not a git repository, base not found, no loadable plan).
Standard library only, Python 3.10+.
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

EXIT_OK, EXIT_FINDINGS, EXIT_USAGE = 0, 1, 64
HERE = Path(__file__).resolve().parent
LAUNCHER = HERE.parent / "app-blueprint" / "scripts" / "blueprint.py"
PLAN_REL = ".blueprint/blueprint.json"
DIFF_FILTER = "ACMRDT"


class Usage(Exception):
    """A usage error the tool detects: the message says what is wrong, next says what to do."""

    def __init__(self, message, next_step):
        super().__init__(message)
        self.next_step = next_step


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write("error: %s\n  next: run python maintainers/check_owned.py --help\n" % message)
        sys.exit(EXIT_USAGE)


def load_launcher(path=LAUNCHER):
    """The launcher as a module, loaded by path the way the tests load it."""
    if not Path(path).is_file():
        raise Usage("the tool launcher is missing: %s" % path,
                    "run this script from a complete checkout (app-blueprint/scripts/blueprint.py must exist)")
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("blueprint_launcher_check_owned", str(path))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit as e:  # the launcher exits only when run as a script, but never let it end this process
        raise Usage("the tool launcher did not load (exit %s)" % e.code, "repair the checkout") from None
    except Exception as e:
        raise Usage("the tool launcher did not load: %s" % e,
                    "repair the checkout, then run this script again") from None
    return mod


def _git(root, *args):
    try:
        return subprocess.run(["git", "-C", str(root)] + list(args), capture_output=True)
    except OSError as e:
        raise Usage("git cannot be run: %s" % e, "install git and put it on the PATH") from None


def repo_root(root):
    r = _git(root, "rev-parse", "--show-toplevel")
    if r.returncode != 0:
        raise Usage("%s is not inside a git repository" % root,
                    "run this from the repository root, or pass --root <repository>")
    return Path(r.stdout.decode("utf-8", "replace").strip())


def changed_paths(root, base):
    """Paths from `git diff --name-only --diff-filter=ACMRDT --no-renames -z <base>...HEAD`, forward slashes."""
    ok = _git(root, "rev-parse", "--verify", "--quiet", base + "^{commit}")
    if ok.returncode != 0:
        raise Usage("base %r was not found in this repository" % base,
                    "fetch it (git fetch origin) or pass an existing branch or commit with --base")
    r = _git(root, "-c", "core.quotepath=false", "diff", "--name-only", "-z", "--no-renames",
             "--diff-filter=" + DIFF_FILTER, base + "...HEAD")
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise Usage("git could not compare %s with HEAD: %s" % (base, err[-1] if err else "unknown error"),
                    "check that HEAD and %s share history (git merge-base %s HEAD)" % (base, base))
    out = []
    for raw in r.stdout.split(b"\0"):
        if raw:
            out.append(raw.decode("utf-8", "surrogateescape").replace("\\", "/"))
    return sorted(set(out))


def classify(tool, plan, workstream, paths):
    """[(path, status, owner)] for every path that is not the workstream's. status is "unplanned" or "owned-by-other"."""
    bad = []
    for raw in paths:
        path = tool.norm(raw)
        owner, _rule, _matches = tool.owner_of(plan, path)
        planned = tool.planned_file(plan, path) is not None or any(
            tool.pattern_instance_rx(p.get("glob", "")).match(path) for p in plan["patterns"])
        if not planned or owner is None:
            bad.append((path, "unplanned", owner))
        elif owner != workstream:
            bad.append((path, "owned-by-other", owner))
    return bad


def run(workstream, base, root, tool=None):
    """The result as a dict; raises Usage for bad usage."""
    tool = tool or load_launcher()
    root = repo_root(root)
    plan_file = root / PLAN_REL
    if not plan_file.is_file():
        raise Usage("%s has no %s" % (root, PLAN_REL),
                    "run this from a checkout that has the repository's own plan, or pass --root")
    try:
        plan = tool.load_plan(root)
    except BaseException as e:  # load_plan exits through die()/BlueprintError on a bad plan
        if isinstance(e, KeyboardInterrupt):
            raise
        raise Usage("%s does not load (%s)" % (PLAN_REL, str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__),
                    "run: python app-blueprint/scripts/blueprint.py validate") from None
    ids = [r.get("id") for r in plan.get("roster", [])]
    if workstream not in ids:
        raise Usage("workstream %r is not in the roster of %s" % (workstream, PLAN_REL),
                    "use one of: " + ", ".join(str(i) for i in ids))
    paths = changed_paths(root, base)
    bad = classify(tool, plan, workstream, paths)
    return {"workstream": workstream, "base": base, "ok": not bad, "changed": paths,
            "violations": [{"path": p, "status": s, "owner": o} for p, s, o in bad]}


def summary(res):
    n, v = len(res["changed"]), len(res["violations"])
    if res["ok"]:
        return "OK: %s changed %d path%s, all its own (base %s)" % (res["workstream"], n, "" if n == 1 else "s", res["base"])
    return "FAIL: %s changed %d path%s, %d not its own (base %s)" % (res["workstream"], n, "" if n == 1 else "s", v, res["base"])


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = _Parser(prog="check_owned.py", description="Check that a branch touches only the files its workstream owns.")
    ap.add_argument("--workstream", required=True, help="roster id of the branch's workstream, e.g. W1-R")
    ap.add_argument("--base", default="v2", help="branch or commit to compare with (default v2)")
    ap.add_argument("--root", default=".", help="repository to check (default: the current folder)")
    ap.add_argument("--json", action="store_true", help="print one JSON object instead of text")
    args = ap.parse_args(argv)
    try:
        res = run(args.workstream, args.base, args.root)
    except Usage as e:
        if args.json:
            print(json.dumps({"ok": False, "error": "usage", "message": str(e), "next": e.next_step}))
        sys.stderr.write("error: %s\n  next: %s\n" % (e, e.next_step))
        return EXIT_USAGE
    if args.json:
        res["summary"] = summary(res)
        print(json.dumps(res, indent=2, ensure_ascii=False))
    elif res["ok"]:
        print(summary(res))
    else:
        print(summary(res))
        for v in res["violations"]:
            if v["status"] == "unplanned":
                extra = "unplanned" + (" (owner rule says %s, but the path is not a planned file)" % v["owner"] if v["owner"] else "")
            else:
                extra = "owned by %s" % v["owner"]
            print("  %s  %s" % (v["path"], extra))
        print("  next: move the change to the owner's branch, or ask the orchestrator to update the plan "
              "(%s) first" % PLAN_REL)
    return EXIT_OK if res["ok"] else EXIT_FINDINGS


if __name__ == "__main__":
    sys.exit(main())
