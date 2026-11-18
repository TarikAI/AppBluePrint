"""core: paths, io, time, git and errors, moved from blueprint.py 1.3.0 (contract 1.5).

TOOL_HOME is the folder holding the launcher (scripts/ in the skill, .blueprint/bin in a project); 1.x called it HERE.
"""
import datetime as _dt
import hashlib
import json
import os
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


def file_sha(path):
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


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
