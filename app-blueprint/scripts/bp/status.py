"""status: the disk scan, the file ladder, proofs and the 1.x guard (contract 1.3, 1.5, 4.5).

From 2.0 a file is verified when its proof is current (proof_current): every input (the file and the transitive
closure of its planned dependsOn) still hashes, text normalised, to what verify recorded. Hasher hashes each version
of a file once per process.
"""
import os
import re
import subprocess

from bp import core
from bp.core import die, norm, now_iso
from bp.globs import glob_match, pattern_instance_rx
from bp.migrate import upgrade_state
from bp.plan import DEFAULT_IGNORE, STATES, features_of, item_verify, owner_of, phase_index, planned_file


def load_state(root, owned=()):
    """state.json for reading, upgraded in memory (as plan.load_state); a state that cannot be used is bad-state
    (core.read_state). Readers take no lock: writes are atomic, so a reader sees the old or the new file."""
    return core.read_state(root, upgrade_state, owned)


def change_state(root, change, owned=()):
    """The one way commands change state.json: core.update_state with the in-memory upgrade (contract 3.2)."""
    return core.update_state(root, change, upgrade_state, owned)


class Hasher:
    """Proof digests with a cache keyed by (path, size, mtime_ns), so each version of a file is read once."""

    def __init__(self):
        self._cache = {}

    def _get(self, path):
        st = os.stat(path)
        key = (os.path.normcase(os.path.abspath(path)), st.st_size, st.st_mtime_ns)
        hit = self._cache.get(key)
        if hit is None:
            hit = self._cache[key] = core._file_digests(path)
        return hit

    def digest(self, path):
        """sha256:<hex> with text line endings normalised (core.proof_digest)."""
        return self._get(path)[0]

    def sha1(self, path):
        """sha1 hex of the same normalised content: the `sha` of a file proof (4.5)."""
        return self._get(path)[1]


HASHER = Hasher()  # one per process (contract 4.5)


def _safe_rel(p):
    """A repo-relative path with forward slashes, or None for an absolute path, a drive path or one with '..'."""
    p = norm(p)
    if not p or p.startswith("/") or re.match(r"^[A-Za-z]:", p) or ".." in p.split("/"):
        return None
    return p


def proof_inputs(root, plan, paths):
    """{path: proof digest} for paths plus the transitive closure of dependsOn over planned files (a visited set, so
    dependsOn cycles end), keeping only files present on disk; sorted by path (contract 4.5). Every path is taken
    literally, whatever its characters ([id], (group), a*b): a dependsOn entry is an input when a file of that name
    exists, so a pattern id or glob drops out unless such a file exists; only planned files' dependsOn are followed."""
    deps = {}
    for f in plan.get("files") or []:
        if isinstance(f, dict) and isinstance(f.get("path"), str):
            deps.setdefault(norm(f["path"]), [d for d in f.get("dependsOn") or [] if isinstance(d, str)])
    seen, todo = set(), [norm(p) for p in paths]
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        todo.extend(norm(d) for d in deps.get(p, ()))
    out = {}
    for p in sorted(seen):
        # every path is taken literally: the proven file and planned files whatever their characters ([id], (group),
        # a*b); a dependsOn entry that is a pattern id or a glob is simply not a file on disk, so it drops out here
        rel = _safe_rel(p)
        if rel is None:
            continue
        fp = root / rel
        try:
            present = fp.is_file()
        except (OSError, ValueError):  # a name the file system cannot hold (e.g. * on Windows)
            present = False
        if present:
            out[rel] = HASHER.digest(fp)
    return out


def proof_current(root, plan, proof, paths=None):
    """True only for a proof that still holds (contract 4.5); anything malformed, failed or unknown is False.

    A file proof ({"ok", "cmd", "sha", "inputs"}) needs "ok" exactly true; a feature proof is {"files": {...}} only.
    With "inputs" (2.0; "files" in a feature proof), the inputs computed now
    must equal the stored ones, set and digests: for `paths` (the proven file, or a feature's items) when given, else
    for the stored paths themselves (which catches edits, deletions and added dependencies, not removed ones). A 1.x
    proof without inputs is current when its sha equals the sha1 of the normalised file; that needs paths=[file]."""
    if not isinstance(proof, dict):
        return False
    is_feature_proof = "files" in proof and "inputs" not in proof and "ok" not in proof and "sha" not in proof
    if not is_feature_proof and proof.get("ok") is not True:  # a file proof counts only with "ok": true (review N4)
        return False
    inputs = proof.get("files") if is_feature_proof else proof.get("inputs")
    if inputs is None:
        if proof.get("ok") is not True or not isinstance(proof.get("sha"), str) or not paths or len(paths) != 1:
            return False
        rel = _safe_rel(paths[0])
        fp = root / rel if rel else None
        return bool(fp and fp.is_file() and HASHER.sha1(fp) == proof["sha"])
    if not isinstance(inputs, dict) or not inputs or not all(isinstance(v, str) for v in inputs.values()):
        return False
    return proof_inputs(root, plan, list(paths) if paths is not None else list(inputs)) == inputs


