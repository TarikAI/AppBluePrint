"""evidence: the evidence store (contract 5): record shape, content-derived ids, digests, freshness, storage, the
export bundle, and the hooks that give validate the evidence ids (validate_context) and the map its Studies tab
(context, map_payload).

Storage (5.5): .blueprint/studies/<study-id>/{study.json, evidence.jsonl, behaviours.json, ...}. These are project
files: nothing machine-specific in them, every file written atomically (a temp name unique to the process beside it,
then a replace, never through a link) inside core.state_lock, with everything that can be prepared (reading,
parsing, hashing) done before the lock is taken (1.8 rule 7, decision 39). Readers take no lock: files are replaced
whole, and evidence add appends whole lines.

Reading is fail-closed: a line that is not a JSON object with an id of the evidence form is skipped (JSON Lines rule);
a damaged study.json is the named error bad-study wherever one study is asked for, and is left out (with a warning on
stderr) where every study is listed; a record that cannot be identified or rehashed is never fresh.

Imports core, version and study.safety only (contract 1.2); asar freshness reaches it through the resolvers table.
"""
import hashlib
import json
import math
import os
import re
import secrets
import stat
import sys
from pathlib import Path

from bp import core
from bp.core import BlueprintError, bp_dir, canonical_json, content_id, now_iso, run_hint, sha256_bytes
from bp.study import safety
from bp.version import TOOL_VERSION

FORMAT = "app-blueprint-evidence/1"
NOTES = "S-notes"
STUDY_ID = re.compile(r"S-[a-z0-9][a-z0-9-]{0,47}")
EVIDENCE_ID = re.compile(r"ev-[0-9a-f]{16}")
BEHAVIOUR_ID = re.compile(r"B-[a-z0-9][a-z0-9-]{0,47}")
KIND = re.compile(r"[a-z]+(\.[a-z0-9-]+)+")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
BY = re.compile(r"tool:[a-z0-9-]+@[0-9A-Za-z.+-]+|agent:[^\s\x00-\x1f\x7f]{1,100}|import:(rea|jadx|apktool)")
BASES = ("observed", "derived", "inferred")
COVERAGES = ("complete", "partial", "unknown")
STUDY_KINDS = ("source", "web", "desktop", "mobile", "data", "compare", "capture", "import", "notes")
BEHAVIOUR_STATUSES = ("candidate", "reviewed", "rejected")
FRESHNESS = ("fresh", "stale", "missing", "unknown")
KEY_MAX, CLAIM_MAX, DATA_MAX = 200, 300, 4096
DATA_TRIMMED = "data trimmed"
BAD_STUDY = "bad-study"  # error identifier for a damaged study.json or behaviours.json (proposed for 7.3)
EVIDENCE_FILE, STUDY_FILE, BEHAVIOURS_FILE = "evidence.jsonl", "study.json", "behaviours.json"
BUNDLE_MAX = 256 * 1024 * 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")


# ------------------------------------------------------------------------------------------------ paths

def studies_dir(root):
    return bp_dir(Path(root)) / "studies"


def is_study_id(sid):
    return isinstance(sid, str) and STUDY_ID.fullmatch(sid) is not None


def study_dir(root, sid):
    """The folder of a study; the id is checked first (whole value), so it can never name another folder."""
    if not is_study_id(sid):
        raise ValueError(f"not a study id: {sid!r}")
    return studies_dir(root) / sid


def _cmd(root, args, module="commands/study"):
    """A console command that runs as printed; module names the command file the project's copy needs (1.8 rule 3)."""
    return run_hint(root, args, module)


def not_found(root, sid):
    shown = sid if is_study_id(sid) else "that id"
    return BlueprintError("study-not-found", f"study {shown} is not in .blueprint/studies",
                          entry=f"study {shown}" if is_study_id(sid) else None, next=_cmd(root, "study list"))


def _bad_study(root, sid, name, why):
    return BlueprintError(BAD_STUDY, f".blueprint/studies/{sid}/{name} {why}; nothing was changed",
                          entry=f".blueprint/studies/{sid}/{name}",
                          next=f"fix or remove .blueprint/studies/{sid}/{name}, then run: {_cmd(root, 'study list')}")


# ------------------------------------------------------------------------------------------------ strict JSON

def _finite_float(text):
    v = float(text)
    if not math.isfinite(v):
        raise ValueError(f"number {text} is not finite")
    return v


def _no_constant(name):
    raise ValueError(f"{name} is not a JSON number")


def _bad_text(obj):
    """Why parsed JSON cannot be stored again: a lone surrogate in a string or a key; None when it can."""
    stack = [obj]
    while stack:
        v = stack.pop()
        if isinstance(v, str):
            if any("\ud800" <= c <= "\udfff" for c in v):
                return "a string holds a lone surrogate"
        elif isinstance(v, dict):
            for k, x in v.items():
                stack.append(k)
                stack.append(x)
        elif isinstance(v, list):
            stack.extend(v)
    return None


def loads(text):
    """Parse JSON strictly: non-finite numbers (NaN, Infinity, 1e999) and lone surrogates are refused (ValueError),
    and so is nesting Python cannot parse (RecursionError becomes ValueError)."""
    try:
        obj = json.loads(text, parse_constant=_no_constant, parse_float=_finite_float)
    except RecursionError:
        raise ValueError("nested too deeply") from None
    # a lone surrogate can only come from a \u escape of one (decoded UTF-8 never holds one): look only then
    why = _bad_text(obj) if _SURROGATE_ESCAPE.search(text) else None
    if why:
        raise ValueError(why)
    return obj


_SURROGATE_ESCAPE = re.compile(r"\\[uU][dD][89a-fA-F]")


def _read_bytes(path):
    """The bytes of a project file that is a plain file; None when it does not exist. A link, a folder, a named pipe
    or a device in its place is ValueError (never read through a link, never opened when it is not a regular file)."""
    try:
        if safety.is_link(path):
            raise ValueError("is a link")
        mode = os.lstat(path).st_mode
        if stat.S_ISDIR(mode):
            raise ValueError("is a folder")
        if not stat.S_ISREG(mode):  # a named pipe or a device would block: never opened
            raise ValueError("is not a regular file")
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except IsADirectoryError:
        raise ValueError("is a folder") from None
    except PermissionError as e:
        if os.path.isdir(path):
            raise ValueError("is a folder") from None
        raise ValueError(f"cannot be read ({e.strerror or e})") from None
    except OSError as e:
        raise ValueError(f"cannot be read ({e.strerror or e})") from None


def _read_json_file(path):
    """(obj, problem): obj None and problem None when the file does not exist."""
    try:
        data = _read_bytes(path)
        if data is None:
            return None, None
        return loads(data.decode("utf-8")), None
    except UnicodeDecodeError:
        return None, "is not UTF-8 text"
    except ValueError as e:
        return None, f"is not valid JSON ({e})" if not str(e).startswith(("is a", "cannot")) else str(e)


# ------------------------------------------------------------------------------------------------ text cleaning

def _text(v):
    return safety.clean_text(v) if isinstance(v, str) else v


