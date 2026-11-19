"""unknowns: open questions with experiments, and the work they block (contract 2.6, 4.3, 7.4, 8.5).

An unknown is a question the plan cannot answer yet. Its `severity` says how much depends on the answer; its
`experiment` (a plan, a timebox, an exit criterion) says how to find out. While an unknown is `open` or
`investigating` and its severity is `high` or `critical`, it blocks every phase, milestone or feature id in `blocks`:
`status --next` skips a blocked phase, `claim` on its files is refused (exit 3, `blocked-by-unknown`), and a blocked
milestone blocks its features. An unknown that is resolved or dropped, or whose severity is `low` or `medium`, blocks
nothing.

Fail closed: the only things that unblock work are the exact words `resolved` and `dropped` (status) and `low` and
`medium` (severity). A missing, misspelled or wrongly typed status or severity still blocks, and `validate` says what
to fix. Hooks defined here: CODES, CODE_EXAMPLES, validate, context, refuse, status_lines, status_json, map_payload.
Every hook reads the plan with `isinstance` checks, so a malformed value never raises.
"""
import posixpath
import re

from bp.core import norm, path_key, project_path
from bp.globs import pattern_instance_rx
from bp.rules import uniq

ID_FORM = re.compile(r"U-[a-z0-9][a-z0-9-]{0,47}")
SEVERITIES = ("low", "medium", "high", "critical")
STATUSES = ("open", "investigating", "resolved", "dropped")
NOT_BLOCKING_SEVERITIES = ("low", "medium")  # below the blocking threshold (2.6)
NOT_BLOCKING_STATUSES = ("resolved", "dropped")
KINDS = ("phase", "milestone", "feature")
TIMEBOX = re.compile(r"[0-9]{1,3}[hdw]")
NEXT_UNKNOWNS = "blueprint.py status --unknowns (or claim again with --force if the lead says so)"

CODES = [
    (r"^unknown \S+: needs question, severity and status", "unknown-incomplete"),
    (r"^unknown \S+: severity must be", "unknown-severity"),
    (r"^unknown \S+: status must be", "unknown-status"),
    (r"^unknown \S+: blocks ", "unknown-blocks"),
    (r"^unknown \S+: (investigating needs experiment|experiment\.)", "unknown-experiment"),
    (r"^unknown \S+: resolved needs decision", "unknown-resolved-no-decision"),
    (r"^unknown \S+: dropped needs notes", "unknown-dropped-no-note"),
    (r"^unknown \S+: evidence ", "unknown-evidence"),
    (r"^unknown \S+ \(\w+\) is open and blocks", "unknown-open-blocker"),
    (r"^decision \S+: unknown ", "decision-unknown"),
]

CODE_EXAMPLES = {
    "unknown-incomplete": "unknown U-a: needs question, severity and status",
    "unknown-severity": "unknown U-a: severity must be low, medium, high or critical (got 'huge')",
    "unknown-status": "unknown U-a: status must be open, investigating, resolved or dropped (got 'done')",
    "unknown-blocks": "unknown U-a: blocks 'P9', which is no phase, milestone or feature",
    "unknown-experiment": "unknown U-a: investigating needs experiment.plan, experiment.timebox (e.g. 2d) and "
                          "experiment.exit",
    "unknown-resolved-no-decision": "unknown U-a: resolved needs decision, the id of the decisions[] entry that "
                                    "settles it",
    "unknown-dropped-no-note": "unknown U-a: dropped needs notes saying why it no longer matters",
    "unknown-evidence": "unknown U-a: evidence ev-0123456789abcdef is not in .blueprint/studies",
    "unknown-open-blocker": "unknown U-a (high) is open and blocks P2; claim and status --next skip them until it "
                            "is resolved",
    "decision-unknown": "decision D-a: unknown 'U-z' does not exist; use an id from unknowns[]",
}


# --- reading the plan without ever raising -------------------------------------------------------------------------

def _dicts(plan, key):
    sec = plan.get(key) if isinstance(plan, dict) else None
    return [x for x in sec if isinstance(x, dict)] if isinstance(sec, list) else []


def _text(v):
    return v.strip() if isinstance(v, str) else ""


def _one(v):
    """Plan text for people: one line, runs of whitespace (newlines included) collapsed to a space."""
    return " ".join(str(v).split())