def folder_rule_segments(rule):
    """The folder names of a plain folder rule ('src/legacy/' -> ['src', 'legacy']); None for any other rule."""
    r = rule.strip().replace("\\", "/")
    if not r.endswith("/") or any(c in r for c in "*?["):
        return None
    return [x for x in r.strip("/").split("/") if x] or None


def repo_files(root, plan, ignored_tracked=None):
    """Files to compare with the plan. An ignored path is kept only if it is an individually planned file, or it
    matches a pattern glob that names, as literal folder segments, every folder of the plain folder rule(s) that
    drop it. If ignored_tracked is a list, tracked files that DEFAULT_IGNORE dropped are appended to it."""
    ignore = DEFAULT_IGNORE + list(plan.get("ignore", []))
    planned = {norm(f.get("path", "")) for f in plan["files"]}
    pats = [(pattern_instance_rx(p["glob"]), norm(p["glob"]).lstrip("/").split("/"))
            for p in plan["patterns"] if p.get("glob")]

    def named_by(rules, gsegs):
        for g in rules:
            segs = folder_rule_segments(g)
            if segs is None or not all(x in gsegs for x in segs):
                return False
        return True

    def kept(path, rules):
        if path in planned:
            return True
        hits = [g for g in rules if glob_match(g, path)]
        return not hits or any(rx.match(path) and named_by(hits, gsegs) for rx, gsegs in pats)

    def planned_inside(rel):
        return any(f.startswith(rel + "/") for f in planned)

    def prune(rel):
        hits = [g for g in ignore if glob_match(g, rel + "/x")]
        return bool(hits) and not planned_inside(rel) and not any(named_by(hits, gsegs) for _, gsegs in pats)

    files = None
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                             cwd=root, capture_output=True, timeout=60)
        if out.returncode == 0:
            files = [norm(p) for p in out.stdout.decode("utf-8", "replace").split("\0") if p]
            if ignored_tracked is not None:
                tr = subprocess.run(["git", "ls-files", "--cached", "-z"], cwd=root, capture_output=True, timeout=60)
                if tr.returncode == 0:
                    tracked = {norm(p) for p in tr.stdout.decode("utf-8", "replace").split("\0") if p}
                    ignored_tracked.extend(sorted(p for p in tracked if (root / p).is_file() and not kept(p, DEFAULT_IGNORE)
                                                  and not p.startswith((".blueprint/", ".git/"))))
    except (OSError, subprocess.SubprocessError):
        files = None
    if files is None:
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = norm(os.path.relpath(dirpath, root))
            rel_dir = "" if rel_dir == "." else rel_dir
            dirnames[:] = [d for d in dirnames if not prune((rel_dir + "/" if rel_dir else "") + d)]
            for fn in filenames:
                files.append((rel_dir + "/" if rel_dir else "") + fn)
    return sorted(p for p in files if (root / p).is_file() and kept(p, ignore))


def effective_status(root, plan, state, f):
    rec = state["files"].get(f["path"], {})
    present = (root / f["path"]).is_file()
    if f.get("action") == "delete":
        return ("verified" if not present else (rec.get("state") if rec.get("state") in ("claimed", "blocked") else "planned")), present
    if rec.get("state") == "blocked":
        return "blocked", present
    if not present:
        return ("claimed" if rec.get("state") == "claimed" else "planned"), present
    if proof_current(root, plan, rec.get("verified"), paths=[f["path"]]):
        return "verified", present
    if rec.get("state") in ("built", "verified"):
        return "built", present
    return "ondisk", present