def _scrubbed(text):
    """Stored text: cleaned, then scrubbed until scrubbing changes nothing (safety.final). Normalising (one line,
    control characters) always happens before this, never after."""
    return safety.final(safety.clean_text(text))


def _clean_data(v, depth=0):
    """A copy of data with every string (keys too) scrubbed, non-finite numbers as null, tuples as lists; nesting past
    40 levels is cut ('...'). (copy, trimmed)."""
    if depth > 40:
        return "...", True
    if isinstance(v, str):
        return _scrubbed(v), False
    if isinstance(v, bool) or v is None or isinstance(v, int):
        return v, False
    if isinstance(v, float):
        return (v if math.isfinite(v) else None), False
    if isinstance(v, dict):
        out, cut = {}, False
        for k, x in v.items():
            cx, c = _clean_data(x, depth + 1)
            out[_scrubbed(str(k))] = cx
            cut = cut or c
        return out, cut
    if isinstance(v, (list, tuple)):
        out, cut = [], False
        for x in v:
            cx, c = _clean_data(x, depth + 1)
            out.append(cx)
            cut = cut or c
        return out, cut
    return _scrubbed(str(v)), False


def _size(obj):
    return len(canonical_json(obj))


def _trim_data(data):
    """data whose canonical JSON is at most 4,096 bytes: keys in sorted order while they fit; a list that does not fit
    keeps its longest prefix that does, a string its longest start; other values that do not fit are left out.
    (data, trimmed)."""
    if _size(data) <= DATA_MAX:
        return data, False
    out = {}
    for k in sorted(data):
        v = data[k]
        if _size(dict(out, **{k: v})) <= DATA_MAX:
            out[k] = v
            continue
        if isinstance(v, (list, str)):
            lo, hi = 0, len(v)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if _size(dict(out, **{k: v[:mid]})) <= DATA_MAX:
                    lo = mid
                else:
                    hi = mid - 1
            if lo:
                out[k] = v[:lo]
    return out, True


# ------------------------------------------------------------------------------------------------ records

def _safe_rel(p):
    """A stored relative path (5.1): forward slashes, not absolute, no drive, no '..' part, no NUL."""
    if not isinstance(p, str) or not p or "\\" in p or _CONTROL.search(p):
        return False
    if p.startswith("/") or re.match(r"[A-Za-z]:", p):
        return False
    return all(part != ".." for step in p.split("!/") for part in step.split("/"))


def _id_object(kind, key, basis, source, claim):
    obj = {"kind": kind, "key": key, "basis": basis, "artifact": source.get("artifact"), "path": source.get("path"),
           "line": source.get("line"), "offset": source.get("offset"), "digest": source.get("digest")}
    if source.get("digest") is None:
        obj["claim"] = claim  # unsourced notes: two notes on one key stay two records (5.2)
    return obj


def record_id(rec):
    """The id a record's content gives (5.2), or None when its fields cannot give one."""
    try:
        src = rec["source"]
        if not isinstance(src, dict):
            return None
        return content_id("ev-", _id_object(rec["kind"], rec["key"], rec["basis"], src, rec.get("claim")), 16)
    except (KeyError, TypeError, ValueError):
        return None


def _int_or_none(v, lowest):
    return v is None or (type(v) is int and v >= lowest)


def make_record(study, kind, key, claim, basis, source, data=None, limitations=(), coverage="complete", links=(), *,
                by, at=None, excerpt_ready=False):
    """One evidence record (5.1), its id derived from its content (5.2). Raises ValueError for a field of the wrong
    form (a lane bug, or an `evidence add` argument the command checks first). Key, claim, excerpt, data strings and
    limitations pass through safety.scrub; the excerpt is cut to one line of 160 characters; a key over 200 or a claim
    over 300 characters is cut, and data over 4,096 bytes of canonical JSON is trimmed, each with a limitation."""
    if not is_study_id(study):
        raise ValueError(f"study {study!r} is not a study id")
    if not isinstance(kind, str) or not KIND.fullmatch(kind):
        raise ValueError(f"kind {kind!r} is not of the form lane.name")
    if basis not in BASES:
        raise ValueError(f"basis {basis!r} is not one of {', '.join(BASES)}")
    if coverage not in COVERAGES:
        raise ValueError(f"coverage {coverage!r} is not one of {', '.join(COVERAGES)}")
    if not isinstance(by, str) or not BY.fullmatch(by):
        raise ValueError(f"by {by!r} is not tool:<lane>@<version>, agent:<id> or import:<rea|jadx|apktool>")
    if not isinstance(key, str) or not isinstance(claim, str):
        raise ValueError("key and claim must be text")
    if not isinstance(source, dict):
        raise ValueError("source must be an object")
    extra = set(source) - {"artifact", "path", "line", "offset", "digest", "excerpt"}
    if extra:
        raise ValueError(f"source has unknown fields: {', '.join(sorted(extra))}")
    artifact, path, digest = source.get("artifact"), source.get("path"), source.get("digest")
    if isinstance(artifact, str):
        artifact = _scrubbed(artifact)
    if isinstance(path, str):
        path = _scrubbed(path)
    if not isinstance(artifact, str) or not artifact or "/" in artifact or "\\" in artifact or _CONTROL.search(artifact):
        raise ValueError(f"source.artifact {artifact!r} must be a base name or '.'")
    if path is not None and not _safe_rel(path):
        raise ValueError(f"source.path {path!r} must be a relative path with forward slashes")
    if digest is not None and not (isinstance(digest, str) and DIGEST.fullmatch(digest)):
        raise ValueError(f"source.digest {digest!r} must be sha256:<64 hex>")
    if not _int_or_none(source.get("line"), 1) or not _int_or_none(source.get("offset"), 0):
        raise ValueError("source.line must be a whole number from 1 and source.offset from 0, or null")
    if data is not None and not isinstance(data, dict):
        raise ValueError("data must be an object")
    lims = []
    for x in [limitations] if isinstance(limitations, str) else list(limitations or ()):
        if not isinstance(x, str):
            raise ValueError("limitations must be text")
        x = _scrubbed(safety.one_line(safety.clean_text(x)))
        if x and x not in lims:
            lims.append(x)
    link_ids = []
    for x in links or ():
        if not isinstance(x, str) or not EVIDENCE_ID.fullmatch(x):
            raise ValueError(f"link {x!r} is not an evidence id")
        if x not in link_ids:
            link_ids.append(x)
    key = _scrubbed(_CONTROL.sub(" ", safety.clean_text(key)))  # normalise first, scrub last (R2-M1)
    if len(key) > KEY_MAX:
        key, lims = _scrubbed(key[:KEY_MAX]), lims + [f"key cut to {KEY_MAX} characters"]
    claim = _scrubbed(safety.one_line(safety.clean_text(claim)))
    if len(claim) > CLAIM_MAX:
        claim, lims = _scrubbed(claim[:CLAIM_MAX]), lims + [f"claim cut to {CLAIM_MAX} characters"]
    if not key.strip() or not claim:
        raise ValueError("key and claim must not be empty")
    ex = source.get("excerpt")
    if ex is not None and not isinstance(ex, str):
        raise ValueError("source.excerpt must be text or null")
    if ex and excerpt_ready:  # taken from a prepared text (Emitter.text): the block rule was applied on the whole text
        ex = _scrubbed(safety.one_line(ex)[:safety.LIMITS["excerpt"]]) or None
    else:  # a line given alone: dropped when it may be key material
        ex = safety.loose_excerpt(ex) or None if ex else None
    clean, cut = _clean_data(data or {})
    clean, trimmed = _trim_data(clean)
    if (cut or trimmed) and DATA_TRIMMED not in lims:
        lims.append(DATA_TRIMMED)
    src = {"artifact": _text(artifact), "path": _text(path), "line": source.get("line"),
           "offset": source.get("offset"), "digest": digest, "excerpt": ex}
    rec = {"id": content_id("ev-", _id_object(kind, key, basis, src, claim), 16), "study": study, "kind": kind,
           "key": key, "claim": claim, "basis": basis, "source": src, "data": clean, "limitations": lims,
           "coverage": coverage, "links": link_ids, "by": _scrubbed(by)}
    for value in (rec["key"], rec["claim"], rec["by"], src["artifact"], src["path"], src["excerpt"], *lims):
        if isinstance(value, str) and safety.scrub(value)[1]:  # the stored value equals its own scrub
            raise ValueError("a stored value still holds a secret after scrubbing")
    if at is not None:
        rec["at"] = at
    return rec


