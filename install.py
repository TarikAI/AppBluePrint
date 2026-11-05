"""Install the app-blueprint skill for Claude Code and/or Codex.

    python install.py                 # both: ~/.claude/skills and ~/.codex/skills
    python install.py --target claude
    python install.py --target codex
    python install.py --dest <folder> # any other agent's skills folder

An existing install is moved to <agent folder>/skill-backups/app-blueprint-<timestamp> first, never deleted.
"""
import argparse
import shutil
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent / "app-blueprint"
TARGETS = {"claude": Path.home() / ".claude" / "skills", "codex": Path.home() / ".codex" / "skills"}
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")


def install(parent):
    dest = parent / "app-blueprint"
    parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        # Outside the skills folder, so the old copy is not loaded as a second skill.
        backups = parent.parent / "skill-backups"
        backups.mkdir(parents=True, exist_ok=True)
        backup = backups / f"app-blueprint-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.move(str(dest), str(backup))
        print(f"moved the old install to {backup}")
    shutil.copytree(SRC, dest, ignore=SKIP)
    print(f"installed {dest}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["claude", "codex", "both"], default="both")
    ap.add_argument("--dest", help="install into this skills folder instead")
    a = ap.parse_args()
    if not (SRC / "SKILL.md").exists():
        raise SystemExit(f"skill not found at {SRC}")
    if a.dest:
        install(Path(a.dest).expanduser())
        return
    for name in (["claude", "codex"] if a.target == "both" else [a.target]):
        install(TARGETS[name])


if __name__ == "__main__":
    main()
