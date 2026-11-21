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
import unicodedata
from pathlib import Path

from bp.core import BlueprintError, norm, run_hint
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
STUDY_ID = re.compile(r"S-[a-z0-9][a-z0-9-]{0,47}")  # every id, level and date check matches the whole value (fullmatch)

LEVEL_RX = r"((level|verifyLevel) must be unit, integration(, e2e or manual| or e2e)|level manual needs an evaluation)"
CODES = [
    (r"(?s)^feature .*?: needs title, statement, kind and owner", "feature-incomplete"),
    (r"(?s)^feature .*?: kind must be add, change, fix or refactor", "feature-kind"),
    (r"(?s)^feature .*?: owner .* is not a roster entry", "feature-owner"),
    (r"(?s)^feature .*?: (milestone |set a milestone)", "feature-milestone"),
    (r"(?s)^feature .*?: no planned file or pattern serves it", "feature-no-files"),
    (r"(?s)^feature .*?: no acceptance criteria", "feature-no-criteria"),
    (r"(?s)^feature .*?: dependsOn |^feature dependency loop", "feature-dependency"),
    (r"(?s)^feature .+ is a draft", "feature-draft"),
    (r"(?s)^criterion .*?: needs scenario, action and expected", "criterion-incomplete"),
    (r"(?s)^(feature|criterion) .*?: " + LEVEL_RX, "criterion-level"),
    (r"(?s)^criterion .*?: test ", "criterion-test"),
    (r"(?s)^(file|pattern) .*: serves feature '.*', which does not exist", "file-unknown-feature"),
    (r"(?s)^(file|pattern) .*: serves no feature", "file-no-feature"),
    (r"(?s)^(file|pattern) .*: has features and infrastructure: true", "file-infra-and-feature"),
]

