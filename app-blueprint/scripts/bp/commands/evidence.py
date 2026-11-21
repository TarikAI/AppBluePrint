"""commands.evidence: `evidence add|list|show|stale|export|import` (contract 7.2). The store's rules are bp.evidence;
this module parses the arguments, checks what the caller typed and prints the result. Domain modules are imported
inside the handlers (contract 1.4). A `next` printed here runs as printed (core.run_hint) and never repeats a value
the caller typed (core.AGAIN instead, 1.8 rule 3).
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

from bp import core
from bp.core import AGAIN, BlueprintError, emit, find_root, run_hint

SUBCOMMANDS = ("add", "list", "show", "stale", "export", "import")
MODULE = "commands/evidence"  # what run_hint needs in the project copy (1.8 rule 3)


def _common(p):
    # --root and --json on every subcommand, so they may follow its arguments; SUPPRESS keeps a --root given before
    # the subcommand
    p.add_argument("--root", default=argparse.SUPPRESS, help="project root (default: found from the script or the "
                                                             "current folder)")
    p.add_argument("--json", action="store_true", help="print one JSON object (contract 7.1)")


def register(name, p):
    """Arguments of evidence (cli.build_parser calls this)."""
    sub = p.add_subparsers(dest="evidence_cmd", metavar="{add,list,show,stale,export,import}", required=True)
    a = sub.add_parser("add", help="record one piece of evidence (an agent's note, or a fact about a project file)")
    a.add_argument("--kind", required=True, help="lane.name, for example note.decision")
    a.add_argument("--key", required=True, help="what the fact is about, at most 200 characters")
    a.add_argument("--claim", required=True, help="one plain sentence, at most 300 characters")
    a.add_argument("--basis", required=True, choices=["observed", "derived", "inferred"])
    a.add_argument("--study", help="the study to add it to (default S-notes, made on first use)")
    a.add_argument("--path", help="a project file the fact is about (repo-relative)")
    a.add_argument("--line", type=int, help="the line of --path (its text becomes the excerpt)")
    a.add_argument("--limitation", action="append", default=[], help="what this evidence does not show")
    a.add_argument("--link", action="append", default=[], help="an evidence id this was derived from")
    a.add_argument("--by", metavar="ID", help="your roster id or name")
    _common(a)
    li = sub.add_parser("list", help="list evidence (with --stale: what changed or is gone)")
    li.add_argument("--study")
    li.add_argument("--kind")
    li.add_argument("--stale", action="store_true", help="only stale or missing records (checks freshness)")
    _common(li)
    s = sub.add_parser("show", help="show one record")
    s.add_argument("id", help="an evidence id, ev- and 16 hex digits")
    _common(s)
    st = sub.add_parser("stale", help="count fresh, stale, missing and unknown evidence per study")
    st.add_argument("--study")
    st.add_argument("--target", help="where the studied file or folder is now (for a target outside the project; "
                                     "needs --study)")
    st.add_argument("--strict", action="store_true", help="exit 1 when any record is stale")
    _common(st)
    ex = sub.add_parser("export", help="write the store (or some studies) to one bundle file")
    ex.add_argument("--out", required=True, help="the bundle file to write")
    ex.add_argument("--study", action="append", default=[], help="a study to export (default: all)")
    _common(ex)
    im = sub.add_parser("import", help="merge a bundle written by evidence export")
    im.add_argument("file")
    _common(im)
    p.set_defaults(fn=cmd_evidence)


def cmd_evidence(args):
    sub = args.evidence_cmd
    args.cmd = f"evidence {sub}"  # the 7.1 envelope names the subcommand
    root = find_root(getattr(args, "root", None))
    require_project(root)
    {"add": _add, "list": _list, "show": _show, "stale": _stale, "export": _export, "import": _import}[sub](args, root)


def require_project(root):
    """Studies live in a project: .blueprint/blueprint.json must exist (no-blueprint, next init)."""
    if not (core.bp_dir(root) / "blueprint.json").is_file():
        raise BlueprintError("no-blueprint", "no .blueprint/blueprint.json here: studies and evidence belong to a "
                             "planned project", next=run_hint(root, "init"))


def usage(message):
    return BlueprintError("usage", message, code=64, next=f"{AGAIN} with a corrected value")


def resolvers():
    """{".asar": bp.study.asar.resolve} when that lane module is built (5.6), else {}."""
    from bp import registry
    asar = registry.load("study.asar")
    fn = getattr(asar, "resolve", None) if asar is not None else None
    return {".asar": fn} if callable(fn) else {}


def _by(raw):
    if raw is None:
        return "agent:unknown"
    if not re.fullmatch(r"[^\s\x00-\x1f\x7f]{1,100}", raw):
        raise usage("--by must be one roster id or name: no spaces, at most 100 characters")
    return "agent:" + raw


def _source(root, raw_path, line):
    """The source of an evidence add (7.2): artifact '.', the repo-relative path, its proof digest when it is a file,
    and line N as the excerpt."""
    from bp.evidence import _safe_rel
    from bp.study import safety
    if raw_path is None:
        if line is not None:
            raise usage("--line needs --path")
        return {"artifact": ".", "path": None, "line": None, "offset": None, "digest": None, "excerpt": None}
    if line is not None and line < 1:
        raise usage("--line counts from 1")
    rel = core.project_path(root, raw_path)
    if rel is None or (rel != "." and not _safe_rel(rel)):
        raise usage("--path must name a place inside the project")
    full = root / rel
    digest = excerpt = None
    if rel != "." and not safety.is_link(full) and full.is_file():
        digest = core.proof_digest(full)
        if line is not None:
            try:
                text, _ = safety.decode_text(safety.read_file(full, safety.LIMITS["textBytes"], safety.Budget()))
                lines = text.split("\n")  # line feeds only, as editors and lanes count
                if line <= len(lines):
                    start = sum(len(x) + 1 for x in lines[:line - 1])
                    # the whole text and the offset, so a line inside a PEM block is seen as one
                    excerpt = safety.excerpt(text, start) or None
            except safety.Skip:
                excerpt = None
    return {"artifact": ".", "path": rel, "line": line, "offset": None, "digest": digest, "excerpt": excerpt}


def _short_name(path):
    """The 8.3 short form of an existing path on Windows (what some programs and CI runners type), else None."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 32768)
        return buf.value if 0 < n < 32768 else None
    except (OSError, AttributeError, ValueError):
        return None


