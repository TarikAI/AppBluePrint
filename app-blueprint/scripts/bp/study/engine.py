"""study.engine: the study record, Target, Emitter, run_study() and manifest_facts() (contract 6.1, 6.3).

A study reads one target (a folder or a file) with one or more lanes and stores what it found as evidence. The order
is fixed by the one write rule (1.8 rule 7, decision 39): everything is read, parsed and hashed first, with no lock
held; then one short step under core.state_lock re-reads what it merges with (the records cited by behaviours or
unknowns, for --replace) and writes evidence.jsonl, study.json and the sharing .gitignore atomically, and the log line.

Lanes are passed in by the commands layer (`lanes`, objects or modules with LANE, KINDS, detect(path) and
run(target, em, opts)); nothing in study/ imports a lane, so loading one never loads another and a lane that is not
built is simply not passed. This module never runs, loads or imports the target, and never calls git or a
subprocess: whether a folder is tracked by git is decided by the caller (`tracked`).
"""
import hashlib
import os
from pathlib import Path

from bp import core, evidence
from bp.core import BlueprintError, now_iso, sha256_bytes
from bp.learn import redact
from bp.study import keys, safety
from bp.version import TOOL_VERSION

LANE_KINDS = ("source", "web", "desktop", "mobile", "data")  # study kinds run by run_study from a target path
OWN_STATEMENT = "the user's own software"                    # --own
PROJECT_STATEMENT = "own project"                            # the source lane on the project or a tracked folder in it
AUTHORIZATION_MIN = 15
STATEMENT_MAX = 500
NOTES_MAX = 200
NOTE_LEN = 300
NOT_AUTHORIZED = ('State that you may analyse this target: --authorization "I wrote this app" or "the vendor\'s '
                  'licence allows analysis" (or --own).')


def proof_digest_bytes(data):
    """core.proof_digest of a file, computed from bytes already read (4.5): text (UTF-8, no NUL) with CRLF and lone CR
    as LF, anything else raw. Lanes read a file once and hash what they read."""
    text = b"\x00" not in data
    if text:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            text = False
    if text:
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return sha256_bytes(data)


def _file_digest(path, budget=None):
    """(raw sha256 digest, size) of a file, streamed; the target as a whole (5.1). With a budget the bytes count in
    bytes_hashed (coverage.bytesHashed, not the read budget) and the time limit is checked per MiB."""
    h, size = hashlib.sha256(), 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
            if budget is not None:
                budget.bytes_hashed += len(chunk)
                budget.check_time()
    return "sha256:" + h.hexdigest(), size


class Target:
    """What a lane reads (6.3). kind "folder" or "file"; name the basename; path the real path on this machine (never
    stored); digest and size for a file target (raw sha256 of the whole file, 5.1). files() lists a folder's files
    (safe, sorted: safety.walk, never .git, .hg, .svn; on the project root without .blueprint/); read(rel, cap)
    reads one (rel "." is a file target itself); zip() opens a file
    target that is an archive (SafeZip); digest_of(rel, data) is the digest rule of 5.1 for what was read.
    Every read counts in the study budget; skips found while listing go to on_skip(rel, reason, note)."""

    def __init__(self, path, budget, root=None, is_project_root=False):
        self.path = Path(path)
        self.budget = budget
        self.root = Path(root) if root is not None else None
        self.is_project_root = is_project_root
        self.kind = "folder" if self.path.is_dir() else "file"
        self.name = self.path.name or "."
        self.digest = self.size = None
        self.hash_stopped = False
        if self.kind == "file":
            limit = budget.limits["hashBytes"]
            self.size = self.path.stat().st_size
            if self.size > limit:
                raise BlueprintError("target-unsupported", f"the target is larger than {limit // 2 ** 30} GiB, the "
                                     "largest file a study hashes and reads", next=f"{core.AGAIN} with a smaller target")
            try:
                self.digest, self.size = _file_digest(self.path, budget)
            except safety.Stop:
                self.hash_stopped = True  # no digest: freshness then works member by member
        self.on_skip = None
        self.on_note = None
        self._zip = None

    def files(self):
        if self.kind != "folder":
            return iter(())
        # the project's own .blueprint folder (the plan, state, studies) is never part of what a study reads
        return safety.walk(self.path, self.budget, skip=self._skip, note=self._note,
                           prune=(".blueprint",) if self.is_project_root else ())

    def _skip(self, rel, reason, note=None):
        if self.on_skip is not None:
            self.on_skip(rel, reason, note)

    def _note(self, text):
        if self.on_note is not None:
            self.on_note(text)

    def read(self, rel, cap):
        if self.kind == "file":
            if rel != ".":
                raise safety.Skip(safety.MISSING)
            return safety.read_file(self.path, cap, self.budget)
        if not evidence._safe_rel(rel) or rel == ".":
            raise safety.Skip("unsafe-path")
        full = self.path / rel
        real = os.path.normcase(os.path.realpath(full))
        base = os.path.normcase(os.path.realpath(self.path))
        if not real.startswith(base.rstrip(os.sep) + os.sep):
            raise safety.Skip("unsafe-path")
        self.budget.check_time()
        return safety.read_file(full, cap, self.budget)

    def zip(self):
        if self.kind != "file":
            raise safety.Skip("unsupported", "a folder is not an archive")
        if self._zip is None:
            self._zip = safety.SafeZip(self.path, self.budget)
        return self._zip

    def digest_of(self, rel, data):
        """The digest a record from rel carries (5.1): a file in a folder target by the proof rule; the single-file
        target itself ('.') its raw digest; an archive member (any depth) its raw bytes."""
        if self.kind == "folder" and "!/" not in rel:
            return proof_digest_bytes(data)
        if self.kind == "file" and rel == ".":
            return self.digest
        return sha256_bytes(data)

    def close(self):
        if self._zip is not None:
            self._zip.close()
            self._zip = None


