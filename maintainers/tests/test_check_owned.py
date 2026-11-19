"""Tests for maintainers/check_owned.py. Run from the repo root:

    python -m unittest discover -s maintainers/tests -v

Every test builds a throwaway git repository in a temporary folder and never touches the real repository. The
throwaway base holds a copy of the real script and the real tool (app-blueprint/scripts) next to a small plan of
its own, because the check must run from a clean checkout of the base. The builder's branch is made on top of it.
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
REPO = HERE.parent.parent
CHECK = HERE.parent / "check_owned.py"
_spec = importlib.util.spec_from_file_location("check_owned_real", CHECK)
check_owned = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_owned)

GIT = shutil.which("git")
NULL_SHA = "0" * 40


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


UNI = "docs/ünï cödé.md"
FILES = ["src/roadmap.py", "src/roadmap_extra.py", "src/features.py", "src/version.py", "src/lower.py",
         "src/orphan.py", "docs/my notes.md", UNI, "docs/theirs notes.md", "win\\dir\\file.txt",
         ".blueprint/blueprint.json"]
OWNERS = [("/src/roadmap.py", "W1-R"), ("/src/roadmap_extra.py", "W1-R"), ("/src/features.py", "W1-F"),
          ("/src/version.py", "orchestrator"), ("/src/lower.py", "w1-r"),  # src/orphan.py has no rule on purpose
          ("/docs/my notes.md", "W1-R"), ("/" + UNI, "W1-R"), ("/docs/theirs notes.md", "W1-F"),
          ("/win/dir/file.txt", "W1-R"), ("/.blueprint/", "W0-C"), ("/gen/", "W1-R")]
ROSTER = ["W0-C", "W1-R", "w1-r", "W1-F", "orchestrator"]
PATTERNS = ["gen/*.txt"]  # future files of W1-R, planned by pattern; gen/x.py is under W1-R's folder but unplanned

TEMPLATE = {}


def build_template():
    """One base repository (branch v2) reused by copying: script, tool and plan, committed."""
    if "dir" in TEMPLATE:
        return TEMPLATE["dir"]
    tmp = tempfile.mkdtemp(prefix="check-owned-template-")
    root = Path(tmp) / "repo"
    root.mkdir()
    shutil.copytree(REPO / "app-blueprint" / "scripts", root / "app-blueprint" / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (root / "maintainers").mkdir()
    shutil.copyfile(CHECK, root / "maintainers" / "check_owned.py")
    for p in ["src/roadmap.py", "src/features.py", "src/version.py", "src/lower.py", "docs/theirs notes.md"]:
        f = root / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("base\n", encoding="utf-8", newline="\n")
    (root / ".blueprint").mkdir()
    (root / PLAN_REL).write_text(json.dumps(plan_json(FILES, OWNERS, ROSTER, PATTERNS), indent=2) + "\n",
                                 encoding="utf-8", newline="\n")
    for args in (["init", "-q"], ["symbolic-ref", "HEAD", "refs/heads/v2"], ["config", "core.autocrlf", "false"],
                 ["add", "-A"], ["commit", "-q", "-m", "base"]):
        subprocess.run([GIT, "-C", str(root), "-c", "user.name=test", "-c", "user.email=nobody",
                        "-c", "commit.gpgsign=false"] + args, check=True, capture_output=True)
    TEMPLATE["dir"] = tmp
    return tmp


PLAN_REL = ".blueprint/blueprint.json"


def tearDownModule():
    if "dir" in TEMPLATE:
        _rmtree(TEMPLATE["dir"])


@unittest.skipUnless(GIT, "git is not installed")
class CheckOwnedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="check-owned-")
        self.addCleanup(_rmtree, self.tmp)
        shutil.copytree(build_template(), self.tmp, dirs_exist_ok=True)
        self.root = Path(self.tmp) / "repo"
        self.script = self.root / "maintainers" / "check_owned.py"

    # helpers
    def git(self, *args, env=None, input_bytes=None, check=True):
        e = dict(os.environ, **(env or {}))
        r = subprocess.run([GIT, "-C", str(self.root), "-c", "user.name=test", "-c", "user.email=nobody",
                            "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"] + list(args),
                           capture_output=True, input=input_bytes, env=e)
        if check:
            self.assertEqual(r.returncode, 0, "git %s failed: %s" % (" ".join(args), r.stderr))
        return r.stdout.decode("utf-8", "replace")

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")

    def branch(self, name="w1/r"):
        self.git("checkout", "-q", "-b", name)

    def finish(self, msg="change"):
        """Commit the builder's work, then go back to a clean checkout of the base, as the orchestrator does."""
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        self.git("checkout", "-q", "v2")

    def plumb(self, entries, parent="v2", name="w1/r"):
        """A branch commit made without touching the working tree. entries: (mode, sha or None to delete, path)."""
        idx = str(Path(self.tmp) / "tmp-index")
        env = {"GIT_INDEX_FILE": idx}
        self.git("read-tree", parent, env=env)
        data = b"".join(("%s %s\t%s" % (m if s else "0", s or NULL_SHA, p)).encode("utf-8", "surrogateescape") + b"\0"
                        for m, s, p in entries)
        self.git("-c", "core.protectNTFS=false", "update-index", "-z", "--index-info", env=env, input_bytes=data)
        tree = self.git("write-tree", env=env).strip()
        commit = self.git("commit-tree", tree, "-p", parent, "-m", "plumbed").strip()
        self.git("update-ref", "refs/heads/" + name, commit)
        os.unlink(idx)
        return commit

    def blob(self, text="x\n"):
        return self.git("hash-object", "-w", "--stdin", input_bytes=text.encode()).strip()

    def cli(self, *args, cwd=None, env=None):
        r = subprocess.run([sys.executable, "-I", str(self.script)] + list(args), capture_output=True, text=True,
                           encoding="utf-8", cwd=str(cwd or self.tmp), env=dict(os.environ, **(env or {})))
        return r.returncode, r.stdout, r.stderr

    def check(self, ws="W1-R", head="w1/r", *extra):
        code, out, err = self.cli("--workstream", ws, "--head", head, "--base", "v2", "--json", *extra)
        try:
            return code, json.loads(out), err
        except ValueError:
            raise AssertionError("not JSON: %r %r" % (out, err))

    def violations(self, res):
        return [(v["path"], v["status"], v["owner"]) for v in res["violations"]]

    # the contract's cases
    def test_branch_touching_only_its_own_files_passes(self):
        self.branch()
        self.write("src/roadmap.py", "changed\n")
        self.write("src/roadmap_extra.py", "new\n")
        self.finish()
        code, out, _ = self.cli("--workstream", "W1-R", "--head", "w1/r", "--base", "v2")
        self.assertEqual(code, 0, out)
        self.assertIn("PASS: W1-R changed 2 paths, all its own", out)
        for sha in (self.git("rev-parse", "v2").strip()[:12], self.git("rev-parse", "w1/r").strip()[:12]):
            self.assertIn(sha, out)  # base, head and merge-base (= base here) are named

    def test_json_names_every_sha_and_the_count(self):
        self.branch()
        self.write("src/roadmap.py", "changed\n")
        self.finish()
        code, res, _ = self.check()
        self.assertEqual(code, 0)
        self.assertEqual((res["workstream"], res["ok"], res["changedCount"], res["changed"], res["violations"]),
                         ("W1-R", True, 1, ["src/roadmap.py"], []))
        self.assertEqual(res["baseSha"], self.git("rev-parse", "v2").strip())
        self.assertEqual(res["headSha"], self.git("rev-parse", "w1/r").strip())
        self.assertEqual(res["mergeBase"], res["baseSha"])
        self.assertIn("summary", res)

    def test_touching_another_workstreams_file_fails_and_names_the_owner(self):
        self.branch()
        self.write("src/roadmap.py", "changed\n")
        self.write("src/features.py", "stolen\n")
        self.finish()
        code, out, _ = self.cli("--workstream", "W1-R", "--head", "w1/r", "--base", "v2")
        self.assertEqual(code, 1, out)
        self.assertIn("src/features.py  owned by W1-F", out)
        self.assertNotIn("src/roadmap.py  ", out)
        self.assertIn("FAIL: W1-R changed 2 paths, 1 not its own", out)

    def test_unplanned_file_fails(self):
        self.branch()
        self.write("src/surprise.py", "x\n")
        self.finish()
        code, res, _ = self.check()
        self.assertEqual((code, self.violations(res)), (1, [("src/surprise.py", "unplanned", None)]))

    def test_unplanned_file_in_the_owners_folder_fails_even_though_a_rule_matches(self):
        self.branch()
        self.write("gen/x.py", "x\n")          # /gen/ is W1-R's by rule, but only gen/*.txt is planned
        self.write(".blueprint/extra.json", "{}\n")
        self.finish()
        code, res, _ = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(self.violations(res), [(".blueprint/extra.json", "unplanned", "W0-C"),
                                                ("gen/x.py", "unplanned", "W1-R")])

    def test_planned_file_without_an_owner_rule_fails(self):
        self.branch()
        self.write("src/orphan.py", "x\n")
        self.finish()
        code, res, _ = self.check()
        self.assertEqual((code, self.violations(res)), (1, [("src/orphan.py", "unplanned", None)]))

    def test_owner_ids_are_compared_exactly(self):
        self.branch()
        self.write("src/lower.py", "changed\n")  # owned by the roster id w1-r, not W1-R
        self.finish()
        code, res, _ = self.check()
        self.assertEqual((code, self.violations(res)), (1, [("src/lower.py", "owned-by-other", "w1-r")]))

    def test_future_file_planned_by_pattern_passes_for_its_owner_only(self):
        self.branch()
        self.write("gen/one.txt", "x\n")
        self.finish()
        self.assertEqual(self.check()[0], 0)
        code, res, _ = self.check("W1-F")
        self.assertEqual((code, self.violations(res)), (1, [("gen/one.txt", "owned-by-other", "W1-R")]))

    def test_deleted_file_of_another_owner_fails(self):
        self.branch()
        (self.root / "src" / "version.py").unlink()
        self.finish()
        code, out, _ = self.cli("--workstream", "W1-R", "--head", "w1/r", "--base", "v2")
        self.assertEqual(code, 1, out)
        self.assertIn("src/version.py  owned by orchestrator", out)

    def test_deleting_its_own_file_passes(self):
        self.branch()
        (self.root / "src" / "roadmap.py").unlink()
        self.finish()
        self.assertEqual(self.check()[0], 0)

    def test_rename_out_of_ones_own_area_fails_and_reports_both_sides(self):
        self.branch()
        self.git("mv", "src/roadmap.py", "src/features_moved.py")
        self.finish()
        code, res, _ = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(res["changed"], ["src/features_moved.py", "src/roadmap.py"])
        self.assertEqual(self.violations(res), [("src/features_moved.py", "unplanned", None)])

    def test_rename_of_anothers_file_into_own_area_reports_the_old_side(self):
        self.branch()
        self.git("mv", "src/features.py", "src/roadmap_extra.py")
        self.finish()
        code, res, _ = self.check()
        self.assertEqual((code, self.violations(res)), (1, [("src/features.py", "owned-by-other", "W1-F")]))
        self.assertIn("src/roadmap_extra.py", res["changed"])

    def test_base_that_moved_after_branching_is_not_counted(self):
        self.branch()
        self.write("src/roadmap.py", "mine\n")
        self.finish()
        self.write("src/features.py", "base moved on\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base moves")
        code, res, _ = self.check()
        self.assertEqual((code, res["changed"]), (0, ["src/roadmap.py"]))
        self.assertNotEqual(res["mergeBase"], res["baseSha"])

    def test_type_change_mode_change_and_gitlink_are_listed(self):
        link = self.blob("src/roadmap.py")
        commit = self.git("rev-parse", "v2").strip()
        feat = self.git("rev-parse", "v2:src/features.py").strip()
        ver = self.git("rev-parse", "v2:src/version.py").strip()
        self.plumb([("120000", link, "src/features.py"),      # file -> symlink (type change)
                    ("100755", ver, "src/version.py"),        # mode only
                    ("160000", commit, "src/lower.py")])      # file -> gitlink
        code, res, _ = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(self.violations(res), [("src/features.py", "owned-by-other", "W1-F"),
                                                ("src/lower.py", "owned-by-other", "w1-r"),
                                                ("src/version.py", "owned-by-other", "orchestrator")])
        self.assertNotEqual(feat, link)

    def test_hostile_git_config_changes_nothing(self):
        commit = self.git("rev-parse", "v2").strip()
        base_blob = self.git("rev-parse", "v2:src/features.py").strip()
        self.plumb([("160000", commit, "src/features.py"),                    # gitlink over another owner's file
                    ("100644", base_blob, "src/roadmap_extra.py")])           # a copy of it into W1-R's area
        for key, val in (("diff.renames", "copies"), ("diff.ignoreSubmodules", "all"), ("diff.noprefix", "true"),
                         ("core.quotepath", "true"), ("diff.external", "false"), ("diff.mnemonicPrefix", "true"),
                         ("diff.relative", "true"), ("core.autocrlf", "true")):
            self.git("config", key, val)
        hostile = {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "diff.ignoreSubmodules", "GIT_CONFIG_VALUE_0": "all",
                   "GIT_CONFIG_KEY_1": "diff.renames", "GIT_CONFIG_VALUE_1": "copies",
                   "GIT_EXTERNAL_DIFF": "false", "GIT_DIFF_OPTS": "--ignore-all-space"}
        code, out, _ = self.cli("--workstream", "W1-R", "--head", "w1/r", "--base", "v2", "--json", env=hostile)
        res = json.loads(out)
        self.assertEqual(code, 1, out)
        self.assertEqual(self.violations(res), [("src/features.py", "owned-by-other", "W1-F")])
        self.assertEqual(res["changed"], ["src/features.py", "src/roadmap_extra.py"])

    # the plan and the code come from the base
    def test_the_plan_is_read_from_the_base_not_from_the_head(self):
        self.branch()
        plan = json.loads((self.root / PLAN_REL).read_text(encoding="utf-8"))
        plan["owners"] += [{"pattern": "/.blueprint/", "owner": "W1-R"}, {"pattern": "/src/features.py", "owner": "W1-R"}]
        self.write(PLAN_REL, json.dumps(plan))
        self.write("src/features.py", "stolen\n")
        self.finish()
        code, res, _ = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(self.violations(res), [(".blueprint/blueprint.json", "owned-by-other", "W0-C"),
                                                ("src/features.py", "owned-by-other", "W1-F")])

    def test_code_added_by_the_head_is_never_loaded(self):
        self.branch()
        self.write("app-blueprint/scripts/bp/roadmap.py",
                   "import sys\n"
                   "for _m in list(sys.modules.values()):\n"
                   "    if hasattr(_m, 'owner_of') and hasattr(_m, 'planned_file'):\n"
                   "        _m.owner_of = lambda plan, path: ('W1-R', 0, [0])\n"
                   "        _m.planned_file = lambda plan, path: {'path': path}\n")
        self.write("src/features.py", "stolen\n")
        self.finish()  # the working tree is now the base: the head's module is not on disk
        code, res, _ = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(self.violations(res), [("app-blueprint/scripts/bp/roadmap.py", "unplanned", None),
                                                ("src/features.py", "owned-by-other", "W1-F")])

    def _attack_branch(self):
        """A branch that edits another owner's file; returns its sha."""
        self.branch()
        self.write("src/features.py", "stolen\n")
        self.finish()
        return self.git("rev-parse", "w1/r").strip()

    def _patch_plan_py(self):
        """Make the working tree's bp/plan.py say that everything is W1-R's and planned."""
        p = self.root / "app-blueprint/scripts/bp/plan.py"
        with open(p, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("\nowner_of = lambda plan, path: ('W1-R', 0, [0])\nplanned_file = lambda plan, path: {'path': path}\n")
        return p

    def test_running_on_the_builders_tree_is_judged_by_the_base(self):
        self.branch()
        self.write("app-blueprint/scripts/bp/roadmap.py", "x = 1\n")
        self.write("src/features.py", "stolen\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "change")  # still checked out: this is the builder's tree
        code, res, _ = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(self.violations(res), [("app-blueprint/scripts/bp/roadmap.py", "unplanned", None),
                                                ("src/features.py", "owned-by-other", "W1-F")])

    def test_edited_working_tree_tool_cannot_change_the_verdict(self):
        self._attack_branch()
        self._patch_plan_py()
        self.write("app-blueprint/scripts/bp/evil.py", "x = 1\n")        # untracked module
        code, res, _ = self.check()
        self.assertEqual((code, self.violations(res)), (1, [("src/features.py", "owned-by-other", "W1-F")]))

    def test_assume_unchanged_and_skip_worktree_cannot_hide_an_edit(self):
        self._attack_branch()
        self._patch_plan_py()
        rel = "app-blueprint/scripts/bp/plan.py"
        for flag in ("--assume-unchanged", "--skip-worktree"):
            self.git("update-index", flag, rel)
            self.assertNotIn(rel, self.git("status", "--porcelain"))   # the attack is invisible to git status
            code, res, _ = self.check()
            self.assertEqual((flag, code), (flag, 1))
            self.git("update-index", "--no-" + flag[2:], rel)

    def test_a_planted_pyc_in_the_working_tree_is_never_read(self):
        import py_compile
        self._attack_branch()
        src = self.root / "app-blueprint/scripts/bp/plan.py"
        evil = Path(self.tmp) / "plan_evil.py"
        evil.write_text(src.read_text(encoding="utf-8") + "\nowner_of = lambda plan, path: ('W1-R', 0, [0])\n"
                        "planned_file = lambda plan, path: {'path': path}\n", encoding="utf-8")
        cfile = importlib.util.cache_from_source(str(src))
        py_compile.compile(str(evil), cfile=cfile, doraise=True,
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        self.assertTrue(os.path.exists(cfile))
        code, res, _ = self.check()
        self.assertEqual((code, self.violations(res)), (1, [("src/features.py", "owned-by-other", "W1-F")]))

    def test_an_edited_script_is_refused_with_the_run_the_bases_copy_hint(self):
        self._attack_branch()
        with open(self.script, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("\n# edited\n")
        code, res, _ = self.check()
        self.assertEqual(code, 64)
        self.assertIn("not the base's maintainers/check_owned.py", res["message"])
        self.assertIn("base's copy", res["next"])

    def test_a_crlf_copy_of_the_script_is_the_same_script(self):
        self._attack_branch()
        data = self.script.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        self.script.write_bytes(data)
        self.assertEqual(self.check()[0], 1)

    def test_base_without_the_script_is_refused(self):
        self.git("rm", "-q", "maintainers/check_owned.py")
        self.git("commit", "-q", "-m", "drop script")
        self.branch()
        self.write("src/roadmap.py", "x\n")
        self.finish()
        self.script.parent.mkdir(exist_ok=True)
        shutil.copyfile(CHECK, self.script)   # on disk, but the base has none
        code, res, _ = self.check()
        self.assertEqual(code, 64)
        self.assertIn("the base has no maintainers/check_owned.py", res["message"])

    # isolation, environment, replace objects
    def test_planted_modules_and_pythonpath_change_nothing(self):
        self.branch()
        self.write("src/roadmap.py", "mine\n")
        self.finish()
        evil = "import os\nos._exit(0)\n"
        cwd = Path(self.tmp) / "elsewhere"
        pp = Path(self.tmp) / "pp"
        for d in (self.script.parent, cwd, pp):
            d.mkdir(parents=True, exist_ok=True)
            for name in ("json.py", "tempfile.py", "subprocess.py", "argparse.py"):
                (d / name).write_text(evil, encoding="utf-8")
        for env in ({}, {"PYTHONPATH": str(pp)}):
            r = subprocess.run([sys.executable, "-I", str(self.script), "--workstream", "W1-R", "--head", "w1/r"],
                               capture_output=True, text=True, encoding="utf-8", cwd=str(cwd),
                               env=dict(os.environ, **env))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("PASS: W1-R changed 1 path", r.stdout)

    def test_a_run_without_dash_I_is_refused_before_git_is_called(self):
        self.branch()
        self.write("src/roadmap.py", "mine\n")
        self.finish()
        empty = Path(self.tmp) / "no-git-here"
        empty.mkdir()
        hostile = ['x"&echo X&"', "x$(X)", "x\nnext: echo X", "x%PATH%", "x;|`y"]
        expected = ("error: run this check isolated\n"
                    "  next: run the same command again with -I: python -I maintainers/check_owned.py "
                    "<the same arguments>\n")
        for head in ["w1/r"] + hostile:
            r = subprocess.run([sys.executable, str(self.script), "--workstream", "W1-R", "--head", head],
                               capture_output=True, text=True, encoding="utf-8", env=dict(os.environ, PATH=str(empty)))
            self.assertEqual(r.returncode, 64, r.stderr)
            self.assertEqual(r.stdout, "")
            self.assertEqual(r.stderr, expected)   # nothing the caller typed is echoed

    def test_no_message_puts_caller_text_into_a_pasteable_command(self):
        for head in ['x"&echo X&"', "x$(X)", "x\nnext: echo X", "x%PATH%", "x;|`y"]:
            code, out, err = self.cli("--workstream", "W1-R", "--head", head)
            self.assertEqual(code, 64)
            lines = err.splitlines()
            self.assertEqual(len(lines), 3, err)                       # error, next, baseSha: a newline cannot add lines
            self.assertTrue(lines[2].startswith("  baseSha "))
            self.assertTrue(lines[0].startswith("error: head "), err)
            self.assertEqual(lines[1], "  next: pass the builder's branch with --head (a full ref name or sha), "
                                       "fetching it first if needed: git fetch origin")
            self.assertIn(repr(head), lines[0])                        # shown as quoted data, escapes included
        self._attack_branch()
        code, out, err = self.cli("--workstream", 'W9"&echo X&"', "--head", "w1/r")
        self.assertEqual(code, 64)
        self.assertEqual(len(err.splitlines()), 5, err)   # error, next, three shas
        self.assertIn("next: use one of: W0-C, ", err)

    def test_ambiguity_next_step_names_no_value(self):
        self._attack_branch()
        self.git("tag", "w1/r", "v2")
        code, res, _ = self.check()
        self.assertEqual(code, 64)
        self.assertEqual(res["next"], "pass the full ref name (refs/heads/..., refs/remotes/... or refs/tags/...) "
                                      "of the one you mean")

    def test_caller_git_variables_change_nothing(self):
        self._attack_branch()
        bogus = str(Path(self.tmp) / "no-such-git-dir")
        hostile = {"GIT_DIR": bogus, "GIT_WORK_TREE": bogus, "GIT_EXTERNAL_DIFF": "false", "GIT_INDEX_FILE": bogus,
                   "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "false",
                   "GIT_NO_REPLACE_OBJECTS": "0", "GIT_CEILING_DIRECTORIES": bogus}
        code, out, err = self.cli("--workstream", "W1-R", "--head", "w1/r", "--json", env=hostile)
        self.assertEqual(code, 1, err)
        self.assertEqual(self.violations(json.loads(out)), [("src/features.py", "owned-by-other", "W1-F")])

    def test_git_replace_cannot_change_the_verdict(self):
        sha = self._attack_branch()
        self.git("replace", sha, self.git("rev-parse", "v2").strip())   # make the head look like the base
        self.assertEqual(self.git("diff-tree", "-r", "--name-only", "v2", "w1/r").strip(), "")  # plain git is fooled
        code, res, _ = self.check()
        self.assertEqual((code, res["headSha"]), (1, sha))
        self.assertEqual(self.violations(res), [("src/features.py", "owned-by-other", "W1-F")])

    def test_the_temporary_tool_folder_is_deleted(self):
        tmpdir = Path(self.tmp) / "scratch"
        tmpdir.mkdir()
        self._attack_branch()
        env = {"TMP": str(tmpdir), "TEMP": str(tmpdir), "TMPDIR": str(tmpdir)}
        self.assertEqual(self.cli("--workstream", "W1-R", "--head", "w1/r", env=env)[0], 1)
        self.assertEqual(self.cli("--workstream", "W9", "--head", "w1/r", env=env)[0], 64)
        self.assertEqual(list(tmpdir.iterdir()), [])

    def test_git_replace_on_a_tree_cannot_change_the_verdict(self):
        self._attack_branch()
        self.git("replace", self.git("rev-parse", "w1/r^{tree}").strip(), self.git("rev-parse", "v2^{tree}").strip())
        self.assertEqual(self.git("diff-tree", "-r", "--name-only", "v2", "w1/r").strip(), "")  # plain git is fooled
        code, res, _ = self.check()
        self.assertEqual((code, self.violations(res)), (1, [("src/features.py", "owned-by-other", "W1-F")]))

    def test_interrupt_while_loading_the_base_tool_is_one_line_exit_130(self):
        scratch = Path(self.tmp) / "scratch"
        scratch.mkdir()
        self.branch()
        self.write("src/roadmap.py", "mine\n")
        self.finish()
        with open(self.root / "app-blueprint/scripts/bp/globs.py", "a", encoding="utf-8", newline="\n") as fh:
            fh.write("\nraise KeyboardInterrupt\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base tool that is interrupted while it loads")
        env = {"TMP": str(scratch), "TEMP": str(scratch), "TMPDIR": str(scratch)}
        code, out, err = self.cli("--workstream", "W1-R", "--head", "w1/r", env=env)
        self.assertEqual(code, 130)
        self.assertEqual(err.strip().splitlines(), ["interrupted: no verdict, nothing was judged"])
        self.assertNotIn("Traceback", err)
        self.assertEqual(list(scratch.iterdir()), [])

    # hostile entries in the base's tool tree
    def ls_tree(self, treeish):
        out = []
        for rec in self.git("ls-tree", "-z", treeish).split("\0"):
            if rec:
                meta, name = rec.split("\t", 1)
                out.append(tuple(meta.split()) + (name,))
        return [(m, t, s, n) for m, t, s, n in out]

    def mktree(self, entries):
        data = b"".join(("%s %s %s\t%s" % e).encode("utf-8") + b"\0" for e in entries)
        return self.git("mktree", "-z", "--missing", input_bytes=data).strip()

    def with_entries(self, commit, parts, change):
        """Tree of commit with the tree at parts replaced by change(entries)."""
        def rec(prefix):
            entries = self.ls_tree(commit + ":" + "/".join(prefix))
            if len(prefix) == len(parts):
                return self.mktree(change(entries))
            nxt = parts[len(prefix)]
            return self.mktree([(m, t, rec(prefix + [nxt]) if n == nxt else s, n) for m, t, s, n in entries])
        return rec([])

    def commit_tree(self, tree, parent, ref):
        sha = self.git("commit-tree", tree, "-p", parent, "-m", "hostile").strip()
        self.git("update-ref", "refs/heads/" + ref, sha)
        return sha

    def hostile_check(self, extra, scratch):
        """A base that holds the extra entries under app-blueprint/scripts, and a builder branch on top of it."""
        base_tree = self.with_entries("v2", ["app-blueprint", "scripts"], lambda es: es + extra)
        self.commit_tree(base_tree, "v2", "hbase")
        blob = self.blob("mine\n")

        def edit(es):
            return [(m, t, blob if n == "roadmap.py" else s, n) for m, t, s, n in es]
        head_tree = self.with_entries("hbase", ["src"], edit)
        self.commit_tree(head_tree, "hbase", "w1/r")
        env = {"TMP": str(scratch), "TEMP": str(scratch), "TMPDIR": str(scratch)}
        code, out, err = self.cli("--workstream", "W1-R", "--head", "refs/heads/w1/r", "--base", "refs/heads/hbase",
                                  "--json", env=env)
        return code, json.loads(out), err

    def test_hostile_entries_in_the_base_tool_are_refused_and_write_nothing(self):
        scratch = Path(self.tmp) / "scratch"
        scratch.mkdir()
        x = self.blob("x = 1\n")
        sub = self.mktree([("100644", "blob", x, "x.py")])
        v2 = self.git("rev-parse", "v2").strip()
        drive = str(Path(self.tmp) / "canary" / "x.py") if os.name == "nt" else "C:\\Users\\Public\\x.py"
        cases = {
            "backslash": ([("100644", "blob", x, "..\\..\\..\\x.py")], "x.py"),
            "drive letter": ([("100644", "blob", x, drive)], "x.py"),
            "dot dot": ([("040000", "tree", sub, "..")], ".."),
            "symlink": ([("120000", "blob", self.blob("target"), "link.py")], "link.py"),
            "gitlink": ([("160000", "commit", v2, "sub")], "sub"),
            "case pair": ([("100644", "blob", x, "Dup.py"), ("100644", "blob", x, "dup.py")], "differ only in case"),
            "device name": ([("100644", "blob", x, "NUL.txt")], "NUL.txt"),
            "trailing dot": ([("100644", "blob", x, "x.py.")], "x.py."),
            "trailing newline": ([("100644", "blob", x, "x.py\n")], "x.py"),
            "colon": ([("100644", "blob", x, "a:b.py")], "a:b.py"),
        }
        for label, (extra, needle) in cases.items():
            with self.subTest(label):
                try:
                    code, res, _err = self.hostile_check(extra, scratch)
                except AssertionError:
                    if label == "dot dot":
                        continue  # this git refuses to build such a tree at all
                    raise
                self.assertEqual(code, 64, res)
                self.assertTrue(res["refused"])
                self.assertNotIn("Traceback", _err)
                self.assertIn("will not export", res["message"])
                self.assertIn(needle, res["message"])
                self.assertEqual(list(scratch.iterdir()), [])                    # temp folder removed
                self.assertFalse((scratch / "x.py").exists())
                self.assertFalse((Path(self.tmp) / "canary").exists())
                self.assertFalse((Path(self.tmp) / "x.py").exists())

    def test_the_export_is_capped_in_entries_and_bytes(self):
        scratch = Path(self.tmp) / "scratch"
        scratch.mkdir()
        x = self.blob("x = 1\n")
        many = [("100644", "blob", x, "f%04d.py" % i) for i in range(2001)]
        code, res, err = self.hostile_check(many, scratch)
        self.assertEqual(code, 64, res)
        self.assertIn("limit is 2000 entries", res["message"])
        self.assertNotIn("Traceback", err)
        self.assertEqual(list(scratch.iterdir()), [])
        big = self.git("hash-object", "-w", "--stdin", input_bytes=b"0" * (65 * 1024 * 1024)).strip()
        code, res, err = self.hostile_check([("100644", "blob", big, "big.dat")], scratch)
        self.assertEqual(code, 64, res)
        self.assertIn("limit is 64 MiB", res["message"])
        self.assertEqual(list(scratch.iterdir()), [])

    def test_a_tool_tree_just_under_the_caps_is_exported_whole(self):
        scratch = Path(self.tmp) / "scratch"
        scratch.mkdir()
        x = self.blob("x = 1\n")
        many = [("100644", "blob", x, "f%04d.py" % i) for i in range(1500)]
        code, res, err = self.hostile_check(many, scratch)
        self.assertEqual((code, res["ok"]), (0, True), res)

    # refs
    def test_tag_named_like_the_base_makes_the_base_ambiguous(self):
        self._attack_branch()
        self.git("tag", "v2", "w1/r")                      # a tag named like the base branch
        code, res, _ = self.check()
        self.assertEqual(code, 64)
        self.assertEqual(res["matches"], ["refs/heads/v2", "refs/tags/v2"])
        self.assertIn("full ref name", res["next"])

    def test_tag_named_like_the_head_makes_the_head_ambiguous(self):
        self._attack_branch()
        self.git("tag", "w1/r", "v2")                      # harmless commit under the branch's name
        code, res, _ = self.check()
        self.assertEqual((code, res["matches"]), (64, ["refs/heads/w1/r", "refs/tags/w1/r"]))
        code, res, _ = self.check(head="refs/heads/w1/r")  # the full ref name is unambiguous
        self.assertEqual((code, res["headRef"]), (1, "refs/heads/w1/r"))

    def test_remote_tracking_and_local_branch_of_the_same_short_name_are_refused(self):
        sha = self._attack_branch()
        self.git("branch", "origin/zz", "v2")
        self.git("update-ref", "refs/remotes/origin/zz", sha)       # different commits
        code, res, _ = self.check(head="origin/zz")
        self.assertEqual((code, res["matches"]), (64, ["refs/heads/origin/zz", "refs/remotes/origin/zz"]))
        self.git("update-ref", "refs/remotes/origin/zz", self.git("rev-parse", "v2").strip())   # same commit
        self.assertEqual(self.check(head="origin/zz")[0], 64)                                   # still refused

    def test_full_ref_names_full_shas_and_abbreviations(self):
        sha = self._attack_branch()
        self.git("tag", "v2", sha)                          # the short base name is now ambiguous ...
        code, out, _ = self.cli("--workstream", "W1-R", "--head", sha, "--base", "refs/heads/v2", "--json")
        res = json.loads(out)                               # ... but the full names work
        self.assertEqual((code, res["baseRef"], res["headRef"], res["headSha"]), (1, "refs/heads/v2", None, sha))
        code, res, _ = self.check("W1-R", sha[:10], "--base", "refs/heads/v2")
        self.assertEqual(code, 64)
        self.assertIn("abbreviated sha", res["message"])
        code, res, _ = self.check(head="0" * 40)
        self.assertEqual(code, 64)

    def test_every_outcome_names_the_resolved_refs_and_a_pass_names_the_sha_to_merge(self):
        self.branch()
        self.write("src/roadmap.py", "mine\n")
        self.finish()
        sha = self.git("rev-parse", "w1/r").strip()
        code, out, _ = self.cli("--workstream", "W1-R", "--head", "w1/r")
        self.assertEqual(code, 0)
        self.assertIn("base v2 = refs/heads/v2 (%s)" % self.git("rev-parse", "v2").strip(), out)
        self.assertIn("head w1/r = refs/heads/w1/r (%s)" % sha, out)
        self.assertTrue(out.rstrip().endswith("merge exactly this head sha: " + sha), out)
        code, res, _ = self.check("W9-X")
        self.assertEqual((res["baseRef"], res["headRef"], res["headSha"]), ("refs/heads/v2", "refs/heads/w1/r", sha))

    # refusals
    def test_unknown_workstream_is_a_refusal_with_json_and_a_next_step(self):
        self.branch()
        self.write("src/roadmap.py", "changed\n")
        self.finish()
        code, out, err = self.cli("--workstream", "W9-X", "--head", "w1/r", "--base", "v2")
        self.assertEqual(code, 64)
        self.assertIn("W9-X", err)
        self.assertIn("next: use one of: W0-C, W1-R", err)
        code, res, _ = self.check("W9-X")
        self.assertEqual((code, res["ok"], res["refused"], res["workstream"]), (64, False, True, "W9-X"))
        self.assertIn("baseSha", res)

    def test_base_not_found_names_origin_v2_and_fetch(self):
        self.branch()
        self.write("src/roadmap.py", "changed\n")
        self.finish()
        code, out, err = self.cli("--workstream", "W1-R", "--head", "w1/r", "--base", "nope")
        self.assertEqual(code, 64)
        self.assertIn("--base origin/v2", err)
        self.assertNotIn(".blueprint/bin", err)

    def test_head_not_found_and_head_is_required(self):
        code, res, _ = self.check(head="no-such-branch")
        self.assertEqual((code, res["refused"]), (64, True))
        self.assertIn("'no-such-branch' was not found", res["message"])
        code, _out, err = self.cli("--workstream", "W1-R", "--base", "v2")
        self.assertEqual(code, 2)  # argparse: --head has no default
        self.assertIn("--head", err)

    def test_head_equal_to_the_base_is_nothing_to_check_not_a_pass(self):
        code, res, _ = self.check(head="v2")
        self.assertEqual(code, 64)
        self.assertIn("nothing to check", res["message"])

    def test_shallow_clone_suggests_unshallow(self):
        self.branch()
        self.write("src/roadmap.py", "one\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "one")
        self.write("src/roadmap.py", "two\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "two")
        self.git("checkout", "-q", "v2")
        clone = Path(self.tmp) / "clone"
        subprocess.run([GIT, "clone", "-q", "--depth", "1", "--no-single-branch", self.root.as_uri(), str(clone)],
                       check=True, capture_output=True)
        script = clone / "maintainers" / "check_owned.py"
        r = subprocess.run([sys.executable, "-I", str(script), "--workstream", "W1-R", "--head", "origin/w1/r",
                            "--base", "origin/v2", "--json"], capture_output=True, text=True, encoding="utf-8")
        res = json.loads(r.stdout)
        self.assertEqual(r.returncode, 64, r.stdout + r.stderr)
        self.assertIn("git fetch --unshallow", res["next"], res)

    def test_missing_or_broken_plan_at_the_base_is_a_refusal_about_the_base(self):
        self.git("rm", "-q", PLAN_REL)
        self.git("commit", "-q", "-m", "no plan")
        self.branch()
        self.write("src/roadmap.py", "changed\n")
        self.finish()
        code, res, _ = self.check()
        self.assertEqual(code, 64)
        self.assertIn("has no .blueprint/blueprint.json", res["message"])
        self.git("checkout", "-q", "v2")
        self.write(PLAN_REL, "{ not json")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "broken plan")
        code, res, _ = self.check()
        self.assertEqual(code, 64)
        self.assertIn("plan at the base", res["message"])
        self.assertIn("the base's plan is the problem", res["next"])
        self.assertNotIn(".blueprint/bin", res["next"])

    # names and raw paths
    def test_paths_with_spaces_and_non_ascii_names_work(self):
        self.branch()
        self.write("docs/my notes.md", "x\n")
        self.write(UNI, "x\n")
        self.finish()
        code, out, _ = self.cli("--workstream", "W1-R", "--head", "w1/r", "--base", "v2")
        self.assertEqual(code, 0, out)
        self.assertIn("changed 2 paths", out)

    def test_non_ascii_name_of_another_owner_is_printed_whole(self):
        self.branch()
        self.write(UNI, "x\n")
        self.finish()
        code, out, _ = self.cli("--workstream", "W1-F", "--head", "w1/r", "--base", "v2")
        self.assertEqual(code, 1)
        self.assertIn(UNI + "  owned by W1-R", out)

    def test_a_path_that_cleaning_would_change_is_unplanned(self):
        blob = self.blob()
        for name in ("src/roadmap.py ", " src/features.py", "src/roadmap.py\n", "src\\roadmap.py",
                     "win\\dir\\file.txt"):
            with self.subTest(name=name):
                self.plumb([("100644", blob, name)])
                code, res, _ = self.check()
                self.assertEqual(code, 1)
                self.assertEqual(self.violations(res), [(name, "unplanned", None)])

    def test_classify_judges_the_raw_path(self):
        tool = check_owned.load_launcher(REPO / "app-blueprint" / "scripts" / "blueprint.py")
        plan = tool.load_plan(self._plan_dir())
        self.assertEqual(check_owned.classify(tool, plan, "W1-R", ["src/roadmap.py", "gen/one.txt"]), [])
        self.assertEqual([b[:2] for b in check_owned.classify(tool, plan, "W1-R", ["src\\roadmap.py", "Src/roadmap.py", "./src/roadmap.py"])],
                         [("src\\roadmap.py", "unplanned"), ("Src/roadmap.py", "unplanned"), ("./src/roadmap.py", "unplanned")])

    def _plan_dir(self):
        d = Path(self.tmp) / "plan-only"
        (d / ".blueprint").mkdir(parents=True)
        shutil.copyfile(self.root / PLAN_REL, d / PLAN_REL)
        return d


class RealPlanTests(unittest.TestCase):
    """The repository's own plan, read-only: the owners the orchestrator relies on."""

    def plan(self):
        if not (REPO / check_owned.PLAN_REL).is_file():
            self.skipTest("no dogfood plan in this checkout")
        tool = check_owned.load_launcher(REPO / "app-blueprint" / "scripts" / "blueprint.py")
        return tool, tool.load_plan(REPO)

    def test_the_dogfood_plan_gives_the_expected_owners(self):
        tool, plan = self.plan()
        want = {"app-blueprint/scripts/bp/roadmap.py": "W1-R", "app-blueprint/scripts/bp/core.py": "W0-A2",
                "app-blueprint/scripts/bp/data.py": "W0-A2", "app-blueprint/scripts/bp/views/__init__.py": "W0-A1",
                "app-blueprint/scripts/bp/version.py": "orchestrator", "app-blueprint/scripts/blueprint.py": "orchestrator",
                "CHANGELOG.md": "orchestrator", "app-blueprint/tests/fixtures_text.py": "W2-FX2",
                "app-blueprint/tests/data/android/sub/deep.bin": "W2-FX1", "app-blueprint/tests/data/v1/a/b.json": "W0-A1",
                "maintainers/learn.py": "W3-L", "README.md": "W3-R2",
                "examples/snag-desk/.blueprint/state.json": "orchestrator", ".blueprint/blueprint.json": "orchestrator",
                "maintainers/check_owned.py": "W0-C", "maintainers/tests/test_check_owned.py": "W0-C"}
        for path, owner in want.items():
            with self.subTest(path=path):
                self.assertEqual(check_owned.classify(tool, plan, owner, [path]), [])
                other = "W1-R" if owner != "W1-R" else "W1-F"
                self.assertEqual([b[1] for b in check_owned.classify(tool, plan, other, [path])], ["owned-by-other"])

    def test_every_roster_id_of_the_contract_table_is_present_and_maintainers_has_no_harness(self):
        _tool, plan = self.plan()
        by_id = {r["id"]: r for r in plan["roster"]}
        for want in ("W0-A1", "W0-A2", "W0-B", "W0-C", "W1-R", "W1-F", "W1-U", "W2-FX1", "W2-FX2", "W2-E", "W2-S",
                     "W2-W", "W2-D", "W2-M", "W2-DA", "W2-T", "W2-SY", "W3-V", "W3-A", "W3-R1", "W3-R2", "W3-L",
                     "W4-E2E", "W4-M", "W4-X", "orchestrator"):
            self.assertIn(want, by_id)
        self.assertNotIn("harness", by_id["maintainers"])


if __name__ == "__main__":
    unittest.main()