def validate_record(rec):
    """The problems of a stored record (5.1), as plain sentences; [] when it is well formed. Used by evidence import
    and study check. A record whose recomputed id differs (a hand edit) is not a problem (evidence list marks it)."""
    if not isinstance(rec, dict):
        return ["the record is not an object"]
    p = []
    rid = rec.get("id")
    if not (isinstance(rid, str) and EVIDENCE_ID.fullmatch(rid)):
        p.append("id is not ev- and 16 hex digits")
    if not is_study_id(rec.get("study")):
        p.append("study is not a study id")
    if not (isinstance(rec.get("kind"), str) and KIND.fullmatch(rec["kind"])):
        p.append("kind is not of the form lane.name")
    for name, most in (("key", KEY_MAX), ("claim", CLAIM_MAX)):
        v = rec.get(name)
        if not isinstance(v, str) or not v.strip():
            p.append(f"{name} is missing")
        elif len(v) > most:
            p.append(f"{name} is longer than {most} characters")
    if rec.get("basis") not in BASES:
        p.append("basis is not observed, derived or inferred")
    if rec.get("coverage") not in COVERAGES:
        p.append("coverage is not complete, partial or unknown")
    if not (isinstance(rec.get("by"), str) and BY.fullmatch(rec["by"])):
        p.append("by is not tool:<lane>@<version>, agent:<id> or import:<rea|jadx|apktool>")
    src = rec.get("source")
    if not isinstance(src, dict):
        p.append("source is not an object")
    else:
        art = src.get("artifact")
        if not isinstance(art, str) or not art or "/" in art or "\\" in art:
            p.append("source.artifact is not a base name")
        if src.get("path") is not None and not _safe_rel(src.get("path")):
            p.append("source.path is not a relative path")
        if not _int_or_none(src.get("line"), 1):
            p.append("source.line is not a whole number from 1")
        if not _int_or_none(src.get("offset"), 0):
            p.append("source.offset is not a whole number from 0")
        d = src.get("digest")
        if d is not None and not (isinstance(d, str) and DIGEST.fullmatch(d)):
            p.append("source.digest is not sha256:<64 hex>")
        ex = src.get("excerpt")
        if ex is not None and (not isinstance(ex, str) or len(ex) > safety.LIMITS["excerpt"] or _CONTROL.search(ex)):
            p.append("source.excerpt is not one line of at most 160 characters")
    data = rec.get("data", {})
    if not isinstance(data, dict):
        p.append("data is not an object")
    else:
        try:
            if _size(data) > DATA_MAX:
                p.append("data is over 4,096 bytes")
        except (TypeError, ValueError, RecursionError):
            p.append("data is not JSON")
    lims = rec.get("limitations")
    if not isinstance(lims, list) or not all(isinstance(x, str) for x in lims):
        p.append("limitations is not a list of text")
    links = rec.get("links", [])
    if not isinstance(links, list) or not all(isinstance(x, str) and EVIDENCE_ID.fullmatch(x) for x in links):
        p.append("links is not a list of evidence ids")
    if "superseded" in rec and not isinstance(rec["superseded"], bool):
        p.append("superseded is not true or false")
    if "at" in rec and not isinstance(rec["at"], str):
        p.append("at is not text")
    return p


def edited(rec):
    """True when the stored id is not the one the record's content gives (a hand edit; informational, 5.2)."""
    rid = record_id(rec)
    return rid is not None and rid != rec.get("id")


# ------------------------------------------------------------------------------------------------ reading

def shape_ok(rec):
    """The minimum shape every reader relies on: an object with an id of the evidence form, a kind of the form
    lane.name, text key and claim, a basis of the three, and a source object. A line without it is malformed: ignored
    and counted, never handed to a consumer."""
    if not isinstance(rec, dict):
        return False
    rid, kind = rec.get("id"), rec.get("kind")
    return (isinstance(rid, str) and EVIDENCE_ID.fullmatch(rid) is not None and isinstance(kind, str)
            and KIND.fullmatch(kind) is not None and isinstance(rec.get("key"), str)
            and isinstance(rec.get("claim"), str) and isinstance(rec.get("basis"), str) and rec["basis"] in BASES
            and isinstance(rec.get("source"), dict))


def _parse_lines(data):
    """(records, malformed lines): JSON Lines, blank lines skipped, malformed lines counted, the first of duplicate
    ids kept; a line is a record only when it has the minimum shape (shape_ok)."""
    out, seen, bad = [], set(), 0
    for line in data.decode("utf-8", errors="replace").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = loads(line)
        except ValueError:
            bad += 1
            continue
        if not shape_ok(rec):
            bad += 1
            continue
        rid = rec["id"]
        if rid in seen:
            continue
        seen.add(rid)
        out.append(rec)
    return out, bad


def load_info(root, study_id):
    """(records, malformed line count) of one study; ([], 0) when it has no evidence.jsonl. A file that is a link or
    cannot be read counts as one malformed line and gives no records (fail closed: nothing reads as present)."""
    path = study_dir(root, study_id) / EVIDENCE_FILE
    try:
        data = _read_bytes(path)
    except ValueError:
        return [], 1
    if data is None:
        return [], 0
    return _parse_lines(data)


def load(root, study_id):
    """The records of one study, in file order (5.9)."""
    return load_info(root, study_id)[0]


def study_ids(root):
    """The ids of the study folders, sorted: folders whose name is a study id (whole value), never through a link."""
    base = studies_dir(root)
    try:
        if safety.is_link(base) or not base.is_dir():
            return []
        names = sorted(e.name for e in os.scandir(base) if is_study_id(e.name))
    except OSError:
        return []
    return [n for n in names if not safety.is_link(base / n) and (base / n).is_dir()]


def load_all(root):
    """{study id: records} for every study folder (5.9)."""
    return {sid: load(root, sid) for sid in study_ids(root)}


