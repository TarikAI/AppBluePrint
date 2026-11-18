"""cli: moved from blueprint.py 1.3.0 (contract 1.5)."""
import argparse
import os
import sys

from bp.version import TOOL_VERSION
from bp.learn import FEEDBACK_KINDS, record_incident
from bp.commands.plan import cmd_contracts, cmd_export, cmd_init, cmd_render, cmd_validate
from bp.commands.progress import cmd_claim, cmd_guard, cmd_hook, cmd_mark, cmd_owner, cmd_status, cmd_sync, cmd_verify
from bp.commands.data import cmd_db_check
from bp.commands.learn import cmd_doctor, cmd_feedback, cmd_report


def main(argv=None):
    ap = argparse.ArgumentParser(prog="blueprint.py", description="Plan an app's files before code, then track them.")
    ap.add_argument("--version", action="version", version=TOOL_VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_, description=help_)
        p.add_argument("--root", help="project root (default: found from the script or the current folder)")
        p.set_defaults(fn=fn)
        return p

    p = add("init", cmd_init, "create .blueprint/ with an empty plan and a copy of this tool")
    p.add_argument("--name")
    p.add_argument("--preset", help="stack preset id, e.g. rust-agent-engine")
    p.add_argument("--refresh-tool", action="store_true", help="only re-copy the script and map template")

    p = add("validate", cmd_validate, "check the plan: fields, phases, dependencies, ownership overlaps")
    p.add_argument("--team", help="AgenticTeam team.json: check owner ids and write access")

    p = add("render", cmd_render, "write STRUCTURE.md, ROSTER.md and map.html")
    p.add_argument("--check", action="store_true", help="fail if the views are out of date")
    p.add_argument("--force", action="store_true", help="render even if validate has errors")

    p = add("sync", cmd_sync, "compare the plan with files on disk; update state and views")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--no-render", action="store_true")
    p.add_argument("--strict", action="store_true", help="exit 1 if there are unplanned files or missing modify targets (for CI)")

    p = add("status", cmd_status, "progress by phase and owner")
    p.add_argument("--owner")
    p.add_argument("--phase")
    p.add_argument("--next", action="store_true", help="list files ready to build now")
    p.add_argument("--json", action="store_true")

    p = add("owner", cmd_owner, "who owns a path, and which rule decided it")
    p.add_argument("path")

    p = add("claim", cmd_claim, "claim a planned file before writing it")
    p.add_argument("path")
    p.add_argument("--by", required=True)
    p.add_argument("--note")
    p.add_argument("--force", action="store_true")

    p = add("mark", cmd_mark, "record a file as built, blocked or back to planned")
    p.add_argument("path")
    p.add_argument("state", choices=["built", "blocked", "planned"])
    p.add_argument("--by", required=True)
    p.add_argument("--note")
    p.add_argument("--force", action="store_true")

    p = add("verify", cmd_verify, "list or run verify commands; passing ones mark files verified")
    p.add_argument("--phase")
    p.add_argument("--owner")
    p.add_argument("--run", action="store_true")
    p.add_argument("--timeout", type=int, default=900)

    p = add("guard", cmd_guard, "exit 2 if a path is unplanned or not owned by --by (for hooks and wrappers)")
    p.add_argument("path")
    p.add_argument("--by", help="owner id (default: $BLUEPRINT_OWNER)")

    p = add("hook", cmd_hook, "Claude Code hook entry point: reads the hook JSON on stdin")
    p.add_argument("when", choices=["pre", "post"])

    p = add("contracts", cmd_contracts, "list or write README contracts for module folders")
    p.add_argument("--write", action="store_true")
    p.add_argument("--module", action="append", metavar="ID", help="only this module (repeatable); default: every module with a path")

    p = add("export", cmd_export, "print codeowners, csv, json, or AgenticTeam add-task commands")
    p.add_argument("format", choices=["codeowners", "csv", "json", "agenticteam"])
    p.add_argument("--team", help="AgenticTeam team.json, to check write access")
    p.add_argument("--json", action="store_true", help="agenticteam: print task objects instead of commands")

    p = add("db-check", cmd_db_check, "compare a live database schema with the migrations (read-only, on request)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--sqlite", help="a SQLite database file, opened read-only")
    g.add_argument("--schema-dump", help="a schema-only SQL dump, e.g. from pg_dump --schema-only")
    p.add_argument("--ignore", nargs="*", help="live tables to ignore, e.g. framework tables")
    p.add_argument("--strict", action="store_true", help="exit 1 when the live schema and the migrations differ")

    p = add("feedback", cmd_feedback, "record a problem with the skill for the maintainers' learning loop (stays on this machine)")
    p.add_argument("--kind", required=True, choices=FEEDBACK_KINDS)
    p.add_argument("--note", required=True, help="what went wrong or what the user corrected, in one or two sentences")
    p.add_argument("--part", help="part of the skill, e.g. 'SKILL.md step 4', 'preset:nextjs-prisma', 'command:sync'")
    p.add_argument("--by", help="who is recording it (default: $BLUEPRINT_OWNER)")

    p = add("report", cmd_report, "print a GitHub issue body for a recorded incident (nothing is sent)")
    p.add_argument("incident", nargs="?", help="incident id (default: the latest)")

    p = add("doctor", cmd_doctor, "versions of this tool, this project's copy and installed skills; the learning store")
    p.add_argument("--strict", action="store_true", help="exit 1 if this project's tool copy is out of date")

    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass
    try:
        if os.environ.get("APP_BLUEPRINT_SELFTEST_CRASH") == "1":  # test hook for the incident path
            raise RuntimeError("selftest crash")
        args.fn(args)
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:  # an unexpected error is a bug in the tool: record it for the learning loop
        record_incident(args, exc)
        sys.exit(70)