CODE_EXAMPLES = {
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
    """The id of a feature, or None when it has none (never a placeholder string a real id could equal)."""
    return f["id"] if isinstance(f.get("id"), str) and f["id"] else None


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
    if not isinstance(have, str) or have not in LEVEL_RANK:
        return False
    return LEVEL_RANK[have] >= (LEVEL_RANK[need] if isinstance(need, str) and need in LEVEL_RANK else 3)


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
    """The 1.x id messages (codes missing-id and duplicate-id) for objects whose id may be of any type. Returns the set
    of real ids (non-empty strings)."""
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
        if fid is None or fid in seen:
            continue  # no id: reported once by _check_ids, and no rule can name it; a duplicate: its first entry counts
        seen.add(fid)
        where = f"feature {fid}"
        draft = f.get("draft") is True
        if not FEATURE_ID.fullmatch(fid):
            E(f"feature id '{fid}' has the wrong form; use F- then lowercase letters, digits and hyphens, "
              "e.g. F-log-snag", code="bad-id", entry=where,
              next="rename the feature and every files[].features entry that names it")
        missing = [k for k in ("title", "statement", "kind", "owner") if _blank(f.get(k))]
        if missing:
            E(f"{where}: needs title, statement, kind and owner (missing: {', '.join(missing)})",
              code="feature-incomplete", entry=where, next=f"fill in {', '.join(missing)} on feature {fid} in features[]")
        if "kind" not in missing and f.get("kind") not in KINDS:
            E(f"{where}: kind must be add, change, fix or refactor (got {_s(f.get('kind'))!r})", code="feature-kind",
              entry=where, next=f"set kind on feature {fid} to add, change, fix or refactor")
        if "owner" not in missing and f["owner"] not in roster:
            E(f"{where}: owner {f['owner']!r} is not a roster entry; add it to roster[] or use an existing id",
              code="feature-owner", entry=where, next=f"set owner on feature {fid} to a roster id")
        ms = f.get("milestone")
        if milestones or ms not in (None, ""):
            if _blank(ms):
                E(f"{where}: set a milestone (the plan has milestones)", code="feature-milestone", entry=where,
                  next=f"set milestone on feature {fid} to one of: {', '.join(sorted(milestones))}")
            elif ms not in milestones:
                E(f"{where}: milestone {_s(ms)!r} does not exist; add it to milestones[] or fix the field",
                  code="feature-milestone", entry=where, next=f"set milestone on feature {fid} to an id from milestones[]")
        lv = f.get("level")
        if lv is not None and lv not in LEVELS:
            E(f"{where}: level must be unit, integration, e2e or manual (got {_s(lv)!r})", code="criterion-level",
              entry=where, next=f"set level on feature {fid} to unit, integration, e2e or manual")
        source = f.get("source") if isinstance(f.get("source"), dict) else {}
        study = source.get("study")
        if study is not None and not (isinstance(study, str) and STUDY_ID.fullmatch(study)):
            E(f"study id '{_s(study)}' has the wrong form; use S- then lowercase letters, digits and hyphens, "
              "e.g. S-acme-android", code="bad-id", entry=where, next=f"fix source.study on feature {fid}")
        if draft:
            W(f"feature {fid} is a draft" + (f" from study {study}" if isinstance(study, str) and study else "")
              + "; review it, plan its files, then remove draft", code="feature-draft", entry=where,
              next=f"plan the files of feature {fid} (files[].features), then remove draft")
        else:
            if not _list(f.get("criteria")):
                E(f"{where}: no acceptance criteria; add criteria[] with scenario, action and expected",
                  code="feature-no-criteria", entry=where,
                  next=f"add criteria[] to feature {fid}, each with scenario, action and expected")
            if fid not in served:
                E(f"{where}: no planned file or pattern serves it; add its id to files[].features, or mark the "
                  "feature draft", code="feature-no-files", entry=where,
                  next=f"add {fid} to the features of the file that builds it")
        for d in _list(f.get("dependsOn")):
            if not isinstance(d, str) or d not in ids:
                E(f"{where}: dependsOn {_s(d)!r} is not a feature; use an id from features[]", code="feature-dependency",
                  entry=where, next=f"remove or fix that entry in dependsOn of feature {fid}")
        _validate_criteria(f, fid, paths, pattern_rx, E)
    _dependency_loops(features, ids, E)
    _validate_links(plan, ids, bool(features), E)


def _validate_criteria(f, fid, paths, pattern_rx, E):
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
            E(f"{where}: needs scenario, action and expected (missing: {', '.join(missing)})",
              code="criterion-incomplete", entry=where, next=f"fill in {', '.join(missing)} on {fid}/{cid}")
        lv = c.get("level")
        if lv is not None and lv not in LEVELS:
            E(f"{where}: level must be unit, integration, e2e or manual (got {_s(lv)!r})", code="criterion-level",
              entry=where, next=f"set level on {fid}/{cid} to unit, integration, e2e or manual")
        vl = c.get("verifyLevel")
        if vl is not None and vl not in COMMAND_LEVELS:
            E(f"{where}: verifyLevel must be unit, integration or e2e (got {_s(vl)!r}); a command cannot reach manual",
              code="criterion-level", entry=where, next=f"set verifyLevel on {fid}/{cid} to unit, integration or e2e")
        if _has_verify(c) and criterion_level(f, c) == "manual":
            E(f"{where}: level manual needs an evaluation; remove verify or lower the level (a command never satisfies "
              "manual, so this criterion could never pass)", code="criterion-level", entry=where,
              next=f"remove verify from {fid}/{cid}, or set level on it (or on the feature) to unit, integration or e2e")
        test = c.get("test")
        if test is not None and (not isinstance(test, str) or not test
                                 or (norm(test) not in paths and not any(rx.match(norm(test)) for rx in pattern_rx))):
            E(f"{where}: test {_s(test)!r} is not a planned file or pattern instance; plan it in files[] or fix the path",
              code="criterion-test", entry=where, next=f"add the test file to files[] or correct test on {fid}/{cid}")


def _dependency_loops(features, ids, E):
    graph = {}
    for f in features:
        fid = _fid(f)
        if fid is not None:
            graph.setdefault(fid, [d for d in _list(f.get("dependsOn")) if isinstance(d, str) and d in ids])
    state = {}

    def visit(n, stack):
        state[n] = 1
        for d in graph.get(n, []):
            if state.get(d) == 1:
                E("feature dependency loop: " + " -> ".join(stack + [n, d]), code="feature-dependency",
                  entry=f"feature {n}", next=f"remove one dependsOn entry of that loop, e.g. {d} from feature {n}")
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
                  code="file-unknown-feature", entry=label, next=f"fix features on {label} to ids from features[]")
        if linked and infra:
            E(f"{label}: has features and infrastructure: true; choose one", code="file-infra-and-feature", entry=label,
              next=f"remove features or infrastructure from {label}")
        if has_features and not linked and not infra and item.get("action") != "delete":
            E(f"{label}: serves no feature; add features[] or set infrastructure: true", code="file-no-feature",
              entry=label,
              next=f"add features: [\"F-...\"] to {label}, or set infrastructure: true if no single feature needs it")


