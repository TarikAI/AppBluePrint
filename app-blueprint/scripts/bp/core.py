"""core: paths, io, time, git, errors, digests and ids (contract 1.3); the 1.x part moved from blueprint.py 1.3.0.

TOOL_HOME is the folder holding the launcher (scripts/ in the skill, .blueprint/bin in a project); 1.x called it HERE.
"""
import codecs
import contextlib
import datetime as _dt
import errno
import hashlib
import json
import os
import random
import re
import secrets
import stat
import subprocess
import sys
import time
from pathlib import Path

try:  # the state lock (state_lock): msvcrt on Windows, flock on POSIX
    import msvcrt
    fcntl = None
except ImportError:
    msvcrt = None
    import fcntl


TOOL_HOME = Path(__file__).resolve().parent.parent


def find_root(arg_root):
    if arg_root:
        return Path(arg_root).resolve()
    if TOOL_HOME.name == "bin" and TOOL_HOME.parent.name == ".blueprint":
        return TOOL_HOME.parent.parent
    cur = Path.cwd().resolve()
    for p in [cur] + list(cur.parents):
        if (p / ".blueprint" / "blueprint.json").exists():
            return p
    return cur


def bp_dir(root):
    return root / ".blueprint"


def now_iso():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path, default=None):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _retry_windows(fn, *args, budget=2.0):
    """fn(*args), retried for up to `budget` seconds on Windows while another process has the file open (a reader
    holding state.json makes os.replace fail with PermissionError there). Elsewhere, or past the budget, it raises."""
    deadline = time.monotonic() + budget
    delay = 0.01
    while True:
        try:
            return fn(*args)
        except PermissionError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.1)


