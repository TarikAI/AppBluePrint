"""Default ignore rules for harness folders match at the repo root only; the plan wins over ignore."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import test_blueprint as tb

GIT = shutil.which("git") is not None


class IgnoreAnchorTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bp-ign-"))
        r = tb.run(["init", "--name", "Ignore"], self.dir)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.path = self.dir / ".blueprint" / "blueprint.json"
        self.plan = json.loads(self.path.read_text(encoding="utf-8"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def touch(self, rel):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8")

    def save(self):
        self.path.write_text(json.dumps(self.plan), encoding="utf-8")

    def sync(self):
        r = tb.run(["sync", "--no-render"], self.dir)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        state = json.loads((self.dir / ".blueprint" / "state.json").read_text(encoding="utf-8"))
        return r.stdout, state

    def test_nested_harness_folders_are_visible_and_root_ones_are_not(self):
        for rel in ("tests/golden/codex/.codex/config.toml", "pkg/.claude/x.md", "pkg/.agentic-team/t.json",
                    ".claude/settings.json", ".codex/c.toml", ".agentic-team/team.json", "node_modules/a/b.js"):
            self.touch(rel)
        _, state = self.sync()
        self.assertEqual(sorted(state["unplanned"]), ["pkg/.agentic-team/t.json", "pkg/.claude/x.md",
                                                      "tests/golden/codex/.codex/config.toml"])

    def test_planned_file_and_pattern_instance_win_over_ignore(self):
        self.touch(".claude/settings.json")
        self.touch("build/out.txt")
        self.touch("dist/gen/a.txt")
        self.touch("dist/gen/b.txt")
        self.plan["files"] = [{"path": ".claude/settings.json", "action": "create"}, {"path": "build/out.txt", "action": "create"}]
        self.plan["patterns"] = [{"id": "gen", "glob": "dist/gen/*.txt"}]
        self.plan["ignore"] = ["dist/"]
        self.save()
        _, state = self.sync()
        self.assertEqual(state["unplanned"], [])
        self.assertEqual(sorted(state["patterns"]["gen"]["instances"]), ["dist/gen/a.txt", "dist/gen/b.txt"])

    @unittest.skipUnless(GIT, "git is not installed")
    def test_notice_when_default_rules_drop_tracked_files(self):
        self.touch("build/out.txt")
        self.touch("src/a.py")
        subprocess.run(["git", "init", "-q"], cwd=self.dir, check=True)
        subprocess.run(["git", "add", "-f", "build/out.txt", "src/a.py"], cwd=self.dir, check=True)
        out, state = self.sync()
        self.assertIn("Ignored by default rules: 1 tracked file(s) (e.g. build/out.txt). "
                      "Plan them or cover them with a pattern to track them.", out)
        self.assertEqual(state["ignoredTracked"], {"count": 1, "paths": ["build/out.txt"]})
        self.assertEqual(state["unplanned"], ["src/a.py"])