def _note_text(text):
    """A study note as stored: secrets scrubbed, paths, URLs, e-mails and tokens redacted (6.2: learn.redact applies to
    notes), one line, at most 300 characters."""
    return safety.one_line(redact(safety.scrub(safety.clean_text(str(text)))[0]))[:NOTE_LEN]


class Emitter:
    """What a lane writes through (6.3). emit() builds a record with evidence.make_record and returns its id (the
    first of two records with one id wins); skip() counts a unit not read, by reason; note() adds a study note;
    missing() names an expected part that is absent. Records are kept in memory until the study is written; the caps
    of 6.2 keep the records with the lowest ids per kind and per study, so the kept set never depends on scan order."""

    def __init__(self, study_id, artifact, budget, by):
        self.study = study_id
        self.artifact = artifact
        self.budget = budget
        self.by = by
        self.records = {}
        self._kinds = {}
        self._over = set()
        self._limit = {}
        self._texts = {}
        self._alias = {}
        self._taken = {}
        self._notes = set()
        self._dropped = set()
        self._scrubbed_path = False
        self.coverage = evidence.empty_coverage()

    def text(self, path, text):
        """Register the text of the file at path once (safety.Prepared, linear); later emits for that path take
        their excerpt from it by line= (or col=, a character offset), in time proportional to the line. A line
        inside a private-key block gives <secret:private-key>. The last few texts are kept."""
        if len(self._texts) >= 4:
            self._texts.pop(next(iter(self._texts)))
        self._texts[path] = safety.Prepared(text)

    def _shown_path(self, path):
        """The path as stored: a path holding a credential-like string is stored with the match replaced, and a
        second path that scrubs to the same text gets ~2 after its first placeholder (~3, ...), in the order the lane
        emits them (sorted, 6.3): two files never collapse into one, and nothing of the secret is stored."""
        if not isinstance(path, str):
            return path
        shown = self._alias.get(path)
        if shown is None:
            scrubbed = safety.final(safety.clean_text(path))
            if scrubbed == safety.clean_text(path):
                return path
            n = self._taken[scrubbed] = self._taken.get(scrubbed, 0) + 1
            shown = scrubbed if n == 1 else scrubbed.replace(">", f">~{n}", 1)
            self._alias[path] = shown
            if not self._scrubbed_path:
                self._scrubbed_path = True
                self.note("a path held a credential-like string; it is stored with the match replaced")
        return shown

    def emit(self, kind, key, claim, *, path, digest, basis="observed", line=None, offset=None, excerpt=None,
             data=None, limitations=(), coverage="complete", links=(), by=None, col=None):
        ready = False
        prepared = self._texts.get(path) if excerpt is None else None
        if prepared is not None and (line is not None or col is not None):
            try:
                excerpt, ready = (prepared.at(col) if col is not None else prepared.line(line)) or None, True
            except (TypeError, ValueError):
                excerpt = None
        src = {"artifact": self.artifact, "path": self._shown_path(path), "line": line, "offset": offset,
               "digest": digest, "excerpt": excerpt}
        try:
            rec = evidence.make_record(self.study, kind, key, claim, basis, src, data, limitations, coverage, links,
                                       by=by or self.by, excerpt_ready=ready)
        except (ValueError, TypeError):
            # a value the record rules refuse (an empty key, a path that is not relative, ...) never ends the study;
            # the note never quotes the value
            valid = isinstance(kind, str) and evidence.KIND.fullmatch(kind)
            self.skip(".", "malformed", f"a {kind} record was not stored: a value the record rules refuse" if valid
                      else "a record with an invalid kind was not stored")
            return None
        rid = rec["id"]
        if rid in self.records:
            return rid
        cap = self.budget.limits["evidencePerKind"]
        if kind in self._limit and rid > self._limit[kind]:
            return rid  # past the per-kind cap and above every kept id: it can never be kept
        self.budget.evidence(kind)
        self.records[rid] = rec
        ids = self._kinds.setdefault(kind, [])
        ids.append(rid)
        if len(ids) > 2 * cap:
            self._prune(kind, cap)
        return rid

    def _prune(self, kind, cap):
        ids = sorted(self._kinds[kind])
        for rid in ids[cap:]:
            del self.records[rid]
        self._kinds[kind] = ids[:cap]
        self._limit[kind] = ids[cap - 1]
        self._over.add(kind)

    def skip(self, path, reason, note=None):
        if reason == safety.MISSING:  # what a read raises when the file went away after it was listed
            reason, note = "error", note or "disappeared while reading"
        if reason not in safety.SKIP_REASONS:
            raise ValueError(f"unknown skip reason {reason!r}")
        cov = self.coverage
        cov["filesSkipped"] += 1
        cov["skipped"][reason] = cov["skipped"].get(reason, 0) + 1
        if note:
            self.note(note)

    def note(self, text, force=False):
        text = _note_text(text)
        if not text or text in self._notes:
            return
        if len(self._notes) >= NOTES_MAX and not force:
            self._dropped.add(text)
            return
        self._notes.add(text)
        self.coverage["notes"].append(text)

    def missing(self, part):
        part = _note_text(part)
        if part and part not in self.coverage["missing"]:
            self.coverage["missing"].append(part)

    def finish(self):
        """The records to store (sorted by id, after the caps) and the coverage."""
        cap, total = self.budget.limits["evidencePerKind"], self.budget.limits["evidence"]
        cov = self.coverage
        for kind in sorted(self._kinds):
            if len(self._kinds[kind]) > cap:
                self._prune(kind, cap)
            if kind in self._over:
                self.note(f"{kind}: more than {cap} records; the {cap} with the lowest ids were kept", force=True)
        kept = sorted(self.records)
        if len(kept) > total:
            for rid in kept[total:]:
                del self.records[rid]
            kept = kept[:total]
            self.note(f"more than {total} records; the {total} with the lowest ids were kept", force=True)
            self._over.add("*")
        if self._over:
            cov["truncated"] = True
            cov["stoppedBy"] = cov["stoppedBy"] or "evidence-limit"
        cov["bytesHashed"] = self.budget.bytes_hashed
        cov["filesSeen"] = self.budget.files_seen
        cov["filesScanned"] = self.budget.files_scanned
        cov["bytesRead"] = self.budget.bytes_read
        cov["skipped"] = dict(sorted(cov["skipped"].items()))
        if self.budget.hardlinks:
            self.note(f"{self.budget.hardlinks} file(s) read have more than one hard link (another name may be "
                      "outside the target)", force=True)
        if self._dropped:
            cov["notes"].append(f"{len(self._dropped)} more notes not kept")
        return [self.records[r] for r in kept], cov