def _spellings(path):
    """Every way a folder can be typed: as given, absolute, resolved (long names), its 8.3 short form, each with
    either separator and without a trailing separator. Very short forms (a drive, '/') are left out."""
    forms = set()
    for form in (str(path), os.path.abspath(str(path)), os.path.realpath(str(path)), _short_name(path)):
        if form:
            for f in (form, form.replace("\\", "/"), form.replace("/", "\\")):
                f = f.rstrip("\\/")
                if len(f) > 3:
                    forms.add(f)
    return forms


_MACHINE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]"                    # a Windows drive path, C:\ or C:/
    r"|(?<![A-Za-z0-9])[A-Za-z]:[\w~$.-]{1,255}\\"         # a drive-relative path, C:Users\x
    r"|\\\\"                                              # UNC and device paths: \\host\..., \\?\, \\.\
    r"|(?<![:/\w.~-])//[^/\s]"                            # //host/... that is no URL (no scheme's colon before it)
    r"|(?<![\w.~-])/(?:home|Users|root|tmp|private|media|Volumes)/"   # POSIX folders that name a user or this machine
    r"|(?<![\w.~-])/var/folders/|(?<![\w.~-])/mnt/[A-Za-z](?:/|\b)"
    r"|(?<![\w.~-])~[\\/]|%USERPROFILE%|%HOMEPATH%|%HOMEDRIVE%|\$HOME\b"
    r"|(?<!\.)\.\.[\\/](?:\.\.[\\/]){0,16}(?:Users|home)[\\/]")   # a relative path climbing into the users' folders


