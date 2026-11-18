"""install: copy the tool into a project's .blueprint/bin (contract 1.7).

Layout of the copy: blueprint.py (the launcher), bp/**/*.py (no __pycache__), the map assets, and VERSION, written
last. The package is built in bin/bp.new and swapped in, so a reader sees either the old or the new package.
"""
import os
import shutil
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


def _replace(src, dst):
    """os.replace, retried on Windows when another process holds the file or folder open."""
    for attempt in range(6):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == 5:
                die(f"Could not replace {dst}: it is in use (PermissionError). Close what holds it open, "
                    "then run: python <skill>/scripts/blueprint.py init --refresh-tool")
            time.sleep(0.2)


def _rmtree(path):
    if path.exists():
        shutil.rmtree(path)


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
    _rmtree(new)  # leftovers of an interrupted refresh
    _rmtree(old)
    pkg = TOOL_HOME / "bp"
    files = _package_files()
    for i, src in enumerate(files):
        rel = src.relative_to(pkg)
        (new / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, new / rel)
        if _stop_after == "2-partial" and i == len(files) // 2:
            return written
    if live.exists():
        _replace(live, old)
    _replace(new, live)
    _rmtree(old)
    written += ["bp/" + p.relative_to(pkg).as_posix() for p in files]
    if _stop_after == 2:
        return written
    # step 3: the launcher; step 4: the assets, each through a temp file
    for step, sources in ((3, [TOOL_HOME / "blueprint.py"]), (4, _assets())):
        for src in sources:
            tmp = dest / (src.name + ".tmp")
            shutil.copy2(src, tmp)
            _replace(tmp, dest / src.name)
            written.append(src.name)
        if _stop_after == step:
            return written
    # step 5: VERSION last, so doctor reports the copy current only when it is complete
    (dest / "VERSION").write_text(TOOL_VERSION + "\n", encoding="utf-8")
    written.append("VERSION")
    return written