# ------------------------------------------------------------------------------------------------ authorization

def authorize(kind, target_is_root, tracked, authorization=None, own=False):
    """The authorization of a study (6.1) as stored, and whether the target is the user's own. Only the source lane
    on the project root, or on a folder inside it that git tracks (`tracked`, decided by the caller), is implicitly
    the user's own. Anything else needs --own or an --authorization statement of at least 15 characters (stored with
    learn.redact and secrets scrubbed, otherwise verbatim); without one: not-authorized (exit 3)."""
    statement = safety.one_line(authorization) if isinstance(authorization, str) else None
    if statement is not None and len(statement) > STATEMENT_MAX:
        raise BlueprintError("usage", f"the --authorization statement must be at most {STATEMENT_MAX} characters",
                             code=64, next=f"{core.AGAIN} with a shorter statement")
    implicit = kind == "source" and (target_is_root or tracked)
    if statement is not None and len(statement.strip()) < AUTHORIZATION_MIN:
        raise BlueprintError("not-authorized", f"the --authorization statement must be at least {AUTHORIZATION_MIN} "
                             "characters, in the user's own words. " + NOT_AUTHORIZED, code=3,
                             next=f'{core.AGAIN} with --authorization "<what the user said>" or --own')
    if own:
        text, mine = OWN_STATEMENT, True
    elif statement is not None:
        text, mine = safety.scrub(redact(safety.clean_text(statement)))[0], implicit
    elif implicit:
        text, mine = PROJECT_STATEMENT, True
    else:
        raise BlueprintError("not-authorized", NOT_AUTHORIZED, code=3,
                             next=f'{core.AGAIN} with --authorization "<what the user said>" or --own')
    return {"statement": text, "by": None, "at": now_iso()}, mine


