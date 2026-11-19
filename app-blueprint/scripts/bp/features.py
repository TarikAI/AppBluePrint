"""features: features[] and criteria[] rules, the file-to-feature links, feature items and the derived feature status
(contract 2.5, 2.7 to 2.9, 4.3, 7.4, 8.3 to 8.5).

A feature says what the app must do; its criteria say how to tell. Files point to the features they serve (the link is
stored on the file only, 2.8) and `feature_items` derives the other direction. The status of a feature is never stored:
`derive_status` computes it from its files and its checks on every run, and fails closed, so a check that is unknown,
skipped, stale, missing or failed never makes a feature look more done than its evidence (4.3).

Hooks: CODES, CODE_EXAMPLES, validate, context, file_tags, roster_sections, map_payload, status_lines, status_json and
status_card. FEATURES.md is the views hook of bp/views/features_md.py, which reads the ctx this module fills.
"""
import json
import re

from bp.core import BlueprintError, norm
from bp.globs import pattern_instance_rx
from bp.plan import GLYPH, STATES, features_of, owner_of, section_index

FEATURE_STATES = ("draft", "planned", "building", "built", "verified", "accepted", "failed", "blocked")
FEATURE_GLYPH = {"draft": "◇", "planned": "○", "building": "◑", "built": "●", "verified": "✔", "accepted": "◉",
                 "failed": "✖", "blocked": "▲"}
FEATURE_LABEL = {s: s for s in FEATURE_STATES}
KINDS = ("add", "change", "fix", "refactor")
LEVELS = ("unit", "integration", "e2e", "manual")
COMMAND_LEVELS = ("unit", "integration", "e2e")
LEVEL_RANK = {"unit": 1, "integration": 2, "e2e": 3, "manual": 3}
DEFAULT_LEVEL = "integration"
RESULTS = ("pass", "fail", "timeout", "skipped", "error", "unknown")
FILE_RANK = {"planned": 0, "claimed": 1, "ondisk": 2, "built": 3, "verified": 4}

FEATURE_ID = re.compile(r"F-[a-z0-9][a-z0-9-]{0,47}")
CRITERION_ID = re.compile(r"C[0-9]{1,3}")

LEVEL_RX = r"(level|verifyLevel) must be unit, integration(, e2e or manual| or e2e)"
CODES = [
    (r"^[a-z ]+ id '.*' has the wrong form", "bad-id"),  # every section's id (2.2): one owner for the whole code
    (r"^feature \S+: needs title, statement, kind and owner", "feature-incomplete"),
    (r"^feature \S+: kind must be add, change, fix or refactor", "feature-kind"),
    (r"^feature \S+: owner .* is not a roster entry", "feature-owner"),
    (r"^feature \S+: (milestone |set a milestone)", "feature-milestone"),
    (r"^feature \S+: no planned file or pattern serves it", "feature-no-files"),
    (r"^feature \S+: no acceptance criteria", "feature-no-criteria"),
    (r"^feature \S+: dependsOn |^feature dependency loop", "feature-dependency"),
    (r"^feature \S+ is a draft", "feature-draft"),
    (r"^criterion \S+: needs scenario, action and expected", "criterion-incomplete"),
    (r"^(feature|criterion) \S+: " + LEVEL_RX, "criterion-level"),
    (r"^criterion \S+: test ", "criterion-test"),
    (r"^(file|pattern) .*: serves feature '.*', which does not exist", "file-unknown-feature"),
    (r"^(file|pattern) .*: serves no feature", "file-no-feature"),
    (r"^(file|pattern) .*: has features and infrastructure: true", "file-infra-and-feature"),
]