def all_ids(root):
    """Every evidence id in the store, superseded records included (5.9)."""
    out = set()
    for recs in load_all(root).values():
        out.update(r["id"] for r in recs)
    return out


def _study_problem(sid, obj):
    """Why a parsed study.json cannot be used, or None."""
    if not isinstance(obj, dict):
        return "is not a JSON object"
    if obj.get("id") != sid:
        return f"has id {obj.get('id')!r}, not {sid}"
    if obj.get("kind") not in STUDY_KINDS:
        return "has no known kind"
    t = obj.get("target")
    if not isinstance(t, dict) or not isinstance(t.get("name"), str) or not isinstance(t.get("own"), bool):
        return "has no target with a name and own"
    if t.get("path") is not None and not _safe_rel(t.get("path")) and t.get("path") != ".":
        return "has a target path that is not inside the project"
    if t.get("digest") is not None and not (isinstance(t["digest"], str) and DIGEST.fullmatch(t["digest"])):
        return "has a target digest that is not sha256:<64 hex>"
    return None  # every other value is type-checked where it is read: a wrong shape shows as empty


def read_study(root, sid):
    """(study record or None, problem or None) for one study folder; (None, None) when it has no study.json."""
    obj, problem = _read_json_file(study_dir(root, sid) / STUDY_FILE)
    if problem:
        return None, problem
    if obj is None:
        return None, None
    return (None, _study_problem(sid, obj)) if _study_problem(sid, obj) else (obj, None)


def load_study(root, sid):
    """The study record of one study: study-not-found when it is not there, bad-study when its study.json is
    damaged (nothing is changed)."""
    if not is_study_id(sid) or not (studies_dir(root) / sid).is_dir() or safety.is_link(studies_dir(root) / sid):
        raise not_found(root, sid)
    obj, problem = read_study(root, sid)
    if problem:
        raise _bad_study(root, sid, STUDY_FILE, problem)
    if obj is None:
        raise not_found(root, sid)
    return obj


def all_studies(root, warn=True):
    """{id: study record} of every readable study; a damaged study.json is not in it, and one warning line names it on
    stderr (warn=False: silently)."""
    out = {}
    for sid in study_ids(root):
        obj, problem = read_study(root, sid)
        if problem and warn:
            print(f"warning: .blueprint/studies/{sid}/{STUDY_FILE} {problem}", file=sys.stderr)
        if obj is not None:
            out[sid] = obj
    return out


def read_behaviours(root, sid):
    """(behaviours list, problem): ([], None) when the study has no behaviours.json."""
    obj, problem = _read_json_file(study_dir(root, sid) / BEHAVIOURS_FILE)
    if problem:
        return [], problem
    if obj is None:
        return [], None
    if not isinstance(obj, dict) or not isinstance(obj.get("behaviours"), list):
        return [], "is not an object with a behaviours list"
    return obj["behaviours"], None


def behaviour_counts(behaviours):
    counts = {s: 0 for s in BEHAVIOUR_STATUSES}
    for b in behaviours:
        st = b.get("status") if isinstance(b, dict) else None
        if st in counts:
            counts[st] += 1
    return counts


# ------------------------------------------------------------------------------------------------ writing

def _ensure_dir(root, sid):
    """The study folder, created when missing; refused (write-failed, nothing written) when it or .blueprint/studies
    is a link or not a folder, so nothing is ever written through a link."""
    base = studies_dir(root)
    for p, label in ((base, ".blueprint/studies"), (base / sid, f".blueprint/studies/{sid}")):
        if safety.is_link(p) or (p.exists() and not p.is_dir()):
            raise BlueprintError("write-failed", f"{label} is a link or not a folder; nothing was written",
                                 entry=label, next=f"move {label} away, then {core.AGAIN}")
        try:
            p.mkdir(exist_ok=True)
        except OSError as e:
            raise BlueprintError("write-failed", f"cannot create {label} ({e.strerror or e}); nothing was written",
                                 entry=label, next=f"make the folder .blueprint writable, then {core.AGAIN}") from None
    return base / sid


def write_text(path, text):
    """Write a project file atomically: a temp file unique to this process beside it (created exclusively), then
    os.replace, which replaces a link rather than writing through it. Call holding core.state_lock."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "x", encoding="utf-8", newline="\n") as f:
            f.write(text)
        core._retry_windows(os.replace, tmp, path)
    except BaseException as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if isinstance(e, OSError):
            raise BlueprintError("write-failed", f"cannot write .blueprint/{path.parent.parent.name}/"
                                 f"{path.parent.name}/{path.name} ({e.strerror or e}); nothing more was written",
                                 next=f"make the folder writable, then {core.AGAIN}") from None
        raise


def _label(path):
    return "/".join(Path(path).parts[-4:])


class Writes:
    """Several project files written as one step: every temp file is prepared first (unique names beside the
    targets, created exclusively); only when all are ready are they moved into place, one by one. A failure while
    preparing writes nothing; a failure while moving says exactly which files were written and which were not.
    Call holding core.state_lock."""

    def __init__(self):
        self.items = []

    def add(self, path, data):
        self.items.append((Path(path), data.encode("utf-8") if isinstance(data, str) else data))

    def commit(self):
        ready = []
        try:
            for path, data in self.items:
                if safety.is_link(path.parent) or (path.exists() and not path.is_file() and not safety.is_link(path)):
                    raise OSError(f"{_label(path)} is in the way (a folder or something else)")
                tmp = path.with_name(f"{path.name}.{os.getpid()}-{secrets.token_hex(4)}.tmp")
                with open(tmp, "xb") as f:
                    f.write(data)
                ready.append((tmp, path))
        except OSError as e:
            for tmp, _ in ready:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            raise BlueprintError("write-failed", f"cannot prepare the files to write ({e.strerror or e}); nothing "
                                 "was written", next=f"make the folder .blueprint/studies writable, then {core.AGAIN}") \
                from None
        done = []
        for i, (tmp, path) in enumerate(ready):
            try:
                core._retry_windows(os.replace, tmp, path)
            except OSError as e:
                for t, _ in ready[i:]:
                    try:
                        os.unlink(t)
                    except OSError:
                        pass
                written = ", ".join(_label(x) for x in done) or "nothing"
                missing = ", ".join(_label(x) for _, x in ready[i:])
                raise BlueprintError("write-failed", f"stopped part-way ({e.strerror or e}): written: {written}; not "
                                     f"written: {missing}", next=f"make those files writable, then {core.AGAIN}") \
                    from None
            done.append(path)
        return done


def dump_json(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def _line(rec):
    return json.dumps(rec, ensure_ascii=False) + "\n"


def sort_records(records):
    """Records sorted by id, the first of duplicate ids kept (5.5)."""
    seen, out = set(), []
    for r in records:
        if r["id"] not in seen:
            seen.add(r["id"])
            out.append(r)
    return sorted(out, key=lambda r: r["id"])


def write_records_held(root, sid, records):
    """Rewrite a study's evidence.jsonl (sorted, deduplicated) atomically. The caller holds core.state_lock."""
    folder = _ensure_dir(root, sid)
    write_text(folder / EVIDENCE_FILE, "".join(_line(r) for r in sort_records(records)))