# ------------------------------------------------------------------------------------------------ the study record

def default_id(target_path):
    """S- + slug of the target's basename without its extension (6.1)."""
    p = Path(target_path)
    name = p.stem if p.is_file() else p.name
    return "S-" + core.slug(safety.scrub(safety.clean_text(name))[0] or "project")


def check_new_id(root, sid, replace):
    """The id a new study may take: a study id (whole value), not S-notes, and not taken unless replace."""
    if not evidence.is_study_id(sid):
        raise BlueprintError("usage", "--id must be S- then lowercase letters, digits and hyphens (at most 50 "
                             "characters)", code=64, next=f"{core.AGAIN} with a valid --id")
    if sid == evidence.NOTES:
        raise BlueprintError("usage", "S-notes is reserved for evidence add; choose another --id", code=64,
                             next=f"{core.AGAIN} with another --id")
    folder = evidence.studies_dir(root) / sid
    if not replace and (folder / evidence.STUDY_FILE).exists():
        raise BlueprintError("study-exists", f"study {sid} exists already", entry=f"study {sid}",
                             next=f"{core.AGAIN} with --replace to study it again, or with another --id")
    return sid


def study_record(sid, kind, target, authorization, own, lanes, limits, coverage, records, at=None):
    """study.json (6.1)."""
    rel = None
    if target.root is not None:
        try:
            r = os.path.relpath(os.path.realpath(target.path), os.path.realpath(target.root))
            if not (r == ".." or r.startswith(".." + os.sep) or os.path.isabs(r)):
                rel = r.replace(os.sep, "/")
        except ValueError:
            rel = None
    return {"id": sid, "kind": kind,
            "target": {"name": safety.scrub(safety.clean_text(target.name))[0], "path": rel, "digest": target.digest,
                       "size": target.size, "own": own},
            "authorization": authorization, "tool": TOOL_VERSION, "at": at or now_iso(),
            "lanes": list(lanes), "limits": dict(limits), "coverage": coverage,
            "counts": evidence.counts_of(records)}


def _cited(root):
    """Evidence ids cited by any behaviours.json and by unknowns[].evidenceFor/evidenceAgainst of the plan; None when
    one of them cannot be read (then every old record is kept, superseded: fail closed, 5.5)."""
    cited = set()
    plan, problem = evidence._read_json_file(core.bp_dir(Path(root)) / "blueprint.json")
    if problem:
        return None
    for u in (plan.get("unknowns") if isinstance(plan, dict) and isinstance(plan.get("unknowns"), list) else []):
        for key in ("evidenceFor", "evidenceAgainst"):
            vals = u.get(key) if isinstance(u, dict) else None
            if isinstance(vals, list):
                cited.update(x for x in vals if isinstance(x, str))
    for sid in evidence.study_ids(root):
        beh, problem = evidence.read_behaviours(root, sid)
        if problem:
            return None
        for b in beh:
            ev = b.get("evidence") if isinstance(b, dict) else None
            if isinstance(ev, list):
                cited.update(x for x in ev if isinstance(x, str))
    return cited


