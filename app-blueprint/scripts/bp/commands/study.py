"""commands.study: `study ...` (contract 7.2).

The engine runs `study source|web|desktop|mobile|data <path>`: this module finds the lane module (imported inside
the handler, so a lane that is not built answers not-built through cli), decides whether a folder inside the project
is tracked by git (the engine never runs git), and prints the result. `study list` and `study show` read the store.

The other subcommands belong to the lane workstreams. Their arguments are declared here, exactly as 7.2 gives them,
and the handler calls `command(sub, args, root)` of the module that owns them (trace: bp.study.trace; compare:
bp.study.compare; capture: bp.study.capture; import: bp.study.importers; synth and check: bp.study.synth; adopt:
bp.study.adopt), which prints through core.emit and raises BlueprintError; it returns the exit status (None or 0
for success). A module that is not built answers not-built.
"""
import argparse
import os
import sys
from pathlib import Path

from bp import core
from bp.core import BlueprintError, emit, find_root, run_hint

LANES = {"source": "source", "web": "web", "desktop": "desktop", "mobile": "mobile", "data": "datafiles"}
DELEGATED = {"trace": "trace", "compare": "compare", "capture": "capture", "import": "importers", "synth": "synth",
             "check": "synth", "adopt": "adopt"}
NOT_COMMITTED = "Not committed: remove .gitignore in the study folder to share it."


def _common(p):
    # as in commands/evidence.py (a command module imports no other one, contract 1.2)
    p.add_argument("--root", default=argparse.SUPPRESS, help="project root (default: found from the script or the "
                                                             "current folder)")
    p.add_argument("--json", action="store_true", help="print one JSON object (contract 7.1)")


def require_project(root):
    """Studies live in a project: .blueprint/blueprint.json must exist (no-blueprint, next init)."""
    if not (core.bp_dir(root) / "blueprint.json").is_file():
        raise BlueprintError("no-blueprint", "no .blueprint/blueprint.json here: studies and evidence belong to a "
                             "planned project", next=run_hint(root, "init"))


def resolvers():
    """{".asar": bp.study.asar.resolve} when that lane module is built (5.6), else {}."""
    from bp import registry
    asar = registry.load("study.asar")
    fn = getattr(asar, "resolve", None) if asar is not None else None
    return {".asar": fn} if callable(fn) else {}


def freshness_of(root, study, records):
    from bp import evidence
    if study is None:
        return {r["id"]: "unknown" for r in records}
    return evidence.freshness_all(root, study, records, resolvers=resolvers())


def _authorization(p, required=False):
    g = p.add_mutually_exclusive_group(required=required)
    g.add_argument("--authorization", metavar="TEXT", help='what the user said allows the analysis, in their words '
                                                           '(at least 15 characters), e.g. "I wrote this app"')
    g.add_argument("--own", action="store_true", help="the target is the user's own software")