CODE_EXAMPLES = {
    "bad-id": "feature id 'Inbox' has the wrong form; use F- then lowercase letters, digits and hyphens",
    "feature-incomplete": "feature F-x: needs title, statement, kind and owner (missing: kind)",
    "feature-kind": "feature F-x: kind must be add, change, fix or refactor (got 'big')",
    "feature-owner": "feature F-x: owner 'ghost' is not a roster entry",
    "feature-milestone": "feature F-x: milestone 'M9' does not exist",
    "feature-no-files": "feature F-x: no planned file or pattern serves it",
    "feature-no-criteria": "feature F-x: no acceptance criteria",
    "feature-dependency": "feature F-x: dependsOn 'F-y' is not a feature",
    "feature-draft": "feature F-x is a draft",
    "criterion-incomplete": "criterion F-x/C1: needs scenario, action and expected (missing: expected)",
    "criterion-level": "criterion F-x/C1: level must be unit, integration, e2e or manual (got 'big')",
    "criterion-test": "criterion F-x/C1: test 'a.rs' is not a planned file or pattern instance",
    "file-unknown-feature": "file a.rs: serves feature 'F-y', which does not exist",
    "file-no-feature": "file a.rs: serves no feature",
    "file-infra-and-feature": "file a.rs: has features and infrastructure: true",
}


# ------------------------------------------------------------------------------------------------- small readers

def _s(v):
    """A string value, else ''."""
    return v if isinstance(v, str) else ""


def _blank(v):
    return not (isinstance(v, str) and v.strip())


def _list(v):
    return v if isinstance(v, list) else []


def _strs(v):
    return [x for x in _list(v) if isinstance(x, str)]


def _set(v):
    """A set of the strings in a list or set (a malformed value from another module reads as empty)."""
    return {x for x in v if isinstance(x, str)} if isinstance(v, (list, set, tuple, frozenset)) else set()


def _dicts(v):
    return [x for x in _list(v) if isinstance(x, dict)]


def _fid(f):
    return f["id"] if isinstance(f.get("id"), str) and f["id"] else "?"


def _criteria(feature):
    """The criteria objects of a feature that have a string id, in order."""
    return [c for c in _dicts(feature.get("criteria")) if isinstance(c.get("id"), str) and c["id"]]


def feature_level(feature):
    lv = feature.get("level")
    return lv if isinstance(lv, str) and lv else DEFAULT_LEVEL


def criterion_level(feature, criterion):
    """The level a criterion needs: its own, else the feature's, else integration (2.5)."""
    lv = criterion.get("level")
    return lv if isinstance(lv, str) and lv else feature_level(feature)


def level_ok(have, need):
    """rank(have) >= rank(need) (2.9). An unknown required level is treated as the strictest; an unknown or missing
    level of a result never satisfies anything."""
    if have not in LEVEL_RANK:
        return False
    return LEVEL_RANK[have] >= LEVEL_RANK.get(need, 3)


# ------------------------------------------------------------------------------------------------------- validate

def _entry_items(plan):
    """[(label, item)] for every planned file and pattern, in plan order."""
    out = []
    for f in _dicts(plan.get("files")):
        if isinstance(f.get("path"), str):
            out.append((f"file {f['path']}", f))
    for p in _dicts(plan.get("patterns")):
        out.append((f"pattern {_s(p.get('id')) or '?'}", p))
    return out


def _check_ids(items, label, E):
    """The 1.x id messages (codes missing-id and duplicate-id) for objects whose id may be of any type."""
    seen = set()
    for it in items:
        v = it.get("id")
        if not isinstance(v, str) or not v:
            E(f"{label} without id: {json.dumps(it, default=str)[:80]}", code="missing-id",
              next=f"give the {label} an id, e.g. F-log-snag")
        elif v in seen:
            E(f"duplicate {label} id: {v}", code="duplicate-id", entry=f"{label} {v}",
              next=f"rename one of the {label}s called {v}")
        else:
            seen.add(v)
    return seen


