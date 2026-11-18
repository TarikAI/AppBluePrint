"""commands.data: moved from blueprint.py 1.3.0 (contract 1.5)."""
import sqlite3
import sys
from pathlib import Path

from bp.core import append_log, die, find_root, now_iso
from bp.plan import load_plan, load_state, save_state
from bp.validate import validate_plan
from bp.views.render import render_all


def cmd_db_check(args):
    """Compare a live schema with the migrations. Read-only; never stores credentials."""
    from bp.data import parse_sql_tables, resource_names, scan_data
    from bp.status import repo_files
    root = find_root(args.root)
    plan = load_plan(root)
    state = load_state(root)
    if args.sqlite:
        src = Path(args.sqlite)
        if not src.is_file():
            die(f"no such SQLite file: {args.sqlite}")
        con = sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT type, name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'").fetchall()
        finally:
            con.close()
        live = {n.lower(): t for t, n in rows}
        label = "sqlite:" + src.name
    else:
        src = Path(args.schema_dump)
        if not src.is_file():
            die(f"no such schema dump: {args.schema_dump}")
        live = {k: v["kind"] for k, v in parse_sql_tables(src.read_text(encoding="utf-8", errors="replace"), src.name).items()}
        label = "dump:" + src.name
    data = state.get("data") or {}
    if not data.get("tables") and plan["stores"]:
        data = scan_data(root, plan, state, repo_files(root, plan))
    migrated = {t["name"] for t in data.get("tables", [])}
    declared = {n.lower() for r in plan["resources"] if r.get("kind") in ("table", "view") for n in resource_names(r)}
    ignore = {x.lower() for x in args.ignore or []}
    live_only = sorted(t for t in live if t not in migrated and t not in ignore)
    not_applied = sorted(t for t in migrated if t not in live)
    declared_missing = sorted(t for t in declared if t not in live and t in migrated)
    data["live"] = {"source": label, "at": now_iso(), "tables": sorted(live), "liveOnly": live_only,
                    "notApplied": not_applied, "declaredMissing": declared_missing}
    state["data"] = data
    save_state(root, state)
    append_log(root, {"event": "db-check", "source": label, "liveOnly": len(live_only), "notApplied": len(not_applied)})
    print(f"Live schema from {label}: {len(live)} tables and views; migrations create {len(migrated)}.")
    for title, items in (("In the live database but in no migration", live_only), ("In migrations but not in the live database", not_applied)):
        if items:
            print(f"  {title}: {', '.join(items)}")
    if not live_only and not not_applied:
        print("  The live schema matches the migrations.")
    errors, _ = validate_plan(plan)
    if not errors:
        render_all(root, plan, state, quiet=True)
    if args.strict and (live_only or not_applied):
        sys.exit(1)
