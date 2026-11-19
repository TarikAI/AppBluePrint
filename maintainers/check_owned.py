#!/usr/bin/env python3
"""check_owned.py: did this branch touch only the files its workstream owns?

The orchestrator runs it before merging a builder's branch (contract 12.1, decision 11), from a CLEAN CHECKOUT OF
THE BASE, never from the builder's tree:

    python maintainers/check_owned.py --workstream W1-R --head <branch-or-sha> [--base v2] [--json]

Nothing from the branch under judgement is trusted:

  - The plan is read from the base: `git show <base>:.blueprint/blueprint.json`, never from the working tree or
    the head. A branch cannot widen its own area by editing the plan.
  - The code that does the owner lookup is the base's. The script refuses (64) unless maintainers/check_owned.py
    and everything under app-blueprint/scripts in the working tree are identical to the base, with no untracked
    file under app-blueprint/scripts. The tool launcher is then loaded from this checkout by path.
  - Changed paths come from plumbing only, with a scrubbed environment and no user config:
    `git diff-tree -r -z --name-only --no-renames --diff-filter=ACMRDT <merge-base> <head>`. A rename is the
    delete of the old path plus the add of the new one; type changes, mode changes and submodule entries count.
  - Each path is judged exactly as git reports it. If cleaning it (strip, separator change, leading ./) would
    alter it, it is unplanned. A path is the workstream's only when it is a planned file or an instance of a planned
    pattern AND the owner rules (last match wins) give it to the workstream (exact roster id).

Output (text and --json) always names the workstream, the base sha, the head sha, the merge-base sha and the number
of changed paths, so the commit that passed can be tied to the commit that is merged.

Exit codes: 0 every changed path is the workstream's; 1 at least one is unplanned or another's; 64 a refusal to
judge (unknown workstream, bad refs, shallow clone, dirty or mismatched tool, broken or missing base plan, nothing to
check); 2 argparse argument errors (contract decision 9). With --json every 0, 1 and 64 outcome is one JSON object on
stdout. Standard library only, Python 3.10+.
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

EXIT_OK, EXIT_FINDINGS, EXIT_USAGE = 0, 1, 64
SCRIPT = Path(__file__).resolve()
ROOT = SCRIPT.parent.parent
SELF_REL = "maintainers/check_owned.py"
TOOL_REL = "app-blueprint/scripts"
LAUNCHER_REL = TOOL_REL + "/blueprint.py"
PLAN_REL = ".blueprint/blueprint.json"
DIFF_FILTER = "ACMRDT"
CLEAN_NEXT = ("run it from a clean checkout of the base: git checkout <base>, no local edits, then "
              "python maintainers/check_owned.py --workstream <id> --head <branch>")


class Refusal(Exception):
    """A refusal to judge (exit 64): the message says what is wrong, next says what to do."""

    def __init__(self, message, next_step, **info):
        super().__init__(message)
        self.next_step = next_step
        self.info = info


def git_env():
    """No user or system config, no GIT_* variable (GIT_DIR, GIT_EXTERNAL_DIFF, GIT_CONFIG_*, ...)."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    return env


def git(root, *args, input_bytes=None):
    cmd = ["git", "-C", str(root), "-c", "core.quotepath=false", "-c", "diff.renames=false",
           "-c", "diff.ignoreSubmodules=none", "-c", "diff.external=", "-c", "core.fsmonitor=false"] + list(args)
    try:
        return subprocess.run(cmd, capture_output=True, input=input_bytes, env=git_env())
    except OSError as e:
        raise Refusal("git cannot be run: %s" % e, "install git and put it on the PATH") from None


def text(r):
    return r.stdout.decode("utf-8", "replace").strip()


def last_line(r):
    err = r.stderr.decode("utf-8", "replace").strip().splitlines()
    return err[-1] if err else "unknown error"


def resolve(root, ref, what):
    r = git(root, "rev-parse", "--verify", "--quiet", "--end-of-options", ref + "^{commit}")
    if r.returncode != 0 or not text(r):
        if what == "base":
            nxt = "pass an existing base with --base (for example --base origin/v2), or fetch it: git fetch origin"
        else:
            nxt = "pass an existing branch, tag or commit with --head, fetching it first if needed: git fetch origin"
        raise Refusal("%s %r was not found in this repository" % (what, ref), nxt)
    return text(r)