def validate(plan, E, W, vctx):
    features = _dicts(plan.get("features"))
    ids = _check_ids(features, "feature", E)
    roster = set(section_index(plan, "roster"))
    milestones = set(section_index(plan, "milestones"))
    paths = {norm(f["path"]) for f in _dicts(plan.get("files")) if isinstance(f.get("path"), str)}
    pattern_rx = [pattern_instance_rx(p["glob"]) for p in _dicts(plan.get("patterns")) if isinstance(p.get("glob"), str)]
    served = set()
    for _, item in _entry_items(plan):
        if item.get("action") != "delete":
            served.update(features_of(item))
    seen = set()
    for f in features:
        fid = _fid(f)
        if fid in seen:
            continue  # a duplicate id is reported once; its first entry is the one that counts
        seen.add(fid)
        where = f"feature {fid}"
        draft = f.get("draft") is True
        if fid != "?" and not FEATURE_ID.fullmatch(fid):
            E(f"feature id '{fid}' has the wrong form; use F- then lowercase letters, digits and hyphens, "
              "e.g. F-log-snag", code="bad-id", entry=where, next="rename the feature and every files[].features entry that names it")
        missing = [k for k in ("title", "statement", "kind", "owner") if _blank(f.get(k))]
        if missing:
            E(f"{where}: needs title, statement, kind and owner (missing: {', '.join(missing)})",
              entry=where, next=f"fill in {', '.join(missing)} on feature {fid} in features[]")
        if "kind" not in missing and f.get("kind") not in KINDS:
            E(f"{where}: kind must be add, change, fix or refactor (got {_s(f.get('kind'))!r})", entry=where,
              next=f"set kind on feature {fid} to add, change, fix or refactor")
        if "owner" not in missing and f["owner"] not in roster:
            E(f"{where}: owner {f['owner']!r} is not a roster entry; add it to roster[] or use an existing id",
              entry=where, next=f"set owner on feature {fid} to a roster id")
        ms = f.get("milestone")
        if milestones or ms not in (None, ""):
            if _blank(ms):
                E(f"{where}: set a milestone (the plan has milestones)", entry=where,
                  next=f"set milestone on feature {fid} to one of: {', '.join(sorted(milestones))}")
            elif ms not in milestones:
                E(f"{where}: milestone {_s(ms)!r} does not exist; add it to milestones[] or fix the field", entry=where,
                  next=f"set milestone on feature {fid} to an id from milestones[]")
        lv = f.get("level")
        if lv is not None and lv not in LEVELS:
            E(f"{where}: level must be unit, integration, e2e or manual (got {_s(lv)!r})", entry=where,
              next=f"set level on feature {fid} to unit, integration, e2e or manual")
        if draft:
            study = _s((f.get("source") or {}).get("study")) if isinstance(f.get("source"), dict) else ""
            W(f"feature {fid} is a draft" + (f" from study {study}" if study else "")
              + "; review it, plan its files, then remove draft", entry=where,
              next=f"plan the files of feature {fid} (files[].features), then remove draft")
        else:
            if not _criteria(f) and not _dicts(f.get("criteria")):
                E(f"{where}: no acceptance criteria; add criteria[] with scenario, action and expected", entry=where,
                  next=f"add criteria[] to feature {fid}, each with scenario, action and expected")
            if fid not in served:
                E(f"{where}: no planned file or pattern serves it; add its id to files[].features, or mark the "
                  "feature draft", entry=where, next=f"add {fid} to the features of the file that builds it")
        for d in _list(f.get("dependsOn")):
            if not isinstance(d, str) or d not in ids:
                E(f"{where}: dependsOn {_s(d)!r} is not a feature; use an id from features[]", entry=where,
                  next=f"remove or fix that entry in dependsOn of feature {fid}")
        _validate_criteria(plan, f, fid, paths, pattern_rx, E)
    _dependency_loops(features, ids, E)
    _validate_links(plan, ids, bool(features), E)


