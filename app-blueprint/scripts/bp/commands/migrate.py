"""commands.migrate: `migrate [--dry-run] [--force] [--json]` writes schema version 2 (contract 3.4).

The pure upgrade is bp.migrate (upgrade_plan, upgrade_state); this module is the only place that writes the result.
Nothing is lost: blueprint.json is read once, those bytes are kept as .blueprint/blueprint.v1.json before anything is
replaced, both outputs are serialised to bytes before the first write (a plan that cannot be written is refused, one
that can is written through a temporary file that is moved into place), and nothing at all is written when the file
cannot be read, is not a loadable version 1 plan, or cannot be replaced. Plan and state have exactly the bytes
core.write_json writes on this platform; the backup is a byte copy. A symlinked blueprint.json or state.json is written
through to its target, so the link survives.
"""
import contextlib
import json
import os
import re
import secrets
import sys
from pathlib import Path

from bp.core import TOOL_HOME, BlueprintError, append_log, bp_dir, emit, find_root, state_lock
from bp.learn import _copy_version
from bp.migrate import plan_version, upgrade_plan, upgrade_state
from bp.plan import load_plan, load_raw_plan, load_state
from bp.validate import validate_plan
from bp.version import MIN_SCHEMA2_TOOL, TOOL_VERSION

PLAN, STATE = ".blueprint/blueprint.json", ".blueprint/state.json"
NEW_SECTIONS = ("milestones", "features", "tasks", "unknowns")


def register(name, p):
    """Arguments of migrate (cli.build_parser calls this)."""
    p.add_argument("--dry-run", action="store_true", help="print what would change; write nothing")
    p.add_argument("--force", action="store_true", help="skip the check of this project's tool copy")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_migrate)


def _run(root, args):
    """A command line the user can run as printed: the project's own copy when it has the command, else this tool."""
    copy = bp_dir(root) / "bin"
    if (copy / "blueprint.py").is_file() and (not args.startswith("migrate") or (copy / "bp" / "commands" / "migrate.py").is_file()):
        return f"python .blueprint/bin/blueprint.py {args}"
    return f'python "{TOOL_HOME / "blueprint.py"}" {args} --root "{root}"'


def _fix_next(root):
    return f"fix .blueprint/blueprint.json, then run: {_run(root, 'validate')}"


class _WriteError(Exception):
    """A file could not be written: `shown` is the project path that failed, `reason` the system's words, `kind` where:
    "tmp" (the temporary file could not be created), "blocked" (something that is not a file is in its place) or
    "replace" (the target could not be replaced: locked by another program, or read-only)."""

    def __init__(self, shown, reason, kind="tmp"):
        super().__init__(shown, reason, kind)
        self.shown, self.reason, self.kind = shown, reason, kind


def _shown(root, name, fallback):
    """A path from an OSError, relative to the project when it is inside it, else as given."""
    if not name:
        return fallback
    try:
        return Path(os.path.realpath(name)).relative_to(Path(os.path.realpath(root))).as_posix()
    except ValueError:
        return str(name)


def _write_failed(root, shown, reason, kind="tmp"):
    again = f", then run: {_run(root, 'migrate')}"
    if kind == "replace":
        return BlueprintError("write-failed", f"cannot write {shown}: {reason}", entry=shown,
                              next=f"close any program that has {shown} open, or make it writable" + again)
    if kind == "blocked":
        return BlueprintError("write-failed", f"cannot write {shown}: it is in the way and is not a file", entry=shown,
                              next=f"remove or rename {shown}" + again)
    exists = (root / shown).exists()
    return BlueprintError("write-failed", f"cannot write {shown}: {reason}", entry=shown if exists else ".blueprint",
                          next=(f"make {shown} writable" if exists else "make the folder .blueprint writable") + again)


