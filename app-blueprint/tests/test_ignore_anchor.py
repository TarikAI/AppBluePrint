"""Default ignore rules for harness folders match at the repo root only; the plan wins over ignore."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import test_blueprint as tb

GIT = shutil.which("git") is not None


class Base(unittest.TestCase):
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


class IgnoreAnchorTest(Base):
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


class IgnorePrecedenceTest(Base):
    """Round two: the tool's own folder is not a notice, and a pattern overrides only a plain folder ignore it names."""

    def instances(self, pid):
        return sorted(self.sync()[1]["patterns"][pid]["instances"])

    def git_add(self):
        subprocess.run(["git", "init", "-q"], cwd=self.dir, check=True)
        subprocess.run(["git", "add", "-f", "."], cwd=self.dir, check=True)

    @unittest.skipUnless(GIT, "git is not installed")
    def test_blueprint_folder_is_never_an_ignored_tracked_file(self):
        self.touch("src/a.py")
        self.git_add()
        out, state = self.sync()
        self.assertNotIn("Ignored by default rules", out)
        self.assertEqual(state["ignoredTracked"], {"count": 0, "paths": []})

    def test_pattern_naming_the_ignored_folder_matches_the_same_with_and_without_git(self):
        self.touch(".claude/agents.md")
        self.touch("pkg/.claude/b.md")
        self.plan["patterns"] = [{"id": "agents", "glob": "**/.claude/*.md"}]
        self.save()
        want = [".claude/agents.md", "pkg/.claude/b.md"]
        self.assertEqual(self.instances("agents"), want)
        if GIT:
            self.git_add()
            self.assertEqual(self.instances("agents"), want)

    def test_wildcard_pattern_does_not_pull_in_node_modules(self):
        self.touch("src/a.test.ts")
        self.touch("node_modules/x/a.test.ts")
        self.plan["patterns"] = [{"id": "tests", "glob": "**/*.test.ts"}]
        self.save()
        self.assertEqual(self.instances("tests"), ["src/a.test.ts"])

    def test_own_ignore_wins_unless_the_pattern_names_the_folder(self):
        self.touch("src/legacy/old.py")
        self.touch("src/ok.py")
        self.plan["ignore"] = ["src/legacy/"]
        self.plan["patterns"] = [{"id": "py", "glob": "src/**/*.py"}]
        self.save()
        self.assertEqual(self.instances("py"), ["src/ok.py"])
        self.plan["patterns"] = [{"id": "py", "glob": "src/legacy/**/*.py"}]
        self.save()
        self.assertEqual(self.instances("py"), ["src/legacy/old.py"])

    def test_ignore_rule_written_with_backslashes_works(self):
        self.touch("src/legacy/old.py")
        self.touch("src/ok.py")
        self.plan["ignore"] = ["src\\legacy\\"]
        self.save()
        self.assertEqual(self.sync()[1]["unplanned"], ["src/ok.py"])