# ----------------------------------------------------------------------------------------------- feature items

def _index(plan, comp):
    """The lookups feature_items and the context share, built once: files and patterns by feature id, rows and
    pattern records by path or id."""
    files_by, pats_by = {}, {}
    for f in _dicts(plan.get("files")):
        if isinstance(f.get("path"), str):
            for fid in set(features_of(f)):
                files_by.setdefault(fid, []).append(f)
    for p in _dicts(plan.get("patterns")):
        for fid in set(features_of(p)):
            pats_by.setdefault(fid, []).append(p)
    return {"files": files_by, "patterns": pats_by,
            "rows": {r.get("path"): r for r in _dicts((comp or {}).get("rows"))},
            "pats": {p.get("id"): p for p in _dicts((comp or {}).get("patterns"))}}


def feature_items(root, plan, state, comp, fid, _idx=None):
    """The files that serve a feature, derived from the links stored on files and patterns (2.8): each planned file
    that names it (action other than delete) with its row status, each instance of a pattern that names it with the
    status computed for {"path": instance, "action": "create"}, and a pattern with no instance yet as one planned
    item whose path is the glob. [{"path", "status", "from"}] sorted by path; `from` is "file" or "pattern:<id>". A
    planned file with no row of comp counts as planned."""
    from bp.status import effective_status
    idx = _idx or _index(plan, comp)
    items = {}
    for f in idx["files"].get(fid, ()):
        if f.get("action") == "delete":
            continue
        path = norm(f["path"])
        status = (idx["rows"].get(path) or {}).get("status")
        items[(path, "file")] = {"path": path, "status": status if status in STATES else "planned", "from": "file"}
    for p in idx["patterns"].get(fid, ()):
        pid = _s(p.get("id"))
        instances = sorted({norm(i) for i in _list((idx["pats"].get(pid) or {}).get("instances"))
                            if isinstance(i, str) and i})
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
    """The check entry of a criterion, read fail-closed: {"result", "level", "via", "stale", ...}. A result is a pass
    only when it is exactly the string "pass"; an entry is fresh only when its stale is exactly False (a missing,
    null, string or number value is stale); a criterion with no entry at all is unknown and not stale (4.3)."""
    e = _criterion_checks(checks).get(cid)
    present = isinstance(e, dict)
    e = e if present else {}
    result = e.get("result") if isinstance(e.get("result"), str) and e["result"] in RESULTS else "unknown"
    return {"result": result, "level": e.get("level") if isinstance(e.get("level"), str) else None,
            "at": e.get("at") if isinstance(e.get("at"), str) else None, "stale": present and e.get("stale") is not False,
            "recordId": e.get("recordId") if isinstance(e.get("recordId"), str) else None,
            "via": e.get("via") if e.get("via") in ("command", "evaluation") else None,
            "repeat": dict(e["repeat"]) if isinstance(e.get("repeat"), dict) else None}


def _has_verify(c):
    return not _blank(c.get("verify"))


def _command_level(feature, c, level):
    """(satisfied, effective level, required level) for a command result at `level` (2.9): the level must be a known
    command level, is capped at the criterion's verifyLevel (integration when absent) and must reach the level the
    criterion requires; a command never satisfies manual."""
    need = criterion_level(feature, c)
    vl = c.get("verifyLevel")
    if vl is not None and not (isinstance(vl, str) and vl in COMMAND_LEVELS):
        return False, None, need  # a verifyLevel that is not exactly a command level never counts (validate reports it too)
    cap = "integration" if vl is None else vl
    if level not in COMMAND_LEVELS:
        return False, None, need
    eff = level if LEVEL_RANK[level] <= LEVEL_RANK[cap] else cap
    return need in COMMAND_LEVELS and LEVEL_RANK[eff] >= LEVEL_RANK[need], eff, need