def commit(root, study, records, replace=False):
    """Write a study in one step under core.state_lock (5.5): with replace, the old records that are cited (or all of
    them, when a citation source cannot be read) and not emitted again are kept with superseded: true; then
    evidence.jsonl, study.json (last, so a study.json always describes complete evidence) and the sharing
    .gitignore are written atomically, and the log line appended. A study that exists without replace is
    study-exists, checked again inside the lock."""
    sid = study["id"]
    new = {r["id"]: r for r in records}
    with core.state_lock(root):
        folder = evidence.studies_dir(root) / sid
        if (folder / evidence.STUDY_FILE).exists() and not replace:
            raise BlueprintError("study-exists", f"study {sid} exists already", entry=f"study {sid}",
                                 next=f"{core.AGAIN} with --replace to study it again, or with another --id")
        if replace and folder.is_dir():
            cited = _cited(root)
            for old in evidence.load(root, sid):
                if old["id"] in new or (cited is not None and old["id"] not in cited):
                    continue
                new[old["id"]] = dict(old, superseded=True)
        out = evidence.sort_records(list(new.values()))
        study = dict(study, counts=evidence.counts_of(out))
        evidence.write_records_held(root, sid, out)
        evidence.write_study_held(root, study)
        evidence.sharing_held(root, sid, study["target"]["own"] is True)
        core.append_log(root, {"event": "study", "study": sid, "kind": study["kind"], "lanes": study["lanes"],
                               "evidence": study["counts"]["evidence"]})
    return study