def write_json(path, data):
    """Write JSON atomically, with the 1.x bytes: through a temp file unique to this process and call
    (<name>.<pid>-<random>.tmp, beside the file, so concurrent writers never share one), then os.replace. The temp
    file is removed on any failure. A read-only target on Windows fails at once rather than being retried."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "x", encoding="utf-8") as f:
            f.write(text)
        if os.name == "nt" and path.is_file() and not os.access(path, os.W_OK):
            os.replace(tmp, path)  # read-only: no other process can make this succeed
        else:
            _retry_windows(os.replace, tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_text_if_changed(path, text):
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return True
    return False


def _append_log_line(root, event):
    event = dict(event)
    event.setdefault("at", now_iso())
    with (bp_dir(root) / "log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def append_log(root, event):
    """Append one event to log.jsonl as one line, inside the state lock (taken here unless this process holds it), so
    parallel writers never tear or interleave lines."""
    if _holding(root):
        _append_log_line(root, event)
        return
    with state_lock(root):
        _append_log_line(root, event)


def norm(p):
    p = str(p).replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def copy_json(obj):
    """A deep copy of parsed JSON (dicts, lists, scalars) made without recursion, so it works at any depth the JSON
    reader accepts (copy.deepcopy recurses and fails at about 450 levels). Dict key order is kept; scalars and other
    objects are shared (scalars are immutable); a container reached twice is copied each time. A cycle (a container
    inside itself, impossible in parsed JSON) raises ValueError("cyclic value") at once (review P10-2)."""
    if not isinstance(obj, (dict, list)):
        return obj
    root = {} if isinstance(obj, dict) else []
    stack = [(iter(obj.items()) if isinstance(obj, dict) else iter(obj), root, id(obj))]
    active = {id(obj)}  # the containers on the current path: meeting one again is a cycle
    while stack:
        items, dst, src_id = stack[-1]
        item = next(items, _END)
        if item is _END:
            stack.pop()
            active.discard(src_id)
            continue
        key, v = item if isinstance(dst, dict) else (None, item)
        if isinstance(v, (dict, list)):
            if id(v) in active:
                raise ValueError("cyclic value")
            child = {} if isinstance(v, dict) else []
            stack.append((iter(v.items()) if isinstance(v, dict) else iter(v), child, id(v)))
            active.add(id(v))
        else:
            child = v
        if isinstance(dst, dict):
            dst[key] = child
        else:
            dst.append(child)
    return root


_END = object()


def json_depth(obj):
    """How deeply parsed JSON nests containers (0 for a scalar, 1 for [] or {}), measured without recursion."""
    deepest, stack = 0, [(obj, 1)]
    while stack:
        v, d = stack.pop()
        if isinstance(v, dict):
            deepest = max(deepest, d)
            stack.extend((x, d + 1) for x in v.values())
        elif isinstance(v, list):
            deepest = max(deepest, d)
            stack.extend((x, d + 1) for x in v)
    return deepest


def path_key(rel):
    """The form to compare two repo-relative paths with: os.path.normcase (lower case on Windows) and forward
    slashes. refuse hooks compare paths with it, as guard and hook pre do (contract 1.4)."""
    return os.path.normcase(str(rel)).replace("\\", "/")


def _strip_long_prefix(raw):
    r"""Drop a leading \\?\ (and turn \\?\UNC\server\share into \\server\share)."""
    s = str(raw)
    if s[:8].upper() == "\\\\?\\UNC\\":
        return "\\\\" + s[8:]
    if s.startswith("\\\\?\\"):
        return s[4:]
    return s


def project_path(root, raw):
    r"""The repo-relative path guard and hook pre decide on (one function, so they cannot drift), or None when it is
    outside the project.

    raw is taken against the project root (an absolute path as it is); \\?\ prefixes are dropped; '.', '..',
    repeated separators, symlinks and junctions are resolved, and case and separators come from the disk for the part
    that exists. When the drive spellings differ (an admin share \\host\C$, subst, a mapped drive), the resolved
    path's existing ancestors are compared with the root by os.path.samefile. On Windows the part that does not exist
    yet loses, in each segment, everything from its first ':' (an alternate data stream names the same file) and
    then the trailing dots and spaces, as Windows does when it creates the file. Forward
    slashes; compare results with path_key."""
    p = Path(_strip_long_prefix(raw))
    if not p.is_absolute():
        p = Path(root) / p
    root_r = Path(root).resolve()
    try:
        full = p.resolve()
    except (OSError, ValueError):
        return None
    rel = None
    try:
        r = os.path.relpath(full, root_r)
        if not (r == ".." or r.startswith(".." + os.sep) or os.path.isabs(r)):
            rel = r
    except ValueError:  # another drive spelling
        pass
    if rel is None:
        for anc in [full] + list(full.parents):
            try:
                if anc.exists() and os.path.samefile(anc, root_r):
                    rel = str(full.relative_to(anc)) or "."
                    break
            except (OSError, ValueError):
                continue
    if rel is None:
        return None
    parts = [x for x in norm(rel).split("/") if x not in ("", ".")]
    if os.name == "nt":  # the part that does not exist yet: Windows drops trailing dots and spaces on create
        base, out = root_r, []
        for i, seg in enumerate(parts):
            if (base / seg).exists():
                base = base / seg
                out.append(seg)
                continue
            # a segment loses everything from its first ':' (an alternate data stream, '::$DATA' or ':x', names the
            # same file), then its trailing dots and spaces; the drive letter is in the root part, never here
            out += [s.split(":", 1)[0].rstrip(" .") or s for s in parts[i:]]
            break
        parts = out
    return "/".join(parts) if parts else "."


def file_sha(path):
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


_CHUNK = 65536


def _file_digests(path):
    """(sha256 digest string, sha1 hex) of a file under the proof rule (contract 4.5), in one streaming pass.

    A file that decodes as UTF-8 and has no NUL byte is text: CRLF and lone CR become LF before hashing. Any other
    file is hashed as raw bytes. Both the raw and the normalised hashes are updated per 64 KiB chunk (a CR at the end
    of a chunk is carried to the next, so a CRLF split across chunks is one line end); the normalised pair is
    returned only if the whole file was text. Status.Hasher caches the result per file version."""
    raw256, raw1 = hashlib.sha256(), hashlib.sha1()
    norm256, norm1 = hashlib.sha256(), hashlib.sha1()
    decoder = codecs.getincrementaldecoder("utf-8")()
    text, carry = True, False
    with path.open("rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            raw256.update(chunk)
            raw1.update(chunk)
            if not text:
                continue
            if b"\x00" in chunk:
                text = False
                continue
            try:
                decoder.decode(chunk)
            except UnicodeDecodeError:
                text = False
                continue
            data = (b"\r" + chunk) if carry else chunk
            carry = data.endswith(b"\r")
            if carry:
                data = data[:-1]
            data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            norm256.update(data)
            norm1.update(data)
    if text:
        try:
            decoder.decode(b"", final=True)  # a multi-byte character cut off at the end is not text
        except UnicodeDecodeError:
            text = False
    if text and carry:
        norm256.update(b"\n")
        norm1.update(b"\n")
    h256, h1 = (norm256, norm1) if text else (raw256, raw1)
    return "sha256:" + h256.hexdigest(), h1.hexdigest()


def proof_digest(path):
    """sha256:<hex> of a file, with text line endings normalised (contract 4.5). Streams; never reads a file whole."""
    return _file_digests(path)[0]


def sha256_bytes(b):
    return "sha256:" + hashlib.sha256(b).hexdigest()


def canonical_json(obj):
    """The canonical JSON bytes that ids are derived from (contract conventions)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def content_id(prefix, obj, n):
    """prefix + the first n hex digits of the sha256 of obj's canonical JSON (ev-..., vr-...)."""
    return prefix + hashlib.sha256(canonical_json(obj)).hexdigest()[:n]