def _write_atomic(root, path, rel, data, unique=False):
    """Write bytes to path (a link is followed to its target) through a temporary file beside it, then replace. The old
    file stays whole on any failure, and the temporary file is removed whatever interrupts the write. unique: the temp
    name is unique to the process, as core.write_json's (state.json, which other commands write too)."""
    target = Path(os.path.realpath(path))
    suffix = f".{os.getpid()}-{secrets.token_hex(4)}.tmp" if unique else ".tmp"
    tmp = target.with_name(target.name + suffix)
    tmp_rel = _shown(root, tmp, rel + suffix)
    keep = False  # a file already at the temporary name that could not be opened for writing is not ours to remove
    try:
        try:
            tmp.write_bytes(data)
        except OSError as exc:
            kind = "blocked" if tmp.exists() and not tmp.is_file() else "tmp"
            keep = tmp.exists()
            raise _WriteError(_shown(root, getattr(exc, "filename", None), tmp_rel) if kind == "tmp" else tmp_rel,
                              getattr(exc, "strerror", None) or str(exc), kind)
        try:
            os.replace(tmp, target)
        except OSError as exc:
            raise _WriteError(rel, getattr(exc, "strerror", None) or str(exc), "replace")
    except BaseException:
        with contextlib.suppress(OSError):
            if not keep and tmp.is_file():
                tmp.unlink()
        raise


def _text(data):
    """The serialisation of core.write_json, so a migrated file is formatted like one init writes."""
    return json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def _encode(data):
    """The bytes core.write_json writes on this platform (text mode turns \\n into os.linesep). Raises UnicodeEncodeError
    for a lone surrogate escape and ValueError for a number that is not finite, before anything is written."""
    return _text(data).replace("\n", os.linesep).encode("utf-8")