def _command_passed(feature, c, checks):
    r = _result(checks, c["id"])
    return (r["result"] == "pass" and r["via"] == "command" and not r["stale"]
            and _command_level(feature, c, r["level"])[0])


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


def _identifiable(feature):
    """Every criterion object has a non-empty string id and no two share one (4.3: a criterion nobody can identify
    uniquely cannot be told apart in the checks, so the feature stays below verified)."""
    ids = []
    for c in _list(feature.get("criteria")):
        if not (isinstance(c, dict) and isinstance(c.get("id"), str) and c["id"]):
            return False
        ids.append(c["id"])
    return len(set(ids)) == len(ids)


def derive_status(feature, items, checks, blocked):
    """The status of a feature (4.3, first matching rule wins). `items` come from feature_items, `checks` is
    ctx["checks"][fid] and `blocked` says that an unknown blocks the feature, its milestone or the phase of one of its
    files. Anything unknown, skipped, stale, below its level, missing or malformed counts as not passed."""
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
    if lo == 4 and command_pass and crits and _identifiable(feature):                   # rule 6
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

def _blocked_sets(ctx):
    b = ctx.get("blocked") if isinstance(ctx.get("blocked"), dict) else {}
    return {"by": b.get("by") if isinstance(b.get("by"), dict) else {}, "features": _set(b.get("features")),
            "milestones": _set(b.get("milestones")), "phases": _set(b.get("phases"))}


def _blocking(bs, fid, milestone, phases):
    """(is blocked, unknown ids): the feature, its milestone or the phase of one of its files is blocked (2.6)."""
    keys = [f"feature:{fid}"] + ([f"milestone:{milestone}"] if milestone else []) + [f"phase:{p}" for p in phases]
    uids = []
    for k in keys:
        for u in _strs(bs["by"].get(k)):
            if u not in uids:
                uids.append(u)
    hit = (fid in bs["features"] or (bool(milestone) and milestone in bs["milestones"])
           or any(p in bs["phases"] for p in phases))
    return hit, uids


def _copy_source(src):
    return {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v) for k, v in src.items()}


def _check_text(row):
    """What the Check column and the card show for a criterion: `pass (integration, 3/3)` only for a command result
    that counts, `below level (...)` for a pass that does not reach its level, else the result, `stale` or `unknown`."""
    if not row["identified"]:
        return "unknown"
    if not row["verify"]:
        return "-"
    if row["stale"]:
        return "stale"
    if row["result"] != "pass":
        return row["result"]
    if row["via"] != "command":
        return "unknown"
    if not row["counted"]:
        return f"below level ({row['effectiveLevel'] or 'no level'} < {row['level']})"
    bits = [row["resultLevel"]] if row["resultLevel"] else []
    rep = row["repeat"]
    if rep and isinstance(rep.get("n"), int) and isinstance(rep.get("passed"), int):
        bits.append(f"{rep['passed']}/{rep['n']}")
    return "pass" + (f" ({', '.join(bits)})" if bits else "")


def _evaluation_text(row):
    """What the Evaluation column and the card show: the evaluation's result for this criterion, with the reason when a
    pass does not count (stale, or below the required level)."""
    if not row["identified"]:
        return "unknown"
    if row["verify"]:  # a command criterion: only what an evaluation recorded about it
        return row["evaluation"] or "-"
    if row["stale"]:
        return "stale"
    if row["counted"]:
        return f"pass ({row['resultLevel']})" if row["resultLevel"] else "pass"
    if row["result"] == "pass" and row["via"] == "evaluation":
        return f"below level ({row['resultLevel'] or 'no level'} < {row['level']})"
    if row["evaluation"] == "pass":
        if not level_ok(row["evaluationLevel"], row["level"]):
            return f"below level ({row['evaluationLevel'] or 'no level'} < {row['level']})"
        return "unknown"
    return row["evaluation"] or "unknown"


