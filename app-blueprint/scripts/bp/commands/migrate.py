"""commands.migrate: `migrate [--dry-run] [--force] [--json]` writes schema version 2 (contract 3.4).

The pure upgrade is bp.migrate (upgrade_plan, upgrade_state); this module is the only place that writes the result.
Nothing is lost: the original bytes of blueprint.json are kept as .blueprint/blueprint.v1.json before anything is
replaced, every file is written to a temporary file and moved into place, and nothing at all is written when the file
cannot be read, is not a loadable version 1 plan, or cannot be replaced. Plan and state are written exactly as
core.write_json writes them (same bytes on the same platform); the backup is a byte copy.
"""
import contextlib
import json
import os
import sys

from bp.core import TOOL_HOME, BlueprintError, append_log, bp_dir, emit, find_root
from bp.learn import _copy_version, _version_tuple
from bp.migrate import plan_version, upgrade_plan, upgrade_state
from bp.plan import load_plan, load_raw_plan, load_state
from bp.validate import validate_plan
from bp.version import MIN_SCHEMA2_TOOL, TOOL_VERSION

PLAN, STATE, BACKUP = ".blueprint/blueprint.json", ".blueprint/state.json", ".blueprint/blueprint.v1.json"
NEW_SECTIONS = ("milestones", "features", "tasks", "unknowns")
FIX_NEXT = "fix .blueprint/blueprint.json, then run: python .blueprint/bin/blueprint.py validate"
REFRESH = "python <skill>/scripts/blueprint.py init --refresh-tool"


def register(name, p):
    """Arguments of migrate (cli.build_parser calls this)."""
    p.add_argument("--dry-run", action="store_true", help="print what would change; write nothing")
    p.add_argument("--force", action="store_true", help="skip the check of this project's tool copy")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_migrate)


def _write_failed(rel, exc):
    return BlueprintError("write-failed", f"cannot write {rel}: {getattr(exc, 'strerror', None) or exc}", entry=rel,
                          next=f"make {rel} writable, then run: python .blueprint/bin/blueprint.py migrate")