def _validate_criteria(plan, f, fid, paths, pattern_rx, E):
    seen = set()
    for n, c in enumerate(_list(f.get("criteria"))):
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        if not isinstance(cid, str) or not cid:
            E(f"criterion without id: {fid} criteria[{n}]", code="missing-id", entry=f"feature {fid}",
              next=f"give criteria[{n}] of feature {fid} an id such as C1")
            cid = f"#{n + 1}"
        elif cid in seen:
            E(f"duplicate criterion id: {fid}/{cid}", code="duplicate-id", entry=f"criterion {fid}/{cid}",
              next=f"rename one of the criteria called {cid} in feature {fid}")
            continue
        else:
            seen.add(cid)
            if not CRITERION_ID.fullmatch(cid):
                E(f"criterion id '{fid}/{cid}' has the wrong form; use C then 1 to 3 digits, e.g. C1", code="bad-id",
                  entry=f"criterion {fid}/{cid}", next=f"rename criterion {cid} of feature {fid} to C1, C2, ...")
        where = f"criterion {fid}/{cid}"
        missing = [k for k in ("scenario", "action", "expected") if _blank(c.get(k))]
        if missing:
            E(f"{where}: needs scenario, action and expected (missing: {', '.join(missing)})", entry=where,
              next=f"fill in {', '.join(missing)} on {fid}/{cid}")
        lv = c.get("level")
        if lv is not None and lv not in LEVELS:
            E(f"{where}: level must be unit, integration, e2e or manual (got {_s(lv)!r})", entry=where,
              next=f"set level on {fid}/{cid} to unit, integration, e2e or manual")
        vl = c.get("verifyLevel")
        if vl is not None and vl not in COMMAND_LEVELS:
            E(f"{where}: verifyLevel must be unit, integration or e2e (got {_s(vl)!r}); a command cannot reach manual",
              entry=where, next=f"set verifyLevel on {fid}/{cid} to unit, integration or e2e")
        test = c.get("test")
        if test is not None and (not isinstance(test, str) or not test
                                 or (norm(test) not in paths and not any(rx.match(norm(test)) for rx in pattern_rx))):
            E(f"{where}: test {_s(test)!r} is not a planned file or pattern instance; plan it in files[] or fix the path",
              entry=where, next=f"add the test file to files[] or correct test on {fid}/{cid}")


def _dependency_loops(features, ids, E):
    graph = {}
    for f in features:
        graph.setdefault(_fid(f), [d for d in _list(f.get("dependsOn")) if isinstance(d, str) and d in ids])
    state = {}

    def visit(n, stack):
        state[n] = 1
        for d in graph.get(n, []):
            if state.get(d) == 1:
                E("feature dependency loop: " + " -> ".join(stack + [n, d]), entry=f"feature {n}",
                  next=f"remove one dependsOn entry of that loop, e.g. {d} from feature {n}")
            elif not state.get(d):
                visit(d, stack + [n])
        state[n] = 2

    for n in graph:
        if not state.get(n):
            visit(n, [])


def _validate_links(plan, ids, has_features, E):
    for label, item in _entry_items(plan):
        linked = features_of(item)
        infra = item.get("infrastructure") is True
        for fid in linked:
            if fid not in ids:
                E(f"{label}: serves feature {fid!r}, which does not exist; add it to features[] or fix the id",
                  entry=label, next=f"fix features on {label} to ids from features[]")
        if linked and infra:
            E(f"{label}: has features and infrastructure: true; choose one", entry=label,
              next=f"remove features or infrastructure from {label}")
        if has_features and not linked and not infra and item.get("action") != "delete":
            E(f"{label}: serves no feature; add features[] or set infrastructure: true", entry=label,
              next=f"add features: [\"F-...\"] to {label}, or set infrastructure: true if no single feature needs it")


# ----------------------------------------------------------------------------------------------- feature items

def feature_items(root, plan, state, comp, fid):
    """The files that serve a feature, derived from the links stored on files and patterns (2.8): each planned file
    that names it (action other than delete) with its row status, each instance of a pattern that names it with the
    status computed for {"path": instance, "action": "create"}, and a pattern with no instance yet as one planned
    item whose path is the glob. [{"path", "status", "from"}] sorted by path; `from` is "file" or "pattern:<id>"."""
    from bp.status import effective_status
    rows = {r.get("path"): r for r in _dicts((comp or {}).get("rows"))}
    pats = {p.get("id"): p for p in _dicts((comp or {}).get("patterns"))}
    items = {}
    for f in _dicts(plan.get("files")):
        path = f.get("path")
        if not isinstance(path, str) or f.get("action") == "delete" or fid not in features_of(f):
            continue
        status = (rows.get(norm(path)) or {}).get("status")
        items[(norm(path), "file")] = {"path": norm(path), "status": status if status in STATES else "planned",
                                       "from": "file"}
    for p in _dicts(plan.get("patterns")):
        if fid not in features_of(p):
            continue
        pid = _s(p.get("id"))
        instances = sorted({norm(i) for i in _list((pats.get(pid) or {}).get("instances")) if isinstance(i, str) and i})
        if not instances:
            glob = _s(p.get("glob"))
            items[(glob, "pattern:" + pid)] = {"path": glob, "status": "planned", "from": "pattern:" + pid}
        for inst in instances:
            try:
                status = effective_status(root, plan, state, {"path": inst, "action": "create"})[0]
            except (OSError, ValueError, KeyError, TypeError):
                status = "planned"
            items[(inst, "pattern:" + pid)] = {"path": inst, "status": status, "from": "pattern:" + pid}
    return [items[k] for k in sorted(items)]


