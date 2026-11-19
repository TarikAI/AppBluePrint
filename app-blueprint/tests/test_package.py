"""W0-A1 (contract 1.5 to 1.7): the package split, the launcher, the copied layout, init --refresh-tool and doctor.

The W0 rule is checked here: STRUCTURE.md and ROSTER.md for the v1 example match the goldens 1.3.0 produced
(tests/data/v1/views.json), and for every plan of the fuzz corpus the current tool and the frozen 1.3.0 file give
the same views, validate errors and warnings, and load-time exits.
"""
import ast
import contextlib
import copy
import hashlib
import io
import json
import os
import re
import shutil
import stat
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

# Contract 1.2, for every module of the 1.1 tree, built or not yet: a module may import only modules of a lower rank;
# same-rank modules may not import each other (views/* excepted). study/* modules are ranked one by one.
IMPORT_RANK = {
    "version": 0, "core": 1, "globs": 2, "learn": 3, "study": 4, "study.safety": 5, "study.keys": 5,
    "study.jsscan": 6, "study.asar": 6, "study.axml": 6, "study.aabproto": 6, "study.dex": 6,
    "migrate": 7, "plan": 8, "rules": 9, "data": 9, "status": 10, "verify": 11, "evidence": 12, "unknowns": 13,
    "features": 14, "records": 15, "roadmap": 16, "signals": 17, "study.engine": 18,
    "study.source": 19, "study.trace": 19, "study.web": 19, "study.desktop": 19, "study.mobile": 19,
    "study.datafiles": 19, "study.compare": 19, "study.capture": 19, "study.importers": 19,
    "study.synth": 20, "study.adopt": 20, "registry": 21, "validate": 22, "views": 23, "export": 24, "install": 24,
    "commands": 25, "cli": 26, "api": 27}
# domain and lane modules: commands/* import them inside their handlers, never at module top (1.2)
DOMAIN = {"rules", "data", "status", "verify", "evidence", "unknowns", "features", "records", "roadmap", "signals",
          "study"}
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
    if len(parts) == 1:
        return -1
    key = ".".join(parts[1:3]) if parts[1] == "study" and len(parts) > 2 else parts[1]
    if key not in IMPORT_RANK:
        raise AssertionError(f"bp.{key} is not in the import order of contract 1.2 (IMPORT_RANK in test_package.py). "
                             "A module the contract's tree does not name: ask the orchestrator; do not add it here "
                             "yourself.")
    return IMPORT_RANK[key]


class NamesAndVersionTest(unittest.TestCase):
    def test_every_1x_name(self):
        names = frozen_names()
        self.assertGreater(len(names), 140)
        missing = [n for n in names if not hasattr(bp, n)]
        self.assertEqual(missing, [], "1.3.0 names missing from the launcher module")
        self.assertEqual(sorted(set(bp.__all__)), sorted(set(names)))

    def test_1x_constants_unchanged(self):
        # W0-A2 changes three, as the contract says: SCHEMA_VERSION and SKELETON become version 2 (1.5 row e, 2.1) and
        # VALIDATION_CODES gains the 2.0 codes in front of the 1.x list (7.4); checked below instead.
        skip = {"TOOL_VERSION", "HERE", "_RX_CACHE", "SCHEMA_VERSION", "SKELETON", "VALIDATION_CODES"}
        for n in frozen_names():
            old = getattr(frozen, n)
            if n in skip or callable(old) or isinstance(old, types.ModuleType):
                continue
            with self.subTest(name=n):
                self.assertEqual(getattr(bp, n), old)
        self.assertEqual(bp.HERE, S.SCRIPTS)
        self.assertIs(bp.subprocess, __import__("subprocess"))
        self.assertEqual(bp.SCHEMA_VERSION, 2)
        new = ("milestones", "features", "tasks", "unknowns")
        self.assertEqual(bp.SKELETON, dict(frozen.SKELETON, version=2, **{k: [] for k in new}))
        self.assertEqual(list(bp.SKELETON)[-4:], list(new))
        n_old = len(frozen.VALIDATION_CODES)
        self.assertEqual(bp.VALIDATION_CODES[-n_old:], frozen.VALIDATION_CODES)

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

    def test_import_rank_covers_the_contract_tree(self):
        """Every module of contract 1.1 has a rank (modules not built yet included); an unknown one is refused."""
        for name in ("unknowns", "features", "records", "roadmap", "evidence", "signals", "study", "study.engine",
                     "study.mobile", "study.adopt", "views.roadmap_md", "commands.migrate", "commands.study"):
            module_rank("bp." + name)
        self.assertLess(module_rank("bp.study.safety"), module_rank("bp.migrate"))
        self.assertLess(module_rank("bp.features"), module_rank("bp.records"))
        self.assertLess(module_rank("bp.study.engine"), module_rank("bp.study.source"))
        with self.assertRaisesRegex(AssertionError, "ask the orchestrator"):
            module_rank("bp.not_in_the_contract")