def run_study(root, kind, target_path, study_id=None, authorization=None, own=False, replace=False,
              time_limit=300, lanes=None, *, tracked=False, opts=None, clock=None):
    """Study a target and store the result (6.3): the target is checked (target-not-found; a link is
    target-unsupported), authorized (6.1), read by every lane in order with one Budget (a Stop sets stoppedBy and
    truncated and ends the reading), and the records are written in one locked step (commit). Returns the study
    record. `lanes` are lane objects (LANE, detect, run); `tracked` says the target is a folder git tracks inside the
    project (decided by the caller, which may run git); `opts` is passed to every lane, with projectRoot,
    isProjectRoot and study added."""
    started = now_iso()
    root = Path(root)
    lanes = list(lanes or [])
    raw = Path(target_path)
    if not os.path.lexists(raw):
        raise BlueprintError("target-not-found", "nothing exists at the target path given",
                             next=f"{core.AGAIN} with the path of a folder or file that exists")
    if safety.is_link(raw) and os.path.normcase(os.path.realpath(raw)) != os.path.normcase(os.path.realpath(root)):
        raise BlueprintError("target-unsupported", "the target is a link; links are never followed. Give the path "
                             "of the folder or file itself", next=f"{core.AGAIN} with the real path")
    real = Path(os.path.realpath(raw))
    if not (real.is_dir() or real.is_file()):
        raise BlueprintError("target-unsupported", "the target is neither a folder nor a regular file",
                             next=f"{core.AGAIN} with the path of a folder or file")
    is_root = os.path.normcase(str(real)) == os.path.normcase(os.path.realpath(root))
    try:
        t = int(time_limit)
    except (TypeError, ValueError, OverflowError):
        t = -1
    if not (0 < t <= safety.LIMITS["timeLimitMax"]):
        raise BlueprintError("usage", f"--time-limit must be a whole number of seconds from 1 to "
                             f"{safety.LIMITS['timeLimitMax']}", code=64, next=f"{core.AGAIN} with another value")
    auth, mine = authorize(kind, is_root, tracked, authorization, own)
    for lane in lanes:
        detect = getattr(lane, "detect", None)
        if callable(detect) and not detect(real):
            name = getattr(lane, "LANE", None) or kind
            formats = getattr(lane, "FORMATS", None)
            raise BlueprintError("target-unsupported", f"the {name} lane does not recognise this target"
                                 + (f"; it reads {formats}" if isinstance(formats, str) and formats else "")
                                 + ". For other apps use the tier C import: study import <path> --from "
                                 "rea|jadx|apktool", next=f"{core.AGAIN} with a target this lane reads")
    sid = check_new_id(root, study_id or default_id(real), replace)
    limits = dict(safety.LIMITS, timeLimit=t)
    budget = safety.Budget(limits, clock=clock) if clock is not None else safety.Budget(limits)
    target = Target(real, budget, root=root, is_project_root=is_root)
    lane_names = [getattr(lane, "LANE", None) or getattr(lane, "__name__", "lane").rsplit(".", 1)[-1]
                  for lane in lanes]
    em = Emitter(sid, "." if target.kind == "folder" else target.name, budget,
                 f"tool:{lane_names[0] if lane_names else kind}@{TOOL_VERSION}")
    target.on_skip = em.skip
    target.on_note = em.note
    if target.hash_stopped:
        em.note("the time limit was reached while the target was hashed; it has no digest, so freshness is checked "
                "member by member", force=True)
    if safety.scrub(safety.clean_text(target.name))[1]:
        em.note("the target's name held a credential-like string; it is stored with the match replaced")
    run_opts = dict(opts or {}, projectRoot=root, isProjectRoot=is_root, study=sid)
    try:
        for lane, name in zip(lanes, lane_names):
            em.by = getattr(lane, "BY", None) or f"tool:{name}@{TOOL_VERSION}"
            try:
                lane.run(target, em, run_opts)
            except safety.Skip as e:
                em.skip(".", e.reason if e.reason in safety.SKIP_REASONS else "error", e.note)
            except safety.PARSE_ERRORS as e:  # a lane that let a parser error escape: its unit, not the study, is lost
                em.skip(".", "malformed", f"the {name} lane stopped on malformed input ({type(e).__name__})")
    except safety.Stop as e:
        em.coverage["truncated"] = True
        em.coverage["stoppedBy"] = e.stoppedBy
        em.note(f"stopped by the {e.stoppedBy}; what was read before is kept", force=True)
    finally:
        target.close()
    records, coverage = em.finish()
    study = study_record(sid, kind, target, auth, mine, lane_names, limits, coverage, records, at=started)
    return commit(root, study, records, replace)


# ------------------------------------------------------------------------------------------------ manifest_facts

_MAIN, _LAUNCHER = "android.intent.action.MAIN", "android.intent.category.LAUNCHER"
_VIEW, _BROWSABLE = "android.intent.action.VIEW", "android.intent.category.BROWSABLE"
SETTINGS = ("usesCleartextTraffic", "debuggable", "allowBackup", "networkSecurityConfig")


def _local(name):
    """An element or attribute name without its namespace: '{uri}name', 'android:name' and 'name' are 'name'."""
    s = str(name)
    if s.startswith("{") and "}" in s:
        s = s.split("}", 1)[1]
    return s.split(":", 1)[1] if ":" in s else s