def _show(v):
    """A short, one-line rendering of any value for a message."""
    s = repr(v) if not isinstance(v, str) else "'" + _one(v) + "'"
    return s if len(s) <= 40 else s[:37] + "...'"


def _ids(plan, key):
    return {x["id"] for x in _dicts(plan, key) if isinstance(x.get("id"), str) and x["id"]}


def _unique_ids(plan, key):
    """Ids that exactly one entry has: a duplicated roster or decision id names no one in particular, so it counts
    for nothing (fail closed)."""
    seen, dup = set(), set()
    for x in _dicts(plan, key):
        i = x.get("id")
        if isinstance(i, str) and i:
            (dup if i in seen else seen).add(i)
    return seen - dup


def _plan_ids(plan):
    return {"phase": _ids(plan, "phases"), "milestone": _ids(plan, "milestones"), "feature": _ids(plan, "features")}


def _uid(u, i):
    """The id a message or a `by` list uses for the unknown at index i (a placeholder when it has no usable id)."""
    return u["id"] if isinstance(u.get("id"), str) and u["id"] else f"unknowns[{i}]"


def _blocks_of(u):
    """The strings of `blocks` (a lone string counts as one entry, so a plan edited by hand does not unblock)."""
    b = u.get("blocks")
    if isinstance(b, str):
        b = [b]
    return [x.strip() for x in b if isinstance(x, str) and x.strip()] if isinstance(b, list) else []


def _resolve(entry, ids):
    """The (kind, id) pairs that a `blocks` entry names: `phase:P2` and friends name one kind; a bare id names every
    kind that has it (more than one is ambiguous, and validate refuses it, while blocking covers all of them)."""
    kind, _, rest = entry.partition(":")
    if rest and kind in KINDS:
        return [(kind, rest)] if rest in ids[kind] else []
    return [(k, entry) for k in KINDS if entry in ids[k]]


def is_blocking(u):
    """True when the unknown blocks the ids in `blocks`: not resolved or dropped, and not low or medium. Anything
    else, including a missing or wrongly typed status or severity, blocks (fail closed)."""
    status, severity = u.get("status"), u.get("severity")
    if isinstance(status, str) and status in NOT_BLOCKING_STATUSES:
        return False
    if isinstance(severity, str) and severity in NOT_BLOCKING_SEVERITIES:
        return False
    return True


# --- blocking -------------------------------------------------------------------------------------------------------

def _scan(plan):
    """(blocked, info, hits): the blocking() result, {unknown id: (severity, status)} of those that block, and the
    list indexes of the unknowns that block at least one id of this plan."""
    out = {"phases": set(), "milestones": set(), "features": set(), "by": {}}
    info, hits = {}, set()
    ids = _plan_ids(plan)
    plural = {"phase": "phases", "milestone": "milestones", "feature": "features"}

    def add(key, uid):
        lst = out["by"].setdefault(key, [])
        if uid not in lst:
            lst.append(uid)

    items = plan.get("unknowns") if isinstance(plan, dict) else None
    for i, u in enumerate(items if isinstance(items, list) else []):
        if not isinstance(u, dict) or not is_blocking(u):
            continue
        uid = _uid(u, i)
        for entry in _blocks_of(u):
            for kind, tid in _resolve(entry, ids):
                out[plural[kind]].add(tid)
                add(f"{kind}:{tid}", uid)
                info[uid] = (u.get("severity"), u.get("status"))
                hits.add(i)
    for f in _dicts(plan, "features"):  # a blocked milestone blocks its features (4.3 rule 2 reads them from here)
        fid, mid = f.get("id"), f.get("milestone")
        if isinstance(fid, str) and fid and isinstance(mid, str) and mid in out["milestones"]:
            out["features"].add(fid)
            for uid in out["by"].get(f"milestone:{mid}", []):
                add(f"feature:{fid}", uid)
    return out, info, hits


def blocking(plan):
    """{"phases": set, "milestones": set, "features": set, "by": {"phase:P2": [uid], "milestone:M1": [uid],
    "feature:F-x": [uid]}} for the plan (2.6). Features in a blocked milestone are in "features", with the milestone's
    unknown ids under their own key."""
    return _scan(plan)[0]


# --- validation -----------------------------------------------------------------------------------------------------