def machine_path(root, text):
    """True when text holds a path on this machine (R2-M7): the project root, the home folder or the temp folder in
    any spelling (either separator, any case on Windows, the 8.3 short and the long form), a Windows drive path, a
    UNC or device path, or a POSIX path under the folders that name a user or this machine. URL paths, routes such
    as /api/users and relative paths are not."""
    import tempfile
    s = str(text)
    if _MACHINE.search(s):
        return True
    norm = s.replace("\\", "/")
    if os.name == "nt":
        norm = norm.lower()
    for folder in {str(root), os.path.expanduser("~"), tempfile.gettempdir(), os.environ.get("TEMP"),
                   os.environ.get("TMP")}:
        if not folder or not os.path.isabs(folder):
            continue
        for form in _spellings(folder):
            form = form.replace("\\", "/")
            if (form.lower() if os.name == "nt" else form) in norm:
                return True
    return False


def _add(args, root):
    from bp import evidence
    from bp.study import safety
    if not evidence.KIND.fullmatch(args.kind):
        raise usage("--kind must be lowercase words joined by dots, for example note.decision")
    for name, value, most in (("--key", args.key, evidence.KEY_MAX), ("--claim", args.claim, evidence.CLAIM_MAX)):
        if not value.strip():
            raise usage(f"{name} must not be empty")
        if len(value) > most:
            raise usage(f"{name} must be at most {most} characters")
    study = args.study or evidence.NOTES
    if not evidence.is_study_id(study):
        raise usage("--study must be a study id: S- then lowercase letters, digits and hyphens")
    if study != evidence.NOTES:
        evidence.load_study(root, study)
    for link in args.link:
        if not evidence.EVIDENCE_ID.fullmatch(link):
            raise usage("--link must be an evidence id: ev- and 16 hex digits")
    if args.link:
        known = evidence.all_ids(root)
        if any(link not in known for link in args.link):
            raise BlueprintError("evidence-not-found", "a --link names evidence that is not in .blueprint/studies",
                                 next=run_hint(root, "evidence list", MODULE))
    src = _source(root, args.path, args.line)
    for name, value in [("--claim", args.claim)] + [("--limitation", x) for x in args.limitation]:
        if machine_path(root, value):
            raise usage(f"{name} holds a path on this machine; nothing was stored. Use --path for a file of the "
                        "project, or describe the place in words")
    rec = evidence.make_record(study, args.kind, args.key, args.claim, args.basis, src, None,
                               list(args.limitation), "unknown", args.link,
                               by=_by(args.by), at=core.now_iso())
    rec, added = evidence.add(root, study, rec)
    emit(args, args.cmd, {"record": rec, "added": added},
         [f"Recorded {rec['id']} in {study}." if added else f"Already recorded: {rec['id']} in {study}."])


def freshness_of(root, sid, study, records, target=None):
    """(states, not checked: {id: limit}) for records of one study; a study whose study.json is damaged is unknown."""
    from bp import evidence
    if study is None:  # a damaged study.json: its records cannot be checked, and that is never silent
        return {r["id"]: "unknown" for r in records}, {r["id"]: "damaged study.json" for r in records}
    return evidence.freshness_check(root, study, records, target=target, resolvers=resolvers())


def _not_checked_line(unchecked):
    limits = sorted(set(unchecked.values()))
    n = len(unchecked)
    return f"  {n} record{'s' if n != 1 else ''} not checked: {', '.join(limits)} reached"


def _studies(root, sid):
    """[(id, study record or None)]: one study (errors when it is not there or damaged), or every study folder (a
    damaged study.json reads as None: its records are listed, never fresh)."""
    from bp import evidence
    if sid is not None:
        if not evidence.is_study_id(sid):
            raise evidence.not_found(root, sid)
        return [(sid, evidence.load_study(root, sid))]
    good = evidence.all_studies(root)
    return [(s, good.get(s)) for s in evidence.study_ids(root)]


def _tags(rec, state):
    """The marks after an id: the freshness (when it was checked), superseded, edited."""
    from bp import evidence
    out = [] if state is None else ["\u26a0 " + state if state in evidence.CHANGED else state]
    if rec.get("superseded") is True:
        out.append("superseded")
    if evidence.edited(rec):
        out.append("edited")
    return " ".join(out)