def _write_atomic(path, rel, data):
    """Write data (bytes, or text as core.write_json writes it) to path through a temporary file in the same folder, then
    replace; the old file stays whole on any failure and no temporary file is left behind."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        if isinstance(data, bytes):
            tmp.write_bytes(data)
        else:
            tmp.write_text(data, encoding="utf-8")  # as core.write_json: the platform's line ends (contract 3.4)
        os.replace(tmp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise _write_failed(rel, exc)


def _json_text(data):
    """The exact serialisation of core.write_json, so a migrated file is formatted like one init writes."""
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _old_copy(root):
    """The version text of this project's tool copy when it is older than MIN_SCHEMA2_TOOL and is not the tool that is
    running, else None. A copy whose launcher is there but whose version cannot be read counts as old."""
    bin_dir = bp_dir(root) / "bin"
    if not (bin_dir / "blueprint.py").is_file() or TOOL_HOME.resolve() == bin_dir.resolve():
        return None
    version = _copy_version(bin_dir)
    if version is None:
        return "unknown"
    return version if _version_tuple(version) < _version_tuple(MIN_SCHEMA2_TOOL) else None


def _read_state(root):
    """(raw state dict, None), or (None, reason) when state.json is absent or cannot be used; it is then left alone."""
    path = bp_dir(root) / "state.json"
    if not path.exists():
        return None, None  # nothing to upgrade
    try:
        raw = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError, RecursionError) as exc:  # UnicodeDecodeError is a ValueError
        return None, f"state.json cannot be read ({type(exc).__name__})"
    return (raw, None) if isinstance(raw, dict) else (None, "state.json is not an object")


def _backup(root, original):
    """Keep the original bytes: .blueprint/blueprint.v1.json when it does not exist (or holds the same bytes), else the
    first free blueprint.v1.N.json, so an older backup is never overwritten. Returns (relative name, written)."""
    d = bp_dir(root)
    n = 1
    while True:
        name = "blueprint.v1.json" if n == 1 else f"blueprint.v1.{n}.json"
        path = d / name
        if not path.exists():
            _write_atomic(path, ".blueprint/" + name, original)
            return ".blueprint/" + name, True
        try:
            if path.read_bytes() == original:
                return ".blueprint/" + name, False
        except OSError:
            pass
        n += 1


def _check_writable(root, targets):
    d = bp_dir(root)
    for rel in targets:
        path = root / rel
        if (path.exists() and not os.access(path, os.W_OK)) or not os.access(d, os.W_OK):
            raise _write_failed(rel, PermissionError(13, "permission denied"))


def cmd_migrate(args):
    root = find_root(args.root)
    dry = bool(getattr(args, "dry_run", False))
    plan_path = bp_dir(root) / "blueprint.json"
    try:
        raw = load_raw_plan(root)  # no-blueprint / bad-json: the 1.x text is already on stderr
    except ValueError as exc:  # a number literal Python refuses to convert; can go once plan.load_raw_plan handles ValueError
        raise BlueprintError("bad-json", f"blueprint.json cannot be read: {exc}", next=FIX_NEXT)
    if isinstance(raw, dict) and plan_version(raw) == 2:  # 3.4 step 1: nothing to do, nothing written
        emit(args, "migrate", {"from": 2, "to": 2, "written": [], "dryRun": dry},
             ["blueprint.json is already schema version 2. Nothing to do."])
        return
    upgrade_plan(raw)  # not an object, version missing, not a whole number, below 1, or too new: its BlueprintError
    old = None if getattr(args, "force", False) else _old_copy(root)  # step 2
    if old is not None:
        raise BlueprintError("old-tool-copy", f"this project's tool copy ({old}) is older than {MIN_SCHEMA2_TOOL}, "
                             "which is the first version that reads schema version 2", entry=".blueprint/bin",
                             next=REFRESH)
    load_plan(root)  # a version 1 plan that does not load (wrong types) is not migrated: it would not load as version 2
    new_plan = upgrade_plan(raw)
    state_raw, state_why = _read_state(root)
    new_state = upgrade_state(state_raw) if state_raw is not None else None
    state_changes = new_state is not None and new_state != state_raw
    added = [k for k in NEW_SECTIONS if k not in raw]
    if dry:  # step 3
        line = "version 1 -> 2; " + (f"adds {', '.join(added)} (empty)" if added else "adds no sections")
        if state_changes:
            line += "; state.json gains stateVersion 2"
        emit(args, "migrate", {"from": 1, "to": 2, "written": [], "dryRun": True}, [line])
        return
    original = plan_path.read_bytes()
    _check_writable(root, [PLAN] + ([STATE] if state_changes else []))
    backup, backup_written = _backup(root, original)  # step 4
    written = [PLAN]
    _write_atomic(plan_path, PLAN, _json_text(new_plan))  # step 5
    if state_changes:
        _write_atomic(bp_dir(root) / "state.json", STATE, _json_text(new_state))
        written.append(STATE)
    elif state_why:
        print(f"warning: {state_why}; it was left as it is", file=sys.stderr)
    if backup_written:
        written.append(backup)
    try:  # step 6: the log and the views never undo a finished migration
        append_log(root, {"event": "migrate", "from": 1, "to": 2, "tool": TOOL_VERSION})
    except OSError as exc:
        print(f"warning: the migration was not logged: {exc.strerror or exc}", file=sys.stderr)
    _render_quietly(root)
    emit(args, "migrate", {"from": 1, "to": 2, "written": written, "dryRun": False},
         [f"Migrated blueprint.json to schema version 2. Backup: {backup}. "
          "Next: add milestones and features (see references/schema.md), then run validate."])


def _render_quietly(root):
    """Regenerate the views when the migrated plan has no validation errors; anything printed goes to stderr."""
    try:
        from bp.views.render import render_all
        plan = load_plan(root)
        errors, _ = validate_plan(plan, root=root)
        if not errors:
            with contextlib.redirect_stdout(sys.stderr):
                render_all(root, plan, load_state(root), quiet=True)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - the plan is already written; say so and go on
        print(f"warning: the views were not regenerated ({type(exc).__name__}); run: python .blueprint/bin/blueprint.py render",
              file=sys.stderr)