def _bad_word(v, allowed):
    """True for a value that is present but not one of the allowed words (wrong type included)."""
    return v is not None and v != "" and not (isinstance(v, str) and v in allowed)


def _experiment_problems(ex, roster):
    """What is wrong with an experiment value, as a list of short phrases (empty when it is complete)."""
    if not isinstance(ex, dict):
        return ["experiment must be an object with plan, timebox and exit"]
    out = []
    if not _text(ex.get("plan")):
        out.append("experiment.plan is missing")
    tb = ex.get("timebox")
    if not isinstance(tb, str) or not TIMEBOX.fullmatch(tb):
        out.append(f"experiment.timebox {_show(tb)} is not like 2d (a number up to 999 and h, d or w)")
    if not _text(ex.get("exit")):
        out.append("experiment.exit is missing")
    owner = ex.get("owner")
    if owner is not None and (not isinstance(owner, str) or owner not in roster):
        out.append(f"experiment.owner {_show(owner)} is not a roster entry")
    return out


def validate(plan, E, W, vctx):
    if not isinstance(plan, dict):
        return
    raw = plan.get("unknowns")
    items = [u for u in raw if isinstance(u, dict)] if isinstance(raw, list) else []
    # the 1.2 id check (missing-id, duplicate-id); an id that is not text counts as missing, and cannot be hashed
    uniq([u if "id" not in u or isinstance(u["id"], str) else dict(u, id="") for u in items], "id", "unknown", E)
    ids = _plan_ids(plan)
    roster = _unique_ids(plan, "roster")
    decisions = _unique_ids(plan, "decisions")
    known = _ids(plan, "unknowns")
    evidence = vctx.get("evidenceIds") if isinstance(vctx, dict) else None
    try:
        evidence = None if evidence is None else set(evidence)
    except TypeError:
        evidence = None
    for i, u in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(u, dict):
            continue
        uid = _one(_uid(u, i))
        entry = f"unknown {uid}"
        status, severity = u.get("status"), u.get("severity")
        if isinstance(u.get("id"), str) and u["id"] and not ID_FORM.fullmatch(u["id"]):
            # bad-id is a cross-section code that the core registers once; it is deliberately not in CODES here
            E(f"unknown id '{_one(u['id'])}' has the wrong form; use U- and lowercase letters, digits and hyphens "
              "(for example U-offline)", code="bad-id", entry=entry, next="rename it in unknowns[] of blueprint.json")
        if not _text(u.get("question")) or not _text(severity) or not _text(status):
            E(f"unknown {uid}: needs question, severity and status", code="unknown-incomplete", entry=entry,
              next="fill question, severity (low, medium, high or critical) and status in blueprint.json")
        if _bad_word(severity, SEVERITIES):
            E(f"unknown {uid}: severity must be low, medium, high or critical (got {_show(severity)})",
              code="unknown-severity", entry=entry, next="set severity to one of those four words in blueprint.json")
        if _bad_word(status, STATUSES):
            E(f"unknown {uid}: status must be open, investigating, resolved or dropped (got {_show(status)})",
              code="unknown-status", entry=entry, next="set status to one of those four words in blueprint.json")
        blocks = u.get("blocks")
        if blocks is None:
            entries = []
        else:
            entries = blocks if isinstance(blocks, list) else [blocks]
        for b in entries:
            hits = _resolve(b.strip(), ids) if isinstance(b, str) else []
            if len(hits) == 1:
                continue
            if len(hits) > 1:
                a, c = hits[0][0], hits[1][0]
                E(f"unknown {uid}: blocks {_show(b)}, which names both a {a} and a {c}; write {a}:{b} or {c}:{b}",
                  code="unknown-blocks", entry=entry, next=f"write {a}:{b} or {c}:{b} in blocks")
            else:
                E(f"unknown {uid}: blocks {_show(b)}, which is no phase, milestone or feature",
                  code="unknown-blocks", entry=entry,
                  next="name an id from phases[], milestones[] or features[] (phase:X or milestone:X when both exist)")
        if evidence is not None:
            for key in ("evidenceFor", "evidenceAgainst"):
                for ev in u.get(key) if isinstance(u.get(key), list) else []:
                    if isinstance(ev, str) and ev not in evidence:
                        W(f"unknown {uid}: evidence {ev} is not in .blueprint/studies", code="unknown-evidence",
                          entry=entry, next="remove it, or import the study that holds it (evidence import)")
        ex = u.get("experiment")
        if ex is None:
            problems = (["experiment.plan, experiment.timebox and experiment.exit are missing"]
                        if status == "investigating" else [])
        else:
            problems = _experiment_problems(ex, roster)
        if problems and status == "investigating":
            E(f"unknown {uid}: investigating needs experiment.plan, experiment.timebox (e.g. 2d) and "
              f"experiment.exit ({'; '.join(problems)})", code="unknown-experiment", entry=entry,
              next='add "experiment": {"plan": "...", "timebox": "2d", "exit": "..."} to the unknown')
        elif problems:
            E(f"unknown {uid}: " + "; ".join(problems), code="unknown-experiment", entry=entry,
              next='complete "experiment": {"plan": "...", "timebox": "2d", "exit": "..."}')
        if status == "resolved":
            d = u.get("decision")
            if not _text(d):
                E(f"unknown {uid}: resolved needs decision, the id of the decisions[] entry that settles it",
                  code="unknown-resolved-no-decision", entry=entry,
                  next="record the outcome in decisions[] and put its id in decision")
            elif not isinstance(d, str) or d not in decisions:
                E(f"unknown {uid}: resolved needs decision {_show(d)} to be the id of a decisions[] entry; it is not",
                  code="unknown-resolved-no-decision", entry=entry,
                  next="add that entry to decisions[], or fix the id in decision")
        if status == "dropped" and not _text(u.get("notes")):
            E(f"unknown {uid}: dropped needs notes saying why it no longer matters", code="unknown-dropped-no-note",
              entry=entry, next="say why in notes")
        if status in ("open", "investigating") and severity in ("high", "critical"):
            targets = [b for b in _blocks_of(u) if _resolve(b, ids)]
            if targets:
                W(f"unknown {uid} ({severity}) is open and blocks {', '.join(targets)}; claim and status --next skip "
                  "them until it is resolved", code="unknown-open-blocker", entry=entry,
                  next="python .blueprint/bin/blueprint.py status --unknowns")
    for d in _dicts(plan, "decisions"):
        ref = d.get("unknown")
        if isinstance(ref, str) and ref and ref not in known:
            did = d["id"] if isinstance(d.get("id"), str) and d["id"] else "?"
            E(f"decision {did}: unknown {_show(ref)} does not exist; use an id from unknowns[] or remove the field",
              code="decision-unknown", entry=f"decision {did}",
              next=("python .blueprint/bin/blueprint.py migrate" if plan.get("_from") == 1
                    else "fix decisions[].unknown in blueprint.json"))


