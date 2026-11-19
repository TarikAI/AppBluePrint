"""install: copy the tool into a project's .blueprint/bin (contract 1.7).

Layout of the copy: blueprint.py (the launcher), bp/**/*.py (no __pycache__), the map assets, MANIFEST (every file
copied, written after the assets) and VERSION, written last. The package is built in bin/bp.new and swapped in, so a
reader sees either the old or the new package. check_package() is the start-up completeness check that reads MANIFEST.
"""
import importlib
import os
from pathlib import Path
import shutil
import stat
import sys
import time

from bp.version import TOOL_VERSION
from bp.core import TOOL_HOME, bp_dir, die

ASSET_NAMES = ("map.template.html", "mindmap.js")


def _assets():
    """The map assets, looked up beside the launcher first (a copy), then in the skill's assets folder."""
    out, seen = [], set()
    for folder in (TOOL_HOME, TOOL_HOME.parent / "assets"):
        if not folder.is_dir():
            continue
        names = [n for n in ASSET_NAMES if (folder / n).is_file()] + sorted(p.name for p in folder.glob("tab-*.js"))
        for n in names:
            if n not in seen:
                seen.add(n)
                out.append(folder / n)
    return out


def _package_files():
    pkg = TOOL_HOME / "bp"
    return sorted((p for p in pkg.rglob("*.py") if "__pycache__" not in p.relative_to(pkg).parts),
                  key=lambda p: p.relative_to(pkg).as_posix())


TOOL_FILES = _package_files() + [TOOL_HOME / "blueprint.py"] + _assets()


def _replace(src, dst, in_use, undo=None):
    """os.replace, retried on Windows when another process holds the file or folder open.

    in_use is the path named in the error (the one being moved or overwritten); undo runs before exiting."""
    for attempt in range(6):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == 5:
                if undo:
                    undo()
                die(f"Could not replace {in_use}: it is in use (PermissionError). Close what holds it open, "
                    "then run: python <skill>/scripts/blueprint.py init --refresh-tool")
            time.sleep(0.2)


def _writable(path):
    """Clear the read-only bit of path (best effort); leaves the other mode bits alone."""
    try:
        os.chmod(path, os.stat(path).st_mode | stat.S_IWRITE)
    except OSError:
        pass


def _copy(src, dst):
    """Copy the bytes only: a read-only skill file must not become a read-only file in .blueprint/bin."""
    if os.path.lexists(dst):
        _writable(dst)
        try:
            os.remove(dst)
        except OSError:
            die(f"Could not replace {dst}: it is in use. Close what holds it open, "
                "then run: python <skill>/scripts/blueprint.py init --refresh-tool")
    shutil.copyfile(src, dst)


def _rmtree(path):
    """Delete a folder even when files in it are read-only (Windows refuses to delete those).

    The read-only bits are cleared by hand first, so no onerror/onexc handler is needed (their names differ across
    Python 3.10 to 3.14). Raises OSError if the folder still cannot be deleted, for example when it is in use.
    """
    if not os.path.lexists(path):
        return
    _writable(path)  # a read-only folder attribute would otherwise leave an undeletable leftover
    for folder, dirs, files in os.walk(path):
        for name in dirs + files:
            _writable(os.path.join(folder, name))
    shutil.rmtree(path)


def _clear_leftover(path):
    """Remove a leftover of an interrupted refresh; if it is locked, move it aside; if that fails too, exit 1."""
    try:
        _rmtree(path)
        return
    except OSError:
        pass
    try:
        os.rename(path, path.with_name(f"bp.stale-{time.time_ns():x}"))
    except OSError:
        die(f"Could not remove {path}, left by an interrupted refresh: it is in use. Close what holds it open "
            "(an editor, a terminal, a running copy of the tool), then run: python <skill>/scripts/blueprint.py "
            "init --refresh-tool")


MANIFEST = "MANIFEST"
_MANIFEST_HEAD = "# app-blueprint tool "