def write_study_records(root, study_id, records):
    """Rewrite a study's evidence.jsonl, sorted and deduplicated, atomically under core.state_lock (5.5, 5.9)."""
    records = [dict(r) for r in records]
    with core.state_lock(root):
        write_records_held(root, study_id, records)


def write_study_held(root, study):
    folder = _ensure_dir(root, study["id"])
    write_text(folder / STUDY_FILE, dump_json(study))


def sharing_held(root, sid, own):
    """A study that is not the user's own gets .blueprint/studies/<S>/.gitignore holding '*' (6.1, decision 12.1 #2,
    pending the owner's confirmation); an own study loses that file when it holds exactly what the tool writes."""
    path = _ensure_dir(root, sid) / ".gitignore"
    if not own:
        if safety.is_link(path) or not path.is_file() or path.read_bytes() != b"*\n":
            write_text(path, "*\n")
    elif not safety.is_link(path) and path.is_file() and path.read_bytes() == b"*\n":
        path.unlink()


def empty_coverage():
    return {"filesSeen": 0, "filesScanned": 0, "filesSkipped": 0, "bytesRead": 0, "bytesHashed": 0, "skipped": {},
            "missing": [],
            "truncated": False, "stoppedBy": None, "notes": []}


def counts_of(records):
    """counts of 6.1: evidence (records that are not superseded), byKind, secrets (secret.embedded records)."""
    live = [r for r in records if isinstance(r, dict) and r.get("superseded") is not True]
    by_kind = {}
    for r in live:
        kind = r.get("kind") if isinstance(r.get("kind"), str) else "?"
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return {"evidence": len(live), "byKind": dict(sorted(by_kind.items())),
            "secrets": by_kind.get("secret.embedded", 0)}


def notes_study():
    """The study record of S-notes, made on first use by evidence add (5.5): kind notes, the user's own project."""
    return {"id": NOTES, "kind": "notes",
            "target": {"name": ".", "path": ".", "digest": None, "size": None, "own": True},
            "authorization": {"statement": "own project", "by": None, "at": now_iso()},
            "tool": TOOL_VERSION, "at": now_iso(), "lanes": [], "limits": {}, "coverage": empty_coverage(),
            "counts": counts_of([])}


def _ensure_newline(path):
    """True when the file is missing or empty or ends with a newline; else a newline is appended first, so the next
    line never joins a cut one (evidence add after a crash mid-line)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size == 0:
        return
    with open(path, "rb") as f:
        f.seek(-1, os.SEEK_END)
        last = f.read(1)
    if last != b"\n":
        with open(path, "ab") as f:
            f.write(b"\n")


def append_held(root, sid, records):
    """Append whole lines to a study's evidence.jsonl (each record one write). The caller holds core.state_lock."""
    path = _ensure_dir(root, sid) / EVIDENCE_FILE
    if safety.is_link(path):
        raise BlueprintError("write-failed", f".blueprint/studies/{sid}/{EVIDENCE_FILE} is a link; nothing was "
                             "written", entry=f".blueprint/studies/{sid}/{EVIDENCE_FILE}",
                             next=f"move it away, then {core.AGAIN}")
    _ensure_newline(path)
    with open(path, "ab") as f:
        for r in records:
            f.write(_line(r).encode("utf-8"))


def add(root, study_id, rec):
    """Append one record to a study (5.9): deduplicated by id, under core.state_lock. S-notes is created on first use;
    any other study must exist (study-not-found) with a readable study.json (bad-study). Everything is computed
    before anything is written: the line, then the study's counts. Returns (record as stored, added: bool)."""
    if not is_study_id(study_id):
        raise not_found(root, study_id)
    with core.state_lock(root):
        folder = studies_dir(root) / study_id
        if study_id == NOTES and not (folder / STUDY_FILE).exists() and not safety.is_link(folder):
            study = notes_study()
        else:
            study = load_study(root, study_id)
        records, _ = load_info(root, study_id)
        have = {r["id"]: r for r in records}
        if rec["id"] in have:
            return have[rec["id"]], False
        rec = dict(rec, study=study_id)
        study = dict(study, counts=counts_of(records + [rec]))
        event = {"event": "evidence-add", "study": study_id, "id": rec["id"], "kind": rec["kind"]}
        dump_json(study), _line(rec)  # both serialise before the first write
        append_held(root, study_id, [rec])
        try:
            write_study_held(root, study)
        except BlueprintError:
            raise BlueprintError("write-failed", f"{rec['id']} is stored in .blueprint/studies/{study_id}/"
                                 f"{EVIDENCE_FILE}, but the count in its {STUDY_FILE} could not be updated; it is "
                                 "stale until the next write to this study, which counts the file again",
                                 entry=f".blueprint/studies/{study_id}/{STUDY_FILE}",
                                 next=f"make the file writable; the next evidence add to this study fixes the count") \
                from None
        core.append_log(root, event)
    return rec, True


# ------------------------------------------------------------------------------------------------ freshness

# Skip reasons and stops that mean "one of the tool's own limits was reached", not "the evidence changed": such a
# record is unknown and reported as not checked (evidence stale fails --strict on it).
LIMIT_REASONS = ("too-large", "ratio", "depth", "limit")


class _Hashes:
    """Digests of files on disk for one freshness pass, keyed by the file's identity and version; bytes of members and
    the open archives read on the way, so a study of 20,000 records hashes each file once and opens each container
    once. Listing an archive is not counted as reading files."""

    def __init__(self, budget):
        self.budget = budget
        self.raw, self.proof, self.blobs, self.zips = {}, {}, {}, {}

    @staticmethod
    def _key(path):
        st = os.stat(path)
        return (os.path.normcase(os.path.realpath(path)), st.st_size, st.st_mtime_ns)

    def raw_digest(self, path):
        k = self._key(path)
        if k not in self.raw:
            self.budget.bytes(k[1])
            self.budget.check_time()
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            self.raw[k] = "sha256:" + h.hexdigest()
        return self.raw[k]

    def proof_digest(self, path):
        k = self._key(path)
        if k not in self.proof:
            self.budget.bytes(k[1])
            self.budget.check_time()
            self.proof[k] = core.proof_digest(Path(path))
        return self.proof[k]

    def zip(self, key, data_or_path, depth):
        z = self.zips.get(key)
        if z is None:
            z = self.zips[key] = safety.SafeZip(data_or_path, self.budget, depth=depth, count_files=False)
        return z

    def close(self):
        for z in self.zips.values():
            z.close()
        self.zips.clear()


def _suffix(name):
    name = str(name).lower()
    dot = name.rfind(".")
    return name[dot:] if dot > name.rfind("/") else ""


