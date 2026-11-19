"""cli: main(), the package completeness check, and the static command table (contract 1.4, 7.1).

main() creates a subparser for every name in registry.COMMAND_TABLE. When the command's module exists, its
register(name, parser) adds the arguments and sets fn; when it does not, the subcommand answers not-built (exit 1,
naming the workstream it arrives with). A BlueprintError is printed as `error:` / `  next:` (or the 7.1 envelope with
--json) and exits with its code; any other exception is an incident (exit 70), as in 1.x.
"""
import argparse
import json
import os
import sys

from bp.version import TOOL_VERSION
from bp import registry
from bp.core import BlueprintError
from bp.install import check_package
from bp.learn import record_incident

# The start-up completeness check (install.check_package): in a project copy every module its MANIFEST lists must
# exist, and every module present must import; otherwise the launcher fails closed (exit 2 for guard and hook pre, 1
# otherwise) instead of a command failing later. Nothing is listed by hand: a later module is covered by its file
# alone. PACKAGE_MODULES is what was checked (every module on disk), kept for tests and doctor-style inspection.
PACKAGE_MODULES = tuple(check_package())

# Help for every subcommand of 2.0 (contract 1.4: a static dict in cli, so an unbuilt command still has its line).
HELP = {
    "init": "create .blueprint/ with an empty plan and a copy of this tool",
    "validate": "check the plan: fields, phases, dependencies, ownership overlaps",
    "render": "write STRUCTURE.md, ROSTER.md and map.html",
    "contracts": "list or write README contracts for module folders",
    "export": "print codeowners, csv, json, or AgenticTeam add-task commands",
    "sync": "compare the plan with files on disk; update state and views",
    "status": "progress by phase and owner",
    "owner": "who owns a path, and which rule decided it",
    "claim": "claim a planned file before writing it",
    "mark": "record a file as built, blocked or back to planned",
    "verify": "list or run verify commands; passing ones mark files verified",
    "guard": "exit 2 if a path is unplanned or not owned by --by (for hooks and wrappers)",
    "hook": "Claude Code hook entry point: reads the hook JSON on stdin",
    "db-check": "compare a live database schema with the migrations (read-only, on request)",
    "feedback": "record a problem with the skill for the maintainers' learning loop (stays on this machine)",
    "report": "print a GitHub issue body for a recorded incident (nothing is sent)",
    "doctor": "versions of this tool, this project's copy and installed skills; the learning store",
    "migrate": "upgrade blueprint.json and state.json to schema version 2",
    "task": "set a task's status: todo, doing, done or blocked",
    "accept": "record an independent evaluation of a feature",
    "evidence": "add, list, show, check, export and import evidence",
    "study": "study a repository, an app or data files and record what they show as evidence",
}


def _register_db_check(p):
    """db-check's 1.x arguments: bp/commands/data.py (W0-A1, unchanged) has no register()."""
    from bp.commands.data import cmd_db_check
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--sqlite", help="a SQLite database file, opened read-only")
    g.add_argument("--schema-dump", help="a schema-only SQL dump, e.g. from pg_dump --schema-only")
    p.add_argument("--ignore", nargs="*", help="live tables to ignore, e.g. framework tables")
    p.add_argument("--strict", action="store_true", help="exit 1 when the live schema and the migrations differ")
    p.set_defaults(fn=cmd_db_check)


LEGACY_ARGS = {"db-check": _register_db_check}


def _not_built(name):
    def fn(args):
        module = registry.COMMAND_TABLE[name].split(".")[-1]
        raise BlueprintError("not-built", f"{name} is not built in tool {TOOL_VERSION}; it arrives with "
                             f"{registry.ARRIVES.get(name, registry.ARRIVES.get(module, 'a later workstream'))} "
                             "(app-blueprint 2.0)",
                             next="update the skill, then run: python <skill>/scripts/blueprint.py init --refresh-tool")
    return fn


def _broken(exc):
    def fn(args):
        raise exc  # a command module that fails to import is a bug: an incident when that command runs
    return fn


def _guarding(args):
    """guard and hook pre fail closed: any 2.0 error there exits 2, which Claude Code treats as a refusal."""
    return getattr(args, "cmd", None) == "guard" or (args.cmd == "hook" and getattr(args, "when", None) == "pre")


def report_error(args, e):
    """Print a BlueprintError: the 7.1 failure envelope on stdout with --json, else `error:` and `  next:` on stderr."""
    if getattr(args, "json", False):
        print(json.dumps({"ok": False, "command": args.cmd, "error": {"code": e.error, "message": e.message,
                                                                      "entry": e.entry, "next": e.next}},
                         indent=2, ensure_ascii=False))
        return
    print(f"error: {e.message}", file=sys.stderr)
    if e.next:
        print(f"  next: {e.next}", file=sys.stderr)


def build_parser():
    ap = argparse.ArgumentParser(prog="blueprint.py", description="Plan an app's files before code, then track them.")
    ap.add_argument("--version", action="version", version=TOOL_VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, module in registry.COMMAND_TABLE.items():
        help_ = HELP.get(name, "")
        p = sub.add_parser(name, help=help_, description=help_)
        p.add_argument("--root", help="project root (default: found from the script or the current folder)")
        try:
            mod = registry.load(module)
            if mod is not None and callable(getattr(mod, "register", None)):
                mod.register(name, p)
            elif mod is not None and name in LEGACY_ARGS:
                LEGACY_ARGS[name](p)
            else:
                p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
                p.set_defaults(fn=_not_built(name), _accepts_any=True)
        except Exception as exc:  # noqa: BLE001 - one broken command module must not break the others
            p.set_defaults(fn=_broken(exc), _accepts_any=True)
    return ap


def main(argv=None):
    ap = build_parser()
    args, extra = ap.parse_known_args(argv)
    if extra and not getattr(args, "_accepts_any", False):
        ap.error("unrecognized arguments: " + " ".join(extra))  # exit 2, as parse_args
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass
    try:
        if os.environ.get("APP_BLUEPRINT_SELFTEST_CRASH") == "1":  # test hook for the incident path
            raise RuntimeError("selftest crash")
        try:
            args.fn(args)
        except ModuleNotFoundError as e:  # a handler's lazy import of a module not in this build (contract 1.4)
            if not (e.name or "").startswith("bp."):
                raise
            short = e.name[3:]
            raise BlueprintError("not-built", f"{args.cmd} needs {e.name}, which is not built in tool {TOOL_VERSION}; "
                                 f"it arrives with {registry.ARRIVES.get(short.split('.')[0], 'a later workstream')} "
                                 "(app-blueprint 2.0)",
                                 next="update the skill, then run: python <skill>/scripts/blueprint.py init --refresh-tool")
    except BlueprintError as e:
        report_error(args, e)
        sys.exit(2 if _guarding(args) else e.code)
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:  # an unexpected error is a bug in the tool: record it for the learning loop
        record_incident(args, exc)
        sys.exit(70)