# --- context, views and status --------------------------------------------------------------------------------------

def _strings(v):
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _payload(u, i, blocking_now):
    """One entry of ctx["unknowns"] and of the map and status --json payloads (8.5)."""
    ex = u.get("experiment")
    ex = {k: ex[k] for k in ("plan", "timebox", "exit", "owner") if isinstance(ex.get(k), str)} if isinstance(ex, dict) else None
    return {"id": u["id"] if isinstance(u.get("id"), str) else "",
            "question": u["question"] if isinstance(u.get("question"), str) else "",
            "severity": u["severity"] if isinstance(u.get("severity"), str) else "",
            "status": u["status"] if isinstance(u.get("status"), str) else "",
            "blocks": _blocks_of(u), "evidenceFor": _strings(u.get("evidenceFor")),
            "evidenceAgainst": _strings(u.get("evidenceAgainst")), "experiment": ex,
            "decision": u["decision"] if isinstance(u.get("decision"), str) and u["decision"] else None,
            "blocking": blocking_now}


def context(root, plan, state, ctx):
    """ctx["unknowns"] (8.5) and ctx["blocked"] (1.4): the ids blocked by open, severe unknowns."""
    ctx["blocked"], _, hits = _scan(plan)
    items = plan.get("unknowns") if isinstance(plan, dict) else None
    ctx["unknowns"] = [_payload(u, i, i in hits)
                       for i, u in enumerate(items if isinstance(items, list) else []) if isinstance(u, dict)]


def map_payload(ctx):
    return {"unknowns": ctx.get("unknowns") or []}


def status_json(ctx):
    return {"unknowns": ctx.get("unknowns") or []}