def _list(args, root):
    """One line per record. Freshness is computed only with --stale (it reads and hashes the targets); then only the
    stale and missing records are listed, each marked, and records the tool could not check are counted."""
    from bp import evidence
    from bp.study import safety
    rows, lines, not_checked = [], [], {}
    for sid, study in _studies(root, args.study):
        records = evidence.load(root, sid)
        if args.kind is not None:
            records = [r for r in records if r.get("kind") == args.kind]
        states, unchecked = freshness_of(root, sid, study, records) if args.stale else ({}, {})
        if args.stale:
            records = [r for r in records if evidence.is_changed(states.get(r["id"]))]
            if unchecked:
                not_checked[sid] = {"count": len(unchecked), "limits": sorted(set(unchecked.values()))}
        if not records and sid not in not_checked:
            continue
        lines.append(f"{sid} ({len(records)} record{'s' if len(records) != 1 else ''})")
        if sid in not_checked:
            lines.append(_not_checked_line(unchecked))
        for r in records:
            state = states.get(r["id"]) if args.stale else None
            rows.append(dict(r, freshness=state, edited=evidence.edited(r)))
            claim = safety.one_line(str(r.get("claim", "")))
            tags = _tags(r, state)
            lines.append(f"  {r['id']}  {r.get('basis')}  " + (tags + "  " if tags else "") + claim)
    if not lines:
        lines = ["No evidence" + (" matches." if args.study or args.kind or args.stale else
                                  " yet. Record some with: " + run_hint(root, "evidence add --help", MODULE))]
    data = {"records": rows}
    if args.stale:
        data["notChecked"] = not_checked
    emit(args, args.cmd, data, lines)


def _show(args, root):
    from bp import evidence
    from bp.study import safety
    if not evidence.EVIDENCE_ID.fullmatch(args.id):
        raise BlueprintError("evidence-not-found", "that is not an evidence id (ev- and 16 hex digits)",
                             next=run_hint(root, "evidence list", MODULE))
    found = []
    for sid, study in _studies(root, None):
        for r in evidence.load(root, sid):
            if r["id"] == args.id:
                found.append((sid, study, r))
    if not found:
        raise BlueprintError("evidence-not-found", f"evidence {args.id} is not in .blueprint/studies",
                             entry=f"evidence {args.id}", next=run_hint(root, "evidence list", MODULE))
    sid, study, rec = found[0]
    states, unchecked = freshness_of(root, sid, study, [rec])
    state = states.get(rec["id"], "unknown")
    src = rec.get("source") if isinstance(rec.get("source"), dict) else {}
    one = safety.one_line
    lines = [f"{rec['id']}  {_tags(rec, state)}",
             f"  study: {sid}" + (f" (also in {', '.join(s for s, _, _ in found[1:])})" if len(found) > 1 else ""),
             f"  kind: {one(str(rec.get('kind')))}", f"  key: {one(str(rec.get('key')))}",
             f"  claim: {one(str(rec.get('claim')))}", f"  basis: {rec.get('basis')}  coverage: {rec.get('coverage')}",
             f"  source: {one(str(src.get('artifact')))}" + (f" {one(str(src['path']))}" if src.get("path") else "")
             + (f" line {src['line']}" if src.get("line") else "") + (f" offset {src['offset']}"
                                                                      if src.get("offset") is not None else ""),
             f"  digest: {src.get('digest')}"]
    if src.get("excerpt"):
        lines.append(f"  excerpt: {one(str(src['excerpt']))}")
    if rec.get("data"):
        lines.append("  data: " + one(json.dumps(rec["data"], ensure_ascii=False, sort_keys=True)))
    for lim in rec.get("limitations") or []:
        lines.append(f"  limitation: {one(str(lim))}")
    if rec.get("links"):
        lines.append("  links: " + ", ".join(str(x) for x in rec["links"]))
    lines.append(f"  by: {one(str(rec.get('by')))}" + (f" at {rec['at']}" if isinstance(rec.get("at"), str) else ""))
    if unchecked:
        lines.insert(1, _not_checked_line(unchecked))
    emit(args, args.cmd, {"record": rec, "freshness": state, "notChecked": unchecked.get(rec["id"]),
                          "studies": [s for s, _, _ in found]}, lines)


