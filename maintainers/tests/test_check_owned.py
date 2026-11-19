"""Tests for maintainers/check_owned.py. Run from the repo root:

    python -m unittest discover -s maintainers/tests -v

Every test builds a throwaway git repository in a temporary folder with a small plan of its own and never
touches the real repository. The tool launcher is the real one (loaded by path, as check_owned.py does).
"""
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECK = HERE.parent / "check_owned.py"
_spec = importlib.util.spec_from_file_location("check_owned", CHECK)
check_owned = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_owned)

GIT = shutil.which("git")


def _rmtree(path):
    def onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=onerror)


def plan_json(files, owners, roster, patterns=()):
    return {
        "version": 2,
        "app": {"name": "throwaway"},
        "modules": [{"id": "m", "path": "", "purpose": "everything"}],
        "phases": [{"id": "P1", "name": "one"}],
        "files": [{"path": p, "action": "create", "module": "m", "phase": "P1", "purpose": "x"} for p in files],
        "patterns": [{"id": "pat-%d" % i, "glob": g, "module": "m", "phase": "P1", "purpose": "x", "instance": "x"}
                     for i, g in enumerate(patterns)],
        "owners": [{"pattern": g, "owner": o} for g, o in owners],
        "roster": [{"id": r, "kind": "agent"} for r in roster],
    }


# W1-R owns roadmap files and docs/*.md notes, W1-F owns features.py, orchestrator owns the plan's version line.
FILES = ["src/roadmap.py", "src/roadmap_extra.py", "src/features.py", "src/version.py",
         "docs/my notes.md", "docs/ünï cödé.md", "docs/theirs notes.md", "win\\dir\\file.txt",
         ".blueprint/blueprint.json"]
OWNERS = [("/src/roadmap.py", "W1-R"), ("/src/roadmap_extra.py", "W1-R"), ("/src/features.py", "W1-F"),
          ("/src/version.py", "orchestrator"), ("/docs/my notes.md", "W1-R"),
          ("/docs/ünï cödé.md", "W1-R"), ("/docs/theirs notes.md", "W1-F"),
          ("/win/dir/file.txt", "W1-R"), ("/.blueprint/", "W0-C"), ("/gen/", "W1-R")]
ROSTER = ["W0-C", "W1-R", "W1-F", "orchestrator"]
PATTERNS = ["gen/*.txt"]  # future files of W1-R, planned by pattern