def _leading(version):
    """(major, minor, patch) from the leading numbers of a version text (2.0.0-rc1 is 2.0.0), or None when there are none
    or one has more than nine digits."""
    m = re.match(r"\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(version))
    if m is None or any(len(g) > 9 for g in m.groups() if g):
        return None
    return tuple(int(x or 0) for x in m.groups())


def _old_copy(root):
    """None when the copy is fine or is the running tool; else the problem as a message for old-tool-copy. A launcher
    with no readable version is a problem too: it cannot be shown to read schema 2."""
    bin_dir = bp_dir(root) / "bin"
    if not (bin_dir / "blueprint.py").is_file() or TOOL_HOME.resolve() == bin_dir.resolve():
        return None
    version = _copy_version(bin_dir)
    have = _leading(version) if version else None
    if have is None:
        return ("this project's tool copy has no readable version; it may be older than " + MIN_SCHEMA2_TOOL +
                ", the first version that reads schema version 2")
    if have < _leading(MIN_SCHEMA2_TOOL):
        return (f"this project's tool copy ({version}) is older than {MIN_SCHEMA2_TOOL}, which is the first version "
                "that reads schema version 2")
    return None


class _Repeats(dict):
    """A parsed object in which a key was repeated: the usual dict (the last value wins) plus the repeated keys."""
    repeated = ()


def _object(pairs):
    """object_pairs_hook: the usual dict; one that has a repeated key is a _Repeats that remembers the keys."""
    out, again = {}, []
    for k, v in pairs:
        if k in out and k not in again:
            again.append(k)
        out[k] = v
    if not again:
        return out
    marked = _Repeats(out)
    marked.repeated = tuple(again)
    return marked


def _repeated_paths(value):
    """The paths (modules[2].id) of every repeated key in a parsed document, in document order."""
    found, stack = [], [("", value)]
    while stack:
        where, v = stack.pop()
        if isinstance(v, dict):
            found.extend(f"{where}.{k}" if where else str(k) for k in getattr(v, "repeated", ()))
            stack.extend((f"{where}.{k}" if where else str(k), x) for k, x in reversed(list(v.items())))
        elif isinstance(v, list):
            stack.extend((f"{where}[{i}]", x) for i, x in reversed(list(enumerate(v))))
    return found


def _non_finite(value):
    """The path of the first number that is not finite (1E400, NaN, Infinity) in a parsed document, or None."""
    stack = [("", value)]
    while stack:
        where, v = stack.pop()
        if isinstance(v, float) and v - v != 0:  # inf and nan
            return where or "the top level"
        if isinstance(v, dict):
            stack.extend((f"{where}.{k}" if where else str(k), x) for k, x in reversed(list(v.items())))
        elif isinstance(v, list):
            stack.extend((f"{where}[{i}]", x) for i, x in reversed(list(enumerate(v))))
    return None


def _read_plan(root):
    """(original bytes, parsed plan): blueprint.json is read once and both the backup and the upgrade
    come from those bytes. A file that does not parse is reported by load_raw_plan with the 1.x text."""
    path = bp_dir(root) / "blueprint.json"
    try:
        original = path.read_bytes()
        return original, json.loads(original.decode("utf-8"), object_pairs_hook=_object)
    except (OSError, ValueError, RecursionError):  # not readable, not UTF-8, not JSON, or too deep (a ValueError)
        load_raw_plan(root)  # raises no-blueprint / bad-json with the 1.x message; it cannot succeed here
        raise


def _read_state(root):
    """(state dict, None), (None, None) when there is no state.json, or (None, why) when it cannot be used: it is left
    as it is."""
    path = bp_dir(root) / "state.json"
    if not path.exists():
        return None, None
    try:
        raw = json.loads(path.read_bytes().decode("utf-8"))
    except OSError as exc:
        return None, f"cannot be read ({exc.strerror or 'read error'})"
    except UnicodeDecodeError:
        return None, "is not UTF-8 text"
    except (ValueError, RecursionError):
        return None, "is not valid JSON"
    return (raw, None) if isinstance(raw, dict) else (None, "is not a JSON object")


def _state_out(state_raw, state_why):
    """(the bytes of the upgraded state, or None when there is nothing to write; why it is left as it is, or None)."""
    if state_raw is None:
        return None, state_why
    new_state = upgrade_state(state_raw)
    if new_state == state_raw:
        return None, None
    try:
        return _encode(new_state), None
    except (UnicodeEncodeError, ValueError, RecursionError):
        return None, "contains text or numbers that cannot be written back"


def _backup(root, original):
    """Keep the original bytes: .blueprint/blueprint.v1.json when it does not exist (or holds the same bytes), else the
    first free blueprint.v1.N.json, so an older backup is never overwritten. Returns (relative name, written)."""
    d = bp_dir(root)
    n = 1
    while True:
        name = "blueprint.v1.json" if n == 1 else f"blueprint.v1.{n}.json"
        path = d / name
        if not path.exists():
            try:
                _write_atomic(root, path, ".blueprint/" + name, original)
            except _WriteError as e:
                raise _write_failed(root, e.shown, e.reason, e.kind)
            return ".blueprint/" + name, True
        try:
            if path.read_bytes() == original:
                return ".blueprint/" + name, False
        except OSError:
            pass
        n += 1


def _check_writable(root, targets):
    """Refuse before anything is written when a file to replace is read-only or the folder refuses new files."""
    d = bp_dir(root)
    for rel in targets:
        path = root / rel
        if path.exists() and not os.access(path, os.W_OK):
            raise _write_failed(root, rel, "permission denied")
    if not os.access(d, os.W_OK) or not all(os.access(Path(os.path.realpath(root / r)).parent, os.W_OK) for r in targets):
        raise BlueprintError("write-failed", "cannot write in .blueprint: permission denied", entry=".blueprint",
                             next=f"make the folder .blueprint writable, then run: {_run(root, 'migrate')}")


def _warn_hard_link(path, rel):
    """An atomic replace cannot keep a hard link: the other name keeps the old bytes. Say so."""
    with contextlib.suppress(OSError):
        if os.stat(os.path.realpath(path)).st_nlink > 1:
            print(f"warning: {rel} had another name (a hard link); that name still holds the version 1 file", file=sys.stderr)


def _state_warning(root, why):
    return (f"warning: .blueprint/state.json {why}; it was left as it is. Fix or delete it, then run: "
            + _run(root, "sync"))


def cmd_migrate(args):
    root = find_root(args.root)
    dry = bool(getattr(args, "dry_run", False))
    original, raw = _read_plan(root)
    if isinstance(raw, dict) and plan_version(raw) == 2:  # 3.4 step 1: nothing to do, nothing written
        emit(args, "migrate", {"from": 2, "to": 2, "written": [], "dryRun": dry},
             ["blueprint.json is already schema version 2. Nothing to do."])
        return
    where = _non_finite(raw)
    if where is not None:  # would be written as Infinity or NaN, which JSON does not have
        raise BlueprintError("bad-json", f"blueprint.json has a number that is not finite ({where}); JSON cannot hold it",
                             entry="blueprint.json", next=_fix_next(root))
    load_plan(root)  # the load checks of 3.3 (shape, types, version): a plan that does not load is not migrated
    repeated = _repeated_paths(raw)
    new_plan = upgrade_plan(raw)
    old = None if getattr(args, "force", False) else _old_copy(root)  # step 2
    if old is not None:
        raise BlueprintError("old-tool-copy", old, entry=".blueprint/bin",
                             next=f'python "{TOOL_HOME / "blueprint.py"}" init --refresh-tool --root "{root}"')
    try:  # both outputs are bytes before anything is written, in a dry run too
        plan_bytes = _encode(new_plan)
    except (UnicodeEncodeError, ValueError, RecursionError):
        raise BlueprintError("bad-json", "blueprint.json contains text that is not valid Unicode (a lone \\udXXX escape)",
                             entry="blueprint.json", next=_fix_next(root))
    state_bytes, state_why = _state_out(*_read_state(root))
    added = [k for k in NEW_SECTIONS if k not in raw]
    if dry:  # step 3
        line = "version 1 -> 2; " + (f"adds {', '.join(added)} (empty)" if added else "adds no sections")
        if state_bytes is not None:
            line += "; state.json gains stateVersion 2"
        elif state_why:
            line += f"; state.json {state_why} and is left as it is"
        for key in repeated:
            print(f'warning: blueprint.json repeats the key "{key}"; the last value is kept and the original goes to the backup',
                  file=sys.stderr)
        emit(args, "migrate", {"from": 1, "to": 2, "written": [], "dryRun": True}, [line])
        return
    _check_writable(root, [PLAN] + ([STATE] if state_bytes is not None else []))
    # From here state.json is read again and replaced under the state lock (core.state_lock), so a change another
    # command made meanwhile is kept; the log line is appended inside it. Nothing above writes or locks anything.
    with state_lock(root):
        state_bytes, state_why = _state_out(*_read_state(root))
        backup, backup_written = _backup(root, original)  # step 4
        for key in repeated:
            print(f'warning: blueprint.json repeats the key "{key}"; the last value was kept and the original is in {backup}',
                  file=sys.stderr)
        written = [PLAN]
        _warn_hard_link(bp_dir(root) / "blueprint.json", PLAN)
        try:  # step 5
            _write_atomic(root, bp_dir(root) / "blueprint.json", PLAN, plan_bytes)
        except _WriteError as e:
            raise _write_failed(root, e.shown, e.reason, e.kind)
        if state_bytes is not None:
            _warn_hard_link(bp_dir(root) / "state.json", STATE)
            try:
                _write_atomic(root, bp_dir(root) / "state.json", STATE, state_bytes, unique=True)
                written.append(STATE)
            except _WriteError as e:  # the plan is migrated; the state upgrades in memory whenever it is read (3.2)
                print(f"warning: {e.shown} could not be written ({e.reason}). blueprint.json is migrated; state.json was left "
                      "as it is and is upgraded in memory whenever it is read", file=sys.stderr)
        elif state_why:
            print(_state_warning(root, state_why), file=sys.stderr)
        if backup_written:
            written.append(backup)
        try:  # step 6: the log and the views never undo a finished migration
            append_log(root, {"event": "migrate", "from": 1, "to": 2, "tool": TOOL_VERSION})
        except OSError as exc:
            print(f"warning: the migration was not logged: {exc.strerror or exc}", file=sys.stderr)
    _render_quietly(root)
    emit(args, "migrate", {"from": 1, "to": 2, "written": written, "dryRun": False},
         [f"Migrated blueprint.json to schema version 2. Backup: {backup}. Next: add milestones and features "
          f"(see references/schema.md in the skill), then run: {_run(root, 'validate')}"])


def _render_quietly(root):
    """Regenerate the views when the migrated plan has no validation errors; anything printed goes to stderr."""
    try:
        from bp.views.render import render_all
        plan = load_plan(root)
        errors, _ = validate_plan(plan, root=root)
        if not errors:
            with contextlib.redirect_stdout(sys.stderr):
                render_all(root, plan, load_state(root), quiet=True)
    except (Exception, SystemExit):  # noqa: BLE001 - the plan is already written; say so and go on
        print(f"warning: the views were not regenerated; run: {_run(root, 'render')}", file=sys.stderr)