def check_tool_is_the_bases(root, base_sha):
    """The code that judges must be the base's: no edit, staged change or extra file in the tool or this script."""
    if SCRIPT != (root / SELF_REL).resolve():
        raise Refusal("this script is not maintainers/check_owned.py of the repository it checks (%s)" % root, CLEAN_NEXT)
    # core.autocrlf=input: a checkout made with autocrlf=true (CRLF on disk) is the same content as the base
    r = git(root, "-c", "core.autocrlf=input", "-c", "core.safecrlf=false", "diff", "--quiet", "--no-ext-diff", "--no-textconv", base_sha, "--", SELF_REL, TOOL_REL)
    if r.returncode == 1:
        raise Refusal("maintainers/check_owned.py or %s differs from the base, so the lookup could be the "
                      "branch's own code" % TOOL_REL, CLEAN_NEXT)
    if r.returncode != 0:
        raise Refusal("git could not compare the tool with the base: %s" % last_line(r), CLEAN_NEXT)
    r = git(root, "ls-files", "--others", "-z", "--", TOOL_REL)
    if r.returncode != 0:
        raise Refusal("git could not list files under %s: %s" % (TOOL_REL, last_line(r)), CLEAN_NEXT)
    extra = [p.decode("utf-8", "replace") for p in r.stdout.split(b"\0") if p and b"__pycache__/" not in p]
    if extra:
        raise Refusal("untracked file under %s: %s" % (TOOL_REL, extra[0]), CLEAN_NEXT)


def load_launcher(path):
    """The launcher as a module, loaded by path the way the tests load it."""
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("blueprint_launcher_check_owned", str(path))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit as e:
        raise Refusal("the tool launcher did not load (exit %s)" % e.code, CLEAN_NEXT) from None
    except Exception as e:
        raise Refusal("the tool launcher did not load: %s" % e, CLEAN_NEXT) from None
    return mod


def load_base_plan(tool, root, base, base_sha):
    r = git(root, "show", "%s:%s" % (base_sha, PLAN_REL))
    if r.returncode != 0:
        raise Refusal("the base %s has no %s, so there is no plan to judge by" % (base, PLAN_REL),
                      "merge the plan to the base first, or pass a base that has it with --base")
    with tempfile.TemporaryDirectory(prefix="check-owned-plan-") as tmp:
        (Path(tmp) / ".blueprint").mkdir()
        (Path(tmp) / PLAN_REL).write_bytes(r.stdout)
        try:
            return tool.load_plan(Path(tmp))
        except BaseException as e:  # load_plan exits through die() or BlueprintError on a bad plan
            if isinstance(e, KeyboardInterrupt):
                raise
            first = (str(e).strip().splitlines() or [type(e).__name__])[0]
            raise Refusal("the plan at the base %s (%s) does not load: %s" % (base, PLAN_REL, first),
                          "the base's plan is the problem: on a checkout of the base run "
                          "python app-blueprint/scripts/blueprint.py validate and fix it there") from None


def changed_paths(root, merge_base, head_sha):
    r = git(root, "diff-tree", "-r", "-z", "--name-only", "--no-renames", "--no-ext-diff", "--no-textconv",
            "--ignore-submodules=none", "--diff-filter=" + DIFF_FILTER, merge_base, head_sha)
    if r.returncode != 0:
        raise Refusal("git could not list the changes: %s" % last_line(r), "check that both commits are present")
    return sorted({raw.decode("utf-8", "surrogateescape") for raw in r.stdout.split(b"\0") if raw})


def classify(tool, plan, workstream, paths):
    """[(path, status, owner)] for every path that is not the workstream's. status: unplanned or owned-by-other.
    The path is judged exactly as given; one that cleaning would alter is unplanned."""
    bad = []
    for path in paths:
        if tool.norm(path) != path or path.startswith("/") or "\n" in path or "\r" in path:
            bad.append((path, "unplanned", None))
            continue
        owner, _rule, _matches = tool.owner_of(plan, path)
        planned = tool.planned_file(plan, path) is not None or any(
            tool.pattern_instance_rx(p.get("glob", "")).match(path) for p in plan["patterns"])
        if not planned or owner is None:
            bad.append((path, "unplanned", owner))
        elif owner != workstream:
            bad.append((path, "owned-by-other", owner))
    return bad


