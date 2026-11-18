"""W0-A1 (contract 1.5 to 1.7): the package split, the launcher, the copied layout, init --refresh-tool and doctor.

The W0 rule is checked here: STRUCTURE.md and ROSTER.md for the v1 example match the goldens 1.3.0 produced
(tests/data/v1/views.json), and for every plan of the fuzz corpus the current tool and the frozen 1.3.0 file give
the same views, validate errors and warnings, and load-time exits.
"""
import ast
import contextlib
import copy
import io
import json
import os
import re
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import support as S

bp = S.load_launcher("blueprint_package_test")
frozen = S.load_frozen()
goldens = S.load_module(S.DATA_V1 / "make_goldens.py", "bp_make_goldens")

# contract 1.2, as far as W0-A1 builds it; same-rank modules may not import each other (views/* excepted)
IMPORT_RANK = {"version": 0, "core": 1, "globs": 2, "learn": 3, "plan": 5, "rules": 6, "data": 6, "status": 7,
               "verify": 8, "validate": 18, "views": 19, "export": 20, "install": 20, "commands": 21, "cli": 22,
               "api": 23}
DOMAIN = {"rules", "data", "status", "verify"}
VERSION_LINE = re.compile(r'^TOOL_VERSION = "([^"]+)"', re.M)