# --------------------------------------------------------------------------------------------------- the status

def _criterion_checks(checks):
    c = checks.get("criteria") if isinstance(checks, dict) else None
    return c if isinstance(c, dict) else {}


def _result(checks, cid):
    """The check entry of a criterion, read fail-closed: {"result", "level", "via", "stale", ...}. A value that is
    not a known result is `unknown`; a stale entry never counts as pass (4.3)."""
    e = _criterion_checks(checks).get(cid)
    e = e if isinstance(e, dict) else {}
    result = e.get("result") if e.get("result") in RESULTS else "unknown"
    return {"result": result, "level": e.get("level") if isinstance(e.get("level"), str) else None,
            "at": e.get("at") if isinstance(e.get("at"), str) else None, "stale": e.get("stale") is True,
            "recordId": e.get("recordId") if isinstance(e.get("recordId"), str) else None,
            "via": e.get("via") if e.get("via") in ("command", "evaluation") else None,
            "repeat": e.get("repeat") if isinstance(e.get("repeat"), dict) else None}


def _has_verify(c):
    return not _blank(c.get("verify"))


def _command_passed(feature, c, checks):
    r = _result(checks, c["id"])
    return r["result"] == "pass" and r["via"] == "command" and not r["stale"]


def _evaluation_passed(feature, c, checks):
    """A criterion without verify passes only through the current evaluation, at a sufficient level (4.3)."""
    ev = checks.get("evaluation") if isinstance(checks, dict) else None
    r = _result(checks, c["id"])
    return (isinstance(ev, dict) and r["result"] == "pass" and r["via"] == "evaluation" and not r["stale"]
            and level_ok(r["level"], criterion_level(feature, c)))


def _evaluation_failed(checks):
    """Rule 3: the current evaluation gives fail for any criterion."""
    ev = checks.get("evaluation") if isinstance(checks, dict) else None
    named = ev.get("criteria") if isinstance(ev, dict) and isinstance(ev.get("criteria"), dict) else {}
    if any(isinstance(v, dict) and v.get("result") == "fail" for v in named.values()):
        return True
    return any(isinstance(v, dict) and v.get("via") == "evaluation" and v.get("result") == "fail"
               for v in _criterion_checks(checks).values())


def _evaluation_names_a_pass(feature, crits, checks):
    """When every criterion has verify: the evaluation must exist and name at least one criterion with result pass at
    a level that satisfies it (4.3 rule 6)."""
    ev = checks.get("evaluation") if isinstance(checks, dict) else None
    named = ev.get("criteria") if isinstance(ev, dict) else None
    if not isinstance(named, dict):
        return False
    return any(isinstance(named.get(c["id"]), dict) and named[c["id"]].get("result") == "pass"
               and level_ok(ev.get("level"), criterion_level(feature, c)) for c in crits)