def _stale(args, root):
    from bp import evidence
    if args.target is not None and args.study is None:
        raise usage("--target needs --study: it says where that one study's target is now")
    target = None
    if args.target is not None:
        target = Path(args.target).resolve()
        if not target.exists():
            raise BlueprintError("target-not-found", "nothing exists at the --target path given",
                                 next=f"{AGAIN} with the path of the studied file or folder")
    out, stale, missing, not_checked, lines = {}, {}, {}, {}, []
    for sid, study in _studies(root, args.study):
        records = evidence.load(root, sid)
        states, unchecked = freshness_of(root, sid, study, records, target)
        out[sid] = evidence.freshness_counts(states)
        stale[sid] = sorted(i for i, s in states.items() if s == "stale")
        missing[sid] = sorted(i for i, s in states.items() if s == "missing")
        c = out[sid]
        lines.append(f"{sid}: {c['fresh']} fresh, {c['stale']} stale, {c['missing']} missing, {c['unknown']} unknown")
        if unchecked:
            not_checked[sid] = {"count": len(unchecked), "limits": sorted(set(unchecked.values()))}
            lines.append(_not_checked_line(unchecked))
        lines += [f"  \u26a0 stale {i}" for i in stale[sid]] + [f"  \u26a0 missing {i}" for i in missing[sid]]
    if not out:
        lines = ["No studies yet."]
    emit(args, args.cmd, {"studies": out, "stale": stale, "missing": missing, "notChecked": not_checked}, lines)
    if args.strict and (any(stale.values()) or any(missing.values()) or not_checked):
        # fail closed: evidence that changed or is gone fails, and so does a check stopped by the tool's own limit;
        # unknown because the target is not available here (outside the project, no --target) is only counted
        sys.exit(1)


def _export(args, root):
    from bp import evidence
    for sid in args.study:
        if not evidence.is_study_id(sid):
            raise evidence.not_found(root, sid)
    bundle = evidence.export_bundle(root, args.study or None)
    out = Path(args.out)
    if out.is_dir():
        raise BlueprintError("write-failed", "--out names a folder; give a file name", next=f"{AGAIN} with a file")
    try:
        core.write_json(out, bundle)
    except OSError as e:
        raise BlueprintError("write-failed", f"cannot write the bundle ({e.strerror or e})",
                             next=f"{AGAIN} with a file in a writable folder") from None
    n = {"studies": len(bundle["studies"]), "records": len(bundle["records"]),
         "behaviours": sum(len(b["behaviours"]) for b in bundle["behaviours"])}
    emit(args, args.cmd, n, [f"Wrote {n['studies']} studies, {n['records']} records to {args.out}."])


def _import(args, root):
    from bp import evidence
    from bp.study import safety
    try:
        data = safety.read_file(Path(args.file), evidence.BUNDLE_MAX, safety.Budget({"totalBytes": evidence.BUNDLE_MAX}))
    except safety.Skip as e:
        why = {"too-large": "larger than 256 MiB", safety.MISSING: "it does not exist"}.get(e.reason, "it cannot be "
                                                                                                    "read")
        raise evidence._bad_bundle(why) from None
    counts = evidence.import_bundle(root, evidence.read_bundle(data))
    parts = "; ".join(f"{p}: {c['added']} added, {c['duplicate']} duplicate, {c['conflict']} conflict, "
                      f"{c['malformed']} malformed" for p, c in ((p, counts[p]) for p in ("studies", "records",
                                                                                           "behaviours")))
    emit(args, args.cmd, counts, [f"Imported: {counts['added']} added, {counts['duplicate']} duplicate, "
                                  f"{counts['conflict']} conflict, {counts['malformed']} malformed ({parts})."])
