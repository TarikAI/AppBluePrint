"""install: copy the tool into a project's .blueprint/bin (contract 1.7).

Layout of the copy: blueprint.py (the launcher), bp/**/*.py (no __pycache__), the map assets, and VERSION, written
last. The package is built in bin/bp.new and swapped in, so a reader sees either the old or the new package.
"""
import os
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


def copy_tool(root, _stop_after=None):
    """Copy the tool into root/.blueprint/bin in the 1.7 order; returns the paths written, relative to bin.

    _stop_after (tests only) stops after step 2 (package), 3 (launcher) or 4 (assets), or with "2-partial" in the
    middle of building bin/bp.new, to leave the interrupted states of contract 1.7.
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
    # step 5: VERSION last, so doctor reports the copy current only when it is complete
    if (dest / "VERSION").exists():
        _writable(dest / "VERSION")
    (dest / "VERSION").write_text(TOOL_VERSION + "\n", encoding="utf-8")
    written.append("VERSION")
    return written