class GoldenViewsTest(unittest.TestCase):
    """W0 rule: byte-identical STRUCTURE.md and ROSTER.md."""

    def test_v1_example_views_match_1_3_0(self):
        want = json.loads((S.DATA_V1 / "views.json").read_text(encoding="utf-8"))
        got = goldens.example_cases(bp)
        self.assertEqual(sorted(got), sorted(want))
        # 1.5 row h: from 2.0 on proofs hash text with normalised line endings, so the 1.3.0 proofs of the CRLF files
        # in examples/snag-desk go stale once. Those files are no longer verified ...
        self.assertNotEqual(got["snag-desk"]["STRUCTURE.md"], want["snag-desk"]["STRUCTURE.md"])
        # ... and once verify has run again (the same proofs, hashed as 2.0 hashes them) the views are 1.3.0's again.
        got["snag-desk"] = self.snag_desk_with_renewed_proofs()
        for case in want:
            for name in ("STRUCTURE.md", "ROSTER.md"):
                with self.subTest(case=case, view=name):
                    self.assertEqual(got[case][name].encode("utf-8"), want[case][name].encode("utf-8"))

    def snag_desk_with_renewed_proofs(self):
        inp = json.loads(goldens.SNAG_DESK.read_text(encoding="utf-8"))
        state = json.loads(inp["state"])
        renewed = 0
        for path, rec in state["files"].items():
            ver, text = rec.get("verified") or {}, inp["files"].get(path)
            if ver.get("sha") and text is not None and "\r" in text:
                ver["sha"] = hashlib.sha1(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()
                renewed += 1
        self.assertGreater(renewed, 0)
        root = Path(tempfile.mkdtemp(prefix="bp-golden-renewed-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / ".blueprint").mkdir()
        goldens.write_tree(root, inp["files"])
        (root / ".blueprint" / "blueprint.json").write_text(inp["blueprint"], encoding="utf-8")
        (root / ".blueprint" / "state.json").write_text(json.dumps(state), encoding="utf-8")
        with mock.patch.object(bp.subprocess, "run", side_effect=OSError("no git in the golden test")):
            s, r = goldens.render(bp, root, scan=False)
        return {"STRUCTURE.md": s, "ROSTER.md": r}

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
                    errors, warnings = tool.validate_plan(plan)
                    if tool is bp:  # 1.5 row d: the schema-v1 warning is new in 2.0
                        warnings = [w for w in warnings if not w.startswith("blueprint.json is schema version 1")]
                    state = tool.load_state(root)
                    if plan["stores"] or plan["resources"]:
                        state["data"] = tool.scan_data(root, plan, state, tool.repo_files(root, plan))
                    comp = tool.compute(root, plan, state)
                    return ("ok", (errors, warnings), tool.render_structure(plan, state, comp),
                            tool.render_roster(plan, state, comp))
            except SystemExit as e:  # a load error already on stderr is not counted twice (getattr: 1.3.0 die())
                return ("exit", e.code, err.getvalue() + ("" if getattr(e, "printed", True) else e.message))

        # 1.5 row k: the top-level version is checked at load from 2.0 on (1.3.0 reported it only in validate)
        version_errors = ("blueprint.json: version is required", "blueprint.json: version must be a whole number",
                          "blueprint.json is schema version")
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
                    label = f"{'/'.join(map(str, path))} = " + ("<deleted>" if delete else json.dumps(value))
                    got = outcome(bp)
                    if path == ("version",):
                        if not (got[:2] == ("exit", 1) and any(v in got[2] for v in version_errors)):
                            differ.append(label + " (row k: expected a load error)")
                    elif got != outcome(frozen):
                        differ.append(label)
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


def make_writable(folder):
    for p in Path(folder).rglob("*"):
        try:
            os.chmod(p, os.stat(p).st_mode | stat.S_IWRITE)
        except OSError:
            pass


def make_read_only(path):
    os.chmod(path, stat.S_IREAD | (os.stat(path).st_mode & 0o555))


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
        make_writable(self.base)
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
        man = self.bin / "MANIFEST"  # an older copy's manifest names its own version
        if man.exists():
            lines = man.read_text(encoding="utf-8").splitlines()
            man.write_text("\n".join(["# app-blueprint tool " + old] + lines[1:]) + "\n", encoding="utf-8")

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

    def assert_fails_to_load(self, rel, error):
        """W0-A2 review N6: a module that exists but fails to import fails closed with its name, as an incident."""
        for r in (self.copy_run("guard", "Cargo.toml"), self.hook_pre("Cargo.toml")):
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn(f"failed to load {rel} ({error}", r.stderr)
            self.assertNotIn("is incomplete", r.stderr)
        r = self.copy_run("validate")
        self.assertEqual(r.returncode, 70, r.stdout + r.stderr)
        self.assertIn(f"failed to load {rel}", r.stderr)

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
        self.assertIn(f"This is already the project's copy (tool {bp.TOOL_VERSION}). Run the skill's script to refresh "
                      "it: python <skill>/scripts/blueprint.py init --refresh-tool", r.stdout)
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
        self.assert_fails_closed()  # roster.py is imported by bp/api.py, so the launcher reports it (W0-A1's message)
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
        if stop in (4, "manifest"):  # before step 5: complete, but VERSION still names the old copy
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
        self.check(self.from_19, 4, "works")  # before MANIFEST: the old one names the old version, so it is not used

    def test_from_19_stopped_after_the_manifest(self):
        self.check(self.from_19, "manifest", "works")

    def test_from_1x_stopped_after_the_manifest(self):
        self.check(self.from_1x, "manifest", "works")


class ManifestTest(CopyTestBase):
    """W0-A2, decision 1 (option D): .blueprint/bin/MANIFEST lists every copied file; the start-up check enforces it.

    The rule: MANIFEST is enforced when its version line is the running TOOL_VERSION. Otherwise, if VERSION says the
    running TOOL_VERSION (the copy was completed by this version, which writes MANIFEST before VERSION), the copy is
    incomplete. Otherwise (the skill folder, a 1.9.0 copy, a refresh stopped before the MANIFEST step) every module
    present on disk is imported and nothing more is required."""

    def manifest(self):
        lines = (self.bin / "MANIFEST").read_text(encoding="utf-8").splitlines()
        return lines[0], lines[1:]

    def test_manifest_lists_every_copied_file(self):
        self.init_package_copy()
        head, files = self.manifest()
        self.assertEqual(head, "# app-blueprint tool " + bp.TOOL_VERSION)
        on_disk = sorted(p.relative_to(self.bin).as_posix() for p in self.bin.rglob("*")
                         if p.is_file() and p.name not in ("VERSION", "MANIFEST"))
        self.assertEqual(files, on_disk)
        self.assertEqual(files, sorted(files))
        self.assertIn("blueprint.py", files)
        self.assertIn("bp/registry.py", files)
        self.assertIn("map.template.html", files)
        self.assertNotIn(b"\r", (self.bin / "MANIFEST").read_bytes())

    def test_missing_or_older_manifest_after_version_is_incomplete(self):
        self.init_package_copy()
        good = (self.bin / "MANIFEST").read_bytes()
        (self.bin / "MANIFEST").unlink()
        self.assert_fails_closed()
        head, files = good.decode("utf-8").splitlines()[0], good.decode("utf-8").splitlines()[1:]
        (self.bin / "MANIFEST").write_text("\n".join(["# app-blueprint tool 1.8.9"] + files) + "\n", encoding="utf-8")
        self.assert_fails_closed()
        (self.bin / "MANIFEST").write_bytes(good)
        self.assert_guards_work()

    def test_a_stale_manifest_mid_refresh_never_breaks_a_complete_package(self):
        """Refresh stopped before the MANIFEST step: VERSION and MANIFEST are the old tool's, and the old manifest
        lists a module the new package no longer has. The new launcher and package work."""
        self.init_package_copy()
        lines = (self.bin / "MANIFEST").read_text(encoding="utf-8").splitlines()
        (self.bin / "MANIFEST").write_text("\n".join(["# app-blueprint tool 1.8.9"] + sorted(lines[1:] + ["bp/gone.py"]))
                                           + "\n", encoding="utf-8")
        (self.bin / "VERSION").write_text("1.8.9\n", encoding="utf-8")
        self.assert_guards_work()
        self.assertEqual(self.copy_run("validate").returncode, 0)

    def test_a_listed_module_that_does_not_import_fails_closed(self):
        self.init_package_copy()
        (self.bin / "bp" / "verify.py").write_text("def broken(:\n", encoding="utf-8")
        self.assert_fails_to_load("bp/verify.py", "SyntaxError")

    def broken_skill(self, source):
        skill = self.base / "skill r2"
        shutil.copytree(S.SCRIPTS, skill / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(S.SKILL / "assets", skill / "assets")
        (skill / "scripts" / "bp" / "features.py").write_text(source, encoding="utf-8")
        return skill / "scripts" / "blueprint.py"

    def test_doctor_still_runs_with_a_broken_module(self):
        """Review R2-N6: doctor reports the failing module, file, error and the repair, and exits non-zero."""
        launcher = self.broken_skill("raise ValueError('bad feature table')\n")
        self.init_package_copy()
        r = S.run_script(launcher, ["doctor", "--root", self.root], self.elsewhere, env=self.env)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        out = r.stdout + r.stderr
        for needle in ("bp.features", "bp/features.py", "ValueError: bad feature table", "install.py",
                       "blueprint.py " + bp.TOOL_VERSION):
            self.assertIn(needle, out)
        self.assertNotIn("Traceback", out)
        r = S.run_script(launcher, ["status", "--root", self.root], self.elsewhere, env=self.env)
        self.assertEqual(r.returncode, 70, "fail-closed is unchanged for every other command")

    def test_a_skill_missing_a_module_says_reinstall(self):
        """Review R2-N7: from the skill folder, a missing bp module points at reinstalling the skill."""
        launcher = self.broken_skill("import bp.does_not_exist  # noqa\n")
        r = S.run_script(launcher, ["status", "--root", self.root], self.elsewhere, env=self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("bp/does_not_exist.py missing (needed by bp.features)", r.stderr)
        self.assertIn("python install.py", r.stderr)

    def test_a_broken_later_module_names_itself_and_is_an_incident(self):
        """Review N6: an import error that is not "module missing" says which module failed and why, records an
        incident, fails closed, and is not called an incomplete copy (here a later module in a temp skill)."""
        skill = self.base / "skill broken"
        shutil.copytree(S.SCRIPTS, skill / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(S.SKILL / "assets", skill / "assets")
        (skill / "scripts" / "bp" / "features.py").write_text("raise ValueError('bad feature table')\n", encoding="utf-8")
        store = self.base / "store"
        env = dict(self.env, APP_BLUEPRINT_NO_CAPTURE="", APP_BLUEPRINT_HOME=str(store))
        launcher = skill / "scripts" / "blueprint.py"
        self.init_package_copy()
        r = S.run_script(launcher, ["guard", "x.py", "--root", self.root], self.elsewhere, env=env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("failed to load bp/features.py (ValueError: bad feature table)", r.stderr)
        self.assertNotIn("is incomplete", r.stderr)
        r = S.run_script(launcher, ["status", "--root", self.root], self.elsewhere, env=env)
        self.assertEqual(r.returncode, 70, r.stderr)
        self.assertIn("failed to load bp/features.py", r.stderr)
        incidents = (store / "incidents.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(incidents), 2)
        self.assertEqual(json.loads(incidents[0])["error"], "ValueError")

    def test_a_hand_damaged_copy_missing_a_later_module_fails_closed(self):
        """A later workstream's module (here a throwaway W3-V guard module in a temp skill) is covered by the manifest
        without any list being edited: deleted from the copy, every command fails closed."""
        skill = self.base / "skill later"
        shutil.copytree(S.SCRIPTS, skill / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(S.SKILL / "assets", skill / "assets")
        (skill / "scripts" / "bp" / "records.py").write_text(
            "def refuse(action, root, plan, path, by):\n"
            "    if action == 'write' and path == '.blueprint/state.json':\n"
            "        return '.blueprint/state.json is written only by blueprint.py'\n", encoding="utf-8")
        r = S.run_script(skill / "scripts" / "blueprint.py", ["init", "--name", "Later", "--root", self.root],
                         self.elsewhere, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.write_example_plan()
        self.assertIn("bp/records.py", self.manifest()[1])
        self.assertEqual(self.copy_run("guard", ".blueprint/state.json").returncode, 2)  # the module works
        self.assert_guards_work()
        (self.bin / "bp" / "records.py").unlink()
        self.assert_fails_closed()
        r = self.copy_run("guard", ".blueprint/state.json")
        self.assertEqual(r.returncode, 2)
        self.assertIn("is incomplete", r.stderr)

    def test_not_built_in_the_skill_but_never_in_a_copy(self):
        """registry: absent from the skill = not built (optional); listed in the copy's MANIFEST and absent = an
        incomplete copy, even if the start-up check were bypassed."""
        from bp import install, registry
        self.assertIsNone(registry.load("records"))  # the skill folder has no MANIFEST: not built
        self.assertFalse(registry.exists("records"))
        with mock.patch.object(install, "listed_missing", lambda name, home=None: name == "records"):
            with self.assertRaisesRegex(ImportError, "listed in this copy's MANIFEST"):
                registry.load("records")
            with self.assertRaisesRegex(ImportError, "listed in this copy's MANIFEST"):
                registry.exists("records")
            self.assertIsNone(registry.load("roadmap"))

    def test_required_modules_rule(self):
        from bp import install
        home = self.base / "home bin"
        (home / "bp").mkdir(parents=True)
        self.assertEqual(install.required_modules(home), [])  # the skill: nothing beyond what is on disk
        (home / "VERSION").write_text("1.8.9\n", encoding="utf-8")
        self.assertEqual(install.required_modules(home), [])  # an old copy, or a refresh before the MANIFEST step
        (home / "VERSION").write_text(bp.TOOL_VERSION + "\n", encoding="utf-8")
        with self.assertRaises(ImportError):
            install.required_modules(home)  # completed by this version, yet no MANIFEST
        install.write_manifest(home, ["bp/a.py", "bp/views/__init__.py", "blueprint.py", "VERSION", "MANIFEST"])
        self.assertEqual(install.required_modules(home), ["bp.a", "bp.views"])
        self.assertEqual((home / "MANIFEST").read_text(encoding="utf-8"),
                         f"# app-blueprint tool {bp.TOOL_VERSION}\nblueprint.py\nbp/a.py\nbp/views/__init__.py\n")


REAL_RMTREE, REAL_RENAME, REAL_REPLACE = shutil.rmtree, os.rename, os.replace


def locked(names, real):
    """A stand-in for a remove or rename primitive that raises PermissionError for folders with these names."""
    def fake(path, *args, **kw):
        if Path(path).name in names:
            raise PermissionError(13, "simulated: in use by another process", str(path))
        return real(path, *args, **kw)
    return fake


class RefreshRobustnessTest(CopyTestBase):
    """Review of PR #2: read-only files and locked leftovers never break a refresh for good (contract 1.7)."""

    def make_skill(self, version, read_only):
        """A copy of the skill's scripts and assets at another version, optionally with every file read-only."""
        skill = self.base / f"skill {version}"
        shutil.copytree(S.SCRIPTS, skill / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(S.SKILL / "assets", skill / "assets")
        for f in (skill / "scripts" / "blueprint.py", skill / "scripts" / "bp" / "version.py"):
            f.write_text(VERSION_LINE.sub(f'TOOL_VERSION = "{version}"', f.read_text(encoding="utf-8")),
                         encoding="utf-8")
        if read_only:
            for f in skill.rglob("*"):
                if f.is_file():
                    make_read_only(f)
        return skill / "scripts" / "blueprint.py"

    def refresh_with(self, launcher):
        return S.run_script(launcher, ["init", "--refresh-tool", "--root", self.root], self.elsewhere, env=self.env)

    def in_process(self, stop=None):
        """copy_tool in this process; returns (exit code or None, stderr)."""
        from bp import install
        err = io.StringIO()
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                install.copy_tool(self.root, _stop_after=stop)
        except SystemExit as e:
            return e.code, err.getvalue()
        return None, err.getvalue()

    def assert_complete(self, version):
        self.assertEqual((self.bin / "VERSION").read_text(encoding="utf-8"), version + "\n")
        self.assertEqual(VERSION_LINE.search(self.copy.read_text(encoding="utf-8")).group(1), version)
        self.assertEqual(VERSION_LINE.search((self.bin / "bp" / "version.py").read_text(encoding="utf-8")).group(1),
                         version)
        self.assert_guards_work()

    def assert_no_leftovers(self):
        self.assertEqual(sorted(p.name for p in self.bin.iterdir() if p.name.startswith("bp.")), [])

    def test_read_only_skill_files(self):
        r = S.run_script(self.make_skill("1.9.0", True), ["init", "--name", "RO", "--root", self.root],
                         self.elsewhere, env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.write_example_plan()
        unwritable = [str(p) for p in self.bin.rglob("*") if p.is_file() and not os.access(p, os.W_OK)]
        self.assertEqual(unwritable, [], "files in .blueprint/bin must not be read-only")
        newer = self.make_skill("1.9.1", True)
        for attempt in (1, 2):
            r = self.refresh_with(newer)
            self.assertEqual(r.returncode, 0, f"refresh {attempt}: {r.stdout}{r.stderr}")
            self.assert_complete("1.9.1")
            self.assert_no_leftovers()

    def test_copy_made_read_only_by_an_older_tool(self):
        """A copy whose files are read-only (as 1.9.0 copy2 left them) still refreshes."""
        self.init_package_copy()
        for f in self.bin.rglob("*"):
            if f.is_file():
                make_read_only(f)
        self.refresh()
        self.assert_complete(bp.TOOL_VERSION)
        self.assert_no_leftovers()

    def test_leftover_bp_old_with_a_read_only_file(self):
        self.init_package_copy()
        (self.bin / "bp.old").mkdir()
        (self.bin / "bp.old" / "x.py").write_text("x = 1\n", encoding="utf-8")
        make_read_only(self.bin / "bp.old" / "x.py")
        self.refresh()
        self.assert_complete(bp.TOOL_VERSION)
        self.assert_no_leftovers()

    def test_leftover_bp_new_is_removed_first(self):
        """A stale module in a leftover bp.new must not end up in bp/."""
        self.init_package_copy()
        (self.bin / "bp.new").mkdir()
        (self.bin / "bp.new" / "stale_module.py").write_text("raise RuntimeError('stale')\n", encoding="utf-8")
        make_read_only(self.bin / "bp.new" / "stale_module.py")
        self.refresh()
        self.assertFalse((self.bin / "bp" / "stale_module.py").exists())
        self.assert_complete(bp.TOOL_VERSION)
        self.assert_no_leftovers()

    def test_locked_leftover_bp_old_is_moved_aside(self):
        self.init_package_copy()
        self.age_package_copy()
        (self.bin / "bp.old").mkdir()
        (self.bin / "bp.old" / "x.py").write_text("x = 1\n", encoding="utf-8")
        with mock.patch("shutil.rmtree", side_effect=locked({"bp.old"}, REAL_RMTREE)):
            code, err = self.in_process()  # bp.old can be neither deleted before nor after the swap
        self.assertIsNone(code, err)
        self.assert_complete(bp.TOOL_VERSION)
        self.refresh()  # nothing is locked any more: the second refresh cleans up
        self.assert_complete(bp.TOOL_VERSION)
        self.assert_no_leftovers()

    def test_locked_leftover_that_cannot_be_moved_exits_1(self):
        self.init_package_copy()
        self.age_package_copy()
        (self.bin / "bp.old").mkdir()
        with mock.patch("shutil.rmtree", side_effect=locked({"bp.old"}, REAL_RMTREE)), \
                mock.patch("os.rename", side_effect=locked({"bp.old"}, REAL_RENAME)):
            code, err = self.in_process()
        self.assertEqual(code, 1, err)
        self.assertIn(str(self.bin / "bp.old"), err)
        self.assertIn("init --refresh-tool", err)
        self.assert_complete("1.8.9")  # untouched: the old copy still works
        self.refresh()
        self.assert_complete(bp.TOOL_VERSION)
        self.assert_no_leftovers()

    def test_locked_bp_names_bp(self):
        self.init_package_copy()
        self.age_package_copy()
        with mock.patch("os.replace", side_effect=locked({"bp"}, REAL_REPLACE)):
            code, err = self.in_process()
        self.assertEqual(code, 1, err)
        self.assertIn(f"{self.bin / 'bp'}:", err)
        self.assertNotIn("bp.old", err)
        self.assert_complete("1.8.9")  # untouched: the old copy still works
        self.refresh()
        self.assert_complete(bp.TOOL_VERSION)
        self.assert_no_leftovers()

    def test_swap_that_fails_halfway_puts_the_old_package_back(self):
        self.init_package_copy()
        self.age_package_copy()
        with mock.patch("os.replace", side_effect=locked({"bp.new"}, REAL_REPLACE)):
            code, err = self.in_process()
        self.assertEqual(code, 1, err)
        self.assertIn(f"{self.bin / 'bp.new'}:", err)
        self.assert_complete("1.8.9")  # bp/ is the old package again, not missing
        self.refresh()
        self.assert_complete(bp.TOOL_VERSION)
        self.assert_no_leftovers()

    def test_single_file_upgrade_interrupted_after_step_2_still_says_upgraded(self):
        self.init_single_file_copy()
        self.in_process(stop=2)
        r = self.refresh()
        self.assertIn(f"Upgraded .blueprint/bin from the single-file tool 1.3.0 to the package tool {bp.TOOL_VERSION}.",
                      r.stdout)
        self.assert_complete(bp.TOOL_VERSION)


class PackageCompletenessTest(CopyTestBase):
    """Review of PR #2: a copy missing any module of the package fails closed at start, never with an incident."""

    def test_every_module_is_checked_at_start(self):
        from bp import cli
        expected = []
        for p in S.PACKAGE.rglob("*.py"):
            parts = ("bp",) + p.relative_to(S.PACKAGE).with_suffix("").parts
            expected.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
        # W0-A2 (decision 1, option D): the list is no longer written by hand; cli checks every module on disk (the
        # launcher imports bp.api and bp.cli itself), so a later module is covered by its file alone
        self.assertEqual(sorted(cli.PACKAGE_MODULES), sorted(expected))

    def test_missing_module_fails_closed(self):
        self.init_package_copy()
        files = sorted(p.relative_to(self.bin / "bp") for p in (self.bin / "bp").rglob("*.py"))
        self.assertGreater(len(files), 20)
        for rel in files:
            with self.subTest(module=rel.as_posix()):
                f = self.bin / "bp" / rel
                f.unlink()
                try:
                    for r, code in ((self.copy_run("guard", "Cargo.toml"), 2), (self.hook_pre("Cargo.toml"), 2),
                                    (self.copy_run("verify", "--run"), 1)):
                        self.assertEqual(r.returncode, code, r.stdout + r.stderr)
                        self.assertIn("is incomplete", r.stderr)
                finally:
                    shutil.copyfile(S.PACKAGE / rel, f)
        self.assert_guards_work()


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


class InstallHelpersTest(unittest.TestCase):
    """Review round 2 nits on PR #2: a read-only folder, and a temp file that cannot be removed."""

    def setUp(self):
        import bp.install as install
        self.install = install
        self.tmp = Path(tempfile.mkdtemp(prefix="bp-install-"))

    def tearDown(self):
        for folder, dirs, files in os.walk(self.tmp):
            for name in dirs + files:
                os.chmod(os.path.join(folder, name), stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rmtree_clears_read_only_on_the_folder_itself(self):
        root = self.tmp / "bp.old"
        root.mkdir()
        (root / "a.py").write_text("x = 1\n", encoding="utf-8")
        seen = []
        real = self.install._writable

        def spy(path):
            seen.append(Path(path))
            real(path)

        with mock.patch.object(self.install, "_writable", spy):
            self.install._rmtree(root)
        self.assertIn(root, seen, "the folder being removed must be made writable too, not only what is inside it")
        self.assertFalse(root.exists())

    def test_copy_exits_1_when_the_old_file_cannot_be_removed(self):
        src = self.tmp / "src.py"
        dst = self.tmp / "dst.py"
        src.write_text("new\n", encoding="utf-8")
        dst.write_text("old\n", encoding="utf-8")
        err = io.StringIO()
        with mock.patch.object(self.install.os, "remove", side_effect=PermissionError(13, "in use")), \
                contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                self.install._copy(src, dst)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("in use", err.getvalue())
        self.assertIn("dst.py", err.getvalue())
        self.assertIn("init --refresh-tool", err.getvalue())