def derive_status(feature, items, checks, blocked):
    """The status of a feature (4.3, first matching rule wins). `items` come from feature_items, `checks` is
    ctx["checks"][fid] and `blocked` says that an unknown blocks the feature, its milestone or the phase of one of its
    files. Anything unknown, skipped, stale, missing or malformed counts as not passed."""
    feature = feature if isinstance(feature, dict) else {}
    checks = checks if isinstance(checks, dict) else {}
    items = _dicts(items)
    if feature.get("draft") is True:                                                    # rule 1
        return "draft"
    if blocked or any(i.get("status") == "blocked" for i in items):                     # rule 2
        return "blocked"
    if _evaluation_failed(checks):                                                      # rule 3
        return "failed"
    ranks = [FILE_RANK.get(i.get("status"), 0) for i in items]                          # rule 4
    lo, hi = (min(ranks), max(ranks)) if ranks else (0, 0)
    crits = _criteria(feature)
    with_verify = [c for c in crits if _has_verify(c)]
    command_pass = (checks.get("commandPass") is True                                   # rule 5
                    and all(_command_passed(feature, c, checks) for c in with_verify))
    if lo == 4 and command_pass and crits:                                              # rule 6
        without = [c for c in crits if not _has_verify(c)]
        if without:
            accepted = all(_evaluation_passed(feature, c, checks) for c in without)
        else:
            accepted = _evaluation_names_a_pass(feature, crits, checks)
        return "accepted" if accepted else "verified"
    if lo >= 3:                                                                         # rule 7
        return "built"
    if hi >= 1:                                                                         # rule 8
        return "building"
    return "planned"                                                                    # rule 9


def status_text(status, awaiting, criteria_total, all_unverified):
    """The status with the awaiting-evaluation count of 4.3: `verified, 2 criteria await evaluation`."""
    if status != "verified" or awaiting <= 0:
        return status
    if all_unverified:
        return f"{status}, {awaiting} of {criteria_total} criteria await evaluation"
    return f"{status}, {awaiting} " + ("criterion awaits" if awaiting == 1 else "criteria await") + " evaluation"


# ------------------------------------------------------------------------------------------------------ context

def _blocking(ctx, fid, milestone, phases):
    """(is blocked, unknown ids): the feature, its milestone or the phase of one of its files is blocked (2.6)."""
    b = ctx.get("blocked") if isinstance(ctx.get("blocked"), dict) else {}
    by = b.get("by") if isinstance(b.get("by"), dict) else {}
    keys = [f"feature:{fid}"] + ([f"milestone:{milestone}"] if milestone else []) + [f"phase:{p}" for p in phases]
    uids = []
    for k in keys:
        for u in _strs(by.get(k)):
            if u not in uids:
                uids.append(u)
    hit = (fid in _set(b.get("features")) or (bool(milestone) and milestone in _set(b.get("milestones")))
           or bool(_set(b.get("phases")) & set(phases)))
    return hit, uids


def _feature_object(root, plan, state, ctx, f):
    fid = f["id"]
    comp = ctx.get("comp") or {}
    checks = (ctx.get("checks") or {}).get(fid) if isinstance(ctx.get("checks"), dict) else None
    checks = checks if isinstance(checks, dict) else {}
    items = feature_items(root, plan, state, comp, fid)
    phases = []
    for it in _dicts(plan.get("files")) + _dicts(plan.get("patterns")):
        ph = it.get("phase")
        if fid in features_of(it) and it.get("action") != "delete" and isinstance(ph, str) and ph not in phases:
            phases.append(ph)
    milestone = f.get("milestone") if isinstance(f.get("milestone"), str) and f.get("milestone") else None
    is_blocked, blocked_by = _blocking(ctx, fid, milestone, phases)
    status = derive_status(f, items, checks, is_blocked)
    crits = _criteria(f)
    ev = checks.get("evaluation") if isinstance(checks.get("evaluation"), dict) else None
    ev_results = ev.get("criteria") if ev and isinstance(ev.get("criteria"), dict) else {}
    out_c, awaiting = [], 0
    for c in crits:
        r = _result(checks, c["id"])
        mine = ev_results.get(c["id"])
        has_verify = _has_verify(c)
        if not has_verify and not _evaluation_passed(f, c, checks):
            awaiting += 1
        out_c.append({"id": c["id"], "scenario": _s(c.get("scenario")), "action": _s(c.get("action")),
                      "expected": _s(c.get("expected")), "mustNot": _s(c.get("mustNot")),
                      "level": criterion_level(f, c), "verify": _s(c.get("verify")) if has_verify else "",
                      "result": r["result"], "resultLevel": r["level"], "resultAt": r["at"], "stale": r["stale"],
                      "via": r["via"], "repeat": r["repeat"],
                      "evaluation": mine.get("result") if isinstance(mine, dict) and mine.get("result") in RESULTS else None})
    counts = {s: 0 for s in STATES}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    unverified = [c for c in crits if not _has_verify(c)]
    source = f.get("source") if isinstance(f.get("source"), dict) else None
    return {"id": fid, "title": _s(f.get("title")), "statement": _s(f.get("statement")), "kind": _s(f.get("kind")),
            "milestone": milestone, "owner": _s(f.get("owner")), "level": feature_level(f), "draft": f.get("draft") is True,
            "status": status,
            "statusText": status_text(status, awaiting, len(crits), len(unverified) == len(crits)),
            "items": items, "itemCounts": counts, "criteria": out_c, "awaitingEvaluation": awaiting,
            "evaluation": ({"verdict": ev.get("verdict"), "by": ev.get("by"), "mode": ev.get("mode"),
                            "level": ev.get("level"), "at": ev.get("at")} if ev else None),
            "blockedBy": blocked_by, "dependsOn": _strs(f.get("dependsOn")), "source": source}