def slug(text, max_len=40):
    """Lowercase; every run of characters outside [a-z0-9] becomes one '-'; trimmed of '-'; at most max_len; 'x' if
    nothing is left."""
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    s = s[:max_len].strip("-")
    return s or "x"


def append_jsonl(path, rec):
    """Append one JSON object as a line, creating the folder (JSON Lines, contract conventions)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def emit(args, command, data, human_lines):
    """Print a command's result: the 7.1 success envelope with --json, else the human lines."""
    if getattr(args, "json", False):
        print(json.dumps({"ok": True, "command": command, "data": data}, indent=2, ensure_ascii=False))
    else:
        for line in human_lines:
            print(line)


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


class BlueprintError(SystemExit):
    """An error raised by code added in 2.0 (contract 7.3). It is a SystemExit, so callers that expect an exit see one:
    .code is the int exit status, .error the identifier of 7.3, .message the text, .entry the plan reference it is
    about and .next the command or edit that fixes it (or None). cli.main prints it (7.1); 1.x paths keep die()."""

    def __init__(self, error, message, *, entry=None, next=None, code=1):  # noqa: A002 (next is the contract's name)
        super().__init__(code)
        self.error = error
        self.message = message
        self.entry = entry
        self.next = next
        self.printed = False  # True when the human message is already on stderr (the plan load errors keep 1.x text)

    def __str__(self):
        return self.message


def _git(root, *args):
    try:
        out = subprocess.run(["git", *args], cwd=root, capture_output=True, timeout=10)
        return out.stdout.decode("utf-8", "replace").strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def read_jsonl(path):
    out, seen = [], set()
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("id") in seen:
            continue
        seen.add(rec.get("id"))
        out.append(rec)
    return out


def read_text_safe(path, limit=1_000_000):
    try:
        if path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def git_ls_files(root):
    """Files git sees in root (tracked, plus untracked ones not ignored), forward slashes; None when git cannot say
    (no git, not a repository, an error)."""
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root,
                             capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return sorted({norm(p) for p in out.stdout.decode("utf-8", "replace").split("\0") if p})


def git_head(root):
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, timeout=20)
        return out.stdout.decode().strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def find_asset(name):
    """An asset next to the copied tool (.blueprint/bin) or in the skill's assets folder."""
    for cand in (TOOL_HOME / name, TOOL_HOME.parent / "assets" / name):
        if cand.exists():
            return cand
    return None


# ---------------------------------------------------------------- state.json: one locked way to change it

STATE_LOCK = "state.lock"
LOCK_TIMEOUT_ENV = "BLUEPRINT_LOCK_TIMEOUT"
LOCK_TIMEOUT = 15.0
LOCK_TIMEOUT_MAX = 600.0
GITIGNORE_LINES = ("state.lock", "*.tmp")
STALE_TMP_SECONDS = 3600
AGAIN = "run the same command again"
_STATE_TMP = re.compile(r"^state\.json\.\d+-[0-9a-f]{8}\.tmp$")  # write_json's temp names for state.json, only
_HELD = set()  # the lock files this process holds: the lock is never nested, so a second take is a bug


def run_hint(root, args, module=None):
    """A command line that runs as printed: `python .blueprint/bin/blueprint.py <args>` when the project's copy has
    the command (module, e.g. "commands/roadmap", names the file it needs; None: any copy), else this running tool
    with --root."""
    copy = bp_dir(root) / "bin"
    if (copy / "blueprint.py").is_file() and (module is None or (copy / "bp" / (module + ".py")).is_file()):
        return f"python .blueprint/bin/blueprint.py {args}"
    return f'python "{TOOL_HOME / "blueprint.py"}" {args} --root "{root}"'


