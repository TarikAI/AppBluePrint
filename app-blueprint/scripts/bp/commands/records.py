"""commands.records: `accept <feature-id> --by ID --mode MODE [--result C1=pass]... [--evidence ev-...]... [--note TEXT]
[--json]` (contract 7.2).

The rules (who may accept, what only a person may say, what must be verified first) are bp.records.record_evaluation,
which also repeats the ownership checks inside the state lock; this module parses the arguments, builds the context,
prints the result and refreshes the views.
"""
import sys

from bp.core import BlueprintError, emit, find_root
from bp.plan import load_plan

MODES = ("browser", "device", "cli", "api", "manual", "code-only")


def register(name, p):
    """Arguments of accept (cli.build_parser calls this)."""
    p.add_argument("feature", help="a feature id from features[], for example F-inbox")
    p.add_argument("--by", required=True, metavar="ID", help="your roster id (not the feature's owner, and owning none of its files)")
    p.add_argument("--mode", required=True, choices=list(MODES),
                   help="how you checked: browser, device or cli (e2e), api (integration), code-only (unit), manual (a person)")
    p.add_argument("--result", action="append", default=[], metavar="C1=pass",
                   help="what you saw for a criterion you actually checked: pass, fail, skipped or unknown (repeat the flag)")
    p.add_argument("--evidence", action="append", default=[], metavar="ev-ID", help="an evidence id for what you observed (repeat the flag)")
    p.add_argument("--note", help="free text, kept redacted")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_accept)


def _refresh_views(args, root, plan):
    """The views, after the record is saved. A failure prints one line on stderr and never changes the exit code."""
    from bp.status import load_state
    from bp.views.render import render_all
    try:
        render_all(root, plan, load_state(root), quiet=True)
    except BlueprintError as e:
        print(f"views not refreshed: {e.message}" + (f"; {e.next}" if e.next else ""), file=sys.stderr)
    except Exception:  # noqa: BLE001 - the evaluation is saved; say so and keep its exit code
        print("views not refreshed: an unexpected error; run render", file=sys.stderr)


def _status_after(root, fid):
    """(status, statusText, plan) of the feature after the record is saved, or (None, None, None) when that cannot be
    read: the record is already written, so a problem here never undoes or fails it."""
    from bp.status import load_state
    from bp.views.context import build_context
    try:
        plan = load_plan(root)
        ctx = build_context(root, plan, load_state(root))
        f = next((x for x in ctx.get("features") or [] if isinstance(x, dict) and x.get("id") == fid), None)
        return (f.get("status"), f.get("statusText"), plan) if f else (None, None, plan)
    except BaseException as e:  # noqa: BLE001 - SystemExit from a load error included
        if isinstance(e, KeyboardInterrupt):
            raise
        return None, None, None


def _awaiting(rec, plan, fid):
    """The criteria the evaluation leaves unsaid or not passed, as `C3 awaits evaluation` texts (at most five)."""
    from bp import records
    from bp.plan import section_index
    feature = section_index(plan, "features").get(fid) or {}
    named = rec.get("criteria") or {}
    out = []
    for c, cid, ok in records._criteria(feature):
        if not ok:
            out.append("a criterion without an id of its own cannot be accepted")
        elif not records._has_verify(c) and (named.get(cid) or {}).get("result") != "pass":
            out.append(f"{cid} awaits evaluation")
    return out[:5] + ([f"and {len(out) - 5} more"] if len(out) > 5 else [])


def cmd_accept(args):
    from bp import records
    from bp.status import load_state
    from bp.views.context import build_context
    root = find_root(args.root)
    plan = load_plan(root)
    results = records.parse_results(args.result)
    evidence = records.parse_evidence(args.evidence)
    ctx = build_context(root, plan, load_state(root))
    rec = records.record_evaluation(root, plan, args.feature, args.by, args.mode, results, evidence,
                                    getattr(args, "note", None), ctx=ctx)
    fid = rec["feature"]
    after, text, fresh = _status_after(root, fid)
    _refresh_views(args, root, fresh or plan)
    fails = sorted(cid for cid, r in rec["criteria"].items() if r["result"] == "fail")
    accepted = after == "accepted" and rec["verdict"] == "pass"
    if accepted:
        line = f"{fid}: accepted by {rec['by']} ({rec['mode']}, {rec['level']})"
    elif after == "failed" or rec["verdict"] == "fail":
        line = f"{fid}: failed ({', '.join(c + ' fail' for c in fails) or 'see the record'})"
    else:
        waits = _awaiting(rec, fresh or plan, fid)
        line = f"{fid}: not accepted (" + (", ".join(waits) or (text or after or "status unknown")) + ")"
    data = {"record": rec, "status": after}
    emit(args, "accept", data, [line])
    sys.exit(0 if accepted else 1)