def run(workstream, base, head):
    """The result as a dict; raises Refusal for anything that stops the check from judging."""
    info = {"workstream": workstream, "base": base, "head": head}
    try:
        root = ROOT
        top = git(root, "rev-parse", "--show-toplevel")
        if top.returncode != 0 or Path(text(top)).resolve() != root.resolve():
            raise Refusal("%s is not the top of a git repository" % root,
                          "run the script of a clean checkout of the base repository: " + CLEAN_NEXT)
        base_sha = info["baseSha"] = resolve(root, base, "base")
        check_tool_is_the_bases(root, base_sha)
        head_sha = info["headSha"] = resolve(root, head, "head")
        mb = git(root, "merge-base", base_sha, head_sha)
        if mb.returncode != 0 or not text(mb):
            shallow = text(git(root, "rev-parse", "--is-shallow-repository")) == "true"
            raise Refusal("%s and %s have no merge base" % (base, head),
                          "this is a shallow clone: git fetch --unshallow" if shallow else
                          "the branch does not share history with the base: rebase or recreate it from the base")
        merge_base = info["mergeBase"] = text(mb)
        n = git(root, "rev-list", "--count", merge_base + ".." + head_sha)
        if n.returncode != 0 or int(text(n) or 0) == 0:
            raise Refusal("nothing to check: %s has no commits beyond the merge-base %s" % (head, merge_base[:12]),
                          "check that --head names the builder's branch with its commits (not the base)")
        tool = load_launcher(root / LAUNCHER_REL)
        plan = load_base_plan(tool, root, base, base_sha)
        ids = [r.get("id") for r in plan.get("roster", [])]
        if workstream not in ids:
            raise Refusal("workstream %r is not in the roster of the base's plan" % workstream,
                          "use one of: " + ", ".join(str(i) for i in ids))
        paths = changed_paths(root, merge_base, head_sha)
        bad = classify(tool, plan, workstream, paths)
    except Refusal as e:
        e.info = dict(info, **e.info)
        raise
    return dict(info, ok=not bad, changedCount=len(paths), changed=paths,
                violations=[{"path": p, "status": s, "owner": o} for p, s, o in bad])


def ids_line(res):
    return "base %s (%s), head %s (%s), merge-base %s" % (res["base"], res["baseSha"][:12], res["head"],
                                                           res["headSha"][:12], res["mergeBase"][:12])


def summary(res):
    n, v = res["changedCount"], len(res["violations"])
    plural = "" if n == 1 else "s"
    if res["ok"]:
        return "PASS: %s changed %d path%s, all its own" % (res["workstream"], n, plural)
    return "FAIL: %s changed %d path%s, %d not its own" % (res["workstream"], n, plural, v)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="check_owned.py", description="Check that a branch touches only the files "
                                 "its workstream owns. Run from a clean checkout of the base.")
    ap.add_argument("--workstream", required=True, help="roster id of the branch's workstream, e.g. W1-R")
    ap.add_argument("--head", required=True, help="the builder's branch, tag or commit (no default)")
    ap.add_argument("--base", default="v2", help="branch or commit to compare with (default v2)")
    ap.add_argument("--json", action="store_true", help="print one JSON object instead of text")
    args = ap.parse_args(argv)
    try:
        res = run(args.workstream, args.base, args.head)
    except Refusal as e:
        if args.json:
            print(json.dumps(dict(e.info, ok=False, refused=True, error="refused", message=str(e), next=e.next_step),
                             indent=2, ensure_ascii=False))
        else:
            sys.stderr.write("error: %s\n  next: %s\n" % (e, e.next_step))
        return EXIT_USAGE
    res["summary"] = summary(res)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(res["summary"])
        print("  " + ids_line(res))
        for v in res["violations"]:
            if v["status"] == "unplanned":
                extra = "unplanned" + (" (an owner rule gives it to %s, but it is not a planned file or pattern "
                                       "instance)" % v["owner"] if v["owner"] else "")
            else:
                extra = "owned by %s" % v["owner"]
            print("  %s  %s" % (v["path"] if v["path"] == v["path"].strip() and "\n" not in v["path"] else repr(v["path"]),
                                extra))
        if res["violations"]:
            print("  next: move the change to the owner's branch, or have the orchestrator change the plan "
                  "(%s on the base) first" % PLAN_REL)
    return EXIT_OK if res["ok"] else EXIT_FINDINGS


if __name__ == "__main__":
    sys.exit(main())
