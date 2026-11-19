"""views.context: build_context(), everything the views and status read (contract 1.4).

ctx starts with root, plan, state and comp (status.compute) and an empty value for every 2.0 key, then each DOMAIN
module's context hook adds its keys in DOMAIN order (unknowns before records before features before roadmap), so a
hook reads what the earlier ones added and a key whose module is not built yet reads as empty:

    unknowns  []                    blocked  {"phases": set(), "milestones": set(), "features": set(), "by": {}}
                                    (by: {"phase:P2" | "milestone:M1" | "feature:F-x": [unknown ids]})
    checks    {fid: {...}} (1.4)    features []    milestones []    tasks []    studies []
"""
from bp import registry
from bp.status import compute

JSON_SECTIONS = ("features", "milestones", "tasks", "unknowns")  # what status --json and export json gain (7.1)


def build_context(root, plan, state):
    ctx = {"root": root, "plan": plan, "state": state, "comp": compute(root, plan, state),
           "unknowns": [], "blocked": {"phases": set(), "milestones": set(), "features": set(), "by": {}},
           "checks": {}, "features": [], "milestones": [], "tasks": [], "studies": []}
    registry.collect("context", root, plan, state, ctx)
    return ctx


def merged(hook, ctx, base, defaults=None):
    """Merge the dicts that a hook (status_json, map_payload) returns into base: a key may be given by one module only
    and never replaces a key of base; defaults (empty values the hooks may fill) are added first. A collision is a bug
    in a hook (RuntimeError, so an incident)."""
    out = dict(base)
    owner = {}
    for k, v in (defaults or {}).items():
        out.setdefault(k, v)
    for name, part in registry.collect(hook, ctx):
        if not isinstance(part, dict):
            raise RuntimeError(f"{name}.{hook} returned {type(part).__name__}, not a dict")
        for k, v in part.items():
            if k in base or k in owner:
                raise RuntimeError(f"{name}.{hook} sets {k!r}, which {owner.get(k, 'the 1.x output')} already sets")
            owner[k] = name
            out[k] = v
    return out


def status_json(ctx, base):
    """The 1.x status --json / export json object plus features, milestones, tasks and unknowns (empty until their
    modules land) and whatever the status_json hooks add (contract 7.1, 8.5)."""
    return merged("status_json", ctx, base, {k: [] for k in JSON_SECTIONS})


def blocked_by(ctx, kind, target):
    """The unknown ids blocking a phase, milestone or feature: ctx["blocked"]["by"] is keyed "phase:P2", "milestone:M1"
    or "feature:F-x" (one format, decided for wave 1)."""
    by = (ctx.get("blocked") or {}).get("by") or {}
    return list(by.get(f"{kind}:{target}") or [])