def compute(root, plan, state):
    """Everything the views need, in one dict."""
    rows = []
    for f in plan["files"]:
        st, present = effective_status(root, plan, state, f)
        owner, rule, _ = owner_of(plan, f["path"])
        rec = state["files"].get(f["path"], {})
        ver = rec.get("verified")
        # stale: the last proof passed but no longer holds (inputs changed); verify --stale re-runs these (4.7)
        stale = (st != "verified" and present and f.get("action") != "delete" and isinstance(ver, dict)
                 and ver.get("ok") is True and not proof_current(root, plan, ver, paths=[f["path"]]))
        rows.append({"path": f["path"], "kind": "file", "action": f.get("action"), "module": f.get("module"),
                     "phase": f.get("phase"), "purpose": f.get("purpose", ""), "owner": owner, "rule": rule,
                     "contract": bool(f.get("contract")), "status": st, "present": present,
                     "consumes": f.get("consumes", []), "produces": f.get("produces", []),
                     "dependsOn": [norm(d) for d in f.get("dependsOn", [])], "verify": item_verify(plan, f),
                     "ac": f.get("ac", []), "by": rec.get("by"), "at": rec.get("at"), "note": rec.get("note"),
                     "uses": [u for u in f.get("uses", []) if isinstance(u, dict)],
                     "observed": ((state.get("data") or {}).get("observed") or {}).get(f["path"], {}),
                     "features": features_of(f), "infrastructure": f.get("infrastructure") is True, "stale": stale})
    pats = []
    for p in plan["patterns"]:
        owner, rule, _ = owner_of(plan, p.get("glob", ""))
        inst = state.get("patterns", {}).get(p.get("id"), {}).get("instances", [])
        pats.append({"id": p.get("id"), "glob": p.get("glob"), "module": p.get("module"), "phase": p.get("phase"),
                     "purpose": p.get("purpose", ""), "instance": p.get("instance", ""), "owner": owner, "rule": rule,
                     "examples": p.get("examples", []), "instances": inst, "verify": item_verify(plan, p)})
    counts = {s: 0 for s in STATES}
    for r in rows:
        counts[r["status"]] += 1
    return {"rows": rows, "patterns": pats, "counts": counts}


def summarize(rows, key):
    out = {}
    for r in rows:
        k = r.get(key) or "(none)"
        d = out.setdefault(k, {s: 0 for s in STATES})
        d[r["status"]] += 1
    return out


def next_ready(plan, rows, owner=None, blocked_phases=frozenset()):
    """Open files of the lowest open phase whose dependencies are built. Files of a blocked phase (an open blocking
    unknown, 2.6) are never offered; the lowest open phase that is not blocked is offered instead."""
    pidx = phase_index(plan)
    by_path = {r["path"]: r for r in rows}
    open_rows = [r for r in rows if r["status"] in ("planned", "claimed", "ondisk") and r["phase"] not in blocked_phases]
    if not open_rows:
        return []
    current = min(pidx.get(r["phase"], 99) for r in open_rows)
    ready = []
    for r in open_rows:
        if pidx.get(r["phase"], 99) != current or (owner and r["owner"] != owner):
            continue
        deps = [by_path.get(d) for d in r["dependsOn"]]
        if all(d is None or d["status"] in ("built", "verified") for d in deps):
            ready.append(r)
    return sorted(ready, key=lambda r: r["path"])


def require_owner(plan, path, by, force):
    owner, _, _ = owner_of(plan, path)
    if owner != by and not force:
        die(f"{path} is owned by {owner}, not {by}. Ask its owner, or use --force if you are the lead reassigning it.", 3)


def worked_by(entry):
    """Every id that claimed or marked a file entry, in first-seen order (state.files[p].workedBy). An entry written
    before the key existed reads as [by] when by is set, else []. Public: the features and records modules read it."""
    if not isinstance(entry, dict):
        return []
    ids = entry.get("workedBy")
    if isinstance(ids, list):
        return [x for x in ids if isinstance(x, str)]
    by = entry.get("by")
    return [by] if isinstance(by, str) and by else []


def set_file_state(root, path, new_state, by, note=None):
    """claim and mark: one file's record (state, by, at, note; planned drops its proof), changed under the state lock
    on the state as it is then, with its log line. workedBy only grows: by joins it (once, as given) and nothing here
    or elsewhere removes an id; only the entry's own removal does (verify and sync never touch it)."""
    def change(state):
        rec = state["files"].setdefault(path, {})
        ids = worked_by(rec)
        if isinstance(by, str) and by and by not in ids:
            ids.append(by)
        rec.update({"state": new_state, "by": by, "at": now_iso(), "workedBy": ids})
        if note is not None:
            rec["note"] = note
        if new_state == "planned":
            rec.pop("verified", None)
        return [{"event": new_state, "path": path, "by": by, "note": note}]
    change_state(root, change)


def guard_decision(root, plan, path, by):
    path = norm(path)
    if path.startswith(".blueprint/"):
        return 0, ""
    if planned_file(plan, path) is None and not any(pattern_instance_rx(p.get("glob", "")).match(path) for p in plan["patterns"]):
        if any(glob_match(g, path) for g in DEFAULT_IGNORE + list(plan.get("ignore", []))):
            return 0, ""
        return 2, (f"{path} is not in the blueprint (.blueprint/blueprint.json). Do not create unplanned files: "
                   f"propose a blueprint change (file entry with purpose, module, phase, owner) and get it approved.")
    if by:
        owner, _, _ = owner_of(plan, path)
        if owner != by:
            return 2, f"{path} is owned by {owner}, not {by}. Write only files you own; ask {owner} or the lead."
    return 0, ""