def _phrase(uid, sev, status):
    return (f"{_one(uid)} ({_one(sev) if isinstance(sev, str) and sev else 'no severity'}, "
            f"{_one(status) if isinstance(status, str) and status else 'no status'})")


def _spellings(root, path):
    """Every way the claimed path can be written that still names one file: as given, with `./`, `..` and repeated
    slashes folded, and (when the root is known) as the project resolves it."""
    first = norm(path)
    out = [first, posixpath.normpath(first)]
    try:
        rel = project_path(root, path) if root is not None else None
    except Exception:  # noqa: BLE001 - a path the resolver cannot read is simply not another spelling
        rel = None
    if rel:
        out.append(rel)
    return out


def _phase_of(plan, spellings):
    keys = {path_key(s) for s in spellings}
    for f in _dicts(plan, "files"):
        if isinstance(f.get("path"), str) and path_key(norm(f["path"])) in keys:
            return f.get("phase")
    for p in _dicts(plan, "patterns"):
        if isinstance(p.get("glob"), str) and p["glob"]:
            rx = pattern_instance_rx(p["glob"])
            if any(rx.match(s) for s in spellings):
                return p.get("phase")
    return None


def refuse(action, root, plan, path, by):
    """Refuse a claim on a file of a phase that an open, severe unknown blocks (2.6): (message, error, next), exit 3.
    Every other action, and every other path, is not this module's business."""
    if action != "claim" or not isinstance(plan, dict):
        return None
    phase = _phase_of(plan, _spellings(root, path))
    if not isinstance(phase, str) or not phase:
        return None
    blocked, info, _ = _scan(plan)
    uids = blocked["by"].get(f"phase:{phase}") or []
    if phase not in blocked["phases"] or not uids:
        return None
    named = ", ".join(_phrase(u, *info.get(u, (None, None))) for u in uids)
    return (f"{_one(norm(path))} is in phase {_one(phase)}, blocked by unknown{'s' if len(uids) > 1 else ''} {named}. Resolve it "
            "first, or use --force if the lead says so.", "blocked-by-unknown", NEXT_UNKNOWNS)


def _detail(u):
    ex = u.get("experiment")
    if not isinstance(ex, dict) or not ex:
        return None
    return (f"experiment {_one(ex.get('timebox') or '?')}: {_one(ex.get('plan') or '?')}; "
            f"ends when: {_one(ex.get('exit') or '?')}")


def status_lines(ctx, args):
    """status: the open blocking unknowns; with --unknowns every unknown, its experiment and what it blocks (7.2).
    Every free-text value is put on one line."""
    unknowns = ctx.get("unknowns") or []
    if getattr(args, "unknowns", False):
        if not unknowns:
            v1 = isinstance(ctx.get("plan"), dict) and ctx["plan"].get("_from") == 1
            return ["", "  Unknowns: none. " + (
                "This is a schema version 1 plan: run python .blueprint/bin/blueprint.py migrate to use unknowns[]."
                if v1 else "Add open questions to unknowns[] in .blueprint/blueprint.json (see references/schema.md), "
                           "then run: python .blueprint/bin/blueprint.py render")]
        live = [u for u in unknowns if u["status"] not in NOT_BLOCKING_STATUSES]
        out = ["", f"  Unknowns: {len(live)} open or investigating, {len(unknowns) - len(live)} resolved or dropped"]
        for u in unknowns:
            tail = f"  blocks {_one(', '.join(u['blocks']))}" if u["blocks"] else ""
            out.append(f"    {_one(u['id']) or '(no id)'}  {_one(u['severity']) or '?'}  {_one(u['status']) or '?'}"
                       + ("  BLOCKING" + tail if u["blocking"] else ""))
            if _one(u["question"]):
                out.append(f"      {_one(u['question'])}")
            if u["experiment"]:
                out.append("      " + _detail(u))
            if u["decision"]:
                out.append(f"      decided in {_one(u['decision'])}")
        return out
    hard = [u for u in unknowns if u["blocking"]]
    if not hard:
        return []
    out = ["", f"  Open blocking unknowns: {len(hard)} (claims on what they block are refused; see status --unknowns)"]
    for u in hard:
        out.append(f"    {_one(u['id']) or '(no id)'} ({_one(u['severity']) or '?'}, {_one(u['status']) or '?'}) blocks "
                   f"{_one(', '.join(u['blocks'])) or 'nothing'}")
    return out