def _criteria_rows(f, checks, ev):
    """One row per criterion object of the feature, never omitting one: a criterion without a usable unique id is shown
    as `?` (or with the id it shares) and reads as unknown."""
    raw = _list(f.get("criteria"))
    counts = {}
    for c in raw:
        if isinstance(c, dict) and isinstance(c.get("id"), str) and c["id"]:
            counts[c["id"]] = counts.get(c["id"], 0) + 1
    ev_results = ev.get("criteria") if ev and isinstance(ev.get("criteria"), dict) else {}
    ev_level = ev.get("level") if ev and isinstance(ev.get("level"), str) else None
    rows = []
    for c in raw:
        c = c if isinstance(c, dict) else {}
        cid = c["id"] if isinstance(c.get("id"), str) and c["id"] else None
        identified = cid is not None and counts[cid] == 1
        has_verify = _has_verify(c)
        r = _result(checks, cid) if identified else _result({}, "")
        mine = ev_results.get(cid) if identified else None
        counted, effective = False, None
        if identified and has_verify:
            counted = _command_passed(f, dict(c, id=cid), checks)
            effective = _command_level(f, c, r["level"])[1]
        elif identified:
            counted = _evaluation_passed(f, dict(c, id=cid), checks)
        row = {"id": cid or "?", "scenario": _s(c.get("scenario")), "action": _s(c.get("action")),
               "expected": _s(c.get("expected")), "mustNot": _s(c.get("mustNot")), "level": criterion_level(f, c),
               "verify": _s(c.get("verify")) if has_verify else "", "result": r["result"], "resultLevel": r["level"],
               "resultAt": r["at"], "stale": r["stale"], "via": r["via"], "repeat": r["repeat"],
               "evaluation": (mine.get("result") if isinstance(mine, dict) and mine.get("result") in RESULTS else None),
               "evaluationLevel": ev_level, "identified": identified, "counted": counted, "effectiveLevel": effective}
        row["checkText"] = _check_text(row)
        row["evaluationText"] = _evaluation_text(row)
        rows.append(row)
    return rows


def _feature_object(root, plan, state, ctx, f, idx, bs):
    fid = f["id"]
    comp = ctx.get("comp") or {}
    checks = (ctx.get("checks") or {}).get(fid) if isinstance(ctx.get("checks"), dict) else None
    checks = checks if isinstance(checks, dict) else {}
    items = feature_items(root, plan, state, comp, fid, idx)
    phases = []
    for it in list(idx["files"].get(fid, ())) + list(idx["patterns"].get(fid, ())):
        ph = it.get("phase")
        if it.get("action") != "delete" and isinstance(ph, str) and ph not in phases:
            phases.append(ph)
    milestone = f.get("milestone") if isinstance(f.get("milestone"), str) and f.get("milestone") else None
    is_blocked, blocked_by = _blocking(bs, fid, milestone, phases)
    status = derive_status(f, items, checks, is_blocked)
    ev = checks.get("evaluation") if isinstance(checks.get("evaluation"), dict) else None
    out_c = _criteria_rows(f, checks, ev)
    unverified = [c for c in out_c if not c["verify"]]
    awaiting = sum(1 for c in unverified if not c["counted"])
    counts = {s: 0 for s in STATES}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    source = _copy_source(f["source"]) if isinstance(f.get("source"), dict) else None
    return {"id": fid, "title": _s(f.get("title")), "statement": _s(f.get("statement")), "kind": _s(f.get("kind")),
            "milestone": milestone, "owner": _s(f.get("owner")), "level": feature_level(f), "draft": f.get("draft") is True,
            "status": status,
            "statusText": status_text(status, awaiting, len(out_c), len(unverified) == len(out_c)),
            "items": items, "itemCounts": counts, "criteria": out_c, "awaitingEvaluation": awaiting,
            "evaluation": ({"verdict": ev.get("verdict"), "by": ev.get("by"), "mode": ev.get("mode"),
                            "level": ev.get("level"), "at": ev.get("at")} if ev else None),
            "blockedBy": blocked_by, "dependsOn": _strs(f.get("dependsOn")), "source": source}


