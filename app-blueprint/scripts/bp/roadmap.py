"""roadmap: milestones[] and tasks[] (contract 2.3, 2.4, 4.2, 4.4, 7.4, 8.5).

Validation of both sections, the task ladder (set_task), the context that fills ctx["milestones"] and ctx["tasks"], the
payloads of the map and of status --json, the lines of status and the Tasks section of ROSTER.md.

Imports no other domain module. Milestone progress reads ctx["features"] (the features module runs before this one);
when that is empty the plan's own features[] stand in, never accepted (fail closed). Everything here tolerates a plan
that did not come through load_plan: a wrongly typed value is skipped, never a crash.
"""
import datetime
import json
import re
from pathlib import Path

from bp.core import BlueprintError, append_log, copy_json, now_iso
from bp.plan import load_state, save_state

TASK_STATES = ("todo", "doing", "done", "blocked")
TASK_GLYPH = {"todo": "○", "doing": "◑", "done": "✔", "blocked": "▲"}
BP = "python .blueprint/bin/blueprint.py"  # how every next step starts: it runs as printed
MILESTONE_ID = re.compile(r"M[0-9]{1,3}")  # always used with fullmatch: `$` would accept a trailing newline
TASK_ID = re.compile(r"T-[a-z0-9][a-z0-9-]{0,47}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

CODES = [
    (r"^milestone \S+: needs name and goal", "milestone-incomplete"),
    (r"^milestone \S+: due ", "milestone-due"),
    (r"^task \S+: needs title and owner", "task-incomplete"),
    (r"^task \S+: owner .* is not a roster entry", "task-owner"),
    (r"^task \S+: (milestone |set a milestone)", "task-milestone"),
    (r"^task \S+: dependsOn |^task dependency loop", "task-dependency"),
    (r"^task \S+: an owner action must be owned by a human", "owner-action-human"),
]
CODE_EXAMPLES = {
    "milestone-incomplete": "milestone M1: needs name and goal",
    "milestone-due": "milestone M1: due '2027-02-30' is not a date (YYYY-MM-DD)",
    "task-incomplete": "task T-x: needs title and owner",
    "task-owner": "task T-x: owner 'nobody' is not a roster entry",
    "task-milestone": "task T-x: milestone 'M9' does not exist",
    "task-dependency": "task T-x: dependsOn 'T-y' names no task or feature",
    "owner-action-human": "task T-x: an owner action must be owned by a human roster entry, not backend-lead",
}


# ---------------------------------------------------------------- small readers (tolerant of any JSON value)

def _s(v):
    return v if isinstance(v, str) else ""


def _list(plan, key):
    v = plan.get(key) if isinstance(plan, dict) else None
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _strings(v):
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _label(item, i):
    """What a message calls an entry: its id, or #<position> when it has none or one with whitespace in it."""
    v = item.get("id")
    return v if isinstance(v, str) and v and not re.search(r"\s", v) else f"#{i + 1}"


def is_date(text):
    """True for a real calendar date written YYYY-MM-DD."""
    if not isinstance(text, str) or not _DATE.fullmatch(text):
        return False
    try:
        datetime.date(int(text[:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        return False
    return True


def section_index(plan, name):
    """{id: entry} of the object entries with a non-empty string id, in plan order, the first of a repeated id winning
    (plan.section_index, but a section that is not a list is empty here instead of an error)."""
    out = {}
    for item in _list(plan, name):
        if isinstance(item.get("id"), str) and item["id"] and item["id"] not in out:
            out[item["id"]] = item
    return out


def _roster(plan):
    """{id: entry}: the first entry of an id, whose kind becomes "conflict" when another entry with that id has a
    different kind, so an id is human only when every entry with it is (fail closed)."""
    out = {}
    for r in _list(plan, "roster"):
        i = r.get("id")
        if not isinstance(i, str):
            continue
        if i not in out:
            out[i] = dict(r)
        elif out[i].get("kind") != r.get("kind"):
            out[i]["kind"] = "conflict"
    return out


def _human(roster, who):
    return who in roster and roster[who].get("kind") == "human"


# ---------------------------------------------------------------- validation (2.3, 2.4, 7.4)

def _ids(items, section, E):
    """Missing and duplicate ids with the 1.x messages, then the id form (bad-id)."""
    seen = set()
    for it in items:
        v = it.get("id")
        if not isinstance(v, str) or not v:
            E(f"{section} without id: {json.dumps(it, default=str)[:80]}")
            continue
        if v in seen:
            E(f"duplicate {section} id: {v}")
        seen.add(v)
        pattern, form = (MILESTONE_ID, "M and one to three digits, for example M1") if section == "milestone" \
            else (TASK_ID, "T- then lowercase letters, digits and hyphens, for example T-domain")
        if not pattern.fullmatch(v):
            E(f"{section} id '{v}' has the wrong form; use {form}", code="bad-id", entry=f"{section} {v}",
              next=f"rename the {section} in .blueprint/blueprint.json (and every reference to it)")
    return seen


def _task_cycles(tasks):
    """Each dependency loop among tasks once, as a list of ids that starts where the walk entered it."""
    deps = {t["id"]: [d for d in _strings(t.get("dependsOn")) if d in tasks] for t in tasks.values()}
    color, found, seen = {}, [], set()
    for start in tasks:
        if color.get(start):
            continue
        color[start] = 1
        path, stack = [start], [(start, iter(deps[start]))]
        while stack:
            node, it = stack[-1]
            for d in it:
                c = color.get(d, 0)
                if c == 0:
                    color[d] = 1
                    path.append(d)
                    stack.append((d, iter(deps[d])))
                    break
                if c == 1:
                    cyc = path[path.index(d):]
                    key = tuple(sorted(cyc))
                    if key not in seen:
                        seen.add(key)
                        found.append(cyc)
            else:
                color[node] = 2
                path.pop()
                stack.pop()
    return found


def validate(plan, E, W, vctx):
    roster = _roster(plan)
    milestones = _list(plan, "milestones")
    mids = _ids(milestones, "milestone", E)
    for i, m in enumerate(milestones):
        lab = _label(m, i)
        if not _s(m.get("name")).strip() or not _s(m.get("goal")).strip():
            E(f"milestone {lab}: needs name and goal", code="milestone-incomplete", entry=f"milestone {lab}",
              next="add name and goal to the milestone in .blueprint/blueprint.json")
        due = m.get("due")
        if due not in (None, "") and not is_date(due):
            E(f"milestone {lab}: due '{str(due)[:40]}' is not a date (YYYY-MM-DD)", code="milestone-due",
              entry=f"milestone {lab}", next="write due as YYYY-MM-DD, for example 2027-01-15, or remove it")
    tasks_list = _list(plan, "tasks")
    _ids(tasks_list, "task", E)
    features = {f["id"] for f in _list(plan, "features") if isinstance(f.get("id"), str)}
    tasks = {}
    for t in tasks_list:
        if isinstance(t.get("id"), str) and t["id"] and t["id"] not in tasks:
            tasks[t["id"]] = t
    for i, t in enumerate(tasks_list):
        lab = _label(t, i)
        entry = f"task {lab}"
        owner = _s(t.get("owner"))
        if not _s(t.get("title")).strip() or not owner.strip():
            E(f"task {lab}: needs title and owner", code="task-incomplete", entry=entry,
              next="add title and owner (a roster id) to the task in .blueprint/blueprint.json")
        elif owner not in roster:
            E(f"task {lab}: owner '{owner}' is not a roster entry", code="task-owner", entry=entry,
              next="use an id from roster[], or add the owner there")
        ms = t.get("milestone")
        if ms not in (None, ""):
            if not isinstance(ms, str) or ms not in mids:
                E(f"task {lab}: milestone '{str(ms)[:40]}' does not exist",
                  code="task-milestone", entry=entry,
                  next=("use one of: " + ", ".join(sorted(mids))) if mids else "add the milestone to milestones[], or remove this field")
        elif mids:
            E(f"task {lab}: set a milestone (the plan has milestones)", code="task-milestone", entry=entry,
              next="add milestone with one of: " + ", ".join(sorted(mids)))
        for d in _strings(t.get("dependsOn")):
            if d not in tasks and d not in features:
                E(f"task {lab}: dependsOn '{d}' names no task or feature", code="task-dependency", entry=entry,
                  next="use the id of a task or a feature, or remove it")
        if t.get("ownerAction") is True and owner in roster and not _human(roster, owner):
            E(f"task {lab}: an owner action must be owned by a human roster entry, not {owner}",
              code="owner-action-human", entry=entry,
              next="set owner to a roster entry whose kind is human, or set ownerAction to false")
    for cyc in _task_cycles(tasks):
        E("task dependency loop: " + " -> ".join(cyc + [cyc[0]]), code="task-dependency", entry=f"task {cyc[0]}",
          next="remove one dependsOn in the loop")


# ---------------------------------------------------------------- the task ladder (4.2, 4.4)

def unmet_dependencies(plan, tid, tasks, features):
    """The ids a task depends on that are not done (tasks, from the ctx task rows) or accepted (features, from the ctx
    feature rows): done is allowed anyway, the command only warns (contract ruling for 2.0: no gating)."""
    task = section_index(plan, "tasks").get(tid) or {}
    status = {t["id"]: t["status"] for t in tasks}
    feat = {f["id"]: f.get("status") for f in features if isinstance(f, dict) and isinstance(f.get("id"), str)}
    out = []
    for d in _strings(task.get("dependsOn")):
        if d in status:
            if status[d] != "done":
                out.append(d)
        elif feat.get(d) != "accepted" and d not in out:  # a feature the features module did not report is not accepted
            out.append(d)
    return out


def _plan_fix(tid, owner, owner_action, roster):
    """What is wrong with the plan around a task when the command we would suggest cannot work, else None."""
    if owner not in roster:
        why = f"task {tid}: owner {owner!r} is not a roster entry"
    elif roster[owner].get("kind") == "conflict":
        why = f"roster id {owner!r} has entries of different kinds"
    elif owner_action and not _human(roster, owner):
        why = f"task {tid} is an owner action, so its owner must be a human roster entry"
    else:
        return None
    return f"fix .blueprint/blueprint.json ({why}), then run: {BP} validate"


def _task_error(error, message, tid, next, code=1):  # noqa: A002 (next is the contract's name)
    return BlueprintError(error, message, entry=f"task {tid}", next=next, code=code)


def set_task(root, plan, tid, status, by, note=None, force=False):
    """Set a task's status in state.json and log it (the `task` command). The rules, in this order:
    unknown task or roster id (exit 1); a status outside the ladder, or `blocked` without a note (64); on an ownerAction
    task, anyone who is not a human roster entry may neither set `done` nor change a task that is `done`, --force or
    not (3, owner-action-human); anyone but the task owner without --force (3, wrong-owner). A roster id counts as
    human only when every entry with that id is. Returns the record written."""
    tasks = section_index(plan, "tasks")
    if tid not in tasks:
        raise _task_error("unknown-task", f"{tid!r} is not a task in the plan", tid,
                          f"{BP} migrate" if plan.get("_from") == 1 else f"{BP} status --tasks")
    task = tasks[tid]
    roster = _roster(plan)
    owner = _s(task.get("owner"))
    owner_action = any(t.get("ownerAction") is True for t in _list(plan, "tasks") if t.get("id") == tid)
    again = _plan_fix(tid, owner, owner_action, roster) or (
        f"{BP} task {tid} {status if status in TASK_STATES else 'doing'} --by {owner}"
        + (' --note "waiting on ..."' if status == "blocked" else ""))
    if by not in roster:
        raise _task_error("unknown-roster", f"--by {by!r} is not in the roster (roster ids: "
                          + (", ".join(sorted(roster)) or "none") + ")", tid, again)
    if status not in TASK_STATES:
        raise _task_error("usage", f"{status!r} is not a task status (use one of: {', '.join(TASK_STATES)})", tid,
                          f"{BP} task --help", 64)
    if status == "blocked" and not (note or "").strip():
        raise _task_error("usage", f"{tid}: say why it is blocked", tid,
                          f'{BP} task {tid} blocked --by {by} --note "waiting on ..."', 64)
    state = load_state(root)
    current = _state_task(state, tid).get("status")
    if owner_action and not _human(roster, by) and (status == "done" or current == "done"):
        person = owner if _human(roster, owner) else None
        why = ("only a person can mark it done" if status == "done" and current != "done"
               else "a person marked it done, and only a person can change that")
        raise _task_error("owner-action-human", f"{tid} is an owner action: {why}, and {by} is not a human roster entry",
                          tid, f"{BP} task {tid} {status} --by {person}" + (' --note "waiting on ..."' if status == "blocked" else "")
                          if person else again, 3)
    if by != owner and not force:
        raise _task_error("wrong-owner", f"{tid} is owned by {owner or 'nobody'}, not {by}; ask {owner or 'the lead'}, "
                          "or use --force if the lead says so", tid, again, 3)
    if not isinstance(state.get("tasks"), dict):
        state["tasks"] = {}
    rec = {"status": status, "by": by, "at": now_iso(), "note": note if note else None}
    state["tasks"][tid] = rec
    save_state(root, state)
    append_log(root, {"event": "task", "task": tid, "status": status, "by": by, "note": rec["note"]})
    return rec


# ---------------------------------------------------------------- context (8.5)

def _state_task(state, tid):
    tasks = state.get("tasks") if isinstance(state, dict) else None
    rec = tasks.get(tid) if isinstance(tasks, dict) else None
    return rec if isinstance(rec, dict) else {}


def task_rows(plan, state):
    rows = []
    for tid, t in section_index(plan, "tasks").items():
        rec = _state_task(state, tid)
        status = rec.get("status") if rec.get("status") in TASK_STATES else "todo"  # anything else never counts as done
        rows.append({"id": tid, "title": _s(t.get("title")), "owner": _s(t.get("owner")),
                     "milestone": _s(t.get("milestone")) or None, "ownerAction": t.get("ownerAction") is True,
                     "dependsOn": _strings(t.get("dependsOn")), "doneWhen": _s(t.get("doneWhen")), "status": status,
                     "by": _s(rec.get("by")) or None, "at": _s(rec.get("at")) or None, "note": _s(rec.get("note")) or None})
    return rows


def feature_rows(plan, ctx):
    """One row per feature of the PLAN, in plan order: the plan decides what exists, which milestone it is in and whether
    it is a draft; the features payload (ctx["features"]) only supplies the status and what goes with it. A feature the
    payload does not report (or reports in another milestone) is `planned`, so a milestone can never read done while one is missing from it."""
    got, torn = {}, set()
    for f in ctx.get("features") if isinstance(ctx.get("features"), list) else []:
        if isinstance(f, dict) and isinstance(f.get("id"), str):
            if f["id"] in got and any(got[f["id"]].get(k) != f.get(k) for k in ("status", "milestone")):
                torn.add(f["id"])  # two entries for one feature that disagree: trust neither, whichever comes first
            got.setdefault(f["id"], f)
    rows = []
    for fid, f in section_index(plan, "features").items():
        rep = got.get(fid, {})
        draft = f.get("draft") is True
        status = rep.get("status") if isinstance(rep.get("status"), str) and rep.get("status") else "planned"
        if status == "draft" or rep.get("draft") is True and not draft or fid in torn:
            status = "planned"  # only the plan makes a draft, and a draft is set below
        row = dict(rep)
        if fid in torn:
            row.pop("statusText", None)
        if "milestone" in rep and (_s(rep.get("milestone")) or None) != (_s(f.get("milestone")) or None):
            status = "planned"  # the payload disagrees with the plan about where it belongs: nothing is trusted
            row.pop("statusText", None)
        if status == "planned" and (rep.get("status") == "draft" or rep.get("draft") is True):
            row.pop("statusText", None)
        row.update({"id": fid, "title": _s(rep.get("title")) or _s(f.get("title")),
                    "milestone": _s(f.get("milestone")) or None, "owner": _s(rep.get("owner")) or _s(f.get("owner")),
                    "draft": draft, "status": "draft" if draft else status})
        row.setdefault("criteria", [c for c in f.get("criteria") or [] if isinstance(c, dict)]
                       if isinstance(f.get("criteria"), list) else [])
        row.setdefault("awaitingEvaluation", 0)
        row.setdefault("blockedBy", [])
        rows.append(row)
    return rows


def is_draft(f):
    """A draft is what the plan says (feature_rows sets `draft` from it), never what a payload's status says."""
    return f.get("draft") is True


def _syncd(state):
    at = state.get("syncedAt") if isinstance(state, dict) else None
    return at[:10] if isinstance(at, str) and is_date(at[:10]) else None


def milestone_rows(plan, state, ctx, tasks):
    feats = feature_rows(plan, ctx)
    by = ((ctx.get("blocked") or {}).get("by") or {}) if isinstance(ctx.get("blocked"), dict) else {}
    today = _syncd(state)
    rows = []
    for mid, m in section_index(plan, "milestones").items():
        fs = [f for f in feats if f.get("milestone") == mid]
        counted = [f for f in fs if not is_draft(f)]
        accepted = sum(1 for f in counted if f.get("status") == "accepted")
        ts = [t for t in tasks if t["milestone"] == mid]
        done_t = sum(1 for t in ts if t["status"] == "done")
        done = bool(counted or ts) and accepted == len(counted) and done_t == len(ts)  # an empty milestone is not done
        due = _s(m.get("due")) or None
        rows.append({"id": mid, "name": _s(m.get("name")), "goal": _s(m.get("goal")), "due": due,
                     "exit": _s(m.get("exit")), "features": [f["id"] for f in fs], "tasks": [t["id"] for t in ts],
                     "progress": {"features": len(counted), "accepted": accepted, "tasks": len(ts), "done": done_t},
                     "done": done, "overdue": bool(not done and today and is_date(due) and due < today),
                     "blockedBy": _strings(by.get(f"milestone:{mid}"))})
    return rows


def context(root, plan, state, ctx):
    ctx["tasks"] = task_rows(plan, state)
    ctx["milestones"] = milestone_rows(plan, state, ctx, ctx["tasks"])


# ---------------------------------------------------------------- shared by the views and status

def feature_status_text(f):
    """The status of a feature as every view shows it: the `statusText` the features module puts on its entry (it owns
    the awaiting-evaluation wording), else the bare status."""
    t = f.get("statusText")
    if isinstance(t, str) and t:
        return t
    return f.get("status") if isinstance(f.get("status"), str) and f.get("status") else "planned"


def criteria_passing(f):
    crit = [c for c in (f.get("criteria") if isinstance(f.get("criteria"), list) else []) if isinstance(c, dict)]
    return sum(1 for c in crit if c.get("result") == "pass"), len(crit)


def open_questions(ctx):
    """Unknowns not resolved or dropped, from ctx["unknowns"] (else the plan's), each with a `blocking` flag."""
    src = ctx.get("unknowns")
    items = [u for u in src if isinstance(u, dict)] if isinstance(src, list) and src else _list(ctx.get("plan") or {}, "unknowns")
    out = []
    for u in items:
        if not isinstance(u.get("id"), str) or u.get("status") in ("resolved", "dropped"):
            continue
        blocking = u.get("blocking")
        if not isinstance(blocking, bool):
            blocking = (u.get("status") in ("open", "investigating") and u.get("severity") in ("high", "critical")
                        and bool(_strings(u.get("blocks"))))
        out.append(dict(u, blocking=blocking))
    return out


def counts(ctx):
    """The numbers of the ROADMAP.md summary line and of status: milestones, done milestones, accepted and counted
    features, done and all tasks, open blocking questions. Drafts are never counted (they can never be accepted)."""
    feats = [f for f in feature_rows(ctx.get("plan") or {}, ctx) if not is_draft(f)]
    ms, ts = ctx.get("milestones") or [], ctx.get("tasks") or []
    return {"milestones": len(ms), "milestonesDone": sum(1 for m in ms if m.get("done")),
            "featuresAccepted": sum(1 for f in feats if f.get("status") == "accepted"), "features": len(feats),
            "tasksDone": sum(1 for t in ts if t.get("status") == "done"), "tasks": len(ts),
            "blocking": sum(1 for u in open_questions(ctx) if u["blocking"])}


def progress_text(m):
    """`1 of 2 features accepted · 0 of 1 tasks done`, naming only what the milestone has."""
    p = m.get("progress") or {}
    parts = []
    if p.get("features"):
        parts.append(f"{p.get('accepted', 0)} of {p['features']} features accepted")
    if p.get("tasks"):
        parts.append(f"{p.get('done', 0)} of {p['tasks']} tasks done")
    return " · ".join(parts) or "nothing in it yet"


def roadmap_next(ctx):
    """The empty state of the Roadmap tab, stored in map.html, so no path of this machine: the project's own copy of the
    tool when it has one (and, for migrate, one that has the command), else the placeholder form with a literal <skill>."""
    root, v1 = ctx.get("root"), (ctx.get("plan") or {}).get("_from") == 1
    copy = None
    try:
        if root is not None:
            copy = Path(root) / ".blueprint" / "bin"
            if not (copy / "blueprint.py").is_file() or (v1 and not (copy / "bp" / "commands" / "migrate.py").is_file()):
                copy = None
    except (OSError, ValueError, TypeError):
        copy = None
    verb = "migrate" if v1 else "render"
    run = f"python .blueprint/bin/blueprint.py {verb}" if copy is not None else f"python <skill>/scripts/blueprint.py {verb} --root ."
    if v1:
        return ("This blueprint is schema version 1 and has no milestones or tasks. Run: " + run
                + ", then add milestones[] and tasks[] to blueprint.json.")
    return "No milestones or tasks yet. Add milestones[] and tasks[] to blueprint.json (see references/schema.md), then run: " + run


def map_payload(ctx):
    """milestones and tasks (8.5), plus `roadmap`: what the Roadmap tab needs to tell the story ROADMAP.md tells with or
    without the features and unknowns modules (plan-based membership and totals, the open questions)."""
    return {"milestones": ctx.get("milestones") or [], "tasks": ctx.get("tasks") or [], "roadmapNext": roadmap_next(ctx),
            "roadmap": {"counts": counts(ctx), "features": copy_json(feature_rows(ctx.get("plan") or {}, ctx)),
                        "unknowns": copy_json(open_questions(ctx))}}


def status_json(ctx):
    return {"milestones": ctx.get("milestones") or [], "tasks": ctx.get("tasks") or []}


def roster_sections(ctx):
    """## Tasks in ROSTER.md: per owner, the tasks they hold; `you` marks the ones only a person can do."""
    tasks = ctx.get("tasks") or []
    if not tasks:
        return []
    order = [r["id"] for r in _list(ctx.get("plan") or {}, "roster") if isinstance(r.get("id"), str)]
    order += [t["owner"] for t in tasks if t["owner"] not in order]
    lines = ["| Owner | Task | Status |", "|---|---|---|"]
    for o in dict.fromkeys(order):
        for t in tasks:
            if t["owner"] == o:
                lines.append(f"| {cell(o)} | {cell(t['id'] + ' ' + t['title'] + (' (you)' if t['ownerAction'] else ''))} | {t['status']} |")
    return [("Tasks", "\n".join(lines))]


def flat(text):
    """One line: runs of whitespace (newlines included) collapse to a space."""
    return " ".join(str(text).split())


def line(text):
    """Plan text as one line of markdown: one line, `&` and `<` written as entities so no raw HTML passes."""
    return flat(text).replace("&", "&amp;").replace("<", "&lt;")


def cell(text):
    """Plan text for a markdown table cell: line(), with `` and `|` escaped (the backslash first)."""
    return line(text).replace("\\", "\\\\").replace("|", "\\|")


def _migrate_or(plan, other):
    return f"{BP} migrate" if isinstance(plan, dict) and plan.get("_from") == 1 else other


def status_lines(ctx, args):
    ms, ts = ctx.get("milestones") or [], ctx.get("tasks") or []
    out = []
    if ms or ts:
        c = counts(ctx)
        out += ["", f"  Roadmap: {c['milestones']} milestone(s) (done {c['milestonesDone']}), tasks done {c['tasksDone']} of {c['tasks']}"]
        for m in ms:
            due = f" · due {flat(m['due'])}" if m.get("due") else ""
            out.append(f"    {flat(m['id'])} {flat(m['name'])}{due} · {progress_text(m)}" + (" · done" if m["done"] else "")
                       + (" · overdue" if m["overdue"] else ""))
        waiting = [t for t in ts if t["ownerAction"] and t["status"] != "done"]
        if waiting:
            out.append("    Waiting on a person: " + ", ".join(f"{flat(t['id'])} ({flat(t['owner'])})" for t in waiting))
    if getattr(args, "tasks", False):
        out += ["", "  Tasks:"] + ([f"    {TASK_GLYPH[t['status']]} {flat(t['id'])}  {t['status']}  {flat(t['owner'])}"
                                    f"{' (you)' if t['ownerAction'] else ''}  {flat(t['title'])}"
                                    + (f"  [{flat(t['note'])}]" if t["status"] == "blocked" and t["note"] else "") for t in ts]
                                   or ["    none in the plan"])
    want = getattr(args, "milestone", None)
    if want:
        m = next((x for x in ms if x["id"] == want), None)
        if m is None:
            raise BlueprintError("unknown-milestone", f"{flat(want)!r} is not a milestone in the plan"
                                 + (" (milestones: " + ", ".join(flat(x["id"]) for x in ms) + ")" if ms else ""),
                                 entry=f"milestone {flat(want)}", next=_migrate_or(ctx.get("plan"), f"{BP} status"))
        feats = {f["id"]: f for f in feature_rows(ctx.get("plan") or {}, ctx)}
        out += ["", f"  {flat(m['id'])} {flat(m['name'])}" + (f" · due {flat(m['due'])}" if m["due"] else "") + f" · {progress_text(m)}"]
        if m["goal"]:
            out.append(f"    Goal: {flat(m['goal'])}")
        if m["exit"]:
            out.append(f"    Exit: {flat(m['exit'])}")
        for fid in m["features"]:
            f = feats.get(fid, {"id": fid})
            out.append(f"    feature {flat(fid)}  {flat(feature_status_text(f))}")
        by_id = {t["id"]: t for t in ts}
        for tid in m["tasks"]:
            t = by_id[tid]
            out.append(f"    task {flat(tid)}  {t['status']}  {flat(t['owner'])}{' (you)' if t['ownerAction'] else ''}  {flat(t['title'])}")
        if m["blockedBy"]:
            out.append("    Blocked by: " + ", ".join(flat(u) for u in m["blockedBy"]))
    return out