def context(root, plan, state, ctx):
    """ctx["features"]: one object per feature in plan order (the shape of 8.5, plus statusText, the status with its
    awaiting-evaluation count). Reads ctx["checks"] and ctx["blocked"], which the modules before this one fill."""
    seen, out = set(), []
    for f in _dicts(plan.get("features")):
        if isinstance(f.get("id"), str) and f["id"] and f["id"] not in seen:
            seen.add(f["id"])
            out.append(_feature_object(root, plan, state, ctx, f))
    ctx["features"] = out


# -------------------------------------------------------------------------------------------- hooks that show it

def file_tags(ctx):
    """{path or glob: [tag]}: the feature ids a file or pattern serves, or `infra`; only when the plan has features
    (STRUCTURE.md of a plan without features is unchanged, 8.3)."""
    plan = ctx["plan"]
    if not _dicts(plan.get("features")):
        return {}
    tags = {}
    for f in _dicts(plan.get("files")):
        key = f.get("path") if isinstance(f.get("path"), str) else None
        t = features_of(f) + (["infra"] if f.get("infrastructure") is True else [])
        if key and t:
            tags[key] = t
    for p in _dicts(plan.get("patterns")):
        key = p.get("glob") if isinstance(p.get("glob"), str) else None
        t = features_of(p) + (["infra"] if p.get("infrastructure") is True else [])
        if key and t:
            tags[key] = t
    return tags


def independent_ids(plan, feature, items):
    """The roster ids that may accept a feature: not its owner and owning none of its items (4.4)."""
    owners = {owner_of(plan, it["path"])[0] for it in items}
    owner = _s(feature.get("owner"))
    return [r["id"] for r in _dicts(plan.get("roster")) if isinstance(r.get("id"), str) and r["id"] != owner
            and r["id"] not in owners]


def _cell(v):
    return str(v if v is not None else "").replace("|", "\\|").replace("\n", " ")


def roster_sections(ctx):
    feats = _list(ctx.get("features"))
    if not feats:
        return []
    plan = ctx["plan"]
    roster = list(section_index(plan, "roster"))
    owned = {}
    for f in feats:
        owned.setdefault(f["owner"], []).append(f["id"])
    sec = []
    if owned:
        body = ["| Owner | Features owned |", "|---|---|"]
        for o in [r for r in roster if r in owned] + sorted(set(owned) - set(roster)):
            body.append(f"| {_cell(o)} | {_cell(', '.join(owned[o]))} |")
        sec.append(("Features owned", "\n".join(body)))
    index = section_index(plan, "features")
    body = ["A feature is accepted by a roster id that is not its owner and owns none of its files "
            "(`blueprint.py accept`).", "", "| Feature | May accept |", "|---|---|"]
    for f in feats:
        if f["draft"]:
            continue
        who = independent_ids(plan, index.get(f["id"], {}), f["items"])
        body.append(f"| {_cell(f['id'])} | {_cell(', '.join(who)) if who else '(nobody: add an evaluator to the roster)'} |")
    sec.append(("Who may accept what", "\n".join(body)))
    return sec