@unittest.skipUnless(GIT, "git is not installed")
class CheckOwnedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="check-owned-")
        self.addCleanup(_rmtree, self.tmp)
        self.root = Path(self.tmp) / "repo"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("symbolic-ref", "HEAD", "refs/heads/v2")
        for p in ["src/roadmap.py", "src/features.py", "src/version.py", "docs/theirs notes.md"]:
            self.write(p, "base\n")
        self.write(".blueprint/blueprint.json",
                   json.dumps(plan_json(FILES, OWNERS, ROSTER, PATTERNS), indent=2) + "\n")
        self.commit("base")
        self.git("checkout", "-q", "-b", "w1/r")

    # helpers
    def git(self, *args):
        r = subprocess.run([GIT, "-C", str(self.root), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                            "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", "-c", "core.quotepath=false"]
                           + list(args), capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, "git %s failed: %s" % (" ".join(args), r.stderr))
        return r.stdout

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")

    def commit(self, msg="change"):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def cli(self, *args):
        r = subprocess.run([sys.executable, str(CHECK), "--root", str(self.root)] + list(args),
                           capture_output=True, text=True, encoding="utf-8")
        return r.returncode, r.stdout, r.stderr

    def paths_of(self, res, key="violations"):
        return {v["path"]: v for v in res[key]} if key == "violations" else res[key]

    # the contract's cases
    def test_branch_touching_only_its_own_files_passes(self):
        self.write("src/roadmap.py", "changed\n")      # modify
        self.write("src/roadmap_extra.py", "new\n")    # add
        self.commit()
        code, out, _ = self.cli("--workstream", "W1-R", "--base", "v2")
        self.assertEqual(code, 0, out)
        self.assertIn("OK: W1-R changed 2 paths", out)

    def test_touching_another_workstreams_file_fails_and_names_the_owner(self):
        self.write("src/roadmap.py", "changed\n")
        self.write("src/features.py", "stolen\n")
        self.commit()
        code, out, _ = self.cli("--workstream", "W1-R", "--base", "v2")
        self.assertEqual(code, 1, out)
        self.assertIn("src/features.py  owned by W1-F", out)
        self.assertNotIn("src/roadmap.py  ", out)  # its own file is not listed as a problem

    def test_unplanned_file_fails(self):
        self.write("src/surprise.py", "x\n")
        self.commit()
        code, out, _ = self.cli("--workstream", "W1-R", "--base", "v2")
        self.assertEqual(code, 1, out)
        self.assertIn("src/surprise.py  unplanned", out)

    def test_deleted_file_of_another_owner_fails(self):
        (self.root / "src" / "version.py").unlink()
        self.commit()
        code, out, _ = self.cli("--workstream", "W1-R", "--base", "v2")
        self.assertEqual(code, 1, out)
        self.assertIn("src/version.py  owned by orchestrator", out)

    def test_deleting_its_own_file_passes(self):
        (self.root / "src" / "roadmap.py").unlink()
        self.commit()
        code, out, _ = self.cli("--workstream", "W1-R", "--base", "v2")
        self.assertEqual(code, 0, out)

    def test_rename_out_of_ones_own_area_fails_and_reports_both_sides(self):
        self.git("mv", "src/roadmap.py", "src/features_moved.py")  # content identical: git sees a pure rename
        self.commit()
        code, out, _ = self.cli("--workstream", "W1-R", "--base", "v2", "--json")
        self.assertEqual(code, 1, out)
        res = json.loads(out)
        self.assertEqual(res["changed"], ["src/features_moved.py", "src/roadmap.py"])  # both sides
        self.assertEqual([v["path"] for v in res["violations"]], ["src/features_moved.py"])
        self.assertEqual(res["violations"][0]["status"], "unplanned")

    def test_rename_of_anothers_file_into_own_area_reports_the_old_side(self):
        self.git("mv", "src/features.py", "src/roadmap_extra.py")  # W1-F's file becomes W1-R's new path
        self.commit()
        res = check_owned.run("W1-R", "v2", self.root)
        self.assertFalse(res["ok"])
        self.assertEqual([(v["path"], v["owner"]) for v in res["violations"]], [("src/features.py", "W1-F")])
        self.assertIn("src/roadmap_extra.py", res["changed"])

    def test_unknown_workstream_is_a_usage_error_with_a_next_step(self):
        self.write("src/roadmap.py", "changed\n")
        self.commit()
        code, out, err = self.cli("--workstream", "W9-X", "--base", "v2")
        self.assertEqual(code, 64)
        self.assertIn("W9-X", err)
        self.assertIn("next: use one of: W0-C, W1-R", err)

    def test_not_a_git_repository_is_a_usage_error(self):
        plain = Path(self.tmp) / "plain"
        plain.mkdir()
        r = subprocess.run([sys.executable, str(CHECK), "--root", str(plain), "--workstream", "W1-R"],
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 64)
        self.assertIn("not inside a git repository", r.stderr)
        self.assertIn("next:", r.stderr)

    def test_base_not_found_is_a_usage_error(self):
        code, _out, err = self.cli("--workstream", "W1-R", "--base", "no-such-branch")
        self.assertEqual(code, 64)
        self.assertIn("'no-such-branch' was not found", err)
        self.assertIn("next:", err)

    def test_missing_plan_is_a_usage_error(self):
        self.git("rm", "-q", ".blueprint/blueprint.json")
        self.commit()
        code, _out, err = self.cli("--workstream", "W1-R", "--base", "v2")
        self.assertEqual(code, 64)
        self.assertIn("has no .blueprint/blueprint.json", err)

    def test_missing_workstream_argument_is_a_usage_error(self):
        code, _out, err = self.cli("--base", "v2")
        self.assertEqual(code, 64)
        self.assertIn("--workstream", err)

    # names git quotes or the shell mangles
    def test_paths_with_spaces_and_non_ascii_names_work(self):
        self.write("docs/my notes.md", "x\n")
        self.write("docs/ünï cödé.md", "x\n")
        self.commit()
        code, out, _ = self.cli("--workstream", "W1-R", "--base", "v2")
        self.assertEqual(code, 0, out)
        self.assertIn("changed 2 paths", out)

    def test_non_ascii_name_of_another_owner_is_printed_whole(self):
        self.write("docs/theirs notes.md", "changed\n")
        self.write("docs/ünï cödé.md", "x\n")
        self.commit()
        res = check_owned.run("W1-F", "v2", self.root)
        self.assertEqual([v["path"] for v in res["violations"]], ["docs/ünï cödé.md"])
        self.assertEqual(res["violations"][0]["owner"], "W1-R")
        code, out, _ = self.cli("--workstream", "W1-F", "--base", "v2")
        self.assertEqual(code, 1)
        self.assertIn("docs/ünï cödé.md  owned by W1-R", out)

    def test_backslashes_do_not_matter(self):
        tool = check_owned.load_launcher()
        plan = tool.load_plan(self.root)
        # a path in either spelling gives the same answer, for a planned file and a pattern instance
        self.assertEqual(check_owned.classify(tool, plan, "W1-R", ["src\\roadmap.py", "gen\\one.txt"]), [])
        bad = check_owned.classify(tool, plan, "W1-R", ["src\\features.py"])
        self.assertEqual(bad, [("src/features.py", "owned-by-other", "W1-F")])
        # the plan itself lists "win\\dir\\file.txt": it is the forward-slash path
        self.assertEqual(check_owned.classify(tool, plan, "W1-R", ["win/dir/file.txt", "win\\dir\\file.txt"]), [])

    # more of the rules
    def test_future_file_planned_by_pattern_passes_for_its_owner_only(self):
        self.write("gen/one.txt", "x\n")
        self.commit()
        self.assertTrue(check_owned.run("W1-R", "v2", self.root)["ok"])
        res = check_owned.run("W1-F", "v2", self.root)
        self.assertEqual([(v["path"], v["status"], v["owner"]) for v in res["violations"]],
                         [("gen/one.txt", "owned-by-other", "W1-R")])

    def test_a_branch_cannot_widen_its_own_area_by_editing_the_plan(self):
        plan = json.loads((self.root / ".blueprint" / "blueprint.json").read_text(encoding="utf-8"))
        plan["owners"].append({"pattern": "/src/features.py", "owner": "W1-R"})
        self.write(".blueprint/blueprint.json", json.dumps(plan))
        self.write("src/features.py", "stolen\n")
        self.commit()
        res = check_owned.run("W1-R", "v2", self.root)
        self.assertIn(".blueprint/blueprint.json", [v["path"] for v in res["violations"]])  # the plan is W0-C's

    def test_json_output_for_a_pass(self):
        self.write("src/roadmap.py", "changed\n")
        self.commit()
        code, out, _ = self.cli("--workstream", "W1-R", "--base", "v2", "--json")
        self.assertEqual(code, 0)
        res = json.loads(out)
        self.assertEqual((res["ok"], res["violations"], res["changed"]), (True, [], ["src/roadmap.py"]))
        self.assertIn("summary", res)

    def test_no_changes_passes(self):
        code, out, _ = self.cli("--workstream", "W1-R", "--base", "v2")
        self.assertEqual(code, 0)
        self.assertIn("changed 0 paths", out)

    def test_base_can_be_a_commit(self):
        base = self.git("rev-parse", "v2").strip()
        self.write("src/features.py", "x\n")
        self.commit()
        code, _out, _err = self.cli("--workstream", "W1-R", "--base", base)
        self.assertEqual(code, 1)