def context(root, plan, state, ctx):
    """ctx["features"]: one object per feature in plan order (the shape of 8.5, plus statusText, the status with its
    awaiting-evaluation count). Reads ctx["checks"] and ctx["blocked"], which the modules before this one fill. The
    file and pattern indexes are built once, so the cost is linear in features plus files."""
    idx = _index(plan, ctx.get("comp"))
    bs = _blocked_sets(ctx)
    seen, out = set(), []
    for f in _dicts(plan.get("features")):
        if isinstance(f.get("id"), str) and f["id"] and f["id"] not in seen:
            seen.add(f["id"])
            out.append(_feature_object(root, plan, state, ctx, f, idx, bs))
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


def roster_ids(plan):
    """The roster ids once each, in roster order (a duplicated id counts once; the roster rules of the core report it)."""
    out = []
    for r in _dicts(plan.get("roster")):
        if isinstance(r.get("id"), str) and r["id"] and r["id"] not in out:
            out.append(r["id"])
    return out


def _fold(s):
    """The form in which two ids are compared for independence only (never to allow anything): compatibility
    characters, case and outer whitespace are folded, so ids that fold to one value are one person here."""
    return unicodedata.normalize("NFKC", s).strip().casefold()


def _same_person(a, b):
    return isinstance(a, str) and isinstance(b, str) and _fold(a) != "" and (a == b or _fold(a) == _fold(b))


def worked_damage(rec):
    """True when a file record's `workedBy` is present but is not a list of strings (null counts as absent), or the
    record itself is neither an object nor null: nobody can then be shown to be independent of that file."""
    if rec is None:
        return False
    if not isinstance(rec, dict):
        return True
    wb = rec.get("workedBy")
    return wb is not None and not (isinstance(wb, list) and all(isinstance(x, str) for x in wb))


def worked_by(rec):
    """Every id recorded as having claimed or marked a file: always the union of the core's `status.worked_by` answer
    (when the core has that reader), the `workedBy` list of the record and its latest `by`. A damaged `workedBy`
    contributes nothing here; `worked_damage` says it is damaged and `independence_problem` then refuses everyone."""
    ids = []
    try:
        from bp import status
        reader = getattr(status, "worked_by", None)
        if callable(reader) and isinstance(rec, dict):
            got = reader(rec)
            ids += [x for x in got if isinstance(x, str)] if isinstance(got, (list, tuple, set)) else []
    except (ImportError, TypeError, ValueError, AttributeError):
        pass
    if isinstance(rec, dict):
        if isinstance(rec.get("workedBy"), list):
            ids += [x for x in rec["workedBy"] if isinstance(x, str)]
        if isinstance(rec.get("by"), str):
            ids.append(rec["by"])
    return ids


def independence_problem(plan, feature, items, by, state=None, _roster=None):
    """Why `by` may not accept the feature, or None: the one rule behind `accept` and the ROSTER.md table (4.4).
    `by` must be a roster id (whole value, never blank), and must not be the feature's owner, the owner (by the owner
    rules) of any item, or an id recorded for any item as having claimed or marked it (`state["files"][path]`: every id
    in `workedBy`, which the core grows with each claim and mark, and the latest `by`). Independence covers the
    feature's items, not the files they depend on: someone who wrote a `dependsOn` file may still accept.
    Ids are compared whole-value, except that two ids that fold to the same value (NFKC, case, outer whitespace)
    count as one person: that can only refuse."""
    files = state.get("files") if isinstance(state, dict) and isinstance(state.get("files"), dict) else {}
    for it in items:  # a damaged record is judged first, for every id: nobody can be shown to be independent of it
        path = it.get("path") if isinstance(it, dict) else None
        if isinstance(path, str) and worked_damage(files.get(path)):
            return (f"the record of {path} in .blueprint/state.json is damaged (workedBy must be a list of ids), so nobody can be "
                    "shown to be independent of it: repair .blueprint/state.json")
    roster = _roster if _roster is not None else roster_ids(plan)
    if not isinstance(by, str) or not by.strip() or by not in roster:
        return f"{by!r} is not in the roster"
    if _same_person(by, feature.get("owner")):
        return f"{by} is the owner of {_s(feature.get('id')) or 'the feature'}"
    for it in items:
        path = it.get("path") if isinstance(it, dict) else None
        if not isinstance(path, str):
            continue
        try:
            owner = owner_of(plan, path)[0]
        except (KeyError, TypeError, AttributeError):
            owner = None
        if _same_person(by, owner):
            return f"{by} owns {path}, which serves {_s(feature.get('id')) or 'the feature'}"
        if any(_same_person(by, who) for who in worked_by(files.get(path))):
            return f"{by} claimed or built {path}, which serves {_s(feature.get('id')) or 'the feature'}"
    return None


