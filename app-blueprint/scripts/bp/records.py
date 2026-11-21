"""records: verification records, criterion checks, acceptance and the protected paths (contract 4.3 to 4.8, 7.2).

What a feature's criteria have shown is kept in `.blueprint/verification/<feature-id>.jsonl`, one record per line, append
only. A `check` record says that a command criterion's `verify` command ran, against which files and with what result;
an `evaluation` record says that someone independent exercised the feature and what they saw. Nothing is stored about
what counts: `context` derives `ctx["checks"]` from the records on every run and `features.derive_status` turns that into
a status, so a record that is stale, damaged, too weak or from the wrong person never makes anything look more done than
its evidence (4.3). Entry and evaluation always agree because both are read from the same record.

The module is fail closed throughout: a line it cannot read is ignored (and counted), an id spelled differently from the
roster's is not the same id, and a criterion that cannot be told apart from another is never recorded as checked. A
record file is a log of events: every record carries a nonce that is part of its id, so two events never share one, and
if a line really is repeated (a hand copy) the last copy holds the position. Among the evaluations that still judge the
files and criteria as they are, the latest decides whoever wrote it; its failures always count, its passes only while
its author is independent. An evaluator can pin the files it looked at (`accept --proof`).

Every write happens inside `core.state_lock` after the record is built (checks run, proofs hashed, the evaluation
computed) and never while a check runs; a record is one whole line and its log line goes in the same hold (decision 39).
Hooks: context, refuse, verify_feature. This module registers no validation code.
"""
import json
import os
import re
import stat
import subprocess
import sys
import time

from bp import core, features, status
from bp.core import (BlueprintError, bp_dir, canonical_json, content_id, norm, now_iso, path_key, run_hint,
                     sha256_bytes)
from bp.learn import redact
from bp.plan import features_of, owner_of, section_index
from bp.version import TOOL_VERSION

