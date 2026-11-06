"""Install the app-blueprint skill for Claude Code and/or Codex, and optionally the maintainer agents.

    python install.py                 # skill for both: ~/.claude/skills and ~/.codex/skills
    python install.py --target claude
    python install.py --target codex
    python install.py --dest <folder> # any other agent's skills folder
    python install.py --agents        # also the maintainer agents, into ~/.claude/agents
    python install.py --agents-only   # only the maintainer agents

An existing install is moved to <agent folder>/skill-backups/app-blueprint-<timestamp> first, never deleted.
Replaced agent files are moved to ~/.claude/agent-backups/<timestamp>/.
"""
import argparse
import shutil
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "app-blueprint"
AGENTS = HERE / "maintainers" / "agents"
TARGETS = {"claude": Path.home() / ".claude" / "skills", "codex": Path.home() / ".codex" / "skills"}
CLAUDE_AGENTS = Path.home() / ".claude" / "agents"
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


def install_agents(dest=CLAUDE_AGENTS):
    files = sorted(AGENTS.glob("*.md"))
    if not files:
        raise SystemExit(f"no agent files in {AGENTS}")
    dest.mkdir(parents=True, exist_ok=True)
    backups = dest.parent / "agent-backups" / time.strftime("%Y%m%d-%H%M%S")
    for f in files:
        target = dest / f.name
        if target.exists() and target.read_bytes() != f.read_bytes():
            backups.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(backups / f.name))
            print(f"moved the old {f.name} to {backups}")
        shutil.copy2(f, target)
        print(f"installed agent {target}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["claude", "codex", "both"], default="both")
    ap.add_argument("--dest", help="install the skill into this skills folder instead")
    ap.add_argument("--agents", action="store_true", help="also install the maintainer agents into ~/.claude/agents")
    ap.add_argument("--agents-only", action="store_true", help="install only the maintainer agents")
    a = ap.parse_args()
    if a.agents_only:
        install_agents()
        return
    if not (SRC / "SKILL.md").exists():
        raise SystemExit(f"skill not found at {SRC}")
    if a.dest:
        install(Path(a.dest).expanduser())
    else:
        for name in (["claude", "codex"] if a.target == "both" else [a.target]):
            install(TARGETS[name])
    if a.agents:
        install_agents()


if __name__ == "__main__":
    main()
