#!/usr/bin/env python3
"""check_owned.py: did this branch touch only the files its workstream owns?

The orchestrator runs it before merging a builder's branch (contract 12.1, decision 11), from the base's copy of
this script, ISOLATED (python -I is mandatory; without it the script refuses at once, exit 64):

    python -I maintainers/check_owned.py --workstream W1-R --head <full ref or sha> [--base v2] [--json]

Trusted: the user account, the git binary, the Python install and this script file. Not trusted: the plan, the tool
code and the branch under judgement. (The self-bytes check below catches a wrong or stale copy of this script, not a
deliberately altered one: take the script from the base.)

  - Isolation: -I keeps PYTHONPATH, PYTHONSTARTUP, the script's folder and the current directory off the import
    path. A start-up hook of a non-isolated interpreter cannot be undone afterwards, so there is no re-execution:
    without -I the script refuses before it does anything else.
  - Refs: --base and --head are a full ref name (refs/heads/..., refs/remotes/..., refs/tags/...), a full 40-hex
    sha, or a short name that matches exactly one ref of refs/heads, refs/remotes and refs/tags (listed with
    `git for-each-ref`). More than one match is refused, even when they point at the same commit. Abbreviated
    shas are refused. Every outcome prints the full ref names and shas it resolved.
  - Plan and tool come from the base's committed blobs: `.blueprint/blueprint.json` and everything under
    app-blueprint/scripts are read with `git cat-file` at the base sha. Every tree entry must be a regular file
    (mode 100644 or 100755) whose path parts match [A-Za-z0-9._-]+ and are not . or .., not a Windows device name,
    not ending in a dot, with no two names differing only in case; any other entry is a refusal naming it. The tool
    is read through one `git cat-file --batch` (at most 2000 entries and 64 MiB), written to a fresh temporary folder (each target is confirmed to lie inside it), loaded from there by path
    with bytecode writing off, and the folder is deleted afterwards. Nothing in the working tree is executed, so
    the script may run from any checkout.
  - The script's own bytes (CRLF read as LF) must equal maintainers/check_owned.py at the base sha, else it
    refuses and says to run the base's copy.
  - Every git call passes --no-replace-objects and runs in a scrubbed environment (no GIT_* variable, no global or
    system config; GIT_NO_REPLACE_OBJECTS=1).
  - Changed paths come from `git diff-tree -r -z --name-only --no-renames --diff-filter=ACMRDT <merge-base> <head>`:
    a rename is a delete plus an add; type changes, mode changes and submodule entries count.
  - Each path is judged exactly as git reports it. If cleaning it (strip, separator change, leading ./) would
    alter it, it is unplanned. A path is the workstream's only when it is a planned file or an instance of a planned
    pattern AND the owner rules (last match wins) give it to the workstream (exact roster id).

The pass text ends with the head sha to merge: the orchestrator merges exactly that sha.

Exit codes: 0 every changed path is the workstream's; 1 at least one is unplanned or another's; 64 a refusal to
judge (not isolated, unknown workstream, bad or ambiguous refs, shallow clone, script differs from the base, broken,
missing or hostile base, nothing to check); 2 argparse argument errors (contract decision 9); 130 interrupted. With
--json every 0, 1 and 64 outcome (isolation aside) is one JSON object on stdout. Standard library only, Python 3.10+.
"""
import os
import sys

if __name__ == "__main__" and not sys.flags.isolated:
    # Before any other import. Nothing the caller typed is echoed: a ref name may hold characters that run commands
    # when a line is pasted into a shell.
    sys.stderr.write("error: run this check isolated" + chr(10)
                     + "  next: run the same command again with -I: python -I maintainers/check_owned.py "
                     + "<the same arguments>" + chr(10))
    sys.exit(64)

import argparse  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

EXIT_OK, EXIT_FINDINGS, EXIT_USAGE = 0, 1, 64
SCRIPT = Path(os.path.abspath(__file__))
SELF_REL = "maintainers/check_owned.py"
TOOL_REL = "app-blueprint/scripts"
LAUNCHER_REL = TOOL_REL + "/blueprint.py"
PLAN_REL = ".blueprint/blueprint.json"
DIFF_FILTER = "ACMRDT"
RUN_NEXT = "run the base's copy of this script: git show <base>:maintainers/check_owned.py"
FULL_SHA = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")
ABBREV_SHA = re.compile(r"[0-9a-fA-F]{4,39}")