def _bad_state(why, next, name="state.json"):  # noqa: A002 (next is the contract's name)
    return BlueprintError("bad-state", f".blueprint/{name} {why}", entry=f".blueprint/{name}", next=next)


def _rule_names(line):
    """The name a .gitignore line is about, for the lines this tool adds: 'state.lock' for state.lock, /state.lock,
    **/state.lock and !state.lock (a negation is the user's choice too); None for comments and blank lines."""
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    s = s.lstrip("!")
    for prefix in ("**/", "/"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s


def ensure_gitignore(root):
    """.blueprint/.gitignore lists state.lock and *.tmp, so the lock file and temp files never show up as project
    changes. Creates the file, or appends only the patterns no line of it already names in any form (a negation such
    as !state.lock included, so the user's choice stands), with the file's own line ending; a user's lines are never
    rewritten, and a final newline is added only when something is appended. Best effort: False when the file cannot
    be read or written."""
    path = bp_dir(root) / ".gitignore"
    try:
        old = path.read_bytes() if path.exists() else b""
        text = old.decode("utf-8", "replace")
        named = {_rule_names(line) for line in text.splitlines()}
        missing = [p for p in GITIGNORE_LINES if p not in named]
        if not missing:
            return True
        eol = "\r\n" if "\r\n" in text else "\n"
        add = (eol if old and not old.endswith(b"\n") else "") + "".join(p + eol for p in missing)
        with path.open("ab") as f:
            f.write(add.encode("utf-8"))
        return True
    except OSError:
        return False


def _lock_timeout():
    """$BLUEPRINT_LOCK_TIMEOUT in seconds, at most 600; a value that is not a finite number of at least 0 means the
    default (15), with one warning on stderr."""
    raw = os.environ.get(LOCK_TIMEOUT_ENV)
    if raw is None:
        return LOCK_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        value = None
    if value is None or value != value or value in (float("inf"), float("-inf")) or value < 0:
        print(f"warning: {LOCK_TIMEOUT_ENV}={raw!r} is not a number of seconds (0 or more); "
              f"using {LOCK_TIMEOUT:g}", file=sys.stderr)
        return LOCK_TIMEOUT
    return min(value, LOCK_TIMEOUT_MAX)


def _try_lock(fd):
    """Take the advisory lock on an open file without blocking; OSError when it is held or cannot be taken."""
    if msvcrt is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fds):
    for fd in reversed(fds):
        try:
            if msvcrt is not None:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass  # closing the file releases it anyway
        finally:
            os.close(fd)


_BUSY = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, getattr(errno, "EDEADLK", -1), getattr(errno, "EDEADLOCK", -1)}


def _open_lock_file(root):
    """The lock file, opened (created when missing). A link or anything but a regular file is bad-state, and a link's
    target is never created (O_NOFOLLOW on POSIX; an lstat check first on Windows)."""
    path = bp_dir(root) / STATE_LOCK
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    except OSError as e:
        raise _lock_error(f"cannot be opened ({e.strerror or e})")
    if st is not None and (not stat.S_ISREG(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400):
        raise _lock_error("is not a regular file (a link, a folder or something else)")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags, 0o666), st is None
    except OSError as e:
        if e.errno == getattr(errno, "ELOOP", None):
            raise _lock_error("is not a regular file (a link, a folder or something else)")
        try:  # a read-only lock file still locks
            return os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)), False
        except OSError:
            raise _lock_error(f"cannot be opened ({e.strerror or e})")


def _lock_error(why):
    return _bad_state(f"{why}, so .blueprint/state.json cannot be written safely",
                      f"make .blueprint/{STATE_LOCK} a plain file (or remove it) and the folder .blueprint writable, "
                      f"then {AGAIN}", name=STATE_LOCK)


def _try_acquire(root):
    """One attempt: the fds held, or None when another process holds the lock. On POSIX the lock is flock on
    state.lock plus flock on the .blueprint folder itself: deleting state.lock while it is held (git clean -X, a
    person) makes a new file, and so a new lock, but never a second lock on the folder."""
    fd, new = _open_lock_file(root)
    fds = [fd]
    try:
        _try_lock(fd)
        if fcntl is not None:
            fds.append(os.open(bp_dir(root), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)))
            _try_lock(fds[-1])
        return fds, new
    except OSError as e:
        _unlock(fds)
        if e.errno not in _BUSY:
            raise _lock_error(f"cannot be locked ({e.strerror or e}); this file system may not support file locks")
        return None, new