def _member(hashes, container_key, data_or_path, suffix, inner, depth, resolvers):
    """Bytes of `inner` read from a container (bytes, or a path for the target itself) with the reader for its
    suffix: the zip family through SafeZip (opened once per pass), anything else through resolvers. Skip(MISSING)
    when it is not there; None when no reader knows the suffix."""
    key = container_key + (inner,)
    if key in hashes.blobs:
        return hashes.blobs[key]
    if suffix in safety.ZIP_SUFFIXES:
        data = hashes.zip(container_key, data_or_path, depth).read(inner, hashes.budget.limits["memberBytes"])
    else:
        fn = (resolvers or {}).get(suffix)
        if fn is None:
            return None
        if not isinstance(data_or_path, (bytes, bytearray)):
            data_or_path = safety.read_file(data_or_path, hashes.budget.limits["totalBytes"], hashes.budget)
        data = fn(data_or_path, inner, hashes.budget)
    hashes.blobs[key] = data
    return data


def locate(root, study, target=None):
    """Where a study's bytes are (5.6 step 2): its target.path inside the project, else the --target given, else
    None."""
    t = study.get("target") if isinstance(study, dict) else None
    if not isinstance(t, dict):
        return None
    p = t.get("path")
    if isinstance(p, str) and p:
        if p != "." and not _safe_rel(p):
            return None
        return Path(root) if p == "." else Path(root) / p
    return Path(target) if target is not None else None


def _digest_of(rec):
    src = rec.get("source") if isinstance(rec, dict) else None
    digest = src.get("digest") if isinstance(src, dict) else None
    return digest if isinstance(digest, str) and DIGEST.fullmatch(digest) else None


UNREADABLE = "unreadable"  # a not-checked reason: the file is there but the system refused to read it


def _one(hashes, located, base, is_dir, rec, resolvers):
    """(state, limit or None) for one record; limit names the tool limit (or UNREADABLE) that stopped the check.
    Neutral unknown only when the tool cannot look: no digest, no target here, a path holding a secret placeholder
    (it cannot be located), or no reader for a container's suffix. A target that is there but can no longer be read
    as what the study recorded (a file replaced by a link or a folder, an archive or member that does not parse) is
    stale."""
    digest = _digest_of(rec)
    if digest is None or located is None:
        return "unknown", None
    path = rec["source"].get("path")
    if not (isinstance(path, str) and (path == "." or _safe_rel(path))) or "<secret:" in path:
        return "unknown", None
    parts = path.split("!/")
    try:
        if is_dir:
            first = located / parts[0]
            if parts[0] == ".":
                return "unknown", None
            if not os.path.lexists(first):
                return "missing", None
            if safety.is_link(first) or not first.is_file():
                return "stale", None
            if not os.path.normcase(os.path.realpath(first)).startswith(base):
                return "stale", None
            if len(parts) == 1:
                return ("fresh" if hashes.proof_digest(first) == digest else "stale"), None
            container, suffix = first, _suffix(parts[0])
            rest = parts[1:]
        else:
            if parts == ["."]:
                return ("fresh" if hashes.raw_digest(located) == digest else "stale"), None
            container, suffix = located, _suffix(located.name)
            rest = parts
        data, key = container, (str(container),)
        for depth, inner in enumerate(rest):
            data = _member(hashes, key, data, suffix, inner, depth, resolvers)
            if data is None:
                return "unknown", None
            key, suffix = key + (inner,), _suffix(inner)
        return ("fresh" if sha256_bytes(data) == digest else "stale"), None
    except safety.Skip as e:
        if e.reason == safety.MISSING:
            return "missing", None
        if e.reason in LIMIT_REASONS:
            return "unknown", e.reason
        if e.reason == "error":
            return "unknown", UNREADABLE
        return "stale", None  # malformed, encrypted, a link, an unsafe name: not what was recorded
    except safety.Stop as e:
        return "unknown", e.stoppedBy
    except safety.PARSE_ERRORS:
        return "stale", None
    except OSError:
        return "unknown", UNREADABLE


def freshness_check(root, study, records, target=None, resolvers=None, budget=None):
    """({record id: fresh | stale | missing | unknown}, {record id: limit}) for records of one study (5.6). The
    second map holds the records that are unknown because the check was stopped: one of the tool's own limits (a
    budget stop: time-limit, byte-limit, file-limit; a member too-large, past the ratio rule, nested past the depth
    limit, or past the archive entry limit) or a file the system refused to read (unreadable). Neutral unknown,
    outside that map, only when the tool cannot look: a null digest, no target here (outside the project and no
    --target), a path holding a secret placeholder, or no reader for a container's suffix. A target that is there
    but is not what was studied (a file target replaced by a folder or a link, a folder replaced by a file) makes
    every record with a digest stale. Each file is hashed once and each archive opened once."""
    budget = budget or safety.Budget()
    hashes = _Hashes(budget)
    out, unchecked = {}, {}
    located = locate(root, study, target)
    is_dir = False
    base = ""
    t = study.get("target") if isinstance(study.get("target"), dict) else {}
    was_file = isinstance(t.get("digest"), str) or type(t.get("size")) is int

    def every(state, limit=None):
        for r in records:
            rid = r.get("id") if isinstance(r, dict) else None
            has = _digest_of(r) is not None
            out[rid] = state if has else "unknown"
            if has and limit:
                unchecked[rid] = limit
        return out, unchecked

    try:
        if located is not None:
            try:
                if not os.path.lexists(located):
                    return every("missing")
                if safety.is_link(located) and Path(located) != Path(root):
                    return every("stale")
                is_dir = located.is_dir()
                if is_dir == was_file or not (is_dir or located.is_file()):
                    return every("stale")
                base = os.path.normcase(os.path.realpath(located)).rstrip(os.sep) + os.sep
                td = t.get("digest")
                if not is_dir and isinstance(td, str) and DIGEST.fullmatch(td) and hashes.raw_digest(located) == td:
                    return every("fresh")
            except safety.Stop as e:
                return every("unknown", e.stoppedBy)
            except OSError:
                return every("unknown", UNREADABLE)
        for r in records:
            rid = r.get("id") if isinstance(r, dict) else None
            out[rid], limit = _one(hashes, located, base, is_dir, r, resolvers)
            if limit:
                unchecked[rid] = limit
        return out, unchecked
    finally:
        hashes.close()


def freshness_all(root, study, records, target=None, resolvers=None, budget=None):
    """{record id: fresh | stale | missing | unknown} (freshness_check without the not-checked map)."""
    return freshness_check(root, study, records, target, resolvers, budget)[0]


def freshness(root, study, rec, target=None, resolvers=None):
    """fresh | stale | missing | unknown for one record (5.6)."""
    return freshness_all(root, study, [rec], target, resolvers).get(rec.get("id") if isinstance(rec, dict) else None,
                                                                     "unknown")


# Three answers for readers (fail closed): fresh; confirmed gone or changed (CHANGED: stale or missing); cannot tell
# (unknown). A record whose file or member is gone can no longer be confirmed, so missing counts with stale.
CHANGED = ("stale", "missing")


def is_changed(state):
    """True for a freshness that says the evidence no longer matches its target: stale or missing."""
    return state in CHANGED


def all_changed(states):
    """True when there is at least one state and every one is stale or missing (study check's behaviour-stale and
    study adopt's refusal, 5.6). An unknown or fresh state makes it False."""
    states = list(states)
    return bool(states) and all(is_changed(s) for s in states)