def register(name, p):
    """Arguments of study (cli.build_parser calls this)."""
    sub = p.add_subparsers(dest="study_cmd", required=True, metavar="{" + ",".join(
        list(LANES) + ["trace", "compare", "capture", "import", "synth", "check", "adopt", "list", "show"]) + "}")
    helps = {"source": "study a repository or folder", "web": "study a HAR file, source maps, bundles or a saved site",
             "desktop": "study an Electron app (app.asar or its folder)", "mobile": "study an APK, AAB, XAPK or IPA",
             "data": "study data files (SQLite, plist, OpenAPI, GraphQL, proto)"}
    for lane, text in helps.items():
        s = sub.add_parser(lane, help=text)
        s.add_argument("path", help="the folder or file to study")
        s.add_argument("--id", help="the study id (default: S- and the target's name)")
        s.add_argument("--replace", action="store_true", help="study again under the same id, keeping cited evidence")
        _authorization(s)
        s.add_argument("--time-limit", type=int, default=300, metavar="S", help="seconds (at most 3600)")
        _common(s)
    t = sub.add_parser("trace", help="trace a route, path or symbol through a source study's import graph")
    t.add_argument("seed")
    t.add_argument("--study", required=True)
    t.add_argument("--depth", type=int, default=3)
    _common(t)
    c = sub.add_parser("compare", help="compare two studies of the same kind")
    c.add_argument("study1")
    c.add_argument("study2")
    c.add_argument("--id")
    _common(c)
    cap = sub.add_parser("capture", help="turn an agent-recorded flow into a study (or print the procedure)")
    cap.add_argument("path", nargs="?")
    cap.add_argument("--har")
    cap.add_argument("--id")
    cap.add_argument("--template", action="store_true", help="print the procedure and an empty capture file")
    _authorization(cap)
    _common(cap)
    im = sub.add_parser("import", help="import a rea bundle or JADX / apktool output (tier C)")
    im.add_argument("path")
    im.add_argument("--from", dest="source", required=True, choices=["rea", "jadx", "apktool"])
    im.add_argument("--id")
    _authorization(im)
    _common(im)
    for name_, text in (("synth", "write candidate behaviours from a study's evidence"),
                        ("check", "check a study's behaviours")):
        s = sub.add_parser(name_, help=text)
        s.add_argument("study")
        _common(s)
    ad = sub.add_parser("adopt", help="turn reviewed behaviours into draft features and unknowns")
    ad.add_argument("study")
    ad.add_argument("--owner", required=True)
    ad.add_argument("--milestone")
    ad.add_argument("--behaviour", action="extend", nargs="+", default=[])
    ad.add_argument("--write", action="store_true")
    _common(ad)
    li = sub.add_parser("list", help="list the studies")
    _common(li)
    sh = sub.add_parser("show", help="show one study and its coverage")
    sh.add_argument("study")
    _common(sh)
    p.set_defaults(fn=cmd_study)


def cmd_study(args):
    sub = args.study_cmd
    args.cmd = f"study {sub}"  # the 7.1 envelope names the subcommand
    root = find_root(getattr(args, "root", None))
    if sub == "capture" and getattr(args, "template", False):
        return _delegate(args, root, sub)  # the procedure needs no project
    require_project(root)
    if sub in LANES:
        return _lane(args, root, sub)
    if sub == "list":
        return _list(args, root)
    if sub == "show":
        return _show(args, root)
    return _delegate(args, root, sub)


def lane_module(kind):
    """The lane module of a study kind (registry.load: imported here, never at module top, contract 1.4)."""
    return _module(LANES[kind], kind)


ARRIVES = {"source": "W2-S", "trace": "W2-S", "web": "W2-W", "desktop": "W2-D", "mobile": "W2-M", "datafiles": "W2-DA",
           "compare": "W2-DA", "capture": "W2-T", "importers": "W2-T", "synth": "W2-SY", "adopt": "W2-SY"}


def _module(name, sub):
    """bp.study.<name>, or not-built (exit 1) naming the workstream it arrives with."""
    from bp import registry
    from bp.version import TOOL_VERSION
    mod = registry.load("study." + name)
    if mod is None:
        raise BlueprintError("not-built", f"study {sub} needs bp/study/{name}.py, which is not built in tool "
                             f"{TOOL_VERSION}; it arrives with {ARRIVES.get(name, 'a later workstream')} "
                             "(app-blueprint 2.0)",
                             next="update the skill, then run: python <skill>/scripts/blueprint.py init --refresh-tool")
    return mod


def _tracked(root, real):
    """True when git tracks at least one file in the folder `real` inside the project (6.1)."""
    try:
        rel = os.path.relpath(real, os.path.realpath(root))
    except ValueError:
        return False
    if rel == "." or rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
        return False
    return bool(core._git(root, "ls-files", "--cached", "--", rel.replace(os.sep, "/") + "/"))


def _skipped_text(cov):
    skipped = cov.get("skipped") if isinstance(cov.get("skipped"), dict) else {}
    n = sum(v for v in skipped.values() if isinstance(v, int))
    if not n:
        return "0 skipped"
    return f"{n} skipped (" + ", ".join(f"{v} {k}" for k, v in sorted(skipped.items())) + ")"