VERIFICATION = "verification"
MODES = {"browser": "e2e", "device": "e2e", "cli": "e2e", "api": "integration", "manual": "manual", "code-only": "unit"}
RESULT_ARGS = ("pass", "fail", "skipped", "unknown")  # what accept --result may say
RECORD_ID = re.compile(r"vr-[0-9a-f]{12}")
EVIDENCE_ID = re.compile(r"ev-[0-9a-f]{16}")
NONCE = re.compile(r"[0-9a-f]{8}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
SAFE_WORD = re.compile(r"[A-Za-z0-9._-]+")
VERDICTS = ("pass", "fail", "partial")
MAX_NOTE = 500
MAX_REPEAT = 100
MAX_LINE = 4_000_000  # a record line longer than this is damage, not a record
MAX_FILE = 256_000_000
DEFAULT_TIMEOUT = 900
BP_ENTRY = ".blueprint/"
_LINE_BREAKS = {chr(0x2028): "\\u2028", chr(0x2029): "\\u2029", chr(0x85): "\\u0085"}  # raw in JSON, but str.splitlines splits on them
_RX_UNC = re.compile(r"(?<![\w\\])\\\\[A-Za-z0-9_.$-]+\\[^\s'\"<>|]*")


# ------------------------------------------------------------------------------------------------ small readers

def _dicts(v):
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _s(v):
    return v if isinstance(v, str) else ""


def _blank(v):
    return not (isinstance(v, str) and v.strip())


def _has_verify(c):
    return not _blank(c.get("verify"))


def _error(error, message, entry=None, next=None, code=1):  # noqa: A002 (next is the contract's name)
    return BlueprintError(error, message, entry=entry, next=next, code=code)


def _bp(root, args, module=None):
    """A console next step that runs as printed (core.run_hint); the copy form when no root is known."""
    return run_hint(root, args, module) if root is not None else f"python .blueprint/bin/blueprint.py {args}"


def _word(text):
    """text when it is safe to put in a command line (a plan id made of plain characters), else None."""
    return text if isinstance(text, str) and SAFE_WORD.fullmatch(text) else None


# ------------------------------------------------------------------------------------------- the plan's criteria

def criterion_digest(criterion):
    """sha256 of the canonical criterion object as it is now (4.3): any edit of the criterion, whatever the field,
    changes it. A criterion that cannot be serialised (nested absurdly deep) raises ValueError and never counts."""
    if not isinstance(criterion, dict) or core.json_depth(criterion) > 80:
        raise ValueError("criterion cannot be digested")
    try:
        return sha256_bytes(canonical_json(criterion))
    except (TypeError, ValueError, RecursionError) as e:
        raise ValueError("criterion cannot be digested") from e


def _digest_or_none(c):
    try:
        return criterion_digest(c)
    except ValueError:
        return None


def _criteria(feature):
    """[(criterion, id or None, identified)] for every criterion object of a feature, in order. A criterion is
    identified only when it has a non-empty string id that no other criterion of the feature shares (4.3)."""
    raw = [c for c in (feature.get("criteria") if isinstance(feature.get("criteria"), list) else [])]
    counts = {}
    for c in raw:
        if isinstance(c, dict) and isinstance(c.get("id"), str) and c["id"]:
            counts[c["id"]] = counts.get(c["id"], 0) + 1
    out = []
    for c in raw:
        c = c if isinstance(c, dict) else {}
        cid = c["id"] if isinstance(c.get("id"), str) and c["id"] else None
        out.append((c, cid, cid is not None and counts[cid] == 1))
    return out


def _command_counts(feature, c, level):
    """True when a command result at `level` counts for criterion c (2.9): a known command level, capped at the
    criterion's verifyLevel (integration without one), reaching the level it requires; never manual."""
    need = features.criterion_level(feature, c)
    vl = c.get("verifyLevel")
    if vl is not None and not (isinstance(vl, str) and vl in features.COMMAND_LEVELS):
        return False
    cap = "integration" if vl is None else vl
    if not isinstance(level, str) or level not in features.COMMAND_LEVELS:
        return False
    eff = level if features.LEVEL_RANK[level] <= features.LEVEL_RANK[cap] else cap
    return need in features.COMMAND_LEVELS and features.LEVEL_RANK[eff] >= features.LEVEL_RANK[need]


def _unrunnable(feature, c, identified):
    """Why a criterion with verify cannot be checked by a command, or None. Such a criterion is never recorded."""
    if not identified:
        return "it has no id of its own (missing, empty or shared with another criterion)"
    vl = c.get("verifyLevel")
    if vl is not None and not (isinstance(vl, str) and vl in features.COMMAND_LEVELS):
        return "its verifyLevel must be unit, integration or e2e"
    need = features.criterion_level(feature, c)
    if need == "manual":
        return "level manual needs an evaluation, a command never satisfies it"
    if need not in features.LEVELS:
        return "its level must be unit, integration, e2e or manual"
    if _digest_or_none(c) is None:
        return "it cannot be read"
    return None


# --------------------------------------------------------------------------------------------- the files of a feature

def _is_safe_rel(p):
    return bool(p) and not p.startswith("/") and not re.match(r"^[A-Za-z]:", p) and ".." not in p.split("/")


def _item_index(plan, state):
    """{feature id: sorted item paths}: the files of the plan that name the feature (action other than delete), the
    recorded instances of the patterns that name it (the glob itself while a pattern has none): the paths
    features.feature_items gives, without computing their status, in one pass over the plan."""
    by = {}
    for f in _dicts(plan.get("files")):
        if isinstance(f.get("path"), str) and f.get("action") != "delete":
            p = norm(f["path"])
            for fid in set(features_of(f)):
                by.setdefault(fid, set()).add(p)
    pats = state.get("patterns") if isinstance(state, dict) and isinstance(state.get("patterns"), dict) else {}
    for p in _dicts(plan.get("patterns")):
        rec = pats.get(p.get("id")) if isinstance(pats.get(p.get("id")), dict) else {}
        inst = sorted({norm(i) for i in (rec.get("instances") if isinstance(rec.get("instances"), list) else [])
                       if isinstance(i, str) and i})
        for fid in set(features_of(p)):
            by.setdefault(fid, set()).update(inst or [_s(p.get("glob"))])
    return {fid: sorted(paths) for fid, paths in by.items()}


def _present(root, paths):
    out = []
    for p in paths:
        rel = norm(p)
        if not _is_safe_rel(rel):
            continue
        try:
            if (root / rel).is_file():
                out.append(rel)
        except (OSError, ValueError):  # a name the file system cannot hold
            continue
    return out


def _now_files(root, plan, state, fid, index=None):
    """The files of a feature's proof as they are now: proof_inputs of the item paths that exist (4.5)."""
    paths = (index if index is not None else _item_index(plan, state)).get(fid, [])
    present = _present(root, paths)
    return status.proof_inputs(root, plan, present) if present else {}


def feature_proof(root, plan, state, comp, fid):
    """{"files": {path: digest}}: the proof a record carries (4.5). Current while the same set of files hashes to the
    same digests; adding a file to the feature, or editing an item or any dependency, makes it stale."""
    return {"files": _now_files(root, plan, state, fid)}


def _proof_current(proof, now_files):
    """The record's proof is exactly {"files": {...}}, not empty, and equal to the files hashed now."""
    return (isinstance(proof, dict) and set(proof) == {"files"} and isinstance(proof["files"], dict)
            and bool(proof["files"]) and proof["files"] == now_files)


# ------------------------------------------------------------------------------------------------------ the roster

def _roster_entries(plan, rid):
    """The roster entries whose id is exactly rid (whole value, nothing stripped); a blank id is nobody."""
    if not isinstance(rid, str) or not rid.strip():
        return []
    return [r for r in _dicts(plan.get("roster")) if r.get("id") == rid]


def in_roster(plan, rid):
    return bool(_roster_entries(plan, rid))


def _roster_ids(plan):
    out = []
    for r in _dicts(plan.get("roster")):
        if isinstance(r.get("id"), str) and r["id"] and r["id"] not in out:
            out.append(r["id"])
    return out


def independence_problem(plan, state, fid, by, index=None):
    """Why `by` may not evaluate the feature, or None: the rule is features.independence_problem, the one behind the
    ROSTER.md table too (4.4): a roster id that is not the owner of the feature, owns no item and is not recorded as
    having claimed or built one."""
    feature = section_index(plan, "features").get(fid) or {}
    paths = (index if index is not None else _item_index(plan, state)).get(fid, [])
    return features.independence_problem(plan, dict(feature, id=fid), [{"path": p} for p in paths], by, state)


def independent_ids(plan, state, fid):
    feature = section_index(plan, "features").get(fid) or {}
    paths = _item_index(plan, state).get(fid, [])
    return features.independent_ids(plan, dict(feature, id=fid), [{"path": p} for p in paths], None, state)


# ---------------------------------------------------------------------------------------------------- the records

def _int(v, low=0):
    return type(v) is int and v >= low


def _valid_proof(p):
    return (isinstance(p, dict) and set(p) == {"files"} and isinstance(p["files"], dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in p["files"].items()))


def _valid_record(rec, fid=None):
    """True when rec has every field of its kind (4.6) with the right type. A record that fails this is damage."""
    try:
        if not isinstance(rec, dict) or not isinstance(rec.get("id"), str) or not RECORD_ID.fullmatch(rec["id"]):
            return False
        if rec.get("kind") not in ("check", "evaluation"):
            return False
        if not isinstance(rec.get("feature"), str) or (fid is not None and rec["feature"] != fid):
            return False
        if not (isinstance(rec.get("at"), str) and isinstance(rec.get("tool"), str)):
            return False
        if not (isinstance(rec.get("nonce"), str) and NONCE.fullmatch(rec["nonce"])):
            return False
        if not (rec.get("by") is None or isinstance(rec["by"], str)) or not (rec.get("note") is None or isinstance(rec["note"], str)):
            return False
        if not _valid_proof(rec.get("proof")):
            return False
        if rec["kind"] == "check":
            rep = rec.get("repeat")
            return (isinstance(rec.get("criterion"), str) and rec["criterion"] != ""
                    and isinstance(rec.get("criterionDigest"), str) and DIGEST.fullmatch(rec["criterionDigest"]) is not None
                    and isinstance(rec.get("cmd"), str) and rec.get("result") in features.RESULTS
                    and rec.get("level") in features.COMMAND_LEVELS
                    and (rec.get("exit") is None or type(rec["exit"]) is int) and _int(rec.get("durationMs"))
                    and isinstance(rep, dict) and set(rep) == {"n", "passed"} and _int(rep["n"], 1)
                    and _int(rep["passed"]) and rep["passed"] <= rep["n"]
                    and (rec["result"] != "pass" or (rep["passed"] == rep["n"] and rec.get("exit") == 0)))  # pass: every run, exit 0
        crit = rec.get("criteria")
        return (rec.get("verdict") in VERDICTS and rec.get("mode") in MODES and rec.get("level") == MODES[rec["mode"]]
                and isinstance(crit, dict)
                and all(isinstance(k, str) and isinstance(v, dict) and v.get("result") in RESULT_ARGS
                        and (v.get("note") is None or isinstance(v["note"], str))
                        and isinstance(v.get("digest"), str) and DIGEST.fullmatch(v["digest"]) is not None
                        for k, v in crit.items())
                and isinstance(rec.get("evidence"), list)
                and all(isinstance(e, str) and EVIDENCE_ID.fullmatch(e) for e in rec["evidence"])
                and rec.get("independent") is True)
    except (TypeError, KeyError, AttributeError):
        return False


def _refuse_constant(name):
    raise ValueError(f"{name} is not valid JSON")


def _rec_id(rec):
    return content_id("vr-", {k: v for k, v in rec.items() if k != "id"}, 12)


def _is_linked(path):
    """A symlink, or on Windows a junction or any reparse point: the tool never writes (or trusts) through one."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def records_path(root, fid):
    if not (isinstance(fid, str) and features.FEATURE_ID.fullmatch(fid)):
        raise ValueError("not a feature id")
    return bp_dir(root) / VERIFICATION / f"{fid}.jsonl"


def read_records(root, fid):
    """(records, damaged): the readable records of one feature in file order, and how many non-blank lines were
    ignored. A line is damaged when it is truncated, not UTF-8, not JSON, nested too deeply, holds NaN or Infinity,
    is not a whole record of its kind (4.6, wrong types included), names another feature, or carries an id that is not
    the content id of the record (a hand edit); the first of two records with one id wins. A file that is a link or
    not a regular file is read as empty and counted damaged. Never raises."""
    try:
        path = records_path(root, fid)
    except ValueError:
        return [], 0
    try:
        if not os.path.lexists(path):
            return [], 0
        if _is_linked(path) or _is_linked(path.parent) or not path.is_file() or path.stat().st_size > MAX_FILE:
            return [], 1
        out, damaged = {}, 0
        with open(path, "rb") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                if len(line) > MAX_LINE:
                    damaged += 1
                    continue
                try:
                    rec = json.loads(line.decode("utf-8"), parse_constant=_refuse_constant)
                except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
                    damaged += 1
                    continue
                if not _valid_record(rec, fid) or _rec_id(rec) != rec["id"]:
                    damaged += 1
                    continue
                out.pop(rec["id"], None)  # the same line twice (a hand copy): the last one holds the position
                out[rec["id"]] = rec
        return list(out.values()), damaged
    except (OSError, ValueError, RecursionError):
        return [], 1


def load_records(root):
    """{feature id: [records]} for every feature that has a verification file; damaged lines are ignored."""
    folder = bp_dir(root) / VERIFICATION
    out = {}
    try:
        names = sorted(e.name for e in os.scandir(folder) if e.name.endswith(".jsonl"))
    except OSError:
        return out
    for name in names:
        fid = name[:-len(".jsonl")]
        if features.FEATURE_ID.fullmatch(fid):
            recs, _ = read_records(root, fid)
            if recs:
                out[fid] = recs
    return out


def _clean_note(text, root=None):
    """A note as stored: valid text (no lone surrogates), the project folder replaced in either separator (and in any
    case on Windows), then learn.redact plus UNC paths, at most 500 characters. What else counts as a secret (a
    password=..., a key) is learn.redact's scope, not promised here: put no secret in a note."""
    if text is None:
        return None
    s = str(text).encode("utf-8", "replace").decode("utf-8", "replace")
    if root is not None:
        flags = re.IGNORECASE if os.name == "nt" else 0  # Windows paths are not case-sensitive
        for spelling in {str(root), root.as_posix(), str(root.resolve()), root.resolve().as_posix()}:
            if len(spelling) > 3:
                parts = [re.escape(x) for x in re.split(r"[\\/]+", spelling) if x]
                lead = "[\\\\/]" if spelling[0] in "\\/" else ""
                s = re.sub(lead + "[\\\\/]+".join(parts), "<project>", s, flags=flags)
    s = _RX_UNC.sub("<path>", redact(s, root))
    s = s.strip()
    return s[:MAX_NOTE] or None


def _dump_line(rec):
    """One record as one whole line: JSON on a single line, with the three characters that str.splitlines would split
    on written as escapes, so no reader can tear it."""
    text = json.dumps(rec, ensure_ascii=False)
    for ch, esc in _LINE_BREAKS.items():
        text = text.replace(ch, esc)
    try:
        return (text + "\n").encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate somewhere: escape everything instead of writing bad UTF-8
        return (json.dumps(rec, ensure_ascii=True) + "\n").encode("utf-8")


def _write_lines(root, fid, recs):
    """Append whole lines to the feature's file; the caller holds the state lock. Never through a link; a file whose
    last line was torn by an interrupted write gets a line break first, so the new record starts on its own line."""
    folder = bp_dir(root) / VERIFICATION
    path = records_path(root, fid)
    rel = f".blueprint/{VERIFICATION}/{fid}.jsonl"
    try:
        if _is_linked(folder) or _is_linked(path):
            raise _error("write-failed", f"cannot write {rel}: it is a link, and the tool never writes through a link",
                         entry=rel, next=f"replace the link with a plain file or folder, then {core.AGAIN}")
        folder.mkdir(parents=True, exist_ok=True)
        if not folder.is_dir() or (path.exists() and not path.is_file()):
            raise _error("write-failed", f"cannot write {rel}: something that is not a file is in the way", entry=rel,
                         next=f"remove or rename it, then {core.AGAIN}")
        if path.exists() and path.stat().st_nlink > 1:
            raise _error("write-failed", f"cannot write {rel}: it has more than one hard link, so the tool would also write "
                         "to another file", entry=rel, next=f"copy it to a new file, remove the old one, then {core.AGAIN}")
        prefix = b""
        if path.exists() and path.stat().st_size:
            with open(path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    prefix = b"\n"
        data = prefix + b"".join(_dump_line(r) for r in recs)
        with open(path, "ab") as f:
            f.write(data)
    except OSError as e:
        raise _error("write-failed", f"cannot write {rel}: {e.strerror or e}", entry=rel,
                     next=f"make it writable, then {core.AGAIN}") from None


def _finish(rec):
    """rec with its nonce (8 random hex, part of what the id is computed from, so two events never share an id) and
    its id: content_id of the record without it (4.6). Returns a new dict, id first."""
    body = {k: v for k, v in rec.items() if k != "id"}
    body.setdefault("nonce", os.urandom(4).hex())
    return dict({"id": content_id("vr-", body, 12)}, **body)


def _commit(root, fid, decide):
    """The one write path: take the state lock, run decide() inside it (it may raise, so nothing is written), append
    the records it returns as whole lines and its events as log lines, release. Returns what decide returned."""
    with core.state_lock(root):
        recs, events = decide()
        if recs:
            _write_lines(root, fid, recs)
        for event in events:
            core.append_log(root, event)
    return recs


def append_record(root, rec):
    """Add one record to its feature's file under the state lock and return it with its id (and `at` and `tool` when
    missing). A record that is not whole (4.6) is a ValueError and nothing is written."""
    rec = dict(rec)
    rec.setdefault("at", now_iso())
    rec.setdefault("tool", TOOL_VERSION)
    rec = _finish(rec)
    if not _valid_record(rec):
        raise ValueError("not a whole verification record")
    records_path(root, rec["feature"])  # ValueError for a feature id that cannot name a file
    _commit(root, rec["feature"], lambda: ([rec], []))
    return rec


# ------------------------------------------------------------------------------------------------ checks and status

def _entry(result="unknown", level=None, at=None, stale=False, record_id=None, via=None, repeat=None):
    return {"result": result, "level": level, "at": at, "stale": stale, "recordId": record_id, "via": via,
            "repeat": repeat}


def _command_entry(feature, c, cid, cands, now_files):
    """The entry of a command criterion: the latest check record that is current (same files, the criterion as it is
    now, the same command) decides; with none current, the latest record is shown stale; with none, unknown."""
    if not cands:
        return _entry()
    digest, cmd = _digest_or_none(c), c.get("verify")
    for rec in reversed(cands):
        if (digest is not None and rec["criterionDigest"] == digest and rec["cmd"] == cmd
                and _proof_current(rec["proof"], now_files())):
            return _entry(rec["result"], rec["level"], rec["at"], False, rec["id"], "command", dict(rec["repeat"]))
    rec = cands[-1]
    return _entry(rec["result"], rec["level"], rec["at"], True, rec["id"], "command", dict(rec["repeat"]))


def _evaluation_entry(cid, evaluation):
    named = evaluation.get("criteria") if isinstance(evaluation, dict) else None
    r = named.get(cid) if isinstance(named, dict) else None
    if isinstance(r, dict) and r.get("result") in features.RESULTS:
        return _entry(r["result"], evaluation.get("level"), evaluation.get("at"), False, evaluation.get("id"), "evaluation")
    return _entry()


def _lazy_now(root, plan, state, fid, index):
    cache = []

    def now():
        if not cache:
            cache.append(_now_files(root, plan, state, fid, index))
        return cache[0]
    return now


def criterion_results(root, plan, state, comp, fid, recs, evaluation, _index=None, _now=None):
    """{criterion id: entry} for every identified criterion of the feature (the `criteria` of ctx["checks"][fid]):
    {"result", "level", "at", "stale", "recordId", "via", "repeat"} with `stale` always written. A command criterion
    reads its check records, a criterion without verify the current evaluation."""
    feature = section_index(plan, "features").get(fid)
    if feature is None:
        return {}
    now = _now or _lazy_now(root, plan, state, fid, _index)
    by_cid = {}
    for rec in recs or []:
        if isinstance(rec, dict) and rec.get("kind") == "check":
            by_cid.setdefault(rec.get("criterion"), []).append(rec)
    out = {}
    for c, cid, identified in _criteria(feature):
        if not identified:
            continue
        out[cid] = (_command_entry(feature, c, cid, by_cid.get(cid, ()), now) if _has_verify(c)
                    else _evaluation_entry(cid, evaluation))
    return out


def _verdict(feature, ev):
    """pass, fail or partial (4.3): fail when any criterion result is fail; pass when every criterion without verify
    has pass at a level that satisfies it (when all have verify, at least one pass at a satisfying level); an
    unidentified criterion can never be passed, so it keeps the verdict at partial."""
    named = ev.get("criteria") if isinstance(ev.get("criteria"), dict) else {}
    if any(isinstance(v, dict) and v.get("result") == "fail" for v in named.values()):
        return "fail"
    crits = _criteria(feature)
    if not crits or any(not ok for _, _, ok in crits):
        return "partial"

    def passed(c, cid):
        r = named.get(cid)
        return isinstance(r, dict) and r.get("result") == "pass" and features.level_ok(ev.get("level"), features.criterion_level(feature, c))
    without = [(c, cid) for c, cid, _ in crits if not _has_verify(c)]
    if without:
        return "pass" if all(passed(c, cid) for c, cid in without) else "partial"
    return "pass" if any(passed(c, cid) for c, cid, _ in crits) else "partial"


def _evaluation_current(root, plan, state, fid, feature, rec, now_files):
    """The evaluation judges the files as they are and the criteria as they are (4.3): its proof is current and every
    criterion it names still exists once with the recorded digest. Who wrote it is judged separately (the passes of an
    author who is not independent count for nothing, `latest_evaluation`)."""
    if not _proof_current(rec["proof"], now_files()):
        return False
    by_id = {cid: c for c, cid, ok in _criteria(feature) if ok}
    for cid, r in rec["criteria"].items():
        c = by_id.get(cid)
        if c is None or _digest_or_none(c) != r["digest"]:
            return False
    return True


def latest_evaluation(root, plan, state, comp, fid, recs, _index=None, _now=None):
    """The current evaluation of a feature, or None (4.3): among the evaluations whose proof is current and whose every
    named criterion still exists once with the recorded digest, the LATEST decides, whoever wrote it, and nothing older
    is consulted. Its fail, skipped and unknown results always count; its passes count only while its author is a
    roster id that is independent now (a pass of anyone else is read as unknown, so the feature is not accepted until
    a new evaluation is recorded). Returns a copy of the record with its verdict recomputed."""
    feature = section_index(plan, "features").get(fid)
    if feature is None:
        return None
    index = _index if _index is not None else _item_index(plan, state)
    now = _now or _lazy_now(root, plan, state, fid, index)
    for rec in reversed([r for r in recs or [] if isinstance(r, dict) and r.get("kind") == "evaluation"]):
        if not _evaluation_current(root, plan, state, fid, feature, rec, now):
            continue
        ev = json.loads(json.dumps(rec))
        if independence_problem(plan, state, fid, rec["by"], index) is not None:
            for r in ev["criteria"].values():
                if r["result"] == "pass":
                    r["result"] = "unknown"
        ev["verdict"] = _verdict(feature, ev)
        return ev
    return None


def _build_checks(root, plan, state, comp, fid, recs, index, with_evaluation=True):
    """ctx["checks"][fid] (1.4): the entries, commandPass and the current evaluation, all from one list of records."""
    feature = section_index(plan, "features").get(fid) or {}
    now = _lazy_now(root, plan, state, fid, index)
    ev = latest_evaluation(root, plan, state, comp, fid, recs, index, now) if with_evaluation else None
    entries = criterion_results(root, plan, state, comp, fid, recs, ev, index, now)
    command_pass = all(
        ok and cid in entries and entries[cid]["result"] == "pass" and entries[cid]["via"] == "command"
        and entries[cid]["stale"] is False and _command_counts(feature, c, entries[cid]["level"])
        for c, cid, ok in _criteria(feature) if _has_verify(c))
    return {"criteria": entries, "commandPass": bool(command_pass), "evaluation": ev}


def context(root, plan, state, ctx):
    """ctx["checks"]: {feature id: {"criteria", "commandPass", "evaluation"}} for every feature of the plan (1.4), read
    fail closed by features. Cost: one read per feature that has a verification file, one hash of its files only when
    it has records, and one digest per criterion."""
    index = _item_index(plan, state)
    folder = bp_dir(root) / VERIFICATION
    try:
        have = {e.name[:-len(".jsonl")] for e in os.scandir(folder) if e.name.endswith(".jsonl")}
    except OSError:
        have = set()
    out = {}
    for fid, feature in section_index(plan, "features").items():
        if feature.get("draft") is True:
            out[fid] = {"criteria": {}, "commandPass": False, "evaluation": None}
            continue
        recs = read_records(root, fid)[0] if fid in have else []
        out[fid] = _build_checks(root, plan, state, ctx.get("comp"), fid, recs, index)
    ctx["checks"] = out


# --------------------------------------------------------------------------------------------------------- running

def _kill_tree(proc):
    """End a timed-out check and everything it started, so nothing keeps running (and writing) after its result was
    recorded as a timeout: the process group on POSIX, taskkill /T on Windows."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError, ProcessLookupError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _run_command(cmd, cwd, timeout, quiet):
    """One run of a check command through the shell: {"result": pass|fail|timeout|error, "exit", "durationMs"}. Its
    input is closed; with quiet its output goes to stderr (the JSON form keeps stdout for the envelope). A command that
    outlives the timeout is killed with everything it started."""
    started = time.monotonic()
    out = None
    if quiet:
        try:
            out = sys.stderr.fileno()
        except (AttributeError, ValueError, OSError):
            out = None
    try:
        proc = subprocess.Popen(cmd, shell=True, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out,
                                start_new_session=os.name != "nt")
    except (OSError, ValueError):
        return {"result": "error", "exit": None, "durationMs": int((time.monotonic() - started) * 1000)}
    try:
        code = proc.wait(timeout=timeout)
        result = "pass" if code == 0 else "fail"
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.wait(timeout=30)
        except subprocess.SubprocessError:
            pass
        result, code = "timeout", None
    except BaseException:
        _kill_tree(proc)
        raise
    return {"result": result, "exit": code, "durationMs": int((time.monotonic() - started) * 1000)}


def _run_group(cmd, root, timeout, repeat, quiet):
    """A command run `repeat` times: pass only when every run passed; else the first non-pass result. A timeout or an
    error ends the repeating (the count of passes then stays what it was)."""
    passed, total_ms, first_bad, exit_code = 0, 0, None, 0
    for _ in range(repeat):
        r = _run_command(cmd, root, timeout, quiet)
        total_ms += r["durationMs"]
        if r["result"] == "pass":
            passed += 1
            continue
        if first_bad is None:
            first_bad, exit_code = r["result"], r["exit"]
        if r["result"] in ("timeout", "error"):
            break
    return {"result": first_bad or "pass", "exit": exit_code if first_bad else 0, "durationMs": total_ms,
            "repeat": {"n": repeat, "passed": passed}}


def _rec_key(rec):
    rec = rec if isinstance(rec, dict) else {}
    return rec.get("state"), rec.get("by"), rec.get("at")


def _reset_since(was, now):
    """The rule verify uses for a file (4.5): a record that went back to planned, or was claimed by an owner other
    than the one recorded when the check started, makes a result from that check unsaveable (a first claim of a file
    with no record is not such a change)."""
    if now == was:
        return False
    if now[0] == "planned":
        return True
    return now[0] == "claimed" and was is not None and was[1] is not None and now[1] != was[1]


class Outcome:
    """What run_feature_checks did: the records it saved, the criteria it did not run (id, reason), the files whose
    reset made it save nothing, and whether the files changed while the checks ran."""

    def __init__(self):
        self.records, self.skipped, self.not_saved, self.changed, self.empty = [], [], {}, False, False
        self.lines = []  # (criterion id, command, record) in run order, for the console


def _feature_or_raise(root, plan, fid, allow_draft=False):
    """The feature object, or unknown-feature: the id must be listed once and have the form of a feature id, because it
    names a file and a feature that cannot be told apart cannot be recorded for; a draft is a usage error unless
    allow_draft (accept judges a draft itself)."""
    entries = [f for f in _dicts(plan.get("features")) if f.get("id") == fid] if isinstance(fid, str) else []
    next_ = _bp(root, "status --features", "features")
    if not entries:
        raise _error("unknown-feature", f"feature {fid!r} is not in the plan", entry=f"feature {fid}", next=next_)
    if len(entries) > 1:
        raise _error("unknown-feature", f"feature {fid!r} is listed {len(entries)} times in features[]; nothing can be "
                     "recorded for it until its id is unique", entry=f"feature {fid}",
                     next=f"fix .blueprint/blueprint.json (duplicate feature id {fid}), then run: {_bp(root, 'validate')}")
    if not features.FEATURE_ID.fullmatch(fid):
        raise _error("unknown-feature", f"feature id {fid!r} has the wrong form, so nothing can be recorded for it",
                     entry=f"feature {fid}",
                     next=f"fix .blueprint/blueprint.json (feature id {fid!r} has the wrong form), then run: {_bp(root, 'validate')}")
    if entries[0].get("draft") is True and not allow_draft:
        raise _error("usage", f"feature {fid} is a draft; plan its files and remove draft first", entry=f"feature {fid}",
                     next=f"fix .blueprint/blueprint.json (feature {fid} is a draft), then run: {_bp(root, 'validate')}", code=64)
    return entries[0]


def _need_roster(root, plan, by, entry):
    if not in_roster(plan, by):
        ids = _roster_ids(plan)
        raise _error("unknown-roster", f"--by {by!r} is not in the roster (roster ids: " + (", ".join(ids) or "none") + ")",
                     entry=entry, next=_bp(root, "status --features", "features"))


def check_run_arguments(root, fid, repeat, timeout):
    """--repeat is a whole number from 1 to 100 and --timeout at least 1 second, with or without --run (usage, 64)."""
    if not _int(repeat, 1) or repeat > MAX_REPEAT:
        raise _error("usage", f"--repeat must be a whole number from 1 to {MAX_REPEAT}", entry=f"feature {fid}",
                     next=_bp(root, f"verify --feature {fid} --run --repeat 3", "commands/progress"), code=64)
    if not _int(timeout, 1):
        raise _error("usage", "--timeout must be 1 second or more", entry=f"feature {fid}",
                     next=_bp(root, f"verify --feature {fid} --run", "commands/progress"), code=64)


def run_feature_checks(root, plan, fid, criterion=None, repeat=1, timeout=DEFAULT_TIMEOUT, by=None, out=None,
                       quiet=False, state=None):
    """Run the `verify` commands of a feature's criteria and save one check record per criterion (4.6).
    One run per distinct command (criteria that share a command share its runs; `repeat` runs it that many times and
    it passes only if every run does). The proof is hashed before the first run and after the last, and nothing is
    saved when the files changed meanwhile; the results of a feature whose file was marked planned, or claimed by
    another id, while the checks ran are not saved either (4.5). The checks run without the lock; the records and
    one log line are appended in one hold of it. Returns the Outcome (`.records` are the saved records)."""
    state = state if state is not None else status.load_state(root)
    feature = _feature_or_raise(root, plan, fid)
    if by is not None:
        _need_roster(root, plan, by, f"feature {fid}")
    check_run_arguments(root, fid, repeat, timeout)
    say = out or (lambda text: print(text, file=sys.stderr if quiet else sys.stdout, flush=True))
    result = Outcome()
    rows = _criteria(feature)
    if criterion is not None:
        if not any(ok and cid == criterion for _, cid, ok in rows):
            raise _error("unknown-criterion", f"{criterion!r} is not a criterion of {fid} (or it has no id of its own)",
                         entry=f"feature {fid}", next=_bp(root, f"status --feature {fid}", "features"))
        rows = [r for r in rows if r[2] and r[1] == criterion]
    runnable = []
    for c, cid, ok in rows:
        if not _has_verify(c):
            if criterion is not None:
                result.skipped.append((cid, "it has no verify command; only an independent evaluation (accept) passes it"))
            continue
        why = _unrunnable(feature, c, ok)
        if why:
            result.skipped.append((cid or "?", why))
        else:
            runnable.append((c, cid))
    if not runnable:
        return result
    index = _item_index(plan, state)
    before = _now_files(root, plan, state, fid, index)
    if not before:
        result.empty = True
        return result
    started = {p: _rec_key((state.get("files") or {}).get(p)) for p in index.get(fid, [])}  # before any check runs
    groups = {}
    for c, cid in runnable:
        groups.setdefault(c["verify"], []).append((c, cid))
    ran = {}
    for cmd in groups:  # one run per distinct command (rule 8)
        say(f"$ {cmd}")
        ran[cmd] = _run_group(cmd, root, timeout, repeat, quiet)
    after = _now_files(root, plan, state, fid, index)
    if after != before:
        result.changed = True
        return result
    recs, lines = [], []
    for cmd, members in groups.items():
        r = ran[cmd]
        for c, cid in members:
            rec = _finish({"at": now_iso(), "tool": TOOL_VERSION, "kind": "check", "feature": fid, "by": by,
                           "proof": {"files": before}, "note": None, "criterion": cid,
                           "criterionDigest": criterion_digest(c), "cmd": cmd, "result": r["result"],
                           "level": c.get("verifyLevel") if c.get("verifyLevel") is not None else "integration",
                           "exit": r["exit"], "durationMs": r["durationMs"], "repeat": dict(r["repeat"])})
            recs.append(rec)
            lines.append((cid, cmd, rec))

    def decide():
        fresh = status.load_state(root)
        files = fresh.get("files") if isinstance(fresh.get("files"), dict) else {}
        reset = {p: _rec_key(files.get(p)) for p in started if _reset_since(started[p], _rec_key(files.get(p)))}
        event = {"event": "verify-feature", "feature": fid, "by": by, "checked": len(recs),
                 "passed": sum(1 for r in recs if r["result"] == "pass"), "records": [r["id"] for r in recs[:50]]}
        if reset:
            result.not_saved.update(reset)
            event["records"], event["notSaved"] = [], sorted(reset)
            return [], [event]
        return recs, [event]
    result.records = _commit(root, fid, decide)
    if result.records:
        result.lines = lines
    return result


def _check_text(rec):
    rep = rec["repeat"]
    return f"{rec['criterion']} {rec['result']} ({rec['level']}, {rep['passed']}/{rep['n']})"


def verify_feature(args, ctx):
    """The `verify --feature F` hook (7.2): without --run, the commands that would run; with --run, run them, save
    the records and print each criterion's result. Returns (records, exit code): 0 only when every criterion with a
    command ran, was saved and passed. Criteria without verify are listed as passed only by an evaluation."""
    root, plan = ctx["root"], ctx["plan"]
    fid = args.feature
    as_json = bool(getattr(args, "json", False))

    def say(text):
        print(text, file=sys.stderr if as_json else sys.stdout, flush=True)
    feature = _feature_or_raise(root, plan, fid)
    state = ctx.get("state") if isinstance(ctx.get("state"), dict) else status.load_state(root)
    recs, damaged = read_records(root, fid)
    if damaged:
        print(f"warning: {damaged} line(s) of .blueprint/{VERIFICATION}/{fid}.jsonl are damaged and ignored", file=sys.stderr)
    run = bool(getattr(args, "run", False))
    criterion = getattr(args, "criterion", None)
    repeat = getattr(args, "repeat", None)
    repeat = 1 if repeat is None else repeat
    timeout = getattr(args, "timeout", None)
    timeout = DEFAULT_TIMEOUT if timeout is None else timeout
    by = getattr(args, "by", None)
    check_run_arguments(root, fid, repeat, timeout)
    if not run:
        digest, _ = current_proof(root, plan, state, fid)
        listed = _list_checks(root, plan, feature, fid, criterion, say)
        if as_json:  # the envelope carries the proof too (the hook may add nothing to {"records": ...} itself)
            print(json.dumps({"ok": True, "command": "verify", "data": {"records": [], "proof": digest}}, indent=2, ensure_ascii=False))
            sys.exit(0)
        say(f"proof: {digest}    # pin it with: accept {fid} --proof <this digest> ...")
        return listed, 0
    outcome = run_feature_checks(root, plan, fid, criterion, repeat, timeout, by, quiet=as_json, state=state)
    for cid, cmd, rec in outcome.lines:
        say("  " + _check_text(rec))
    for cid, why in outcome.skipped:
        say(f"  {cid} is not run: {why}")
    for c, cid, ok in _criteria(feature):
        if not _has_verify(c) and criterion is None:
            say(f"  {cid or '?'} is passed only by an independent evaluation (accept)")
    if outcome.empty:
        say(f"  none of the files that serve {fid} exist yet, so a result could never count: build them first")
        return [], 1
    if outcome.changed:
        say(f"  not recorded: the files of {fid} changed while the checks ran; run the same command again")
        return [], 1
    if outcome.not_saved:
        for p, (st, who, _) in sorted(outcome.not_saved.items()):
            what = "marked planned" if st == "planned" else "claimed"
            say(f"  {p}: not recorded: it was {what} by {who} while the check ran; run it again when it is built")
        return [], 1
    if not outcome.records and not outcome.skipped:
        say(f"  {fid} has no criterion with a verify command")
    ok = all(r["result"] == "pass" for r in outcome.records) and not outcome.skipped
    return outcome.records, 0 if ok else 1


def _list_checks(root, plan, feature, fid, criterion, say):
    groups = {}
    for c, cid, ok in _criteria(feature):
        if criterion is not None and cid != criterion:
            continue
        if _has_verify(c):
            why = _unrunnable(feature, c, ok)
            if why:
                say(f"  {cid or '?'} cannot be checked: {why}")
            else:
                groups.setdefault(c["verify"], []).append(cid)
        elif criterion is None:
            say(f"  {cid or '?'} is passed only by an independent evaluation (accept)")
    if groups:
        say(f"Checks of {fid} (add --run to run and record them):")
        for cmd, cids in groups.items():
            say(f"  $ {cmd}    # {', '.join(cids)}")
    elif criterion is None:
        say(f"{fid} has no criterion with a verify command")
    return []


# --------------------------------------------------------------------------------------------------------- accepting

def parse_results(items, root=None):
    """{criterion id: result} from `--result C1=pass` arguments: the whole value after the last `=` must be pass, fail,
    skipped or unknown, the id is taken as written, and an id given twice is a usage error."""
    out = {}
    again = _bp(root, "accept --help", "commands/records")
    for raw in items or []:
        cid, eq, value = str(raw).rpartition("=")
        if not eq or not cid or value not in RESULT_ARGS:
            raise _error("usage", f"--result must look like C1=pass (pass, fail, skipped or unknown); got {raw!r}",
                         next=again, code=64)
        if cid in out:
            raise _error("usage", f"--result names {cid!r} twice", next=again, code=64)
        out[cid] = value
    return out


def parse_evidence(items, root=None):
    out = []
    for raw in items or []:
        if not isinstance(raw, str) or not EVIDENCE_ID.fullmatch(raw):
            raise _error("usage", f"--evidence must be an evidence id such as ev-0123456789abcdef; got {raw!r}",
                         next=_bp(root, "accept --help", "commands/records"), code=64)
        if raw not in out:
            out.append(raw)
    return out


def _fallback_context(root, plan, state):
    """A context without the views: what accept needs when its caller gave none (the command always gives one)."""
    ctx = {"root": root, "plan": plan, "state": state, "comp": status.compute(root, plan, state),
           "blocked": {"phases": set(), "milestones": set(), "features": set(), "files": set(), "by": {}},
           "checks": {}, "features": []}
    try:
        from bp import unknowns
        unknowns.context(root, plan, state, ctx)
    except ImportError:
        pass
    context(root, plan, state, ctx)
    features.context(root, plan, state, ctx)
    return ctx


def _not_verified(root, ctx, plan, fid, feature):
    """None when the feature stands verified without any evaluation (4.4: every item verified and every command
    criterion passing), else the not-verified error. A blocked or draft feature is refused too."""
    fobj = next((f for f in ctx.get("features") or [] if isinstance(f, dict) and f.get("id") == fid), None)
    nxt = _bp(root, f"verify --feature {fid} --run", "commands/progress")  # fid passed the id check of _feature_or_raise
    if fobj is None:
        return _error("not-verified", f"{fid} has no status to judge (the features module is not built)", entry=f"feature {fid}",
                      next=nxt, code=3)
    if fobj.get("status") in ("blocked", "draft"):
        return _error("not-verified", f"{fid} is {fobj['status']}, so it cannot be accepted", entry=f"feature {fid}",
                      next=_bp(root, f"status --feature {fid}", "features"), code=3)
    state, comp = ctx["state"], ctx.get("comp")
    recs = read_records(root, fid)[0]
    checks0 = _build_checks(root, plan, state, comp, fid, recs, _item_index(plan, state), with_evaluation=False)
    if features.derive_status(feature, fobj.get("items") or [], checks0, False) == "verified":
        return None
    items = [i for i in fobj.get("items") or [] if isinstance(i, dict) and i.get("status") != "verified"]
    open_checks = [cid for c, cid, ok in _criteria(feature) if _has_verify(c) and not (
        ok and cid in checks0["criteria"] and checks0["criteria"][cid]["result"] == "pass"
        and checks0["criteria"][cid]["stale"] is False and checks0["criteria"][cid]["via"] == "command"
        and _command_counts(feature, c, checks0["criteria"][cid]["level"]))]
    why = []
    if items:
        why.append(f"{len(items)} file(s) not verified (e.g. {_s(items[0].get('path'))})")
    if open_checks:
        why.append("criteria without a passing, current check: " + ", ".join((x or "?") for x in open_checks[:5]))
    if any(not ok for _, _, ok in _criteria(feature)):
        why.append("a criterion has no id of its own")
    if items:  # the files come first: their proofs are what the feature's checks are recorded against
        nxt = _bp(root, "verify --run", "commands/progress")
    return _error("not-verified", f"{fid} is {fobj.get('status')}, not verified: " + ("; ".join(why) or "its checks do not hold"),
                  entry=f"feature {fid}", next=nxt, code=3)


def _refuse_acceptor(root, plan, state, fid, feature, by):
    """The not-independent refusal (exit 3), judged on the plan and state given."""
    problem = independence_problem(plan, state, fid, by)
    if problem is not None:
        who = independent_ids(plan, state, fid)
        raise _error("not-independent", f"{problem}, so {by} cannot accept it; "
                     + (f"roster ids that may: {', '.join(who)}" if who else "no roster id may: add an evaluator to the roster"),
                     entry=f"feature {fid}",
                     next=(_bp(root, f"status --feature {fid}", "features") if who else
                           f"fix .blueprint/blueprint.json (no roster id is independent of {fid}: add an evaluator to roster[]), "
                           f"then run: {_bp(root, 'validate')}"), code=3)


def proof_digest(files):
    """The digest an evaluator pins with `accept --proof` and sees in `verify --feature F`: sha256 of the canonical JSON
    of the feature proof's files, in the form `sha256:<64 hex>`."""
    return sha256_bytes(canonical_json({"files": files}))


def current_proof(root, plan, state, fid):
    """(digest, files) of the feature's proof as it is now."""
    files = _now_files(root, plan, state, fid)
    return proof_digest(files), files


def _proof_changed(root, fid, pinned, now):
    return _error("proof-changed", f"the files of {fid} are not the ones you looked at (you pinned {pinned}, they are now {now}); "
                  "look again, then accept what you saw", entry=f"feature {fid}",
                  next=_bp(root, f"verify --feature {fid}", "commands/progress"))


def _check_evidence(root, fid, evidence, known):
    """Cited evidence ids must be in `known` (the evidenceIds the evidence module supplies through validate_context,
    1.4) when it is supplied: anything that is not None is a supplied value, and one that cannot answer `in` refuses."""
    if known is None or not evidence:
        return
    try:
        missing = [e for e in evidence if e not in known]
    except TypeError:
        missing = list(evidence)
    if missing:
        raise _error("evidence-not-found", f"evidence {', '.join(missing[:3])} is not in this project's evidence",
                     entry=f"feature {fid}", next=_bp(root, "evidence list", "commands/evidence"))


def record_evaluation(root, plan, fid, by, mode, results, evidence, note, ctx=None, proof=None, evidence_ids=None):
    """Record an independent evaluation of a feature and return the record (4.4, 4.6). The refusals, in order:
    unknown feature, unknown roster id, unknown criterion, evidence-not-found, not-independent, not-verified,
    proof-changed. The record is built first; then, inside the state lock, the roster, ownership and claims are read
    again and judged again on the plan and state as they are by then (and, when `proof` is given, the files are
    digested again and must still be the ones the evaluator pinned), and only then is it appended with its log line.
    `evidence_ids`: what the evidence module supplies (None when no module does)."""
    feature = _feature_or_raise(root, plan, fid, allow_draft=True)
    state = status.load_state(root)
    _need_roster(root, plan, by, f"feature {fid}")
    if mode not in MODES:
        raise _error("usage", f"--mode must be one of {', '.join(MODES)}", entry=f"feature {fid}",
                     next=_bp(root, "accept --help", "commands/records"), code=64)
    results = dict(results or {})
    by_id = {cid: c for c, cid, ok in _criteria(feature) if ok}
    for cid in results:
        if cid not in by_id:
            raise _error("unknown-criterion", f"{cid!r} is not a criterion of {fid} (or it has no id of its own)",
                         entry=f"feature {fid}", next=_bp(root, f"status --feature {fid}", "features"))
    ctx = ctx or _fallback_context(root, plan, state)
    _check_evidence(root, fid, evidence, evidence_ids)
    _refuse_acceptor(root, plan, state, fid, feature, by)
    problem = _not_verified(root, ctx, plan, fid, feature)
    if problem is not None:
        raise problem
    if proof is not None:
        now, _ = current_proof(root, plan, state, fid)
        if now != proof:
            raise _proof_changed(root, fid, proof, now)
    level = MODES[mode]
    rec = {"at": now_iso(), "tool": TOOL_VERSION, "kind": "evaluation", "feature": fid, "by": by,
           "proof": feature_proof(root, plan, state, ctx.get("comp"), fid), "note": _clean_note(note, root),
           "verdict": "partial", "mode": mode, "level": level,
           "criteria": {cid: {"result": r, "note": None, "digest": criterion_digest(by_id[cid])} for cid, r in results.items()},
           "evidence": list(evidence or []), "independent": True}
    rec["verdict"] = _verdict(feature, rec)
    rec = _finish(rec)

    def decide():
        from bp.plan import load_plan  # the plan and the state as they are now, inside the lock
        fresh_plan, fresh_state = load_plan(root), status.load_state(root)
        fresh_feature = _feature_or_raise(root, fresh_plan, fid, allow_draft=True)
        _need_roster(root, fresh_plan, by, f"feature {fid}")
        _refuse_acceptor(root, fresh_plan, fresh_state, fid, fresh_feature, by)
        out = rec
        if proof is not None:  # the files, digested again under the lock (unchanged files come from the hash cache)
            now, files = current_proof(root, fresh_plan, fresh_state, fid)
            if now != proof:
                raise _proof_changed(root, fid, proof, now)
            if files != rec["proof"]["files"]:
                out = _finish({k: v for k, v in dict(rec, proof={"files": files}).items() if k not in ("id", "nonce")})
        return [out], [{"event": "accept", "feature": fid, "by": by, "mode": mode, "verdict": out["verdict"], "record": out["id"]}]
    saved = _commit(root, fid, decide)
    return saved[0]


# -------------------------------------------------------------------------------------------------- protected paths

PROTECTED_FILES = ("state.json", "state.lock", ".gitignore", "log.jsonl")
PROTECTED_FOLDERS = (VERIFICATION, "bin")
GENERATED_VIEWS = ("STRUCTURE.md", "ROSTER.md", "ROADMAP.md", "FEATURES.md", "map.html")


def _protected_command(root, plan, parts):
    """The command that writes a protected path (a console text, run as printed), from what the plan confirms: a
    feature id from the file name is used only when the plan has that feature."""
    head = parts[0].casefold()
    if head in {n.casefold() for n in ("state.json", core.STATE_LOCK, ".gitignore", "log.jsonl")}:
        return _bp(root, "status")
    if head in {n.casefold() for n in GENERATED_VIEWS}:
        return _bp(root, "render")
    if head == "bin":
        return _bp(root, "init --refresh-tool")
    if head == "studies":
        return _bp(root, "study list", "commands/study")
    name = parts[1] if len(parts) == 2 else ""
    fid = name[:-len(".jsonl")] if name.endswith(".jsonl") else None
    if fid and _word(fid) and isinstance(plan, dict) and fid in section_index(plan, "features"):
        return _bp(root, f"verify --feature {fid} --run", "commands/progress")
    return _bp(root, "status --features", "features")


def protected(path):
    """True when `path` (core.project_path's root-relative form) is a file or folder only blueprint.py writes (4.8):
    state.json, state.lock, .gitignore, log.jsonl, the generated views, everything under verification/ and bin/, and
    studies/<id>/study.json. Compared with core.path_key and then case-folded on every platform, so every spelling the
    guard resolves is covered, and a differently cased name is refused even on a case-sensitive disk (the safe side:
    a case-insensitive mount under Linux is one file)."""
    parts = [x.casefold() for x in path_key(path).split("/")] if isinstance(path, str) else []
    if len(parts) < 2 or parts[0] != ".blueprint":
        return False
    rest = parts[1:]
    if len(rest) == 1 and rest[0] in {n.casefold() for n in PROTECTED_FILES + GENERATED_VIEWS}:
        return True
    if rest[0] in {n.casefold() for n in PROTECTED_FOLDERS}:
        return True
    return len(rest) == 3 and rest[0] == "studies" and rest[2] == "study.json"


def refuse(action, root, plan, path, by):
    """The guard hook (1.4): a write to a path only blueprint.py writes (4.8) is refused with a message that names the
    command to use. Claims are not this module's business. A hook that cannot read the path refuses."""
    if action != "write":
        return None
    if not isinstance(path, str):
        return "the guard cannot tell which file this write is for, so it refuses it"
    if not protected(path):
        return None
    shown = " ".join(path.split())
    parts = norm(path).split("/")[1:]
    return f"{shown} is written only by blueprint.py. Use the command instead: {_protected_command(root, plan, parts)}"