def freshness_counts(states):
    counts = {s: 0 for s in FRESHNESS}
    for s in states.values():
        counts[s if s in counts else "unknown"] += 1
    return counts


# ------------------------------------------------------------------------------------------------ export and import

def export_bundle(root, study_ids_=None):
    """The one-file bundle of 5.7: studies, records and behaviours sorted by id, no time at the top level, so the same
    store exports byte-identical bundles. study_ids_ None: every study. A damaged study.json or behaviours.json is
    bad-study (named, nothing written); an id that is not there is study-not-found."""
    ids = study_ids(root) if study_ids_ is None else sorted(set(study_ids_))
    studies, records, behaviours = [], [], []
    for sid in ids:
        studies.append(load_study(root, sid))
        records += load(root, sid)
        beh, problem = read_behaviours(root, sid)
        if problem:
            raise _bad_study(root, sid, BEHAVIOURS_FILE, problem)
        if (study_dir(root, sid) / BEHAVIOURS_FILE).exists():
            behaviours.append({"study": sid, "behaviours": beh})
    return {"format": FORMAT, "tool": TOOL_VERSION, "studies": sorted(studies, key=lambda s: s["id"]),
            "records": sorted(records, key=lambda r: (r["id"], str(r.get("study")))),
            "behaviours": sorted(behaviours, key=lambda b: b["study"])}


def _bad_bundle(why):
    return BlueprintError("bad-bundle", f"the file is not an evidence bundle ({why}); nothing was imported",
                          next=f"export one with evidence export, then {core.AGAIN}")


