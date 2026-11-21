"""commands.roadmap: `task <id> todo|doing|done|blocked --by ID [--note TEXT] [--force] [--json]` (contract 7.2).

The rules (who may set what, the owner-action rule, a note for blocked) are bp.roadmap.set_task; this module parses
the arguments, loads the plan and prints the result.
"""
import sys

from bp.core import emit, find_root
from bp.plan import load_plan


def register(name, p):
    """Arguments of task (cli.build_parser calls this)."""
    p.add_argument("task", help="a task id from tasks[], for example T-domain")
    p.add_argument("status", choices=["todo", "doing", "done", "blocked"])
    p.add_argument("--by", required=True, metavar="ID", help="your roster id")
    p.add_argument("--note", help="why (required for blocked)")
    p.add_argument("--force", action="store_true", help="set a task you do not own (the lead's call)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_task)


def cmd_task(args):
    from bp import roadmap  # inside the handler, as every command module does (contract 1.4)
    root = find_root(args.root)
    plan = load_plan(root)
    roadmap.set_task(root, plan, args.task, args.status, args.by, getattr(args, "note", None),
                     bool(getattr(args, "force", False)))
    data = {"task": args.task, "status": args.status, "by": args.by}
    unmet = _unmet(root, plan, args.task) if args.status == "done" else []
    if unmet:  # allowed, but said once (no gating in 2.0)
        text = (f"{args.task} is done, but it depends on {', '.join(unmet)}, which "
                f"{'is' if len(unmet) == 1 else 'are'} not done or accepted yet")
        print(f"warning: {text}", file=sys.stderr)
        data["warnings"] = [text]
    emit(args, "task", data, [f"{args.task}: {args.status} (by {args.by})"])


def _unmet(root, plan, tid):
    """Dependencies of the task that are not done (tasks) or accepted (features), from the current context."""
    from bp import roadmap
    from bp.plan import load_state
    from bp.views.context import build_context
    try:
        ctx = build_context(root, plan, load_state(root))
    except Exception:  # noqa: BLE001 - the task is already recorded; a context problem must not undo or fail it
        ctx = {"tasks": roadmap.task_rows(plan, load_state(root)), "features": []}
    return roadmap.unmet_dependencies(plan, tid, ctx.get("tasks") or [], ctx.get("features") or [])
