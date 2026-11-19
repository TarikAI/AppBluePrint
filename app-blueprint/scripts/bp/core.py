"""core: paths, io, time, git, errors, digests and ids (contract 1.3); the 1.x part moved from blueprint.py 1.3.0.

TOOL_HOME is the folder holding the launcher (scripts/ in the skill, .blueprint/bin in a project); 1.x called it HERE.
"""
import codecs
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


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


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_text_if_changed(path, text):
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return True
    return False


def append_log(root, event):
    event = dict(event)
    event.setdefault("at", now_iso())
    with (bp_dir(root) / "log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def norm(p):
    p = str(p).replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p


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