def read_bundle(data):
    """The parsed bundle (5.7) from the bytes of a file; bad-bundle when it is not one (nothing is imported)."""
    if len(data) > BUNDLE_MAX:
        raise _bad_bundle("larger than 256 MiB")
    try:
        obj = loads(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise _bad_bundle("not UTF-8 text") from None
    except ValueError as e:
        raise _bad_bundle(f"not valid JSON: {e}") from None
    if not isinstance(obj, dict) or obj.get("format") != FORMAT:
        raise _bad_bundle(f'"format" is not {FORMAT}')
    for key in ("studies", "records", "behaviours"):
        if not isinstance(obj.get(key, []), list):
            raise _bad_bundle(f'"{key}" is not a list')
    return obj


def _same(a, b):
    return canonical_json(a) == canonical_json(b)


def _short_text(v, most=300):
    return _scrubbed(safety.one_line(safety.clean_text(v))[:most]) if isinstance(v, str) else None


def _count(v):
    return v if type(v) is int and v >= 0 else 0


def _texts(v, most=200):
    out = []
    for x in v if isinstance(v, list) else []:
        t = _short_text(x)
        if t and t not in out and len(out) < most:
            out.append(t)
    return out


def rebuild_study(s):
    """A study record from a bundle, rebuilt from the known keys of 6.1 with every value type-checked and capped
    (a wrong shape becomes empty); None when its id, kind or target cannot be used. counts are recomputed after the
    records are merged."""
    sid = s.get("id") if isinstance(s, dict) else None
    if not is_study_id(sid) or _study_problem(sid, s):
        return None
    t, a = s["target"], s.get("authorization") if isinstance(s.get("authorization"), dict) else {}
    c = s.get("coverage") if isinstance(s.get("coverage"), dict) else {}
    skipped = c.get("skipped") if isinstance(c.get("skipped"), dict) else {}
    limits = s.get("limits") if isinstance(s.get("limits"), dict) else {}
    lanes = s.get("lanes") if isinstance(s.get("lanes"), list) else []
    return {"id": sid, "kind": s["kind"],
            "target": {"name": _short_text(t["name"], 255) or ".", "path": t.get("path"),
                       "digest": t.get("digest"), "size": t["size"] if type(t.get("size")) is int else None,
                       "own": t["own"]},
            "authorization": {"statement": _short_text(a.get("statement"), 500) or "",
                              "by": _short_text(a.get("by"), 100), "at": _short_text(a.get("at"), 40)},
            "tool": _short_text(s.get("tool"), 40), "at": _short_text(s.get("at"), 40),
            "lanes": [x for x in lanes if isinstance(x, str) and re.fullmatch(r"[a-z0-9-]{1,40}", x)][:20],
            "limits": {k: v for k, v in list(limits.items())[:40]
                       if isinstance(k, str) and re.fullmatch(r"[A-Za-z]{1,40}", k) and type(v) is int},
            "coverage": {"filesSeen": _count(c.get("filesSeen")), "filesScanned": _count(c.get("filesScanned")),
                         "filesSkipped": _count(c.get("filesSkipped")), "bytesRead": _count(c.get("bytesRead")),
                         "bytesHashed": _count(c.get("bytesHashed")),
                         "skipped": {k: _count(v) for k, v in skipped.items() if k in safety.SKIP_REASONS},
                         "missing": _texts(c.get("missing")), "truncated": c.get("truncated") is True,
                         "stoppedBy": c.get("stoppedBy") if c.get("stoppedBy") in safety.STOP_REASONS else None,
                         "notes": _texts(c.get("notes"))},
            "counts": counts_of([])}


def _holds_secret(rec):
    """True when any stored text of a record matches a secret pattern (an imported record is never re-scrubbed, so
    such a record is refused)."""
    texts, stack = [], [rec]
    while stack:
        v = stack.pop()
        if isinstance(v, str):
            texts.append(v)
        elif isinstance(v, dict):
            stack.extend(v.keys())
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
    return any(safety.scrub(t)[1] for t in texts)


def import_bundle(root, bundle):
    """Merge a bundle into the store (5.7), all or nothing: studies by id (rebuilt from the known keys; an existing
    study with other content is kept and reported conflict, and nothing of the bundle is merged into it), records by
    id within their study (existing kept; a record that fails validate_record or holds a secret-pattern match is
    malformed), behaviours by id per study (existing kept). The bundle is checked before core.state_lock is taken;
    inside it the store is read and every write is planned, and only then written (new study folders, appended
    evidence lines, behaviours.json with the new entries after the old, study counts). A damaged study file met
    while planning is bad-study and nothing is written. Returns counts: added, duplicate, conflict, malformed, in
    total and per part."""
    parts = {p: {"added": 0, "duplicate": 0, "conflict": 0, "malformed": 0} for p in ("studies", "records",
                                                                                     "behaviours")}
    studies, records, behaviours = {}, {}, {}
    for s in bundle.get("studies", []):
        rebuilt = rebuild_study(s)
        if rebuilt is None or rebuilt["id"] in studies:
            parts["studies"]["malformed"] += 1
            continue
        studies[rebuilt["id"]] = rebuilt
    for r in bundle.get("records", []):
        if validate_record(r) or _holds_secret(r):
            parts["records"]["malformed"] += 1
            continue
        records.setdefault(r["study"], []).append(r)
    for group in bundle.get("behaviours", []):
        sid = group.get("study") if isinstance(group, dict) else None
        items = group.get("behaviours") if isinstance(group, dict) else None
        if not is_study_id(sid) or not isinstance(items, list):
            parts["behaviours"]["malformed"] += 1
            continue
        for b in items:
            if not isinstance(b, dict) or not (isinstance(b.get("id"), str) and BEHAVIOUR_ID.fullmatch(b["id"])):
                parts["behaviours"]["malformed"] += 1
                continue
            behaviours.setdefault(sid, []).append(b)
    with core.state_lock(root):
        present = set(study_ids(root))
        new_studies, conflict = {}, set()
        for sid, s in sorted(studies.items()):
            if sid in present:
                have = load_study(root, sid)
                if _same(dict(rebuild_study(have) or {}, counts=None), dict(s, counts=None)):
                    parts["studies"]["duplicate"] += 1
                else:
                    parts["studies"]["conflict"] += 1
                    conflict.add(sid)
                continue
            new_studies[sid] = s
        known = present | set(new_studies)
        appends, beh_writes = {}, {}
        for sid, recs in sorted(records.items()):
            if sid not in known:
                parts["records"]["malformed"] += len(recs)  # its study is in neither the bundle nor here
                continue
            if sid in conflict:
                parts["records"]["conflict"] += len(recs)
                continue
            have = {r["id"]: r for r in load(root, sid)} if sid in present else {}
            if sid in present:
                load_study(root, sid)
            new = []
            for r in recs:
                if r["id"] in have:
                    parts["records"]["duplicate" if _same(have[r["id"]], r) else "conflict"] += 1
                    continue
                have[r["id"]] = r
                new.append(r)
            if new:
                appends[sid] = (new, list(have.values()))
                parts["records"]["added"] += len(new)
        for sid, items in sorted(behaviours.items()):
            if sid not in known:
                parts["behaviours"]["malformed"] += len(items)
                continue
            if sid in conflict:
                parts["behaviours"]["conflict"] += len(items)
                continue
            old, problem = read_behaviours(root, sid) if sid in present else ([], None)
            if problem:
                raise _bad_study(root, sid, BEHAVIOURS_FILE, problem)
            have = {b.get("id"): b for b in old if isinstance(b, dict)}
            new = []
            for b in items:
                if b["id"] in have:
                    parts["behaviours"]["duplicate" if _same(have[b["id"]], b) else "conflict"] += 1
                    continue
                have[b["id"]] = b
                new.append(b)
            if new:
                beh_writes[sid] = dump_json({"study": sid, "behaviours": old + new})
                parts["behaviours"]["added"] += len(new)
        study_writes = {}
        for sid in sorted(set(new_studies) | set(appends)):
            base = new_studies.get(sid) or load_study(root, sid)
            every = appends[sid][1] if sid in appends else []
            study_writes[sid] = dict(base, counts=counts_of(every))
        # every check is done: prepare every file, then move them into place (Writes)
        writes = Writes()
        for sid in sorted(set(new_studies) | set(appends) | set(beh_writes)):
            folder = _ensure_dir(root, sid)
            if sid in study_writes:
                writes.add(folder / STUDY_FILE, dump_json(study_writes[sid]))
            if sid in new_studies and new_studies[sid]["target"]["own"] is not True:
                writes.add(folder / ".gitignore", "*\n")
            if sid in appends:
                path = folder / EVIDENCE_FILE
                try:
                    old = _read_bytes(path) or b""
                except ValueError as e:
                    raise _bad_study(root, sid, EVIDENCE_FILE, str(e)) from None
                if old and not old.endswith(b"\n"):
                    old += b"\n"
                writes.add(path, old + "".join(_line(r) for r in appends[sid][0]).encode("utf-8"))
            if sid in beh_writes:
                writes.add(folder / BEHAVIOURS_FILE, beh_writes[sid])
        writes.commit()
        parts["studies"]["added"] += len(new_studies)
        total = {k: sum(p_[k] for p_ in parts.values()) for k in ("added", "duplicate", "conflict", "malformed")}
        core.append_log(root, dict({"event": "evidence-import"}, **total))
    return dict(total, **parts)


# ------------------------------------------------------------------------------------------------ hooks

def validate_context(root, plan):
    """evidenceIds for validate (1.4): every evidence id in the store, so unknowns can warn about ids that are not
    there (unknown-evidence). Without a root nothing is supplied, and the check is skipped. When no unknown cites
    evidence there is nothing to check, so the store is not read (validate runs before every render)."""
    if root is None:
        return {}
    cites = False
    for u in plan.get("unknowns") if isinstance(plan, dict) and isinstance(plan.get("unknowns"), list) else []:
        if isinstance(u, dict) and any(isinstance(u.get(k), list) and u[k] for k in ("evidenceFor", "evidenceAgainst")):
            cites = True
            break
    return {"evidenceIds": all_ids(root) if cites else set()}


BEHAVIOURS_SMALL = 1024 * 1024  # the map counts behaviours only from a behaviours.json up to this size


def summary(study, behaviours=None):
    """One entry of the studies payload (8.5), from study.json alone (and the behaviour counts when given). No
    freshness: that is computed only on demand (evidence stale, evidence list --stale, evidence show)."""
    t = study.get("target") if isinstance(study.get("target"), dict) else {}
    auth = study.get("authorization") if isinstance(study.get("authorization"), dict) else {}
    counts = study.get("counts") if isinstance(study.get("counts"), dict) else {}
    by_kind = counts.get("byKind") if isinstance(counts.get("byKind"), dict) else {}
    return {"id": study["id"], "kind": study.get("kind"),
            "target": {"name": t.get("name") if isinstance(t.get("name"), str) else None, "own": t.get("own") is True},
            "authorization": {"statement": auth.get("statement") if isinstance(auth.get("statement"), str) else None},
            "tool": study.get("tool") if isinstance(study.get("tool"), str) else None,
            "at": study.get("at") if isinstance(study.get("at"), str) else None,
            "coverage": core.copy_json(study.get("coverage") if isinstance(study.get("coverage"), dict) else {}),
            "counts": {"evidence": _count(counts.get("evidence")),
                       "byKind": {k: _count(v) for k, v in by_kind.items() if isinstance(k, str)},
                       "secrets": _count(counts.get("secrets"))},
            "behaviours": behaviour_counts(behaviours) if behaviours is not None else None}


def _small_behaviours(root, sid):
    """The behaviours of a study when its behaviours.json is small (the map's budget), else None."""
    path = study_dir(root, sid) / BEHAVIOURS_FILE
    try:
        if not path.is_file() or path.stat().st_size > BEHAVIOURS_SMALL:
            return [] if not path.exists() else None
    except OSError:
        return None
    beh, problem = read_behaviours(root, sid)
    return None if problem else beh


def context(root, plan, state, ctx):
    """ctx["studies"] (8.5): one summary per readable study, sorted by id, read from the small study.json files (and
    behaviours.json up to 1 MiB). No target is opened or hashed and no evidence.jsonl is parsed here, so studies never
    slow down status, claim, mark, sync, task or render."""
    ctx["studies"] = [summary(study, _small_behaviours(root, sid))
                      for sid, study in sorted(all_studies(root, warn=False).items())]


def map_payload(ctx):
    """The Studies tab's data (8.5): the key `studies` only."""
    studies = ctx.get("studies")
    return {"studies": core.copy_json(studies) if isinstance(studies, list) else []}