def write_manifest(dest, files):
    """Write dest/MANIFEST: a first line "# app-blueprint tool <TOOL_VERSION>", then every copied file (relative to
    dest, forward slashes, sorted, LF), never VERSION or MANIFEST itself. Temp file plus replace, like every step."""
    names = sorted({f for f in files if f not in ("VERSION", MANIFEST)})
    tmp = dest / (MANIFEST + ".tmp")
    tmp.write_bytes((_MANIFEST_HEAD + TOOL_VERSION + "\n" + "".join(n + "\n" for n in names)).encode("utf-8"))
    if (dest / MANIFEST).exists():
        _writable(dest / MANIFEST)  # Windows will not replace a read-only file
    _replace(tmp, dest / MANIFEST, in_use=dest / MANIFEST)


def read_manifest(home):
    """(version, [relative paths]) from home/MANIFEST, or None when it is missing or unreadable."""
    try:
        lines = (home / MANIFEST).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    if not lines or not lines[0].startswith(_MANIFEST_HEAD):
        return None
    return lines[0][len(_MANIFEST_HEAD):].strip(), [ln for ln in lines[1:] if ln.strip()]


def _module_name(rel):
    """bp/views/render.py -> bp.views.render; bp/study/__init__.py -> bp.study."""
    parts = rel[:-3].split("/")
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def package_modules(home=TOOL_HOME):
    """Every module of the package present on disk, as names, sorted (bp/**/*.py, no __pycache__)."""
    pkg = home / "bp"
    return sorted(_module_name("bp/" + p.relative_to(pkg).as_posix()) for p in pkg.rglob("*.py")
                  if "__pycache__" not in p.relative_to(pkg).parts)


def required_modules(home=TOOL_HOME):
    """The modules this tool copy must have, from its MANIFEST (contract 1.7, the manifest rule):

    - MANIFEST present and its version line is this tool's TOOL_VERSION: every bp/**.py it lists is required.
    - otherwise, VERSION next to the launcher says this TOOL_VERSION: the copy was completed by this version, which
      writes MANIFEST before VERSION, so a missing or older MANIFEST means a damaged copy: raises ImportError.
    - otherwise (the skill folder, a copy made by 1.9.0, or a refresh stopped before the MANIFEST step, when MANIFEST
      and VERSION still belong to the old tool): nothing is required beyond what is on disk.
    Returns a sorted list of module names."""
    man = read_manifest(home)
    if man is not None and man[0] == TOOL_VERSION:
        return sorted(_module_name(p) for p in man[1] if p.startswith("bp/") and p.endswith(".py"))
    try:
        version = (home / "VERSION").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        version = None
    if version == TOOL_VERSION:
        raise ImportError(f"{home / MANIFEST} is missing or from another version")
    return []


def _failed_file(home, name, exc):
    """The bp/... file an import error comes from: a SyntaxError's file, else the innermost frame inside the package,
    else the module being imported."""
    pkg = (home / "bp").resolve()
    candidates = [getattr(exc, "filename", None)] if isinstance(exc, SyntaxError) else []
    tb = exc.__traceback__
    frames = []
    while tb is not None:
        frames.append(tb.tb_frame.f_code.co_filename)
        tb = tb.tb_next
    candidates += list(reversed(frames))
    for f in candidates:
        try:
            return "bp/" + Path(f).resolve().relative_to(pkg).as_posix()
        except (TypeError, ValueError, OSError):
            continue
    return name.replace(".", "/") + ".py"


def check_package(home=TOOL_HOME):
    """The start-up completeness check (cli). Returns (module names present, failure), failure being None or
    (bp/... file, exception) for the first module that exists but fails to import.

    Missing is "incomplete": a module the MANIFEST requires, a folder's __init__.py, or a bp module that an import
    asks for and that is not there raise ImportError, which the launcher reports as an incomplete copy (exit 2 for
    guard and hook pre, 1 otherwise). Broken is a bug: any other import error is returned, and cli names the file,
    records an incident and fails closed (review N6)."""
    present = package_modules(home)
    packages = {m.rsplit(".", 1)[0] for m in present if "." in m}  # every folder holding modules needs __init__.py
    missing = [m for m in sorted(set(required_modules(home)) | packages) if m not in present]
    if missing:
        names = [m.replace(".", "/") + ("/__init__.py" if m in packages else ".py") for m in missing]
        raise ImportError(f"{', '.join(names)} missing")
    failure = None
    for name in present:
        if name in ("bp.api", "bp.cli"):  # the launcher imports these itself (bp.api imports bp.cli)
            continue
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError as e:
            if (e.name == "bp" or (e.name or "").startswith("bp.")) and e.name not in present:
                raise ImportError(f"{e.name.replace('.', '/')}.py missing (needed by {name})") from e
            failure = failure or (_failed_file(home, name, e), e)
            continue
        except Exception as e:  # noqa: BLE001 - reported by cli as a named failure and an incident
            failure = failure or (_failed_file(home, name, e), e)
            continue
        if not getattr(mod, "__file__", None):  # a folder without __init__.py imports as a namespace package
            raise ImportError(f"{name.replace('.', '/')}/__init__.py is missing")
    return present, failure


