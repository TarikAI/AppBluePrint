"""migrate: schema version 1 -> 2 of blueprint.json and state.json, pure and in memory (contract 3.1, 3.2).

load_plan runs upgrade_plan on every plan it reads, so all code sees version 2; the migrate command (W0-B) writes the
result. Nothing here reads or writes files. Imports core only (contract 1.2).
"""
import copy

from bp.core import BlueprintError

NEW_SECTIONS = ("milestones", "features", "tasks", "unknowns")  # plan.SECTIONS_V2; repeated here so migrate stays pure


def plan_version(raw):
    """The top-level version when it is an int (bool excluded), else 0 (missing, not a whole number, not an object)."""
    v = raw.get("version") if isinstance(raw, dict) else None
    return v if type(v) is int else 0


def upgrade_plan(raw):
    """A new dict at schema version 2. Version 2: a deep copy. Version 1: every key in its order, version set to 2, and
    the four new sections appended as [] when missing; nothing else changes. Any other version: schema-too-new.
    load_plan reports a missing or non-integer version before calling this (3.3)."""
    if not isinstance(raw, dict):
        raise BlueprintError("bad-shape", "blueprint.json must be an object", next="fix it, then run validate")
    v = raw.get("version")
    if type(v) is not int:
        what = "is required" if v is None else "must be a whole number"
        raise BlueprintError("bad-shape", f"blueprint.json: version {what}", next="fix it, then run validate")
    if v == 2:
        return copy.deepcopy(raw)
    if v < 1:
        raise BlueprintError("bad-shape", "blueprint.json: version must be 1 or 2", next="fix it, then run validate")
    if v != 1:
        raise BlueprintError("schema-too-new", f"blueprint.json is schema version {v}; this tool reads 1 and 2",
                             next="update the skill, then run init --refresh-tool")
    out = {k: copy.deepcopy(val) for k, val in raw.items()}
    out["version"] = 2
    for key in NEW_SECTIONS:
        if key not in out:
            out[key] = []
    return out


def upgrade_state(raw):
    """state.json at stateVersion 2: a state without stateVersion is version 1 and gains "stateVersion": 2 (first) and
    "tasks": {} (last); every other key and value is kept, including 1.x file proofs (still honoured, 4.5). A state
    that already has a stateVersion is returned as a copy."""
    if not isinstance(raw, dict):
        return raw
    if "stateVersion" in raw:
        return copy.deepcopy(raw)
    out = {"stateVersion": 2}
    out.update(copy.deepcopy(raw))
    out.setdefault("tasks", {})
    return out