def map_payload(ctx):
    return {"features": _list(ctx.get("features")), "featureStates": list(FEATURE_STATES),
            "featureGlyph": dict(FEATURE_GLYPH), "featureLabel": dict(FEATURE_LABEL), "levels": list(LEVELS)}


def status_json(ctx):
    return {"features": _list(ctx.get("features"))}


def _counts(feats):
    c = {s: 0 for s in FEATURE_STATES}
    for f in feats:
        c[f["status"]] = c.get(f["status"], 0) + 1
    return c


def status_lines(ctx, args):
    feats = _list(ctx.get("features"))
    if not feats:
        return []
    c = _counts(feats)
    lines = ["", f"  Features: {len(feats)}: " + ", ".join(f"{FEATURE_GLYPH[s]} {c[s]} {s}" for s in FEATURE_STATES if c[s])]
    if getattr(args, "features", False):
        for f in feats:
            lines.append(f"    {FEATURE_GLYPH[f['status']]} {f['id']:<24} {f['statusText']}  ({f['owner']}) {f['title']}")
            if f["blockedBy"]:
                lines.append(f"        blocked by {', '.join(f['blockedBy'])}")
    return lines


def status_card(ctx, fid):
    """The card of one feature for `status --feature F`: files, criteria with their results, the evaluation."""
    f = next((x for x in _list(ctx.get("features")) if x["id"] == fid), None)
    if f is None:
        raise BlueprintError("unknown-feature", f"feature {fid} is not in the plan", entry=f"feature {fid}",
                             next="blueprint.py status --features")
    lines = [f"{f['id']}  {f['title']}", f"  Status: {FEATURE_GLYPH[f['status']]} {f['statusText']}"]
    if f["statement"]:
        lines.append(f"  {f['statement']}")
    lines.append(f"  {f['kind']} · {f['milestone'] or 'no milestone'} · {f['owner']} · {f['level']}")
    lines.append("  Depends on: " + (", ".join(f["dependsOn"]) or "none"))
    if f["blockedBy"]:
        lines.append("  Blocked by: " + ", ".join(f["blockedBy"]))
    if f["source"] and f["source"].get("study"):
        lines.append(f"  Source: study {f['source']['study']}")
    lines.append(f"  Files ({len(f['items'])}):")
    lines += [f"    {GLYPH.get(i['status'], '?')} {i['path']}" + (f"  ({i['from']})" if i["from"] != "file" else "")
              for i in f["items"]] or ["    none: add this feature's id to files[].features"]
    lines.append(f"  Criteria ({len(f['criteria'])}):")
    for c in f["criteria"]:
        lines.append(f"    {c['id']} [{c['level']}] {check_text(c)}")
        lines.append(f"      Given {c['scenario']}")
        lines.append(f"      When  {c['action']}")
        lines.append(f"      Then  {c['expected']}")
        if c["mustNot"]:
            lines.append(f"      Must not: {c['mustNot']}")
        lines.append("      Check: " + (f"$ {c['verify']}" if c["verify"]
                                         else "passed only by an independent evaluation (accept)"))
        if c["evaluation"]:
            lines.append(f"      Evaluation: {c['evaluation']}")
    ev = f["evaluation"]
    lines.append("  Latest evaluation: " + (f"{ev['by']} · {ev['mode']} · {ev['verdict']} · {str(ev['at'] or '')[:10]}"
                                            if ev else "none (a feature is accepted only by `blueprint.py accept`)"))
    return lines


def check_text(c):
    """`pass (integration, 3/3)`, `fail`, `stale` or `unknown`: what the checks say about one criterion (8.2)."""
    if c["stale"]:
        return "stale"
    if c["result"] != "pass":
        return c["result"]
    bits = [c["resultLevel"]] if c.get("resultLevel") else []
    rep = c.get("repeat")
    if isinstance(rep, dict) and isinstance(rep.get("n"), int) and isinstance(rep.get("passed"), int):
        bits.append(f"{rep['passed']}/{rep['n']}")
    return "pass" + (f" ({', '.join(bits)})" if bits else "")
