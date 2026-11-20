"""validate: findings from the validate hooks, validation codes, friction (contract 1.4, 3.3, 7.4).

validate_findings runs the validate hook of every DOMAIN module in order (rules, data, then the 2.0 modules), then
the 1.x team check. Each finding is {"level", "code", "entry", "message", "next"}; a rule that passes no code gets
the code its message classifies as. validate_plan keeps the 1.x return: (error messages, warning messages).
"""
import re

from bp.learn import project_ident, store_append
from bp.migrate import upgrade_plan
from bp import data as _data, registry, rules

# Codes the core owns; they come before every hook's (registry.codes()): schema-v1 (this module's own finding) and
# bad-id, the id-form error of every section (contract 2.2, 7.4), which many modules emit with code="bad-id" and
# none may register.
CODES = [(r"^blueprint\.json is schema version 1\b", "schema-v1"),
         (r"(?s)^[a-z ]+ id '.*' has the wrong form", "bad-id")]  # (?s): an invalid id may hold a newline
CODE_EXAMPLES = {"schema-v1": "blueprint.json is schema version 1; it still works.",
                 "bad-id": "feature id 'Inbox' has the wrong form; use F- then lowercase letters, digits and hyphens"}


def codes():
    """[(regex, code)] in classification order: this module's, then the hooks' (new modules, data, rules). A module
    that registers a code the core owns is a RuntimeError naming it; so is a core example another regex claims."""
    hooked = registry.codes()
    owners = {code: m.__name__ for m in registry.modules("DOMAIN") for _, code in getattr(m, "CODES", None) or []}
    for _, code in CODES:
        if code in owners:
            raise RuntimeError(f"validation code {code!r} is given by validate (the core) and {owners[code][3:]}; "
                               "the core owns it: emit it with code=, do not register it")
    full = CODES + hooked
    for code, example in CODE_EXAMPLES.items():
        got = next((c for rx, c in full if re.search(rx, example)), "other")
        if got != code:
            raise RuntimeError(f"validation code overlap: the core example {example!r} ({code}) classifies as {got}")
    core = {c for _, c in CODES}
    for m in registry.modules("DOMAIN"):  # a module example the core claims first (review of #15, M2)
        for code, example in (getattr(m, "CODE_EXAMPLES", None) or {}).items():
            got = next((c for rx, c in full if re.search(rx, example)), "other")
            if got != code and got in core:
                raise RuntimeError(f"validation code overlap: the example {example!r} of {m.__name__[3:]} ({code}) is "
                                   f"claimed by validate (the core, {got}) first; narrow the module's message or regex")
    return full


try:  # as loaded at start; classify_error reads codes() each time, so modules added later count
    VALIDATION_CODES = codes()
except Exception:  # noqa: BLE001 - a broken module is reported by cli's start-up check (named, an incident)
    VALIDATION_CODES = CODES + _data.CODES + rules.CODES


def classify_error(msg):
    for rx, code in codes():
        if re.search(rx, msg):
            return code
    return "other"


def validation_context(plan, team=None, evidence_ids=None, root=None):
    """vctx for the validate hooks: {"team", "evidenceIds", "root"} plus what every DOMAIN module's
    validate_context(root, plan) -> dict returns (the evidence module supplies evidenceIds). A key given by two modules,
    or a hook setting team or root, is a bug (RuntimeError); an explicit evidence_ids argument wins over a hook's."""
    vctx = {"team": team, "evidenceIds": evidence_ids, "root": root}
    owner = {}
    for name, part in registry.collect("validate_context", root, plan):
        if not isinstance(part, dict):
            raise RuntimeError(f"{name}.validate_context returned {type(part).__name__}, not a dict")
        for k, v in part.items():
            if k in ("team", "root") or k in owner:
                raise RuntimeError(f"{name}.validate_context sets {k!r}, which {owner.get(k, 'validate')} already sets")
            owner[k] = name
            if k == "evidenceIds" and evidence_ids is not None:
                continue
            vctx[k] = v
    return vctx


def validate_findings(plan, team=None, evidence_ids=None, root=None):
    """Every finding for the plan, in order. A dict that did not come through load_plan is accepted too: a version 1
    dict is upgraded as a copy first (and warned about, like a loaded version 1 plan); any version other than the int
    1 or 2 is the error `version must be 1 or 2` (contract 3.3)."""
    findings = []

    def adder(level):
        def add(msg, code=None, entry=None, next=None):  # noqa: A002 (next is the contract's name)
            findings.append({"level": level, "code": code or classify_error(msg), "entry": entry, "message": msg,
                             "next": next})
        return add

    E, W = adder("error"), adder("warning")
    from_v1 = isinstance(plan, dict) and plan.get("_from") == 1
    if isinstance(plan, dict) and type(plan.get("version")) is int and plan["version"] == 1:
        plan, from_v1 = upgrade_plan(plan), True
    if from_v1:
        migrate_built = registry.command_built("migrate")  # the hint only once the command exists (1.5 row d)
        W("blueprint.json is schema version 1; it still works."
          + (" To use milestones, features, tasks and unknowns, run: blueprint.py migrate" if migrate_built else ""),
          code="schema-v1", entry="blueprint.json", next="blueprint.py migrate" if migrate_built else None)
    vctx = validation_context(plan, team, evidence_ids, root)
    registry.collect("validate", plan, E, W, vctx)
    rules.validate_team(plan, team, E)
    return findings


def validate_plan(plan, team=None, evidence_ids=None, root=None):
    """(errors, warnings) as message lists, the 1.x return of validate_plan."""
    found = validate_findings(plan, team, evidence_ids, root)
    return ([f["message"] for f in found if f["level"] == "error"],
            [f["message"] for f in found if f["level"] == "warning"])


def record_friction(root, findings_or_errors):
    """One friction record (SPEC.md) counting the codes of the errors: Finding dicts carry theirs, messages are
    classified."""
    codes_seen = {}
    for e in findings_or_errors:
        c = (e.get("code") or "other") if isinstance(e, dict) else classify_error(e)
        codes_seen[c] = codes_seen.get(c, 0) + 1
    pid, name = project_ident(root)
    store_append("friction.jsonl", {"codes": codes_seen, "projectId": pid, "project": name})
