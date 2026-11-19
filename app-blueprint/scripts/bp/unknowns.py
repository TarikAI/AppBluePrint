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
import os
import posixpath
import re

from pathlib import Path

from bp.core import TOOL_HOME, bp_dir, norm, path_key, project_path
from bp.globs import pattern_instance_rx
from bp.rules import uniq

ID_FORM = re.compile(r"U-[a-z0-9][a-z0-9-]{0,47}")
SEVERITIES = ("low", "medium", "high", "critical")
STATUSES = ("open", "investigating", "resolved", "dropped")
NOT_BLOCKING_SEVERITIES = ("low", "medium")  # below the blocking threshold (2.6)
NOT_BLOCKING_STATUSES = ("resolved", "dropped")
KINDS = ("phase", "milestone", "feature")
TIMEBOX = re.compile(r"[0-9]{1,3}[hdw]")
MAX_NAMED = 5  # unknowns a refusal names before "and N more"
RESOLVE_FIRST = " Resolve it first, or use --force if the lead says so."

CODES = [
    (r"^unknown \S+: needs question, severity and status", "unknown-incomplete"),
    (r"^unknown \S+: severity must be", "unknown-severity"),
    (r"^unknown \S+: status must be", "unknown-status"),
    (r"^unknown \S+: blocks ", "unknown-blocks"),
    (r"^unknown \S+: (investigating needs experiment|experiment( must|\.))", "unknown-experiment"),
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
    """The id a `by` list uses for the unknown at index i (a placeholder when it has no usable id)."""
    return u["id"] if isinstance(u.get("id"), str) and u["id"] else f"unknowns[{i}]"


def _label(u, i):
    """The name a validation message uses: the id, or a placeholder when it is missing or has whitespace in it."""
    uid = u.get("id")
    return uid if isinstance(uid, str) and uid and not any(c.isspace() for c in uid) else f"unknowns[{i}]"


def _run(root, args, needs=None):
    """A command line that runs as printed (as commands/migrate.py does): the project's own copy when it has the
    command (`needs` is the module of the copy that provides it), else this tool with --root."""
    if root is None:
        return f"python .blueprint/bin/blueprint.py {args}"
    copy = Path(bp_dir(Path(root))) / "bin"
    if (copy / "blueprint.py").is_file() and (needs is None or (copy / "bp" / needs).is_file()):
        return f"python .blueprint/bin/blueprint.py {args}"
    return f'python "{TOOL_HOME / "blueprint.py"}" {args} --root "{root}"'


def _blocks_of(u):
    """The strings of `blocks`, exactly as written (a lone string counts as one entry). Never stripped: " P2" names
    nothing, and validate says so."""
    b = u.get("blocks")
    if isinstance(b, str):
        b = [b]
    return [x for x in b if isinstance(x, str)] if isinstance(b, list) else []


def _resolve(entry, ids):
    """The (kind, id) pairs that a `blocks` entry names: `phase:P2` and friends name one kind; a bare id names every
    kind that has it (more than one is ambiguous, and validate refuses it, while blocking covers all of them)."""
    kind, _, rest = entry.partition(":")
    if rest and kind in KINDS:
        return [(kind, rest)] if rest in ids[kind] else []
    return [(k, entry) for k in KINDS if entry in ids[k]]


def is_blocking(u, decisions=frozenset()):
    """True when the unknown blocks the ids in `blocks`: not resolved (with its decision in `decisions`) or dropped,
    and not low or medium. Anything else, including a missing or wrongly typed status or severity and a `resolved`
    whose decision names nothing, blocks (fail closed)."""
    status, severity = u.get("status"), u.get("severity")
    if isinstance(status, str) and status == "dropped":
        return False
    if isinstance(status, str) and status == "resolved":
        d = u.get("decision")
        if isinstance(d, str) and d in decisions:
            return False
    if isinstance(severity, str) and severity in NOT_BLOCKING_SEVERITIES:
        return False
    return True


# --- blocking -------------------------------------------------------------------------------------------------------

def _scan(plan):
    """(blocked, info, hits, why): the blocking() result, {unknown id: (severity, status)} of those that block, the
    list indexes of the unknowns that block at least one id of this plan, and why each blocked feature is blocked:
    {"direct": {fid: [uid]}, "milestone": {fid: (mid, [uid])}}. One pass over the unknowns."""
    out = {"phases": set(), "milestones": set(), "features": set(), "by": {}}
    info, hits, seen = {}, set(), {}
    ids = _plan_ids(plan)
    decisions = _unique_ids(plan, "decisions")
    plural = {"phase": "phases", "milestone": "milestones", "feature": "features"}

    def add(key, uid):
        members = seen.setdefault(key, set())
        if uid not in members:
            members.add(uid)
            out["by"].setdefault(key, []).append(uid)

    items = plan.get("unknowns") if isinstance(plan, dict) else None
    for i, u in enumerate(items if isinstance(items, list) else []):
        if not isinstance(u, dict) or not is_blocking(u, decisions):
            continue
        uid = _uid(u, i)
        for entry in _blocks_of(u):
            for kind, tid in _resolve(entry, ids):
                out[plural[kind]].add(tid)
                add(f"{kind}:{tid}", uid)
                info[uid] = (u.get("severity"), u.get("status"))
                hits.add(i)
    why = {"direct": {k[len("feature:"):]: list(v) for k, v in out["by"].items() if k.startswith("feature:")},
           "milestone": {}}
    for f in _dicts(plan, "features"):  # a blocked milestone blocks its features (4.3 rule 2 reads them from here)
        fid, mid = f.get("id"), f.get("milestone")
        if isinstance(fid, str) and fid and isinstance(mid, str) and mid in out["milestones"]:
            out["features"].add(fid)
            why["milestone"][fid] = (mid, list(out["by"].get(f"milestone:{mid}", [])))
            for uid in out["by"].get(f"milestone:{mid}", []):
                add(f"feature:{fid}", uid)
    return out, info, hits, why


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


def _kinds_text(kinds, b):
    """The ambiguity message for an id that names more than one kind, naming every kind."""
    if len(kinds) == 2:
        return f"names both a {kinds[0]} and a {kinds[1]}; write {kinds[0]}:{b} or {kinds[1]}:{b}"
    named = ", ".join("a " + k for k in kinds[:-1]) + f" and a {kinds[-1]}"
    return f"names {named}; write {', '.join(k + ':' + b for k in kinds[:-1])} or {kinds[-1]}:{b}"


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
    root = vctx.get("root") if isinstance(vctx, dict) else None
    evidence = vctx.get("evidenceIds") if isinstance(vctx, dict) else None
    try:
        evidence = None if evidence is None else set(evidence)
    except TypeError:
        evidence = None
    for i, u in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(u, dict):
            continue
        uid = _label(u, i)
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
            hits = _resolve(b, ids) if isinstance(b, str) else []
            if len(hits) == 1:
                continue
            if len(hits) > 1:
                E(f"unknown {uid}: blocks {_show(b)}, which " + _kinds_text([k for k, _ in hits], _one(b)),
                  code="unknown-blocks", entry=entry, next="write the kind and a colon before the id in blocks")
            else:
                E(f"unknown {uid}: blocks {_show(b)}, which is no phase, milestone or feature (ids are matched "
                  "exactly)", code="unknown-blocks", entry=entry,
                  next="name an id from phases[], milestones[] or features[] (phase:X or milestone:X when both exist)")
        if evidence is not None:
            for key in ("evidenceFor", "evidenceAgainst"):
                for ev in u.get(key) if isinstance(u.get(key), list) else []:
                    if isinstance(ev, str) and ev not in evidence:
                        W(f"unknown {uid}: evidence {_one(ev)} is not in .blueprint/studies", code="unknown-evidence",
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
                E(f"unknown {uid}: resolved needs decision, the id of the decisions[] entry that settles it; it keeps "
                  "blocking until then", code="unknown-resolved-no-decision", entry=entry,
                  next="record the outcome in decisions[] and put its id in decision")
            elif not isinstance(d, str) or d not in decisions:
                E(f"unknown {uid}: resolved needs decision {_show(d)} to be the id of a decisions[] entry; it is not, "
                  "so it keeps blocking", code="unknown-resolved-no-decision", entry=entry,
                  next="add that entry to decisions[], or fix the id in decision")
        if status == "dropped" and not _text(u.get("notes")):
            E(f"unknown {uid}: dropped needs notes saying why it no longer matters", code="unknown-dropped-no-note",
              entry=entry, next="say why in notes")
        if status in ("open", "investigating") and severity in ("high", "critical"):
            targets = [_one(b) for b in _blocks_of(u) if _resolve(b, ids)]
            if targets:
                W(f"unknown {uid} ({severity}) is open and blocks {', '.join(targets)}; claim and status --next skip "
                  "them until it is resolved", code="unknown-open-blocker", entry=entry,
                  next=_run(root, "status --unknowns", "unknowns.py"))
    for d in _dicts(plan, "decisions"):
        ref = d.get("unknown")
        if isinstance(ref, str) and ref and ref not in known:
            did = _one(d["id"]) if isinstance(d.get("id"), str) and d["id"] else "?"
            E(f"decision {did}: unknown {_show(ref)} does not exist; use an id from unknowns[] or remove the field",
              code="decision-unknown", entry=f"decision {did}",
              next=(_run(root, "migrate", "commands/migrate.py") if plan.get("_from") == 1
                    else "fix decisions[].unknown in blueprint.json"))


# --- which claims are refused ---------------------------------------------------------------------------------------

def _spellings(root, path):
    """Every way the claimed path can be written that still names one file: as given, with `./`, `..` and repeated
    slashes folded, and (when the root is known) as the project resolves it (a junction or symlink included)."""
    first = norm(path)
    out = [first, posixpath.normpath(first)]
    try:
        rel = project_path(root, path) if root is not None else None
    except Exception:  # noqa: BLE001 - a path the resolver cannot read is simply not another spelling
        rel = None
    if rel and rel not in out:
        out.append(rel)
    return out


class _Plan:
    """What claim decisions need from a plan, computed once: the blocking scan, the plan's feature ids, planned files
    by path key and the patterns with their instance expressions."""

    def __init__(self, plan):
        self.plan = plan
        self.blocked, self.info, self.hits, self.why = _scan(plan)
        self.features = _ids(plan, "features")
        self.files, self.pos = [], {}
        self.by_key = {}
        for f in _dicts(plan, "files"):
            if isinstance(f.get("path"), str):
                self.pos[id(f)] = len(self.files)
                self.files.append(f)
                self.by_key.setdefault(path_key(norm(f["path"])), []).append(f)
        fold = re.IGNORECASE if os.path.normcase("A") == "a" else 0  # a case-insensitive disk: fail closed
        self.patterns = [(p, re.compile((rx := pattern_instance_rx(p["glob"])).pattern, rx.flags | fold))
                         for p in _dicts(plan, "patterns") if isinstance(p.get("glob"), str) and p["glob"]]

    def served(self, entries):
        """The feature ids the entries serve, in order, from the plan: only ids that name a feature of features[]
        (compared exactly) count, so a typo is never a free feature. Infrastructure is just a file with none."""
        out = []
        for e in entries:
            for f in e.get("features") if isinstance(e.get("features"), list) else []:
                if isinstance(f, str) and f in self.features and f not in out:
                    out.append(f)
        return out

    def phase_blocked(self, entry):
        phase = entry.get("phase")
        return isinstance(phase, str) and phase in self.blocked["phases"] and bool(self.blocked["by"].get(f"phase:{phase}"))

    def matches(self, spellings):
        """(files, patterns) that any spelling names: planned files whose path key equals a spelling's, in plan
        order, and patterns whose instance expression matches a spelling."""
        keys = {path_key(s) for s in spellings}
        files, seen = [], set()
        for k in keys:
            for f in self.by_key.get(k, []):
                if id(f) not in seen:
                    seen.add(id(f))
                    files.append(f)
        files.sort(key=lambda f: self.pos[id(f)])
        pats = [p for p, rx in self.patterns if any(rx.match(s) for s in spellings)]
        return files, pats

    def judge(self, files, pats):
        """The first reason a claim is refused, as (kind, entry-or-patterns, served): every matching planned file is
        judged on its own, the matching patterns together (phase of any one, features as a union); None if free."""
        for f in files:
            if self.phase_blocked(f):
                return "phase", [f], []
            served = self.served([f])
            if served and all(x in self.blocked["features"] for x in served):
                return "features", [f], served
        for p in pats:
            if self.phase_blocked(p):
                return "phase", [p], []
        served = self.served(pats)
        if served and all(x in self.blocked["features"] for x in served):
            return "features", pats, served
        return None


def _named(uids, info):
    shown = ", ".join(_phrase(u, *info.get(u, (None, None))) for u in uids[:MAX_NAMED])
    if len(uids) > MAX_NAMED:
        shown += f", and {len(uids) - MAX_NAMED} more (see status --unknowns)"
    return shown


def _decide(pl, root, path):
    """(message, error, next) when claiming `path` is refused under `pl` (a _Plan), else None."""
    files, pats = pl.matches(_spellings(root, path))
    verdict = pl.judge(files, pats)
    if verdict is None:
        return None
    kind, entries, served = verdict
    shown = _one(norm(path))
    if entries[0] in files:
        subject = _one(norm(entries[0]["path"]))
    else:
        subject = f"{shown} (pattern {', '.join(_one(e.get('id', '?')) for e in entries)})"
    nxt = _run(root, "status --unknowns", "unknowns.py")
    if kind == "phase":
        phase = entries[0]["phase"]
        uids = pl.blocked["by"][f"phase:{phase}"]
        return (f"{subject} is in phase {_one(phase)}, blocked by unknown{'s' if len(uids) > 1 else ''} "
                f"{_named(uids, pl.info)}." + RESOLVE_FIRST, "blocked-by-unknown", nxt)
    clauses = []
    for fid in served:
        parts = []
        direct = pl.why["direct"].get(fid) or []
        if direct:
            parts.append(f"blocked by unknown{'s' if len(direct) > 1 else ''} {_named(direct, pl.info)}")
        if fid in pl.why["milestone"]:
            mid, uids = pl.why["milestone"][fid]
            parts.append(f"whose milestone {_one(mid)} is blocked by unknown{'s' if len(uids) > 1 else ''} "
                         f"{_named(uids, pl.info)}")
        clauses.append(f"{_one(fid)}, {' and '.join(parts)}")
    what = f"feature {clauses[0]}" if len(clauses) == 1 else "blocked features: " + "; ".join(clauses)
    return (f"{subject} serves only {what}." + RESOLVE_FIRST, "blocked-by-unknown", nxt)


def blocked_files(plan, root=None):
    """The planned file paths that a claim refuses: the same decision `refuse` makes, so the two never disagree."""
    pl = _Plan(plan)
    return {f["path"] for f in pl.files if _decide(pl, root, f["path"])}


def refuse(action, root, plan, path, by):
    """Refuse a claim (2.6): (message, "blocked-by-unknown", next), exit 3. Every planned file and every pattern that
    any spelling of the path names is judged, and the claim is refused when one of them would be: its phase is
    blocked by an open, severe unknown, or it serves at least one feature and every feature it serves is blocked (by
    an unknown naming the feature, or its milestone). The refusal names the entry that caused it."""
    if action != "claim" or not isinstance(plan, dict):
        return None
    return _decide(_Plan(plan), root, path)


# --- context, views and status --------------------------------------------------------------------------------------

def _strings(v):
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _payload(u, ids, blocking_now):
    """One entry of ctx["unknowns"] and of the map and status --json payloads (8.5). `unresolved` lists the `blocks`
    entries that name no phase, milestone or feature of the plan."""
    ex = u.get("experiment")
    ex = {k: ex[k] for k in ("plan", "timebox", "exit", "owner") if isinstance(ex.get(k), str)} if isinstance(ex, dict) else None
    blocks = _blocks_of(u)
    return {"id": u["id"] if isinstance(u.get("id"), str) else "",
            "question": u["question"] if isinstance(u.get("question"), str) else "",
            "severity": u["severity"] if isinstance(u.get("severity"), str) else "",
            "status": u["status"] if isinstance(u.get("status"), str) else "",
            "blocks": blocks, "unresolved": [b for b in blocks if not _resolve(b, ids)],
            "evidenceFor": _strings(u.get("evidenceFor")),
            "evidenceAgainst": _strings(u.get("evidenceAgainst")), "experiment": ex,
            "decision": u["decision"] if isinstance(u.get("decision"), str) and u["decision"] else None,
            "blocking": blocking_now}


def context(root, plan, state, ctx):
    """ctx["unknowns"] (8.5) and ctx["blocked"] (1.4): the ids blocked by open, severe unknowns, and the planned files
    that claim refuses (an extra key, "files")."""
    pl = _Plan(plan)
    ctx["blocked"] = pl.blocked
    ctx["blocked"]["files"] = {f["path"] for f in pl.files if _decide(pl, root, f["path"])}
    ids = _plan_ids(plan)
    items = plan.get("unknowns") if isinstance(plan, dict) else None
    ctx["unknowns"] = [_payload(u, ids, i in pl.hits)
                       for i, u in enumerate(items if isinstance(items, list) else []) if isinstance(u, dict)]


def _next_step(ctx):
    """The one command to run when there are no unknowns: render, or migrate for a version 1 plan."""
    root, plan = ctx.get("root"), ctx.get("plan")
    if isinstance(plan, dict) and plan.get("_from") == 1:
        return "run: " + _run(root, "migrate", "commands/migrate.py")
    return "run: " + _run(root, "render")


def map_payload(ctx):
    return {"unknowns": ctx.get("unknowns") or [], "unknownsNext": _next_step(ctx)}


def status_json(ctx):
    return {"unknowns": ctx.get("unknowns") or []}


def _phrase(uid, sev, status):
    return (f"{_one(uid)} ({_one(sev) if isinstance(sev, str) and sev else 'no severity'}, "
            f"{_one(status) if isinstance(status, str) and status else 'no status'})")


def _detail(u):
    ex = u.get("experiment")
    if not isinstance(ex, dict) or not ex:
        return None
    return (f"experiment {_one(ex.get('timebox') or '?')}: {_one(ex.get('plan') or '?')}; "
            f"ends when: {_one(ex.get('exit') or '?')}")


def _blocks_text(u):
    """`blocks` for people, one line, marking each entry that names nothing in the plan."""
    nothing = set(u.get("unresolved") or [])
    return ", ".join(_one(b) + (" (names nothing)" if b in nothing else "") for b in u["blocks"])


def status_lines(ctx, args):
    """status: the open blocking unknowns; with --unknowns every unknown, its experiment and what it blocks (7.2).
    Every free-text value is put on one line."""
    unknowns = ctx.get("unknowns") or []
    if getattr(args, "unknowns", False):
        if not unknowns:
            return ["", "  Unknowns: none. " + ("This is a schema version 1 plan; " if isinstance(ctx.get("plan"), dict)
                                                  and ctx["plan"].get("_from") == 1 else
                                                  "Add open questions to unknowns[] in .blueprint/blueprint.json "
                                                  "(see references/schema.md); ") + _next_step(ctx)]
        live = [u for u in unknowns if u["status"] not in NOT_BLOCKING_STATUSES]
        out = ["", f"  Unknowns: {len(live)} open or investigating, {len(unknowns) - len(live)} resolved or dropped"]
        for u in unknowns:
            tail = f"  blocks {_blocks_text(u)}" if u["blocks"] else ""
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
                   f"{_blocks_text(u) or 'nothing'}")
    return out