def _lane(args, root, kind):
    from bp.study import engine
    lane = lane_module(kind)
    raw = Path(args.path)
    real = os.path.realpath(raw)
    tracked = kind == "source" and os.path.isdir(real) and _tracked(root, real)
    study = engine.run_study(root, kind, raw, study_id=args.id, authorization=args.authorization, own=args.own,
                             replace=args.replace, time_limit=args.time_limit, lanes=[lane], tracked=tracked)
    cov, counts = study["coverage"], study["counts"]
    line = (f"Studied {study['target']['name']} ({kind}): {counts['evidence']} evidence records, "
            f"{cov['filesScanned']} files scanned, {_skipped_text(cov)}, {counts['secrets']} secrets redacted.")
    if cov.get("truncated"):
        line += f" Stopped by the {cov.get('stoppedBy')}: what was read before is kept."
    lines = [line + " Next: " + run_hint(root, f"study synth {study['id']}", "commands/study")]
    if not study["target"]["own"]:
        lines.append(NOT_COMMITTED)
    emit(args, args.cmd, {"study": study}, lines)


def _list(args, root):
    from bp import evidence
    rows, lines = [], []
    for sid, study in sorted(evidence.all_studies(root).items()):
        records = evidence.load(root, sid)
        beh, _ = evidence.read_behaviours(root, sid)
        s = evidence.summary(root, study, records, beh, freshness_of(root, study, records))
        rows.append(s)
        b = s["behaviours"]
        lines.append(f"{sid}  {s['kind']}  {evidence.safety.one_line(str(s['target']['name']))}  "
                     f"{s['counts']['evidence']} evidence  {s['freshness']['stale']} stale  "
                     f"behaviours {b['reviewed']}/{sum(b.values())} reviewed")
    if not rows:
        lines = ["No studies yet. Start with: " + run_hint(root, "study source . --own", "commands/study")]
    emit(args, args.cmd, {"studies": rows}, lines)


def _show(args, root):
    from bp import evidence
    study = evidence.load_study(root, args.study)
    beh, problem = evidence.read_behaviours(root, study["id"])
    if problem:
        print(f"warning: .blueprint/studies/{study['id']}/behaviours.json {problem}", file=sys.stderr)
    one = evidence.safety.one_line
    t, cov = study["target"], study.get("coverage") or {}
    auth = study.get("authorization") if isinstance(study.get("authorization"), dict) else {}
    lines = [f"{study['id']}  {study['kind']}  {one(str(t.get('name')))}" + ("  (own)" if t.get("own") else ""),
             f"  authorization: {one(str(auth.get('statement')))}",
             f"  tool {study.get('tool')}, studied at {study.get('at')}, lanes: "
             + ", ".join(str(x) for x in study.get("lanes") or []),
             f"  coverage: {cov.get('filesSeen', 0)} files seen, {cov.get('filesScanned', 0)} scanned, "
             f"{_skipped_text(cov)}, {cov.get('bytesRead', 0)} bytes read"
             + (f", stopped by the {cov.get('stoppedBy')}" if cov.get("truncated") else ""),
             f"  evidence: {(study.get('counts') or {}).get('evidence', 0)}, behaviours: "
             f"{len(beh)} ({evidence.behaviour_counts(beh)['reviewed']} reviewed)"]
    for m in cov.get("missing") or []:
        lines.append(f"  missing: {one(str(m))}")
    for n in cov.get("notes") or []:
        lines.append(f"  note: {one(str(n))}")
    if (evidence.study_dir(root, study["id"]) / ".gitignore").is_file():
        lines.append(NOT_COMMITTED)
    emit(args, args.cmd, {"study": study, "behaviours": beh}, lines)


def _delegate(args, root, sub):
    mod = _module(DELEGATED[sub], sub)
    fn = getattr(mod, "command", None)
    if not callable(fn):
        from bp.version import TOOL_VERSION
        raise BlueprintError("not-built", f"study {sub} is not built in tool {TOOL_VERSION} (bp/study/"
                             f"{DELEGATED[sub]}.py has no command())",
                             next="update the skill, then run: python <skill>/scripts/blueprint.py init --refresh-tool")
    code = fn(sub, args, root)
    if code:
        sys.exit(code)