_REQUIRED = {}


def listed_missing(name, home=TOOL_HOME):
    """True when bp.<name> is required by this copy's MANIFEST (registry: such a module is never "not built"). The
    manifest is read once per process; the start-up check has already enforced it."""
    if home not in _REQUIRED:
        try:
            _REQUIRED[home] = frozenset(required_modules(home))
        except ImportError:
            _REQUIRED[home] = None
    req = _REQUIRED[home]
    return req is None or ("bp." + name) in req


def copy_tool(root, _stop_after=None):
    """Copy the tool into root/.blueprint/bin in the 1.7 order; returns the paths written, relative to bin.

    _stop_after (tests only) stops after step 2 (package), 3 (launcher), 4 (assets) or "manifest" (MANIFEST written,
    VERSION not yet), or with "2-partial" in the middle of building bin/bp.new, to leave the interrupted states of
    contract 1.7.
    """
    dest = bp_dir(root) / "bin"
    if TOOL_HOME.resolve() == dest.resolve():
        print(f"This is already the project's copy (tool {TOOL_VERSION}). Run the skill's script to refresh it: "
              "python <skill>/scripts/blueprint.py init --refresh-tool")
        return []
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    # step 2: the package, built beside the old one and swapped in
    new, old, live = dest / "bp.new", dest / "bp.old", dest / "bp"
    for stale in sorted(dest.glob("bp.stale-*")):  # leftovers that were locked last time: best effort
        try:
            _rmtree(stale)
        except OSError:
            pass
    _clear_leftover(new)  # leftovers of an interrupted refresh
    _clear_leftover(old)
    pkg = TOOL_HOME / "bp"
    files = _package_files()
    for i, src in enumerate(files):
        rel = src.relative_to(pkg)
        (new / rel).parent.mkdir(parents=True, exist_ok=True)
        _copy(src, new / rel)
        if _stop_after == "2-partial" and i == len(files) // 2:
            return written
    had_live = live.exists()
    if had_live:
        _replace(live, old, in_use=live)

    def put_back():  # the new package could not take its place: keep the old one working
        if had_live and not live.exists():
            try:
                os.replace(old, live)
            except OSError:
                pass

    _replace(new, live, in_use=new, undo=put_back)
    try:
        _rmtree(old)  # best effort: the next refresh clears or moves aside what is left
    except OSError:
        pass
    written += ["bp/" + p.relative_to(pkg).as_posix() for p in files]
    if _stop_after == 2:
        return written
    # step 3: the launcher; step 4: the assets, each through a temp file
    for step, sources in ((3, [TOOL_HOME / "blueprint.py"]), (4, _assets())):
        for src in sources:
            tmp = dest / (src.name + ".tmp")
            _copy(src, tmp)
            if (dest / src.name).exists():
                _writable(dest / src.name)  # Windows will not replace a read-only file
            _replace(tmp, dest / src.name, in_use=dest / src.name)
            written.append(src.name)
        if _stop_after == step:
            return written
    # step 4m: MANIFEST, the list of every file copied, through a temp file (see check_package for how it is read)
    write_manifest(dest, written)
    if _stop_after == "manifest":
        return written
    # step 5: VERSION last, so doctor reports the copy current only when it is complete
    if (dest / "VERSION").exists():
        _writable(dest / "VERSION")
    (dest / "VERSION").write_text(TOOL_VERSION + "\n", encoding="utf-8")
    written.append("VERSION")
    return written
