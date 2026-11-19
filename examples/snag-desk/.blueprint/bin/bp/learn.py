"""learn: moved from blueprint.py 1.3.0 (contract 1.5)."""
import hashlib
import json
import os
import platform
import re
import secrets
import sys
import traceback
from pathlib import Path

from bp.version import TOOL_VERSION
from bp.core import _git, bp_dir, find_root, now_iso


ISSUES_URL = "https://github.com/TarikAI/AppBluePrint/issues"


NO_CAPTURE_ENV = "APP_BLUEPRINT_NO_CAPTURE"


FEEDBACK_KINDS = ("bug", "guidance", "preset", "feature")


def store_dir():
    return Path(os.environ.get("APP_BLUEPRINT_HOME") or (Path.home() / ".app-blueprint"))


def capture_on():
    return not os.environ.get(NO_CAPTURE_ENV)


def project_identity(root):
    """A machine-independent name for a project: its git origin plus its folder inside the repo.

    The same project cloned on two machines gets the same identity, so it counts once toward the
    learning loop's two-project bar. Without a remote, the absolute path is the best available."""
    url = _git(root, "config", "--get", "remote.origin.url")
    if url:
        u = re.sub(r"^[a-z][a-z0-9+.-]*://", "", url.strip(), flags=re.I)  # scheme
        u = re.sub(r"^[^@/]+@", "", u)  # credentials or git@
        u = re.sub(r"^([^/:]+):(?!\d)", r"\1/", u)  # scp-style host:path
        u = re.sub(r"\.git$", "", u.rstrip("/")).lower()
        return u + "#" + _git(root, "rev-parse", "--show-prefix")
    return str(Path(root).resolve())


def project_ident(root):
    """(projectId, project name) for a blueprinted project, else (None, None). Writes nothing."""
    if not root or not (bp_dir(root) / "blueprint.json").exists():
        return None, None
    pid = hashlib.sha256(("app-blueprint:" + project_identity(root)).encode("utf-8")).hexdigest()[:12]
    return pid, Path(root).resolve().name


_RX_WINPATH = re.compile(r"\b[A-Za-z]:[\\/][^\s'\"<>|]*")


_RX_UNIXPATH = re.compile(r"(?<![\w.])/(?:home|Users|root|var|tmp|private|mnt|opt|srv)/[^\s'\"<>|]*")


_RX_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")


_RX_URL = re.compile(r"\bhttps?://([^/\s'\"<>]+)[^\s'\"<>]*")


_RX_TOKEN = re.compile(r"\b(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{24,}\b")


def redact(text, root=None):
    s = str(text)
    if root:
        s = s.replace(str(Path(root).resolve()), "<project>")
    s = _RX_URL.sub(lambda m: "<url:" + m.group(1) + ">", s)
    s = _RX_WINPATH.sub("<path>", s)
    s = _RX_UNIXPATH.sub("<path>", s)
    s = _RX_EMAIL.sub("<email>", s)
    s = _RX_TOKEN.sub("<token>", s)
    return s


def store_append(name, rec):
    """Append one record to a store file. Returns the record id, or None when capture is off or fails."""
    if not capture_on():
        return None
    try:
        rec = dict(rec)
        rec.setdefault("id", secrets.token_hex(6))
        rec.setdefault("at", now_iso())
        rec.setdefault("tool", TOOL_VERSION)
        d = store_dir()
        d.mkdir(parents=True, exist_ok=True)
        with (d / name).open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec["id"]
    except OSError:
        return None


def record_incident(args, exc):
    tb = traceback.extract_tb(exc.__traceback__)
    frames = [f"{Path(fr.filename).name}:{fr.lineno}:{fr.name}" for fr in tb][-8:]
    root = None
    try:
        root = find_root(getattr(args, "root", None))
    except Exception:  # never let incident capture fail the report
        root = None
    pid, name = (None, None)
    try:
        pid, name = project_ident(root)
    except OSError:
        pass
    rec = {"python": platform.python_version(), "os": platform.system(), "command": getattr(args, "cmd", None),
           "error": type(exc).__name__, "message": redact(str(exc), root)[:500], "frames": frames,
           "projectId": pid, "project": name}
    rid = store_append("incidents.jsonl", rec)
    print(f"blueprint.py hit an unexpected error: {type(exc).__name__}: {redact(str(exc), root)[:300]}", file=sys.stderr)
    if rid:
        print(f"Recorded as incident {rid} in {store_dir()}. To report it, run: blueprint.py report {rid}", file=sys.stderr)
    else:
        print(f"Not recorded ({NO_CAPTURE_ENV} is set or the store is not writable). Please report it at {ISSUES_URL}", file=sys.stderr)


def _version_tuple(v):
    try:
        return tuple(int(x) for x in str(v).strip().split(".")[:3])
    except ValueError:
        return (0,)


def _file_tool_version(path):
    """The TOOL_VERSION = "x.y.z" line of a tool file, or None."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r'^TOOL_VERSION = "([^"]+)"', text, re.M)
    return m.group(1) if m else None


def installed_skill_versions():
    """{file read: version} per installed skill: bp/version.py (package installs), else blueprint.py (1.x installs)."""
    found = {}
    for base in (Path.home() / ".claude" / "skills", Path.home() / ".codex" / "skills", Path.home() / ".agents" / "skills"):
        scripts = base / "app-blueprint" / "scripts"
        for script in (scripts / "bp" / "version.py", scripts / "blueprint.py"):
            v = _file_tool_version(script) if script.exists() else None
            if v:
                found[str(script)] = v
                break
    return found


def copy_layout(bin_dir):
    """package if bin_dir/bp/version.py exists; single-file if only bin_dir/blueprint.py exists; else missing."""
    if (bin_dir / "bp" / "version.py").is_file():
        return "package"
    if (bin_dir / "blueprint.py").is_file():
        return "single-file"
    return "missing"


def _copy_version(bin_dir):
    """A project copy's version: VERSION, else the TOOL_VERSION line of bp/version.py, else of blueprint.py."""
    vfile = bin_dir / "VERSION"
    if vfile.is_file():
        try:
            return vfile.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            pass
    for script in (bin_dir / "bp" / "version.py", bin_dir / "blueprint.py"):
        v = _file_tool_version(script) if script.is_file() else None
        if v:
            return v
    return None
