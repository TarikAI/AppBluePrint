"""validate: validate_plan(), VALIDATION_CODES, classify_error(), record_friction(), moved from blueprint.py 1.3.0."""
import re

from bp.learn import project_ident, store_append
from bp import rules
from bp.data import validate_data


def validate_plan(plan, team=None):
    errors, warnings = [], []
    E, W = errors.append, warnings.append
    rules.validate(plan, E, W, {"team": team})
    validate_data(plan, E, W)
    rules.validate_team(plan, team, E)
    return errors, warnings


VALIDATION_CODES = [
    (r"^store ", "store-invalid"),
    (r"^resource ", "resource-invalid"),
    (r"uses unknown resource|access for ", "uses-invalid"),
    (r"^version must be", "version"),
    (r"^app\.name is empty", "app-name"),
    (r" without \w+:", "missing-id"),
    (r"^duplicate ", "duplicate-id"),
    (r"^no phases", "no-phases"),
    (r"^stack entry .*missing", "stack-incomplete"),
    (r"depends on unknown module", "module-dependency"),
    (r"^module .*empty purpose", "module-purpose"),
    (r"dependency cycle", "module-cycle"),
    (r"kind must be agent or human", "roster-kind"),
    (r"use a repo-relative path", "file-path"),
    (r"action must be", "file-action"),
    (r"unknown module", "unknown-module"),
    (r"unknown phase", "unknown-phase"),
    (r"empty purpose", "missing-purpose"),
    (r"no verify command", "missing-verify"),
    (r"which is not planned", "unplanned-dependency"),
    (r"lands in a later phase", "later-phase-dependency"),
    (r"^pattern ", "pattern-incomplete"),
    (r"needs pattern and owner", "owner-rule-incomplete"),
    (r"is not in the roster", "owner-not-in-roster"),
    (r"no owner rule matches", "unowned"),
    (r"partly overlap", "owner-overlap"),
    (r"not an agent in team\.json", "team-unknown-agent"),
    (r"not workspace-write", "team-no-write"),
]


def classify_error(msg):
    for rx, code in VALIDATION_CODES:
        if re.search(rx, msg):
            return code
    return "other"


def record_friction(root, errors):
    codes = {}
    for e in errors:
        c = classify_error(e)
        codes[c] = codes.get(c, 0) + 1
    pid, name = project_ident(root)
    store_append("friction.jsonl", {"codes": codes, "projectId": pid, "project": name})