def _remove_stale_temp_files(root):
    """Remove state.json.<pid>-<hex>.tmp files (write_json's names for state.json, nothing else) older than an hour:
    what a killed writer leaves behind. Called holding the lock."""
    cutoff = time.time() - STALE_TMP_SECONDS
    try:
        entries = list(os.scandir(bp_dir(root)))
    except OSError:
        return
    for entry in entries:
        try:
            if _STATE_TMP.match(entry.name) and entry.is_file(follow_symlinks=False) and \
                    entry.stat(follow_symlinks=False).st_mtime < cutoff:
                os.unlink(entry.path)
        except OSError:
            pass


def _acquire(root):
    timeout = _lock_timeout()
    deadline = time.monotonic() + timeout
    delay = 0.005
    created = False
    while True:
        fds, new = _try_acquire(root)
        created = created or new
        if fds is not None:
            break
        if time.monotonic() >= deadline:
            raise BlueprintError("state-busy", f"another blueprint command is writing .blueprint/state.json "
                                 f"(waited {timeout:g} s for .blueprint/{STATE_LOCK})",
                                 entry=".blueprint/state.json", next=AGAIN)
        time.sleep(delay * (0.5 + random.random()))
        delay = min(delay * 2, 0.05)
    if created:
        ensure_gitignore(root)  # the first lock: keep the lock and temp files out of the project's changes
    _remove_stale_temp_files(root)
    return fds


@contextlib.contextmanager
def state_lock(root):
    """The one lock on state.json and log.jsonl: an OS advisory lock on .blueprint/state.lock (msvcrt on Windows;
    flock on POSIX, on the lock file and the .blueprint folder), so a process that dies never leaves the project
    locked. Waits up to $BLUEPRINT_LOCK_TIMEOUT seconds (default 15, at most 600), then raises state-busy. Never
    nested (a second take in one process is a RuntimeError) and never held across a subprocess: hold it only to read,
    change and write state, append log lines and write views. Windows and WSL processes do not share it."""
    key = os.path.normcase(os.path.abspath(bp_dir(root) / STATE_LOCK))
    if key in _HELD:
        raise RuntimeError("the state lock is already held by this process; it is never nested")
    fds = _acquire(root)
    _HELD.add(key)
    try:
        yield
    finally:
        _HELD.discard(key)
        _unlock(fds)


def _holding(root):
    return os.path.normcase(os.path.abspath(bp_dir(root) / STATE_LOCK)) in _HELD


def state_default():
    """The state of a project with no state.json (1.x's default)."""
    return {"files": {}, "patterns": {}, "unplanned": [], "syncedAt": None, "head": None}


def _files_damage(raw):
    if raw.get("files") is None:
        raw["files"] = {}
    if not isinstance(raw["files"], dict):
        return f'"files" must be an object (got {_json_kind(raw["files"])})'
    for p, rec in raw["files"].items():
        if not isinstance(rec, dict):
            return f"files[{json.dumps(p, ensure_ascii=False)}] must be an object (got {_json_kind(rec)})"
    return None


def _patterns_damage(raw):
    if "patterns" in raw and raw["patterns"] is None:
        raw["patterns"] = {}
    pats = raw.get("patterns", {})
    if not isinstance(pats, dict):
        return f'"patterns" must be an object (got {_json_kind(pats)})'
    for pid, entry in pats.items():
        where = f"patterns[{json.dumps(pid, ensure_ascii=False)}]"
        if not isinstance(entry, dict):
            return f"{where} must be an object (got {_json_kind(entry)})"
        inst = entry.get("instances", [])
        if not isinstance(inst, (list, str, dict)):
            return f"{where}.instances must be a list (got {_json_kind(inst)})"
        bad = [x for x in inst if not isinstance(x, str)] if isinstance(inst, list) else []
        if bad:
            return f"{where}.instances must hold paths (got {_json_kind(bad[0])})"
    return None


def _kind_damage(key, ok, kind):
    def check(raw):
        v = raw.get(key)
        return f'"{key}" must be {kind} (got {_json_kind(v)})' if v and not isinstance(v, ok) else None
    return check


# key -> (check, the empty value an owner that rewrites the key repairs it to). Only shapes the commands dereference
# are checked, the ones on which 1.3.0 crashed.
_STATE_CHECKS = {"files": (_files_damage, dict), "patterns": (_patterns_damage, dict),
                 "unplanned": (_kind_damage("unplanned", (list, str), "a list"), list),
                 "data": (_kind_damage("data", (dict,), "an object"), dict)}