def frozen_names():
    """Every top-level function, class, assignment and import binding of the frozen 1.3.0 file."""
    names = []
    for node in ast.parse(S.FROZEN.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names += [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names += [(a.asname or a.name).split(".")[0] for a in node.names]
    return names


def module_rank(modname):
    parts = modname.split(".")
    assert parts[0] == "bp", modname
    return IMPORT_RANK[parts[1]] if len(parts) > 1 else -1


class NamesAndVersionTest(unittest.TestCase):
    def test_every_1x_name(self):
        names = frozen_names()
        self.assertGreater(len(names), 140)
        missing = [n for n in names if not hasattr(bp, n)]
        self.assertEqual(missing, [], "1.3.0 names missing from the launcher module")
        self.assertEqual(sorted(set(bp.__all__)), sorted(set(names)))

    def test_1x_constants_unchanged(self):
        skip = {"TOOL_VERSION", "HERE", "_RX_CACHE"}
        for n in frozen_names():
            old = getattr(frozen, n)
            if n in skip or callable(old) or isinstance(old, types.ModuleType):
                continue
            with self.subTest(name=n):
                self.assertEqual(getattr(bp, n), old)
        self.assertEqual(bp.HERE, S.SCRIPTS)
        self.assertIs(bp.subprocess, __import__("subprocess"))

    def test_version_lines_match(self):
        launcher = VERSION_LINE.search(S.LAUNCHER.read_text(encoding="utf-8")).group(1)
        package = VERSION_LINE.search((S.PACKAGE / "version.py").read_text(encoding="utf-8")).group(1)
        self.assertEqual(launcher, package)
        self.assertEqual(bp.TOOL_VERSION, package)
        self.assertTrue(package.startswith("1.9."), package)

    def test_import_order(self):
        """A module imports only modules above it (1.2); commands import domain modules inside handlers only."""
        problems = []
        for path in sorted(S.PACKAGE.rglob("*.py")):
            rel = path.relative_to(S.SCRIPTS).with_suffix("")
            me = ".".join(rel.parts[:-1] if rel.name == "__init__" else rel.parts)
            tree = ast.parse(path.read_text(encoding="utf-8"))
            top = set(tree.body)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("bp"):
                    targets = [node.module] if node.module != "bp" else ["bp." + a.name for a in node.names]
                elif isinstance(node, ast.Import):
                    targets = [a.name for a in node.names if a.name.startswith("bp")]
                else:
                    continue
                for t in targets:
                    same_views = t.startswith("bp.views.") and me.startswith("bp.views.")
                    if module_rank(t) >= module_rank(me) and not same_views:
                        problems.append(f"{me} imports {t}")
                    if me.startswith("bp.commands.") and t.split(".")[1] in DOMAIN and node in top:
                        problems.append(f"{me} imports domain module {t} at module top")
        self.assertEqual(problems, [])


class GoldenViewsTest(unittest.TestCase):
    """W0 rule: byte-identical STRUCTURE.md and ROSTER.md."""

    def test_v1_example_views_match_1_3_0(self):
        want = json.loads((S.DATA_V1 / "views.json").read_text(encoding="utf-8"))
        got = goldens.example_cases(bp)
        self.assertEqual(sorted(got), sorted(want))
        for case in want:
            for name in ("STRUCTURE.md", "ROSTER.md"):
                with self.subTest(case=case, view=name):
                    self.assertEqual(got[case][name].encode("utf-8"), want[case][name].encode("utf-8"))

    def test_fuzz_corpus_matches_1_3_0(self):
        tf = goldens.fuzz_corpus()
        base = json.loads(S.EXAMPLE.read_text(encoding="utf-8"))
        root = Path(tempfile.mkdtemp(prefix="bp-fuzz-golden-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / ".blueprint").mkdir()
        goldens.write_tree(root, {f["path"]: tf.MIGRATION if f["path"].endswith(".sql") else tf.MAIN_RS
                                  for f in base["files"]})
        goldens.write_tree(root, {"extra/loose.txt": "x\n"})

        def outcome(tool):
            err = io.StringIO()
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    plan = tool.load_plan(root)
                    found = tool.validate_plan(plan)
                    state = tool.load_state(root)
                    if plan["stores"] or plan["resources"]:
                        state["data"] = tool.scan_data(root, plan, state, tool.repo_files(root, plan))
                    comp = tool.compute(root, plan, state)
                    return ("ok", found, tool.render_structure(plan, state, comp), tool.render_roster(plan, state, comp))
            except SystemExit as e:
                return ("exit", e.code, err.getvalue())

        count, differ = 0, []
        with mock.patch.object(bp.subprocess, "run", side_effect=OSError("no git in the fuzz golden test")):
            for path in tf.mutation_paths(base):
                for value, delete in [(v, False) for v in tf.VALUES] + [(None, True)]:
                    plan = copy.deepcopy(base)
                    try:
                        tf.apply(plan, path, value, delete)
                    except (KeyError, IndexError):
                        continue
                    count += 1
                    (root / ".blueprint" / "blueprint.json").write_text(json.dumps(plan), encoding="utf-8")
                    if outcome(bp) != outcome(frozen):
                        differ.append(f"{'/'.join(map(str, path))} = " + ("<deleted>" if delete else json.dumps(value)))
        self.assertGreater(count, 1500)
        self.assertEqual(differ, [], f"{len(differ)} of {count} fuzz plans differ from 1.3.0")


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.saved_modules = {k: v for k, v in sys.modules.items() if k == "bp" or k.startswith("bp.")}
        self.saved_path = list(sys.path)
        self.tmp = Path(tempfile.mkdtemp(prefix="bp-foreign-"))
        (self.tmp / "bp").mkdir()
        (self.tmp / "bp" / "__init__.py").write_text("FOREIGN = True\n", encoding="utf-8")

    def tearDown(self):
        for k in [k for k in sys.modules if k == "bp" or k.startswith("bp.")]:
            del sys.modules[k]
        sys.modules.update(self.saved_modules)
        sys.path[:] = self.saved_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_real_bp(self, mod):
        self.assertTrue(hasattr(mod, "validate_plan"))
        self.assertFalse(hasattr(sys.modules["bp"], "FOREIGN"))
        self.assertEqual(Path(sys.modules["bp"].__file__).resolve().parent, S.PACKAGE.resolve())

    def test_foreign_bp_in_sys_modules_is_replaced(self):
        for k in [k for k in sys.modules if k == "bp" or k.startswith("bp.")]:
            del sys.modules[k]
        sys.modules["bp"] = S.load_module(self.tmp / "bp" / "__init__.py", "bp")
        self.assert_real_bp(S.load_launcher("blueprint_foreign_modules"))

    def test_foreign_bp_first_on_path_never_wins(self):
        for k in [k for k in sys.modules if k == "bp" or k.startswith("bp.")]:
            del sys.modules[k]
        sys.path.insert(0, str(self.tmp))
        self.assert_real_bp(S.load_launcher("blueprint_foreign_path"))
        self.assertEqual(Path(sys.path[0]).resolve(), S.SCRIPTS.resolve())

    def test_import_error_propagates_when_imported(self):
        """Loaded as a module (not run), a broken package raises instead of exiting."""
        for k in [k for k in sys.modules if k == "bp" or k.startswith("bp.")]:
            del sys.modules[k]
        broken = self.tmp / "scripts"
        shutil.copytree(S.SCRIPTS, broken, ignore=shutil.ignore_patterns("__pycache__"))
        (broken / "bp" / "cli.py").unlink()
        with self.assertRaises(ImportError):
            S.load_module(broken / "blueprint.py", "blueprint_broken")


class CopyTestBase(unittest.TestCase):
    """Projects in a folder whose name has a space."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="bp pkg "))
        self.root = self.base / "my project"
        self.root.mkdir()
        self.elsewhere = self.base / "else where"
        self.elsewhere.mkdir()
        self.bin = self.root / ".blueprint" / "bin"
        self.copy = self.bin / "blueprint.py"
        home = self.base / "empty home"  # doctor must not see the skills installed on this machine
        home.mkdir()
        self.env = S.tool_env(HOME=str(home), USERPROFILE=str(home))

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def init_package_copy(self):
        r = S.run_script(S.LAUNCHER, ["init", "--name", "Pkg", "--root", self.root], self.elsewhere, env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.write_example_plan()

    def init_single_file_copy(self):
        r = S.run_script(S.FROZEN, ["init", "--name", "Old", "--root", self.root], self.elsewhere, env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.bin / "VERSION").read_text(encoding="utf-8"), "1.3.0\n")
        self.assertFalse((self.bin / "bp").exists())
        self.write_example_plan()

    def write_example_plan(self):
        plan = json.loads(S.EXAMPLE.read_text(encoding="utf-8"))
        (self.root / ".blueprint" / "blueprint.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")

    def age_package_copy(self, old="1.8.9"):
        """Make the package copy look like an older consistent 1.9-line copy."""
        for f in (self.copy, self.bin / "bp" / "version.py"):
            f.write_text(VERSION_LINE.sub(f'TOOL_VERSION = "{old}"', f.read_text(encoding="utf-8")), encoding="utf-8")
        (self.bin / "VERSION").write_text(old + "\n", encoding="utf-8")

    def copy_run(self, *args, stdin=None):
        return S.run_script(self.copy, list(args), self.elsewhere, stdin=stdin, env=self.env)

    def hook_pre(self, rel):
        payload = json.dumps({"cwd": str(self.root), "tool_input": {"file_path": str(self.root / rel)}})
        return self.copy_run("hook", "pre", stdin=payload)

    def assert_guards_work(self):
        for args, code in ((("guard", "Cargo.toml"), 0), (("guard", "scratch/new.py"), 2)):
            r = self.copy_run(*args)
            self.assertEqual(r.returncode, code, f"{args}: {r.stdout}{r.stderr}")
        self.assertEqual(self.hook_pre("Cargo.toml").returncode, 0)
        r = self.hook_pre("scratch/new.py")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("not in the blueprint", r.stderr)

    def assert_fails_closed(self):
        for r in (self.copy_run("guard", "Cargo.toml"), self.hook_pre("Cargo.toml")):
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("is incomplete", r.stderr)
        r = self.copy_run("validate")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("is incomplete", r.stderr)
        self.assertIn("init --refresh-tool", r.stderr)

    def assert_no_pycache(self):
        self.assertEqual([str(p) for p in self.bin.rglob("__pycache__")], [])

    def refresh(self):
        r = S.run_script(S.LAUNCHER, ["init", "--refresh-tool", "--root", self.root], self.elsewhere, env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def copy_tool_in_process(self, stop):
        from bp import install
        with contextlib.redirect_stdout(io.StringIO()):
            install.copy_tool(self.root, _stop_after=stop)


class CopiedToolTest(CopyTestBase):
    def test_copy_layout_and_tool_files(self):
        self.init_package_copy()
        from bp import install
        self.assertIn(S.LAUNCHER, install.TOOL_FILES)
        self.assertTrue(all("__pycache__" not in p.parts for p in install.TOOL_FILES))
        pkg = sorted(p.relative_to(S.PACKAGE).as_posix() for p in S.PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)
        self.assertEqual(sorted(p.relative_to(self.bin / "bp").as_posix() for p in (self.bin / "bp").rglob("*.py")), pkg)
        for name in ("blueprint.py", "map.template.html", "mindmap.js"):
            self.assertTrue((self.bin / name).is_file(), name)
            self.assertIn(name, [p.name for p in install.TOOL_FILES])
        self.assertEqual((self.bin / "VERSION").read_text(encoding="utf-8"), bp.TOOL_VERSION + "\n")
        self.assertEqual(self.copy.read_bytes(), S.LAUNCHER.read_bytes())
        self.assertFalse((self.bin / "bp.new").exists() or (self.bin / "bp.old").exists())

    def test_copy_runs_from_any_folder_and_writes_no_pycache(self):
        self.init_package_copy()
        r = self.copy_run("validate")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OK: 12 files", r.stdout)
        r = S.run_script(self.copy, ["status"], self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = S.run_script(Path(".blueprint") / "bin" / "blueprint.py", ["sync", "--quiet"], self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.root / ".blueprint" / "map.html").is_file())
        self.assert_guards_work()
        self.assert_no_pycache()

    def test_refresh_from_inside_the_copy_says_so(self):
        self.init_package_copy()
        before = {p: p.stat().st_mtime_ns for p in self.bin.rglob("*") if p.is_file()}
        r = S.run_script(self.copy, ["init", "--refresh-tool"], self.root, env=self.env)  # init's root is the cwd
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("This is already the project's copy (tool 1.9.0). Run the skill's script to refresh it: "
                      "python <skill>/scripts/blueprint.py init --refresh-tool", r.stdout)
        self.assertNotIn("Refreshed", r.stdout)
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in self.bin.rglob("*") if p.is_file()})

    def test_refresh_of_a_package_copy(self):
        self.init_package_copy()
        self.age_package_copy()
        r = self.refresh()
        self.assertIn(f"Refreshed .blueprint/bin to tool {bp.TOOL_VERSION}.", r.stdout)
        self.assertNotIn("migrate", r.stdout + r.stderr)
        self.assertEqual((self.bin / "VERSION").read_text(encoding="utf-8"), bp.TOOL_VERSION + "\n")
        self.assert_guards_work()

    def test_upgrade_from_a_single_file_copy(self):
        self.init_single_file_copy()
        self.assert_guards_work()
        r = self.refresh()
        self.assertIn(f"Upgraded .blueprint/bin from the single-file tool 1.3.0 to the package tool {bp.TOOL_VERSION}.",
                      r.stdout)
        self.assertNotIn("migrate", r.stdout + r.stderr)
        self.assertTrue((self.bin / "bp" / "version.py").is_file())
        self.assertEqual(self.copy.read_bytes(), S.LAUNCHER.read_bytes())
        self.assert_guards_work()
        r = self.copy_run("doctor", "--strict")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"this project's tool copy: {bp.TOOL_VERSION} (package)", r.stdout)
        self.assert_no_pycache()

    def test_incomplete_copy_fails_closed(self):
        self.init_package_copy()
        (self.bin / "bp" / "cli.py").unlink()  # missing module: ImportError
        self.assert_fails_closed()
        self.refresh()
        (self.bin / "bp" / "views" / "roster.py").write_text("def render_roster(:\n", encoding="utf-8")  # SyntaxError
        self.assert_fails_closed()
        shutil.rmtree(self.bin / "bp")  # no package at all
        self.assert_fails_closed()
        self.refresh()
        self.assert_guards_work()


class InterruptedRefreshTest(CopyTestBase):
    """Contract 1.7: stop the copy after each step, check guard and hook pre, then a normal refresh must succeed."""

    def check(self, start, stop, expect):
        start()
        self.copy_tool_in_process(stop)
        if expect == "works":
            self.assert_guards_work()
            r = self.copy_run("validate")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        else:
            self.assert_fails_closed()
        if stop == 4:  # before step 5: complete, but VERSION still names the old copy
            r = self.copy_run("doctor", "--strict")
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("Out of date", r.stdout)
        self.refresh()
        self.assertFalse((self.bin / "bp.new").exists() or (self.bin / "bp.old").exists())
        self.assertEqual((self.bin / "VERSION").read_text(encoding="utf-8"), bp.TOOL_VERSION + "\n")
        self.assert_guards_work()
        r = self.copy_run("doctor", "--strict")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assert_no_pycache()

    def from_1x(self):
        self.init_single_file_copy()

    def from_19(self):
        self.init_package_copy()
        self.age_package_copy()

    def test_from_1x_stopped_while_building_the_package(self):
        self.check(self.from_1x, "2-partial", "works")

    def test_from_1x_stopped_after_step_2(self):
        self.check(self.from_1x, 2, "works")  # the old single file ignores bp/

    def test_from_1x_stopped_after_step_3(self):
        self.check(self.from_1x, 3, "works")

    def test_from_1x_stopped_after_step_4(self):
        self.check(self.from_1x, 4, "works")

    def test_from_19_stopped_while_building_the_package(self):
        self.check(self.from_19, "2-partial", "works")

    def test_from_19_stopped_after_step_2(self):
        self.check(self.from_19, 2, "fails closed")  # old launcher, new bp/: version mismatch

    def test_from_19_stopped_after_step_3(self):
        self.check(self.from_19, 3, "works")

    def test_from_19_stopped_after_step_4(self):
        self.check(self.from_19, 4, "works")


class DoctorTest(CopyTestBase):
    def fake_home(self):
        home = self.base / "home"
        old = home / ".claude" / "skills" / "app-blueprint" / "scripts"
        old.mkdir(parents=True)
        (old / "blueprint.py").write_text('TOOL_VERSION = "1.3.0"\n', encoding="utf-8")
        new = home / ".codex" / "skills" / "app-blueprint" / "scripts" / "bp"
        new.mkdir(parents=True)
        (new / "version.py").write_text('TOOL_VERSION = "1.9.0"\nSCHEMA_VERSION = 1\n', encoding="utf-8")
        (new.parent / "blueprint.py").write_text('TOOL_VERSION = "1.9.0"\n', encoding="utf-8")
        return S.tool_env(HOME=str(home), USERPROFILE=str(home))

    def test_doctor_sees_single_file_and_package_installs(self):
        env = self.fake_home()
        self.init_single_file_copy()
        r = S.run_script(S.LAUNCHER, ["doctor", "--root", self.root], self.elsewhere, env=env)
        self.assertIn("installed skill: 1.3.0", r.stdout)
        self.assertIn("installed skill: 1.9.0", r.stdout)
        self.assertIn(str(Path("scripts") / "bp" / "version.py"), r.stdout)
        self.assertIn("this project's tool copy: 1.3.0 (single-file)", r.stdout)
        self.assertIn("blueprint.json schema: 1", r.stdout)
        self.assertNotIn("migrate", r.stdout)
        self.assertIn("Out of date", r.stdout)
        r = S.run_script(S.LAUNCHER, ["doctor", "--strict", "--root", self.root], self.elsewhere, env=env)
        self.assertEqual(r.returncode, 1)

    def test_single_file_copy_without_version_file(self):
        self.init_single_file_copy()
        (self.bin / "VERSION").unlink()
        r = S.run_script(S.LAUNCHER, ["doctor", "--root", self.root], self.elsewhere, env=self.env)
        self.assertIn("this project's tool copy: 1.3.0 (single-file)", r.stdout)
        self.assertIn("Out of date", r.stdout)

    def test_package_copy_is_current(self):
        self.init_package_copy()
        (self.bin / "VERSION").unlink()  # falls back to bp/version.py
        r = S.run_script(S.LAUNCHER, ["doctor", "--strict", "--root", self.root], self.elsewhere, env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"this project's tool copy: {bp.TOOL_VERSION} (package)", r.stdout)


if __name__ == "__main__":
    unittest.main()