def safe(text):
    """text with control characters and line separators written as escapes (one line, nothing raw)."""
    out = []
    for ch in str(text):
        if ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 32 or ord(ch) in (127, 0x85, 0x2028, 0x2029):
            out.append("\\x%02x" % ord(ch) if ord(ch) < 256 else "\\u%04x" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


class Refusal(Exception):
    """A refusal to judge (exit 64): the message says what is wrong, next says what to do. Both are one line each.
    A caller-controlled value appears only as data inside the message (repr-quoted), never inside a command."""

    def __init__(self, message, next_step, **info):
        super().__init__(safe(message))
        self.next_step = safe(next_step)
        self.info = info


def git_env():
    """No GIT_* variable, no user or system config, no replace objects."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0",
               GIT_NO_REPLACE_OBJECTS="1")
    return env


def git(root, *args, input_bytes=None):
    cmd = ["git", "--no-replace-objects", "-C", str(root), "-c", "core.quotepath=false", "-c", "diff.renames=false",
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


def list_refs(root):
    r = git(root, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes", "refs/tags")
    if r.returncode != 0:
        raise Refusal("git could not list the refs: %s" % last_line(r), "check the repository")
    return [line for line in text(r).splitlines() if line]


def resolve(root, refs, value, what):
    """(full ref name or None, commit sha) from a full ref, a full sha or a short name matching exactly one ref."""
    if FULL_SHA.fullmatch(value):
        r = git(root, "rev-parse", "--verify", "--quiet", value.lower() + "^{commit}")
        if r.returncode != 0 or not text(r):
            raise Refusal("%s %r is not a commit of this repository" % (what, value),
                          "fetch it (git fetch origin) or pass a full ref name")
        return None, text(r)
    if value.startswith("refs/"):
        found = [value] if value in refs else []
    else:
        found = [p + value for p in ("refs/heads/", "refs/remotes/", "refs/tags/") if p + value in refs]
    if len(found) > 1:
        raise Refusal("%s %r matches %d refs: %s" % (what, value, len(found), ", ".join(repr(f) for f in found)),
                      "pass the full ref name (refs/heads/..., refs/remotes/... or refs/tags/...) of the one you mean",
                      matches=found)
    if not found:
        if ABBREV_SHA.fullmatch(value):
            raise Refusal("%s %r looks like an abbreviated sha, which is not accepted" % (what, value),
                          "pass the full 40-hex sha or a full ref name")
        nxt = ("pass an existing base with --base (for example --base origin/v2), or fetch it: git fetch origin"
               if what == "base" else
               "pass the builder's branch with --head (a full ref name or sha), fetching it first if needed: "
               "git fetch origin")
        raise Refusal("%s %r was not found among refs/heads, refs/remotes and refs/tags" % (what, value), nxt)
    r = git(root, "rev-parse", "--verify", "--quiet", found[0] + "^{commit}")
    if r.returncode != 0 or not text(r):
        raise Refusal("%s %r does not point at a commit" % (what, found[0]), "pass a branch or a commit sha")
    return found[0], text(r)


def blob(root, sha, rel):
    r = git(root, "cat-file", "blob", "%s:%s" % (sha, rel))
    return r.stdout if r.returncode == 0 else None


def check_script_is_the_bases(root, base_sha):
    base = blob(root, base_sha, SELF_REL)
    if base is None:
        raise Refusal("the base has no %s" % SELF_REL, "merge this script to the base first; " + RUN_NEXT)
    mine = SCRIPT.read_bytes()
    if mine.replace(b"\r\n", b"\n") != base.replace(b"\r\n", b"\n"):
        raise Refusal("this script is not the base's %s" % SELF_REL, RUN_NEXT)


PART = re.compile(r"[A-Za-z0-9._-]+")
DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", re.IGNORECASE)


def hostile(path, why):
    return Refusal("the base's %s holds an entry the check will not export: %r (%s)" % (TOOL_REL, path, why),
                   "do not use this base: have the orchestrator repair it (a regular file, plain name) before judging")


def check_entry(path, mode, typ):
    if typ != "blob" or mode not in ("100644", "100755"):
        raise hostile(path, "mode %s, type %s: only regular files (100644, 100755) are accepted" % (mode, typ))
    if not path.startswith(TOOL_REL + "/"):
        raise hostile(path, "outside %s" % TOOL_REL)
    for part in path.split("/"):
        if part in (".", "..") or not PART.fullmatch(part):
            raise hostile(path, "path part %r is not made of letters, digits, '.', '_' and '-'" % part)
        if part.endswith(".") or DEVICE.fullmatch(part.split(".")[0]):
            raise hostile(path, "path part %r is a reserved or ambiguous name" % part)


MAX_ENTRIES = 2000
MAX_BYTES = 64 * 1024 * 1024


def read_blobs(root, shas):
    """{sha: bytes} for every sha, through one `git cat-file --batch` process."""
    uniq = sorted(set(shas))
    r = git(root, "cat-file", "--batch", input_bytes=b"".join(s.encode("ascii") + b"\n" for s in uniq))
    out, pos, data = {}, 0, r.stdout
    for sha in uniq:
        end = data.find(b"\n", pos)
        head = data[pos:end].split() if end >= 0 else []
        if len(head) != 3 or head[1] != b"blob" or head[0].decode("ascii", "replace") != sha:
            raise Refusal("git could not read the blob %s of the base's %s" % (sha, TOOL_REL), "check the repository")
        size = int(head[2])
        out[sha] = data[end + 1:end + 1 + size]
        if len(out[sha]) != size:
            raise Refusal("git returned a short blob %s" % sha, "check the repository")
        pos = end + 1 + size + 1
    return out


def export_tool(root, base_sha, dest):
    """app-blueprint/scripts at the base sha, written from its committed blobs into dest. Every entry is checked
    before anything is written; an entry that is not a plain regular file is a refusal, never skipped. At most
    MAX_ENTRIES entries and MAX_BYTES bytes."""
    r = git(root, "ls-tree", "-r", "-l", "-z", "--full-tree", base_sha, "--", TOOL_REL)
    records = [raw for raw in (r.stdout.split(b"\0") if r.returncode == 0 else []) if raw]
    if len(records) > MAX_ENTRIES:
        raise Refusal("the base's %s has %d entries; the limit is %d entries" % (TOOL_REL, len(records), MAX_ENTRIES),
                      "do not use this base: the real tool has about 40 files; have the orchestrator repair it")
    entries, seen, total = [], {}, 0
    for raw in records:
        meta, _, path = raw.decode("utf-8", "surrogateescape").partition(chr(9))
        mode, typ, sha, size = meta.split()
        check_entry(path, mode, typ)
        total += int(size)
        parts = path.split("/")
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            first = seen.setdefault(prefix.lower(), prefix)
            if first != prefix:
                raise hostile(path, "%r and %r differ only in case" % (first, prefix))
        entries.append((path, sha))
    if total > MAX_BYTES:
        raise Refusal("the base's %s holds %d bytes; the limit is %d MiB" % (TOOL_REL, total, MAX_BYTES >> 20),
                      "do not use this base: the real tool is well under 1 MiB; have the orchestrator repair it")
    if not entries:
        raise Refusal("the base has no %s" % TOOL_REL, "pass a base that has the tool with --base")
    inside = Path(dest).resolve()
    targets = []
    for path, sha in entries:
        target = Path(dest).joinpath(*path.split("/"))
        if inside not in target.resolve().parents:
            raise hostile(path, "it would be written outside the temporary folder")
        targets.append((target, sha))
    blobs = read_blobs(root, [sha for _t, sha in targets])
    for target, sha in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blobs[sha])
    return Path(dest) / LAUNCHER_REL


def load_launcher(path):
    """The launcher as a module, loaded by path with bytecode writing off."""
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("blueprint_launcher_check_owned", str(path))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit as e:
        raise Refusal("the base's tool launcher did not load (exit %s)" % e.code, "the base is broken: repair it") from None
    except Exception as e:
        raise Refusal("the base's tool launcher did not load: %s" % e, "the base is broken: repair it") from None
    return mod


def load_base_plan(tool, root, base, base_sha, tmp):
    data = blob(root, base_sha, PLAN_REL)
    if data is None:
        raise Refusal("the base %r has no %s, so there is no plan to judge by" % (base, PLAN_REL),
                      "merge the plan to the base first, or pass a base that has it with --base")
    plan_dir = Path(tmp) / "plan"
    (plan_dir / ".blueprint").mkdir(parents=True)
    (plan_dir / PLAN_REL).write_bytes(data)
    try:
        return tool.load_plan(plan_dir)
    except BaseException as e:  # load_plan exits through die() or BlueprintError on a bad plan
        if isinstance(e, KeyboardInterrupt):
            raise
        first = (str(e).strip().splitlines() or [type(e).__name__])[0]
        raise Refusal("the plan at the base %r (%s) does not load: %s" % (base, PLAN_REL, first),
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


def _rmtree(path):
    def onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=onerror)


def run(workstream, base, head):
    """The result as a dict; raises Refusal for anything that stops the check from judging."""
    info = {"workstream": workstream, "base": base, "head": head}
    try:
        root = SCRIPT.parent
        top = git(root, "rev-parse", "--show-toplevel")
        if top.returncode != 0:
            raise Refusal("%s is not inside a git repository" % root, "run the base's copy of this script, from a "
                          "checkout of the repository")
        refs = list_refs(root)
        info["baseRef"], info["baseSha"] = resolve(root, refs, base, "base")
        info["headRef"], info["headSha"] = resolve(root, refs, head, "head")
        base_sha, head_sha = info["baseSha"], info["headSha"]
        check_script_is_the_bases(root, base_sha)
        mb = git(root, "merge-base", base_sha, head_sha)
        if mb.returncode != 0 or not text(mb):
            shallow = text(git(root, "rev-parse", "--is-shallow-repository")) == "true"
            raise Refusal("%r and %r have no merge base" % (base, head),
                          "this is a shallow clone: git fetch --unshallow" if shallow else
                          "the branch does not share history with the base: rebase or recreate it from the base")
        merge_base = info["mergeBase"] = text(mb)
        n = git(root, "rev-list", "--count", merge_base + ".." + head_sha)
        if n.returncode != 0 or int(text(n) or 0) == 0:
            raise Refusal("nothing to check: %r has no commits beyond the merge-base %s" % (head, merge_base[:12]),
                          "check that --head names the builder's branch with its commits (not the base)")
        tmp = tempfile.mkdtemp(prefix="check-owned-")
        try:
            tool = load_launcher(export_tool(root, base_sha, tmp))
            plan = load_base_plan(tool, root, base, base_sha, tmp)
            ids = [r.get("id") for r in plan.get("roster", [])]
            if workstream not in ids:
                raise Refusal("workstream %r is not in the roster of the base's plan" % workstream,
                              "use one of: " + ", ".join(str(i) for i in ids))
            paths = changed_paths(root, merge_base, head_sha)
            bad = classify(tool, plan, workstream, paths)
        finally:
            _rmtree(tmp)
    except Refusal as e:
        e.info = dict(info, **e.info)
        raise
    return dict(info, ok=not bad, changedCount=len(paths), changed=paths,
                violations=[{"path": p, "status": s, "owner": o} for p, s, o in bad])


def ref_line(res, key):
    ref = res.get(key + "Ref")
    return "%s %s = %s (%s)" % (key, safe(res[key]), safe(ref) if ref else "full sha given", res[key + "Sha"])


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
    ap = argparse.ArgumentParser(prog="python -I maintainers/check_owned.py",
                                 description="Check that a branch touches only the files its workstream owns. "
                                 "Run the base's copy, from any checkout of the repository.")
    ap.add_argument("--workstream", required=True, help="roster id of the branch's workstream, e.g. W1-R")
    ap.add_argument("--head", required=True,
                    help="the builder's branch: a full ref name, a full 40-hex sha, or a short name that matches "
                    "exactly one ref (no default)")
    ap.add_argument("--base", default="v2", help="the base, same forms as --head (default v2)")
    ap.add_argument("--json", action="store_true", help="print one JSON object instead of text")
    args = ap.parse_args(argv)
    try:
        res = run(args.workstream, args.base, args.head)
    except KeyboardInterrupt:
        sys.stderr.write("interrupted: no verdict, nothing was judged\n")
        return 130
    except Refusal as e:
        if args.json:
            print(json.dumps(dict(e.info, ok=False, refused=True, error="refused", message=str(e), next=e.next_step),
                             indent=2, ensure_ascii=False))
        else:
            sys.stderr.write("error: %s\n  next: %s\n" % (e, e.next_step))
            for key in ("baseSha", "headSha", "mergeBase"):
                if e.info.get(key):
                    sys.stderr.write("  %s %s\n" % (key, e.info[key]))
        return EXIT_USAGE
    res["summary"] = summary(res)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(res["summary"])
        print("  " + ref_line(res, "base"))
        print("  " + ref_line(res, "head"))
        print("  merge-base %s" % res["mergeBase"])
        for v in res["violations"]:
            if v["status"] == "unplanned":
                extra = "unplanned" + (" (an owner rule gives it to %s, but it is not a planned file or pattern "
                                       "instance)" % v["owner"] if v["owner"] else "")
            else:
                extra = "owned by %s" % v["owner"]
            shown = v["path"] if safe(v["path"]) == v["path"] and v["path"] == v["path"].strip() else repr(v["path"])
            print("  %s  %s" % (shown, extra))
        if res["violations"]:
            print("  next: move the change to the owner's branch, or have the orchestrator change the plan "
                  "(%s on the base) first" % PLAN_REL)
        else:
            print("  merge exactly this head sha: %s" % res["headSha"])
    return EXIT_OK if res["ok"] else EXIT_FINDINGS


if __name__ == "__main__":
    sys.exit(main())