def _damage(raw, owned=()):
    """The first part of a parsed state that the commands cannot use, as words for bad-state, or None. Missing or null
    "files" and "patterns" are empty (filled in here). A key in `owned` (one the calling command rewrites wholesale,
    as sync does patterns) is repaired to its empty value in place instead, as 1.3.0's rewrite did; files is never
    owned. Anything 1.3.0 read without crashing is still read."""
    for key, (check, empty) in _STATE_CHECKS.items():
        why = check(raw)
        if why and key in owned and key != "files":
            raw[key] = empty()
            why = None
        if why:
            return why
    return None


def read_state(root, prepare=None, owned=()):
    """state.json as a dict (the 1.x default when there is none), then prepare(state) if given (the in-memory upgrade).
    A state.json that cannot be used is error bad-state (exit 1), raised before the command writes anything, and the
    file is left as it is: a folder, unreadable, not UTF-8, not JSON (a UTF-8 BOM included, as in 1.3.0), not an
    object, or damaged (_damage: "files" or a file record not an object, and the like). Damage confined to a key in
    `owned`, which the caller rewrites wholesale, is repaired instead (_damage)."""
    path = bp_dir(root) / "state.json"
    fix = f"fix .blueprint/state.json or move it away (progress is then tracked from scratch), then run: " \
          f"{run_hint(root, 'sync')}"
    if path.is_dir():
        raise _bad_state("is a folder, not a file",
                         f"remove or rename the folder .blueprint/state.json, then run: {run_hint(root, 'sync')}")
    try:
        data = _retry_windows(path.read_bytes)
    except FileNotFoundError:
        raw = state_default()
    except OSError as e:
        raise _bad_state(f"cannot be read ({e.strerror or e})",
                         f"make .blueprint/state.json readable, then run: {run_hint(root, 'status')}")
    else:
        try:
            raw = json.loads(data.decode("utf-8"))
        except UnicodeDecodeError:
            raise _bad_state("is not UTF-8 text", fix) from None
        except (ValueError, RecursionError) as e:
            raise _bad_state(f"is not valid JSON ({e})" if isinstance(e, ValueError) else "is not valid JSON "
                             "(nested too deeply)", fix) from None
        if not isinstance(raw, dict):
            raise _bad_state(f"must be a JSON object (got {_json_kind(raw)})", fix)
        why = _damage(raw, owned)
        if why:
            raise _bad_state(f"is damaged: {why}", fix)
    return prepare(raw) if prepare is not None else raw

def _json_kind(v):
    return ("null" if v is None else "boolean" if isinstance(v, bool) else "number" if isinstance(v, (int, float))
            else "string" if isinstance(v, str) else "list" if isinstance(v, list) else "object")


def write_state(root, state):
    """Write state.json atomically; call it holding state_lock. On Windows, a file another program keeps open past
    write_json's retries is state-busy; a file or folder that cannot be written is bad-state."""
    path = bp_dir(root) / "state.json"
    try:
        write_json(path, state)
    except PermissionError as e:
        if os.name == "nt" and path.is_file() and os.access(path, os.W_OK) and os.access(path.parent, os.W_OK):
            raise BlueprintError("state-busy", ".blueprint/state.json is open in another program (Windows refuses to "
                                 "replace it while it is open); nothing was written", entry=".blueprint/state.json",
                                 next=f"close it, then {AGAIN}") from None
        raise _bad_state(f"cannot be written ({e.strerror or e})",
                         f"make .blueprint/state.json and the folder .blueprint writable, then {AGAIN}") from None
    except OSError as e:
        raise _bad_state(f"cannot be written ({e.strerror or e})",
                         f"make .blueprint/state.json and the folder .blueprint writable, then {AGAIN}") from None


def update_state(root, change, prepare=None, owned=()):
    """The one way to change state.json: take state_lock, re-read state.json inside it (prepare upgrades it), apply
    change(state) in place, write it atomically, append the log events change returned (one line each), release.
    Compute before calling this; change only sets the keys and entries its command owns, so another writer's
    different entries survive and the same entry is last-writer-wins. owned: the keys change rewrites wholesale, whose
    damage is repaired rather than refused (read_state). change may raise to write nothing; it must not
    call update_state or state_lock (never nested), but may call append_log. Returns the state written."""
    with state_lock(root):
        state = read_state(root, prepare, owned)
        events = change(state) or ()
        write_state(root, state)
        for event in events:
            _append_log_line(root, event)
    return state