def _as_text(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(int(v))
    return str(v)


def _as_int(v):
    """versionCode, minSdk, targetSdk (6.7): an integer when the value is all decimal digits, else the text as
    written; absent: None."""
    if v is None:
        return None
    if isinstance(v, int) and not isinstance(v, bool):
        return int(v)
    s = _as_text(v)
    return int(s) if s.isascii() and s.isdigit() else s


def _as_bool(v):
    """exported, launcher, autoVerify (6.7): a boolean when the value is true or false (any case), else None."""
    if isinstance(v, bool):
        return v
    s = v.lower() if isinstance(v, str) else None
    return {"true": True, "false": False}.get(s)


def _tree(elements):
    """Nodes {name, attrs, children} from [(depth, name, attrs)]; entries of another shape are left out."""
    nodes, stack = [], []
    for item in elements if isinstance(elements, (list, tuple)) else []:
        if not (isinstance(item, (list, tuple)) and len(item) == 3 and isinstance(item[0], int)
                and isinstance(item[1], str) and isinstance(item[2], dict)):
            continue
        depth, name, attrs = item
        node = {"name": _local(name), "attrs": {_local(k): v for k, v in attrs.items()}, "children": []}
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if stack:
            stack[-1][1]["children"].append(node)
        stack.append((depth, node))
        nodes.append(node)
    return nodes


def _qualify(name, package):
    """A component name resolved against the package: '.Name' and 'Name' (no dot) belong to it (6.7)."""
    if not isinstance(name, str) or not name:
        return None
    if name.startswith("."):
        return (package or "") + name
    if "." not in name and package:
        return f"{package}.{name}"
    return name


def _names(node, child):
    return [c["attrs"].get("name") for c in node["children"] if c["name"] == child]


def manifest_facts(elements, em, *, path, digest, by, basis="observed", limitations=()):
    """The Android rows of 6.7 (mobile.package, permission, screen, service, deeplink, setting) from manifest
    elements in the axml.parse shape [(depth, name, attrs)]: from AXML, aapt2 proto manifests and text manifests
    alike (attribute names with or without a namespace). Value types as 6.7 says. Returns the ids emitted."""
    nodes = _tree(elements)
    manifest = next((n for n in nodes if n["name"] == "manifest"), None)
    if manifest is None:
        return []
    out = []
    lims = list(limitations)

    def emit(kind, key, claim, data):  # a key the record rules refuse becomes a malformed skip (Emitter.emit)
        out.append(em.emit(kind, key, claim, path=path, digest=digest, basis=basis, data=data, limitations=lims,
                           by=by))

    attrs = manifest["attrs"]
    package = attrs.get("package") if isinstance(attrs.get("package"), str) else None
    sdk = next((c["attrs"] for c in manifest["children"] if c["name"] == "uses-sdk"), {})
    if package:
        emit("mobile.package", package, f"The Android app's package is {package}.",
             {"versionName": _as_text(attrs.get("versionName")), "versionCode": _as_int(attrs.get("versionCode")),
              "minSdk": _as_int(sdk.get("minSdkVersion")), "targetSdk": _as_int(sdk.get("targetSdkVersion"))})
    for name in _names(manifest, "uses-permission"):
        if isinstance(name, str):
            emit("mobile.permission", name, f"The app requests the permission {name}.", {})
    for app in (c for c in manifest["children"] if c["name"] == "application"):
        for setting in SETTINGS:
            if setting in app["attrs"]:
                value = _as_text(app["attrs"][setting])
                emit("mobile.setting", setting, f"The application sets {setting} to {value}.", {"value": value})
        for comp in app["children"]:
            cname = _qualify(comp["attrs"].get("name"), package)
            filters = [c for c in comp["children"] if c["name"] == "intent-filter"]
            exported = _as_bool(comp["attrs"].get("exported"))
            if comp["name"] in ("activity", "activity-alias") and cname:
                launcher = any(_MAIN in _names(f, "action") and _LAUNCHER in _names(f, "category") for f in filters)
                emit("mobile.screen", cname, f"The app has the screen {cname}.",
                     {"exported": exported, "launcher": launcher})
            elif comp["name"] in ("service", "receiver", "provider") and cname:
                emit("mobile.service", cname, f"The app declares the {comp['name']} {cname}.",
                     {"type": comp["name"], "exported": exported})
            for f in filters:
                if _VIEW not in _names(f, "action") or _BROWSABLE not in _names(f, "category"):
                    continue
                datas = [c["attrs"] for c in f["children"] if c["name"] == "data"]
                schemes = [d["scheme"] for d in datas if isinstance(d.get("scheme"), str) and d["scheme"]]
                hosts = [d["host"] for d in datas if isinstance(d.get("host"), str) and d["host"]]
                paths = [d[k] for d in datas for k in ("path", "pathPrefix", "pathPattern")
                         if isinstance(d.get(k), str) and d[k]]
                auto = _as_bool(f["attrs"].get("autoVerify"))
                for scheme in schemes:
                    for host in hosts or [None]:
                        for p in (paths if host else []) or [None]:
                            key = f"{scheme}://" + (host or "") + ("" if p is None else (p if p.startswith("/")
                                                                                          else "/" + p))
                            emit("mobile.deeplink", key, f"The app opens links to {key}.",
                                 {"activity": cname, "autoVerify": auto})
    return out