class RealPlanTests(unittest.TestCase):
    """The repository's own plan, read-only: the owners the orchestrator relies on."""

    def test_the_dogfood_plan_gives_the_expected_owners(self):
        root = HERE.parent.parent
        if not (root / check_owned.PLAN_REL).is_file():
            self.skipTest("no dogfood plan in this checkout")
        tool = check_owned.load_launcher()
        plan = tool.load_plan(root)
        want = {"app-blueprint/scripts/bp/roadmap.py": "W1-R", "app-blueprint/scripts/bp/core.py": "W0-A2",
                "app-blueprint/scripts/bp/version.py": "orchestrator", "CHANGELOG.md": "orchestrator",
                "app-blueprint/tests/fixtures_text.py": "W2-FX2", "maintainers/learn.py": "W3-L",
                "README.md": "W3-R2", "examples/snag-desk/.blueprint/state.json": "orchestrator",
                "maintainers/check_owned.py": "W0-C", ".blueprint/blueprint.json": "W0-C"}
        for path, owner in want.items():
            with self.subTest(path=path):
                self.assertEqual(check_owned.classify(tool, plan, owner, [path]), [])
                other = "W1-R" if owner != "W1-R" else "W1-F"
                self.assertEqual([b[1] for b in check_owned.classify(tool, plan, other, [path])], ["owned-by-other"])

    def test_every_roster_id_of_the_contract_table_is_present(self):
        root = HERE.parent.parent
        if not (root / check_owned.PLAN_REL).is_file():
            self.skipTest("no dogfood plan in this checkout")
        plan = check_owned.load_launcher().load_plan(root)
        ids = {r["id"] for r in plan["roster"]}
        for want in ("W0-A1", "W0-A2", "W0-B", "W0-C", "W1-R", "W1-F", "W1-U", "W2-FX1", "W2-FX2", "W2-E", "W2-S",
                     "W2-W", "W2-D", "W2-M", "W2-DA", "W2-T", "W2-SY", "W3-V", "W3-A", "W3-R1", "W3-R2", "W3-L",
                     "W4-E2E", "W4-M", "W4-X", "orchestrator"):
            self.assertIn(want, ids)


if __name__ == "__main__":
    unittest.main()