def independent_ids(plan, feature, items, _roster=None, state=None):
    """The roster ids that may accept a feature (4.4), by independence_problem: not its owner, owning none of its
    items and not recorded as having claimed or built one. An id listed twice is one id."""
    roster = _roster if _roster is not None else roster_ids(plan)
    return [r for r in roster if independence_problem(plan, feature, items, r, state, roster) is None]


def _cell(v):
    """A table cell: one line, `&` and `<` as entities (as FEATURES.md does), a backslash escaped, then the pipe."""
    text = re.sub(r"\s+", " ", str(v if v is not None else "")).strip()
    return text.replace("&", "&amp;").replace("<", "&lt;").replace("\\", "\\\\").replace("|", "\\|")


def roster_sections(ctx):
    feats = _list(ctx.get("features"))
    if not feats:
        return []
    plan = ctx["plan"]
    roster = roster_ids(plan)
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
        who = independent_ids(plan, index.get(f["id"], {}), f["items"], roster, ctx.get("state"))
        body.append(f"| {_cell(f['id'])} | {_cell(', '.join(who)) if who else '(nobody: add an evaluator to the roster)'} |")
    sec.append(("Who may accept what", "\n".join(body)))
    return sec


def _launcher(ctx):
    """How to start the tool in text that is written into project files: `python .blueprint/bin/blueprint.py` when the
    project has its own copy, else the placeholder form with a literal <skill>. Never the real path of this machine."""
    root = ctx.get("root")
    try:
        has_copy = root is not None and (Path(root) / ".blueprint" / "bin" / "blueprint.py").is_file()
    except (OSError, ValueError, TypeError):
        has_copy = False
    return ("python .blueprint/bin/blueprint.py", "") if has_copy else ("python <skill>/scripts/blueprint.py", " --root .")


def feature_next(ctx):
    """The one-line empty state of the Features tab: the next command, in the launcher form of _launcher."""
    run, tail = _launcher(ctx)
    if ctx["plan"].get("_from") == 1:
        return ("This blueprint is schema version 1 and has no features. Run: " + run + " migrate" + tail
                + ", then add features[] with acceptance criteria and link files to them with files[].features.")
    return ("No features yet. Add features[] with acceptance criteria to blueprint.json, link files to them with "
            "files[].features, then run: " + run + " render" + tail)


def map_payload(ctx):
    return {"features": _list(ctx.get("features")), "featureStates": list(FEATURE_STATES),
            "featureGlyph": dict(FEATURE_GLYPH), "featureLabel": dict(FEATURE_LABEL), "levels": list(LEVELS),
            "featureNext": feature_next(ctx)}


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
                             next=run_hint(ctx["root"], "status --features", "features") if ctx.get("root") is not None
                             else "python .blueprint/bin/blueprint.py status --features")
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
        lines.append(f"    {c['id']} [{c['level']}] {c['checkText']}")
        lines.append(f"      Given {c['scenario']}")
        lines.append(f"      When  {c['action']}")
        lines.append(f"      Then  {c['expected']}")
        if c["mustNot"]:
            lines.append(f"      Must not: {c['mustNot']}")
        lines.append("      Check: " + (f"$ {c['verify']}" if c["verify"]
                                         else "passed only by an independent evaluation (accept)"))
        if not c["verify"] or c["evaluation"]:
            lines.append(f"      Evaluation: {c['evaluationText']}")
    ev = f["evaluation"]
    lines.append("  Latest evaluation: " + (f"{ev['by']} · {ev['mode']} · {ev['verdict']} · {str(ev['at'] or '')[:10]}"
                                            if ev else "none (a feature is accepted only by `blueprint.py accept`)"))
    return lines
