"""W0-A2: schema version 2 and the in-memory upgrade (contract 3), state version 2 (4.1), proofs (4.5), findings and
codes (1.4, 7.4), BlueprintError and the output envelope (7.1, 7.3).

Run: python -m unittest discover -s app-blueprint/tests
"""
import atexit
import contextlib
import copy
import hashlib
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

import support as S

import bp
import bp.cli  # noqa: F401 - its start-up check runs on the real tree, before any hermetic package
import bp.commands
import bp.views
from bp import core, migrate, plan as bplan, registry, version

frozen = S.load_frozen("blueprint_frozen_for_validate")

# The W0 foundations: the modules every build has. Everything else under bp/ (the wave 1 to 3 domain, view and command
# modules, signals, study/*) belongs to a later workstream and may or may not be present in the tree under test.
# A new core module (one every build has, written by a W0 workstream) must be added here, or the hermetic package and
# skill copy would hide it as a later module (review of #15, N3); test_hermetic_skill_has_only_the_core then fails
# loudly. In process, "not built" is simulated with sys.modules[name] = None; the real "file absent" path is covered by
# the hermetic skill copy that the subprocess tests run (N4).
CORE_MODULES = frozenset("bp." + m for m in (
    "version", "core", "globs", "learn", "migrate", "plan", "rules", "data", "status", "verify", "registry", "validate",
    "views", "views.context", "views.structure", "views.roster", "views.render", "export", "install", "api", "cli",
    "commands", "commands.plan", "commands.progress", "commands.data", "commands.learn")) | {"bp"}


def later_modules():
    """Every non-core module name the tests must control: those the registry names, and those present on disk."""
    from bp import install
    named = {"bp." + n for n in registry.DOMAIN + registry.VIEWS} | {"bp." + m for m in registry.COMMAND_TABLE.values()}
    named |= {"bp.signals", "bp.evidence", "bp.study"}
    return sorted((named | set(install.package_modules())) - CORE_MODULES)


@contextlib.contextmanager
def extension(files, hermetic=True):
    """A hermetic package for the duration: every later workstream's module is hidden (sys.modules[name] = None, so it
    reads as not built), except the stand-ins given as "rel/path.py": source, which are written to a temp folder put
    FIRST on the package path, so a stand-in shadows a real module of the same name. Everything is restored after,
    so no stand-in or hidden module leaks into another test (test_extension_is_hermetic_and_restored). With
    hermetic=False (tests about the real tree, like test_plan_fuzz_v2) nothing is hidden and the stand-ins go LAST on
    the package path, so a real module always wins over a stand-in."""
    tmp = Path(tempfile.mkdtemp(prefix="bp-ext-"))
    names, added = set(), []
    hidden = later_modules() if hermetic else []  # not hermetic: real modules win, stand-ins only fill the gaps
    for rel in files:
        names.add("bp." + ".".join(Path(rel).with_suffix("").parts))
    touched = set(hidden) | names
    saved = {n: sys.modules[n] for n in list(sys.modules) if n in touched or n.startswith("bp.study.")}
    # the attribute the import system sets on the parent package (bp.features, bp.views.x, ...): a from-import reads
    # it before sys.modules, so it is saved, removed for the block and put back after (review of #15, M1)
    attrs = {}
    for n in touched:
        parent, _, leaf = n.rpartition(".")
        pkg = sys.modules.get(parent)
        if pkg is not None:
            attrs[n] = (pkg, leaf, pkg.__dict__.get(leaf, _MISSING))
    try:
        for rel, src in files.items():
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(src), encoding="utf-8")
        for n, (pkg, leaf, _) in attrs.items():
            if hermetic or n in names and n not in sys.modules:
                pkg.__dict__.pop(leaf, None)
        for n in hidden:
            sys.modules.pop(n, None)
            if n not in names:
                sys.modules[n] = None  # not built in this hermetic package
        for n in names:
            if hermetic or n not in sys.modules:
                sys.modules.pop(n, None)
        for pkg, folder in ((bp, tmp), (bp.views, tmp / "views"), (bp.commands, tmp / "commands")):
            if folder.is_dir():
                pkg.__path__.insert(0 if hermetic else len(pkg.__path__), str(folder))
                added.append((pkg, str(folder)))
        importlib.invalidate_caches()
        yield tmp
    finally:
        for pkg, folder in added:
            pkg.__path__.remove(folder)
        for n in touched:
            sys.modules.pop(n, None)
        for n in [k for k in sys.modules if k.startswith("bp.study.") and k not in saved]:
            sys.modules.pop(n, None)
        sys.modules.update(saved)
        for n, (pkg, leaf, value) in attrs.items():
            if value is _MISSING:
                pkg.__dict__.pop(leaf, None)
            else:
                pkg.__dict__[leaf] = value
        importlib.invalidate_caches()
        shutil.rmtree(tmp, ignore_errors=True)


_MISSING = object()


def hermetic_skill(dest):
    """A copy of the skill (scripts and assets) holding only the core: every later workstream's module file and tab
    script is removed. Subprocess tests that assert "not built" or empty results run the launcher from here, so they
    hold whatever modules the real skill has."""
    shutil.copytree(S.SCRIPTS, dest / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(S.SKILL / "assets", dest / "assets")
    pkg = dest / "scripts" / "bp"
    for p in sorted(pkg.rglob("*.py"), reverse=True):
        rel = p.relative_to(pkg).with_suffix("").parts
        name = ".".join(("bp",) + (rel[:-1] if rel[-1] == "__init__" else rel))
        if name not in CORE_MODULES:
            p.unlink()
    for folder in sorted((d for d in pkg.rglob("*") if d.is_dir() and d.name != "__pycache__"), reverse=True):
        if not any(folder.rglob("*.py")):
            shutil.rmtree(folder, ignore_errors=True)
    for tab in (dest / "assets").glob("tab-*.js"):
        tab.unlink()
    return dest / "scripts" / "blueprint.py"



class Hermetic:
    """Mixin: every test runs in a hermetic package (extension({})) and, through the CLI, from a hermetic copy of the
    skill, so what it asserts does not depend on which later workstreams' modules exist. A test that is about the
    real tree says so and does not use this mixin."""

    def setUp(self):
        super().setUp()
        stack = contextlib.ExitStack()  # closes cleanly even when a test is interrupted (review of #15, N1)
        stack.enter_context(extension({}))
        self.addCleanup(stack.close)


_HERMETIC = {}


def hermetic_launcher():
    """The launcher of a hermetic skill copy, made once per test process."""
    if "launcher" not in _HERMETIC:
        base = Path(tempfile.mkdtemp(prefix="bp-hermetic-skill-"))
        atexit.register(shutil.rmtree, base, True)  # removed when the test process ends (review of #15, N1)
        _HERMETIC["launcher"] = hermetic_skill(base / "skill")
    return _HERMETIC["launcher"]


# Contract 2.10, verbatim: the smallest plan that uses all four new sections. Other 2.0 tests import it from here.
V2_EXAMPLE = r'''
{
  "version": 2,
  "app": {"name": "Snag Desk", "summary": "Log snags through an API and review them in an inbox.", "platforms": ["web"], "preset": "rust-agent-engine"},
  "stack": [{"part": "HTTP API", "choice": "Axum", "version": "0.8", "docs": "https://docs.rs/axum/latest/axum/", "verified": "2026-11-18"}],
  "approach": {"layout": "Cargo workspace plus apps/web", "notes": ""},
  "modules": [
    {"id": "api", "path": "crates/snag-api", "layer": "interface", "purpose": "HTTP API for snags", "dependsOn": [], "verify": "cargo test -p snag-api"},
    {"id": "web", "path": "apps/web", "layer": "ui", "purpose": "Inbox for reviewing snags", "dependsOn": ["api"], "verify": "pnpm --dir apps/web test"},
    {"id": "repo", "path": "", "layer": "build", "purpose": "Workspace configuration", "dependsOn": [], "verify": "cargo metadata --format-version 1"}
  ],
  "phases": [
    {"id": "P1", "name": "Walking skeleton", "goal": "One snag goes in and shows in the inbox.", "exit": "Both test suites pass", "rollback": "Revert the P1 merge commit"},
    {"id": "P2", "name": "Offline inbox", "goal": "The inbox works without signal.", "exit": "Offline tests pass"}
  ],
  "milestones": [{"id": "M1", "name": "Private beta", "goal": "One site team logs and reviews snags", "due": "2027-01-15", "exit": "F-log-snag and F-inbox accepted"}],
  "features": [
    {"id": "F-log-snag", "title": "Log a snag", "statement": "A client records a snag through the API and gets its id back.", "kind": "add",
     "owner": "backend-lead", "milestone": "M1", "level": "integration",
     "criteria": [
       {"id": "C1", "scenario": "The API runs on an empty database", "action": "POST /snags with a title", "expected": "201 with the new id; GET /snags lists it",
        "mustNot": "Create a second row when the request is retried with the same Idempotency-Key", "verify": "cargo test -p snag-api --test snags", "test": "crates/snag-api/tests/snags.rs"},
       {"id": "C2", "scenario": "The API runs", "action": "POST /snags without a title", "expected": "422 naming the title field", "level": "unit", "verify": "cargo test -p snag-api validation"}]},
    {"id": "F-inbox", "title": "Review snags in the inbox", "statement": "A site lead sees open snags, newest first.", "kind": "add",
     "owner": "frontend-engineer", "milestone": "M1", "level": "e2e", "dependsOn": ["F-log-snag"],
     "criteria": [{"id": "C1", "scenario": "Two snags exist", "action": "Open the inbox", "expected": "Both are listed, newest first", "level": "manual"}]}
  ],
  "tasks": [{"id": "T-domain", "title": "Register snagdesk.example and point it at the host", "owner": "owner", "milestone": "M1", "ownerAction": true, "doneWhen": "The domain serves the inbox"}],
  "unknowns": [{"id": "U-offline", "question": "Must the inbox work offline on site?", "severity": "high", "status": "investigating", "blocks": ["P2"],
                "evidenceFor": [], "evidenceAgainst": [],
                "experiment": {"plan": "Ask three site leads; measure signal at two sites", "timebox": "3d", "exit": "Two of three need offline, or every site has signal", "owner": "owner"}}],
  "files": [
    {"path": "Cargo.toml", "action": "create", "module": "repo", "phase": "P1", "purpose": "Workspace manifest", "infrastructure": true},
    {"path": "crates/snag-api/src/main.rs", "action": "create", "module": "api", "phase": "P1", "purpose": "POST /snags and GET /snags", "produces": ["POST /snags", "GET /snags"], "features": ["F-log-snag"]},
    {"path": "crates/snag-api/tests/snags.rs", "action": "create", "module": "api", "phase": "P1", "purpose": "API tests for creating and listing snags", "dependsOn": ["crates/snag-api/src/main.rs"], "features": ["F-log-snag"]},
    {"path": "apps/web/src/Inbox.tsx", "action": "create", "module": "web", "phase": "P1", "purpose": "Inbox list of open snags", "consumes": ["GET /snags"], "features": ["F-inbox"]},
    {"path": "apps/web/src/offline.ts", "action": "create", "module": "web", "phase": "P2", "purpose": "Offline cache for the inbox", "features": ["F-inbox"]}
  ],
  "patterns": [],
  "owners": [
    {"pattern": "*", "owner": "cto-architect"},
    {"pattern": "crates/", "owner": "backend-lead"},
    {"pattern": "apps/web/", "owner": "frontend-engineer"}
  ],
  "roster": [
    {"id": "cto-architect", "kind": "agent", "harness": "claude-code", "role": "Plan and workspace"},
    {"id": "backend-lead", "kind": "agent", "harness": "claude-code", "role": "API"},
    {"id": "frontend-engineer", "kind": "agent", "harness": "codex", "role": "Inbox"},
    {"id": "evaluator", "kind": "agent", "harness": "claude-code", "role": "Accepts features; owns no files"},
    {"id": "owner", "kind": "human", "role": "Product owner"}
  ],
  "decisions": [], "ignore": [], "stores": [], "resources": []
}
'''
NEW = ("milestones", "features", "tasks", "unknowns")


def v2_plan():
    return json.loads(V2_EXAMPLE)


def v1_plan():
    return json.loads(S.EXAMPLE.read_text(encoding="utf-8"))


def v1_fixtures():
    """Every 1.x plan the tests have: the example, examples/snag-desk as 1.3.0 left it, the 1.3.0 skeleton, and a plan
    with unknown keys in an unusual order."""
    snag = json.loads(json.loads((S.DATA_V1 / "snag-desk-input.json").read_text(encoding="utf-8"))["blueprint"])
    odd = {"zeta": {"keep": [1, None]}, "version": 1, "files": [{"path": "a", "custom": {"x": 1}}], "alpha": None}
    return {"example": v1_plan(), "snag-desk": snag, "skeleton-1.3.0": copy.deepcopy(frozen.SKELETON), "odd": odd}


def quiet(fn, *args, **kw):
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        return fn(*args, **kw), err.getvalue()


class TempRoot(unittest.TestCase):
    """A temp project for CLI tests against the real tree (other workstreams' tests build on it). The W0-A2 tests use
    HermeticRoot below."""

    def launcher(self):
        return S.LAUNCHER

    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="bp v2 "))
        (self.root / ".blueprint").mkdir()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_plan(self, plan):
        (self.root / ".blueprint" / "blueprint.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")

    def touch(self, rel, data="x\n"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            p.write_bytes(data)
        else:
            p.write_bytes(data.encode("utf-8"))
        return p

    def cli(self, *args, stdin=None, **env):
        """Run the launcher on this project; --root is added to every command but hook (which reads cwd)."""
        argv = list(args) if args[0] == "hook" else list(args) + ["--root", self.root]
        return S.run_script(self.launcher(), argv, self.root, stdin=stdin, env=S.tool_env(**env))

    def load_exit(self, plan):
        """load_plan on plan: (exit code or None, stderr)."""
        self.write_plan(plan)
        err = io.StringIO()
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                bplan.load_plan(self.root)
        except SystemExit as e:
            unprinted = "" if getattr(e, "printed", True) else (getattr(e, "message", "") or "")
            return e.code, err.getvalue() + unprinted
        return None, err.getvalue()




class HermeticRoot(Hermetic, TempRoot):
    """TempRoot in a hermetic package and CLI (see Hermetic): what these tests assert holds whichever later modules
    exist."""

    def launcher(self):
        return hermetic_launcher()

# ---------------------------------------------------------------------------------------------------- versions

class VersionTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(version.SCHEMA_VERSION, 2)
        self.assertEqual(version.SUPPORTED_SCHEMAS, (1, 2))
        self.assertEqual(version.STATE_VERSION, 2)
        self.assertEqual(version.MIN_SCHEMA2_TOOL, "1.9.1")
        # not pinned (the orchestrator raises it): any release that reads schema 2, compared on its numbers
        def nums(v):
            return tuple(int(x) for x in v.split(".")[:3])
        self.assertGreaterEqual(nums(version.TOOL_VERSION), nums(version.MIN_SCHEMA2_TOOL))

    def test_skeleton_is_version_2(self):
        sk = bplan.SKELETON
        self.assertEqual(sk["version"], 2)
        keys = list(sk)
        self.assertEqual(keys[keys.index("ignore") + 1:keys.index("ignore") + 5], list(NEW))
        self.assertEqual([sk[k] for k in NEW], [[], [], [], []])
        self.assertEqual(bplan.SECTIONS_V2, NEW)
        old = copy.deepcopy(frozen.SKELETON)
        self.assertEqual({k: v for k, v in sk.items() if k not in NEW and k != "version"},
                         {k: v for k, v in old.items() if k != "version"})


# ---------------------------------------------------------------------------------------------------- migrate

class UpgradePlanTest(unittest.TestCase):
    def test_v1_is_lossless_ordered_and_idempotent(self):
        for name, raw in v1_fixtures().items():
            with self.subTest(fixture=name):
                before = copy.deepcopy(raw)
                up = migrate.upgrade_plan(raw)
                self.assertEqual(raw, before, "upgrade_plan must not change its input")
                self.assertEqual(up["version"], 2)
                added = [k for k in NEW if k not in raw]
                self.assertEqual(list(up), list(raw) + added, "keys keep their order; missing sections are appended")
                for k in added:
                    self.assertEqual(up[k], [])
                back = {k: v for k, v in up.items() if k not in added}
                back["version"] = 1
                self.assertEqual(back, raw)
                self.assertEqual(migrate.upgrade_plan(up), up)
                self.assertEqual(migrate.upgrade_plan(migrate.upgrade_plan(raw)), up)

    def test_result_shares_nothing_with_the_input(self):
        raw = v1_plan()
        up = migrate.upgrade_plan(raw)
        up["files"][0]["path"] = "changed"
        up["app"]["name"] = "changed"
        self.assertEqual(raw, v1_plan())

    def test_existing_new_sections_are_kept(self):
        raw = v1_plan()
        raw["features"] = [{"id": "F-x", "custom": True}]
        up = migrate.upgrade_plan(raw)
        self.assertEqual(up["features"], [{"id": "F-x", "custom": True}])
        self.assertEqual(list(up)[-3:], ["milestones", "tasks", "unknowns"])

    def test_v2_is_returned_unchanged_as_a_copy(self):
        raw = v2_plan()
        up = migrate.upgrade_plan(raw)
        self.assertEqual(up, raw)
        self.assertIsNot(up, raw)
        up["features"].clear()
        self.assertEqual(raw, v2_plan())

    def test_zero_or_negative_is_a_shape_error(self):  # review N3
        for v in (0, -1):
            raw = v1_plan()
            raw["version"] = v
            with self.assertRaises(core.BlueprintError) as cm:
                migrate.upgrade_plan(raw)
            self.assertEqual((cm.exception.error, cm.exception.message),
                             ("bad-shape", "blueprint.json: version must be 1 or 2"))

    def test_other_versions_are_schema_too_new(self):
        for v in (3, 99, 2 ** 70):
            with self.subTest(version=v):
                raw = v1_plan()
                raw["version"] = v
                with self.assertRaises(core.BlueprintError) as cm:
                    migrate.upgrade_plan(raw)
                e = cm.exception
                self.assertIsInstance(e, SystemExit)
                self.assertEqual((e.error, e.code), ("schema-too-new", 1))
                self.assertEqual(e.message, f"blueprint.json is schema version {v}; this tool reads 1 and 2")
                self.assertIn("init --refresh-tool", e.next)

    def test_plan_version(self):
        for raw, want in (({"version": 1}, 1), ({"version": 2}, 2), ({"version": 7}, 7), ({}, 0),
                          ({"version": "1"}, 0), ({"version": True}, 0), ({"version": 1.0}, 0), ([], 0)):
            with self.subTest(raw=raw):
                self.assertEqual(migrate.plan_version(raw), want)


class UpgradeStateTest(unittest.TestCase):
    V1 = {"files": {"a.rs": {"state": "built", "by": "x", "at": "t", "note": None,
                             "verified": {"ok": True, "cmd": "c", "at": "t", "sha": "ab" * 20}}},
          "patterns": {}, "unplanned": ["u"], "syncedAt": "t", "head": "h", "custom": [1]}

    def test_v1_state_gains_version_and_tasks(self):
        raw = copy.deepcopy(self.V1)
        up = migrate.upgrade_state(raw)
        self.assertEqual(raw, self.V1)
        self.assertEqual(up["stateVersion"], 2)
        self.assertEqual(up["tasks"], {})
        self.assertEqual({k: v for k, v in up.items() if k not in ("stateVersion", "tasks")}, self.V1)
        self.assertEqual(migrate.upgrade_state(up), up)

    def test_any_state_version_but_2_is_version_1(self):  # review N2
        for v in (1, 0, "2", None, 3):
            with self.subTest(stateVersion=v):
                up = migrate.upgrade_state(dict(copy.deepcopy(self.V1), stateVersion=v))
                self.assertEqual((up["stateVersion"], up["tasks"]), (2, {}))

    def test_v2_state_gets_tasks(self):  # review R2-N5
        self.assertEqual(migrate.upgrade_state({"stateVersion": 2, "files": {}}), {"stateVersion": 2, "files": {}, "tasks": {}})

    def test_v2_state_is_kept(self):
        raw = dict(copy.deepcopy(self.V1), stateVersion=2, tasks={"T-x": {"status": "done"}})
        self.assertEqual(migrate.upgrade_state(raw), raw)


# ---------------------------------------------------------------------------------------------------- load_plan

class LoadPlanTest(HermeticRoot):
    def test_v1_file_is_upgraded_in_memory_only(self):
        self.write_plan(v1_plan())
        before = (self.root / ".blueprint" / "blueprint.json").read_bytes()
        plan, _ = quiet(bplan.load_plan, self.root)
        self.assertEqual(plan["version"], 2)
        self.assertEqual(plan["_from"], 1)
        for k in NEW:
            self.assertEqual(plan[k], [])
        self.assertEqual((self.root / ".blueprint" / "blueprint.json").read_bytes(), before)
        self.assertEqual(bplan.load_raw_plan(self.root), v1_plan())

    def test_from_in_a_file_is_ignored(self):  # review N1: "_from" is an in-memory marker only
        self.write_plan(dict(v2_plan(), _from=1))
        plan, _ = quiet(bplan.load_plan, self.root)
        self.assertNotIn("_from", plan)
        from bp import validate
        self.assertEqual(validate.validate_findings(plan), [])
        r = self.cli("status")
        self.assertNotIn("Schema version 1", r.stdout)
        self.write_plan(dict(v1_plan(), _from=7))
        self.assertEqual(quiet(bplan.load_plan, self.root)[0]["_from"], 1)

    def test_v2_file_loads_without_from(self):
        self.write_plan(v2_plan())
        plan, _ = quiet(bplan.load_plan, self.root)
        self.assertNotIn("_from", plan)
        self.assertEqual([f["id"] for f in plan["features"]], ["F-log-snag", "F-inbox"])
        self.assertEqual(plan["features"][0]["criteria"][1]["level"], "unit")

    def test_version_is_checked_at_load(self):
        for value, want in ((None, "blueprint.json: version is required"),
                            (True, "blueprint.json: version must be a whole number (got boolean)"),
                            (1.0, "blueprint.json: version must be a whole number (got number)"),
                            ("1", "blueprint.json: version must be a whole number (got string)"),
                            ([1], "blueprint.json: version must be a whole number (got list)")):
            with self.subTest(version=value):
                plan = v1_plan()
                plan["version"] = value
                code, err = self.load_exit(plan)
                self.assertEqual(code, 1)
                self.assertIn(want, err)
                self.assertIn("Fix it, then run: python .blueprint/bin/blueprint.py validate", err)
        plan = v1_plan()
        del plan["version"]
        code, err = self.load_exit(plan)
        self.assertEqual(code, 1)
        self.assertIn("blueprint.json: version is required", err)

    def test_too_new_version_at_load(self):
        plan = v1_plan()
        plan["version"] = 3
        self.write_plan(plan)
        with self.assertRaises(core.BlueprintError) as cm:
            quiet(bplan.load_plan, self.root)
        self.assertEqual((cm.exception.error, cm.exception.code), ("schema-too-new", 1))

    TYPE_CASES = [
        (("features",), "F-x", "features must be a list (got string)"),
        (("features", 0), "x", "features[0] must be an object (got string)"),
        (("features", 0, "criteria"), "x", "features[0].criteria must be a list (got string)"),
        (("features", 0, "criteria", 0), 5, "features[0].criteria[0] must be an object (got number)"),
        (("features", 0, "criteria", 0, "scenario"), 5, "features[0].criteria[0].scenario must be a string (got number)"),
        (("features", 0, "criteria", 1, "level"), ["unit"], "features[0].criteria[1].level must be a string (got list)"),
        (("features", 0, "draft"), "yes", "features[0].draft must be a boolean (got string)"),
        (("features", 0, "source"), "S-x", "features[0].source must be an object (got string)"),
        (("features", 0, "source"), {"study": 5, "behaviours": "B-x"}, "features[0].source.study must be a string (got number)"),
        (("features", 0, "source"), {"study": "S-x", "behaviours": "B-x"}, "features[0].source.behaviours must be a list (got string)"),
        (("features", 1, "dependsOn"), [5], "features[1].dependsOn[0] must be a string (got number)"),
        (("features", 1, "kind"), 1, "features[1].kind must be a string (got number)"),
        (("tasks", 0, "ownerAction"), "yes", "tasks[0].ownerAction must be a boolean (got string)"),
        (("tasks", 0, "dependsOn"), "T-x", "tasks[0].dependsOn must be a list (got string)"),
        (("milestones", 0, "due"), 20270115, "milestones[0].due must be a string (got number)"),
        (("milestones",), {"id": "M1"}, "milestones must be a list (got object)"),
        (("unknowns", 0, "experiment"), "x", "unknowns[0].experiment must be an object (got string)"),
        (("unknowns", 0, "experiment", "timebox"), 3, "unknowns[0].experiment.timebox must be a string (got number)"),
        (("unknowns", 0, "blocks"), "P2", "unknowns[0].blocks must be a list (got string)"),
        (("unknowns", 0, "evidenceFor"), [1], "unknowns[0].evidenceFor[0] must be a string (got number)"),
        (("files", 1, "features"), "F-log-snag", "files[1].features must be a list (got string)"),
        (("files", 0, "infrastructure"), "yes", "files[0].infrastructure must be a boolean (got string)"),
        (("phases", 0, "rollback"), 1, "phases[0].rollback must be a string (got number)"),
    ]

    def test_new_sections_and_fields_are_type_checked(self):
        for path, value, want in self.TYPE_CASES:
            with self.subTest(path=path, value=value):
                plan = v2_plan()
                node = plan
                for step in path[:-1]:
                    node = node[step]
                node[path[-1]] = value
                code, err = self.load_exit(plan)
                self.assertEqual(code, 1, err)
                self.assertIn("blueprint.json: " + want, err)

    def test_changed_1x_sections_are_type_checked(self):
        plan = v2_plan()
        plan["patterns"] = [{"id": "p", "glob": "a/*.rs", "features": [3], "infrastructure": 1}]
        plan["decisions"] = [{"id": "D1", "decision": "x", "unknown": 5, "alternatives": "a", "date": 20270101}]
        code, err = self.load_exit(plan)
        self.assertEqual(code, 1)
        for want in ("patterns[0].features[0] must be a string (got number)",
                     "patterns[0].infrastructure must be a boolean (got number)",
                     "decisions[0].unknown must be a string (got number)",
                     "decisions[0].alternatives must be a list (got string)",
                     "decisions[0].date must be a string (got number)"):
            self.assertIn(want, err)

    def test_every_problem_is_reported_at_once(self):
        plan = v2_plan()
        plan["files"][0]["path"] = 5           # a 1.3.0 problem
        plan["tasks"][0]["ownerAction"] = "y"   # 2.0 problems
        plan["features"][0]["criteria"] = "x"
        code, err = self.load_exit(plan)
        self.assertEqual(code, 1)
        lines = [ln for ln in err.splitlines() if ln.startswith("blueprint.json: ")]
        self.assertEqual(lines, ["blueprint.json: files[0].path must be a string (got number)",
                                 "blueprint.json: features[0].criteria must be a list (got string)",
                                 "blueprint.json: tasks[0].ownerAction must be a boolean (got string)"])

    def test_objects_that_global_string_fields_would_reject_pass(self):
        plan = v2_plan()
        plan["features"][0]["source"] = {"study": "S-x", "behaviours": ["B-1"]}
        plan["unknowns"][0]["notes"] = "n"
        code, err = self.load_exit(plan)
        self.assertIsNone(code, err)

    def test_null_means_missing_in_new_sections(self):
        plan = v2_plan()
        plan["features"][0]["notes"] = None
        plan["features"][0]["criteria"][0]["mustNot"] = None
        plan["unknowns"][0]["experiment"]["owner"] = None
        plan["tasks"] = None
        plan["files"][0]["features"] = None
        self.write_plan(plan)
        loaded, _ = quiet(bplan.load_plan, self.root)
        self.assertNotIn("notes", loaded["features"][0])
        self.assertNotIn("mustNot", loaded["features"][0]["criteria"][0])
        self.assertNotIn("owner", loaded["unknowns"][0]["experiment"])
        self.assertNotIn("features", loaded["files"][0])
        self.assertEqual(loaded["tasks"], [])

    def test_helpers(self):
        self.write_plan(v2_plan())
        plan, _ = quiet(bplan.load_plan, self.root)
        idx = bplan.section_index(plan, "features")
        self.assertEqual(list(idx), ["F-log-snag", "F-inbox"])
        self.assertIs(idx["F-inbox"], plan["features"][1])
        self.assertEqual(bplan.section_index(plan, "tasks")["T-domain"]["owner"], "owner")
        self.assertEqual(bplan.features_of(plan["files"][1]), ["F-log-snag"])
        self.assertEqual(bplan.features_of(plan["files"][0]), [])
        self.assertEqual(bplan.features_of({"features": "F-x"}), [])

    def test_commands_exit_1_without_a_traceback(self):
        plan = v2_plan()
        plan["features"][0]["criteria"] = "x"
        self.write_plan(plan)
        for args in (["validate"], ["status"], ["render"], ["export", "json"]):
            r = self.cli(*args)
            self.assertEqual(r.returncode, 1, (args, r.stdout, r.stderr))
            self.assertIn("features[0].criteria must be a list (got string)", r.stderr)
            self.assertNotIn("Traceback", r.stderr)

    def test_schema_too_new_through_the_cli(self):
        plan = v1_plan()
        plan["version"] = 3
        self.write_plan(plan)
        r = self.cli("status")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("error: blueprint.json is schema version 3; this tool reads 1 and 2", r.stderr)
        self.assertIn("  next: update the skill, then run init --refresh-tool", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        r = self.cli("guard", "Cargo.toml")
        self.assertEqual(r.returncode, 2, "the guard fails closed on a plan it cannot read")


# ---------------------------------------------------------------------------------------------------- state

class StateVersionTest(HermeticRoot):
    def test_load_state_upgrades_and_the_next_save_writes_it(self):
        self.write_plan(v1_plan())
        (self.root / ".blueprint" / "state.json").write_text(json.dumps(UpgradeStateTest.V1), encoding="utf-8")
        st = bplan.load_state(self.root)
        self.assertEqual((st["stateVersion"], st["tasks"]), (2, {}))
        r = self.cli("claim", "Cargo.toml", "--by", "cto-architect")
        self.assertEqual(r.returncode, 0, r.stderr)
        saved = json.loads((self.root / ".blueprint" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["stateVersion"], 2)
        self.assertEqual(saved["custom"], [1])
        self.assertEqual(saved["files"]["a.rs"], UpgradeStateTest.V1["files"]["a.rs"])

    def test_missing_state_is_version_2(self):
        st = bplan.load_state(self.root)
        self.assertEqual(st["stateVersion"], 2)
        self.assertEqual(st["files"], {})


# ---------------------------------------------------------------------------------------------------- init, refresh, doctor

class InitTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="bp init "))
        home = self.base / "home"
        home.mkdir()
        self.env = S.tool_env(HOME=str(home), USERPROFILE=str(home))

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def init(self, root, script=None):
        root.mkdir(parents=True, exist_ok=True)
        r = S.run_script(script or hermetic_launcher(), ["init", "--name", "New", "--root", root], self.base, env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def test_init_writes_version_2(self):
        root = self.base / "a"
        self.init(root)
        plan = json.loads((root / ".blueprint" / "blueprint.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["version"], 2)
        keys = list(plan)
        self.assertEqual(keys[keys.index("ignore") + 1:], list(NEW))
        state = json.loads((root / ".blueprint" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual((state["stateVersion"], state["tasks"]), (2, {}))
        r = S.run_script(hermetic_launcher(), ["status", "--root", root], self.base, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Schema version", r.stdout)

    def test_init_of_a_new_project_from_another_projects_copy(self):
        first = self.base / "first"
        self.init(first)
        r = self.init(self.base / "second", first / ".blueprint" / "bin" / "blueprint.py")
        self.assertIn("Created .blueprint/", r.stdout)
        self.assertNotIn("already the project's copy", r.stdout)
        self.assertTrue((self.base / "second" / ".blueprint" / "bin" / "bp" / "migrate.py").is_file())
        self.assertTrue((self.base / "second" / ".blueprint" / "bin" / "map.template.html").is_file())

    def test_refresh_and_status_on_a_v1_plan(self):
        root = self.base / "old"
        self.init(root)
        (root / ".blueprint" / "blueprint.json").write_text(json.dumps(v1_plan()), encoding="utf-8")
        r = S.run_script(hermetic_launcher(), ["init", "--refresh-tool", "--root", root], self.base, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Your blueprint is schema version 1. It keeps working.", r.stdout)
        self.assertNotIn("migrate", r.stdout)  # the core-only tree has no migrate command: no hint (test_migrate has the rest)
        r = S.run_script(hermetic_launcher(), ["status", "--root", root], self.base, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        last = r.stdout.rstrip().splitlines()[-1]
        self.assertTrue(last.startswith("Schema version 1"), last)
        self.assertNotIn("migrate", last)

    def test_doctor_compares_the_copy_with_min_schema2_tool(self):
        root = self.base / "doc"
        self.init(root)
        (root / ".blueprint" / "bin" / "VERSION").write_text("1.9.0\n", encoding="utf-8")
        r = S.run_script(hermetic_launcher(), ["doctor", "--root", root], self.base, env=self.env)
        self.assertIn("blueprint.json schema: 2", r.stdout)
        self.assertIn(f"older than {version.MIN_SCHEMA2_TOOL} and cannot read schema version 2", r.stdout)
        (root / ".blueprint" / "bin" / "VERSION").write_text(version.TOOL_VERSION + "\n", encoding="utf-8")
        r = S.run_script(hermetic_launcher(), ["doctor", "--strict", "--root", root], self.base, env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("cannot read schema version 2", r.stdout)


# ---------------------------------------------------------------------------------------------------- core helpers

class CoreHelpersTest(unittest.TestCase):
    def test_blueprint_error(self):
        e = core.BlueprintError("unknown-feature", "no feature F-x", entry="feature F-x", next="status --features",
                                code=3)
        self.assertIsInstance(e, SystemExit)
        self.assertEqual((e.code, e.error, e.message, e.entry, e.next, str(e)),
                         (3, "unknown-feature", "no feature F-x", "feature F-x", "status --features", "no feature F-x"))
        self.assertEqual(core.BlueprintError("x", "m").code, 1)

    def test_digests_and_ids(self):
        self.assertEqual(core.sha256_bytes(b"abc"), "sha256:" + hashlib.sha256(b"abc").hexdigest())
        self.assertEqual(core.canonical_json({"b": 1, "a": "\u00e9", "c": [1, {"z": None}]}),
                         '{"a":"\u00e9","b":1,"c":[1,{"z":null}]}'.encode("utf-8"))
        obj = {"kind": "check", "feature": "F-x"}
        self.assertEqual(core.content_id("vr-", obj, 12),
                         "vr-" + hashlib.sha256(core.canonical_json(obj)).hexdigest()[:12])
        self.assertEqual(core.content_id("vr-", dict(reversed(list(obj.items()))), 12), core.content_id("vr-", obj, 12))

    def test_slug(self):
        for text, kw, want in (("Hello, World!", {}, "hello-world"), ("", {}, "x"), ("--- ***", {}, "x"),
                               ("  Log a Snag  ", {}, "log-a-snag"), ("a" * 50, {}, "a" * 40),
                               ("ab cd", {"max_len": 3}, "ab"), ("\u00c9t\u00e9 2026", {}, "t-2026")):
            with self.subTest(text=text):
                self.assertEqual(core.slug(text, **kw), want)

    def test_append_jsonl(self):
        tmp = Path(tempfile.mkdtemp(prefix="bp-jsonl-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "a" / "b.jsonl"
        core.append_jsonl(path, {"id": "1", "t": "\u00e9"})
        core.append_jsonl(path, {"id": "2"})
        self.assertEqual(core.read_jsonl(path), [{"id": "1", "t": "\u00e9"}, {"id": "2"}])
        self.assertEqual(path.read_text(encoding="utf-8").count("\n"), 2)

    def test_emit(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            core.emit(mock.Mock(json=True), "task", {"task": "T-x"}, ["T-x: done"])
        self.assertEqual(json.loads(out.getvalue()), {"ok": True, "command": "task", "data": {"task": "T-x"}})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            core.emit(mock.Mock(json=False), "task", {"task": "T-x"}, ["T-x: done", "second"])
        self.assertEqual(out.getvalue(), "T-x: done\nsecond\n")

    def test_git_ls_files(self):
        tmp = Path(tempfile.mkdtemp(prefix="bp-git-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertIsNone(core.git_ls_files(tmp / "missing"))
        try:
            ok = subprocess.run(["git", "init", "-q", str(tmp)], capture_output=True).returncode == 0
        except OSError:
            ok = False
        if not ok:
            self.skipTest("git is not available")
        (tmp / "sub").mkdir()
        (tmp / "sub" / "a.txt").write_text("a", encoding="utf-8")
        (tmp / "skip.log").write_text("x", encoding="utf-8")
        (tmp / ".gitignore").write_text("*.log\n", encoding="utf-8")
        self.assertEqual(sorted(core.git_ls_files(tmp)), [".gitignore", "sub/a.txt"])


# ---------------------------------------------------------------------------------------------------- proofs

CHUNK = 65536


class ProofDigestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bp-proof-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def digest(self, data):
        p = self.tmp / "f"
        p.write_bytes(data)
        return core.proof_digest(p)

    @staticmethod
    def sha(data):
        return "sha256:" + hashlib.sha256(data).hexdigest()

    def test_text_line_endings_are_normalised(self):
        lf = "one\ntwo\n\nth\u00e9\n".encode("utf-8")
        for variant in (lf, lf.replace(b"\n", b"\r\n"), lf.replace(b"\n", b"\r"), b"one\r\ntwo\r\r\nth\xc3\xa9\n"):
            with self.subTest(variant=variant):
                self.assertEqual(self.digest(variant), self.sha(lf))

    def test_binary_and_nul_are_hashed_raw(self):
        for data in (b"\xff\xfe\r\nab", b"text\x00with nul\r\n", b"ok\r\n\xc3"):  # bad utf-8, NUL, truncated char
            with self.subTest(data=data):
                self.assertEqual(self.digest(data), self.sha(data))

    def test_chunk_boundaries(self):
        cases = {
            "CRLF split across chunks": (b"a" * (CHUNK - 1) + b"\r\nb\r", b"a" * (CHUNK - 1) + b"\nb\n"),
            "lone CR at a chunk end": (b"a" * (CHUNK - 1) + b"\rb", b"a" * (CHUNK - 1) + b"\nb"),
            "CR CR LF across chunks": (b"a" * (CHUNK - 1) + b"\r\r\n", b"a" * (CHUNK - 1) + b"\n\n"),
            "two-byte char split": (b"a" * (CHUNK - 1) + "\u00e9\r\n".encode("utf-8"), b"a" * (CHUNK - 1) + "\u00e9\n".encode("utf-8")),
            "NUL in the second chunk": (b"a" * CHUNK + b"\x00\r\n", None),
            "empty": (b"", b""),
        }
        for name, (data, normalised) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.digest(data), self.sha(data if normalised is None else normalised))

    def test_streams_in_chunks(self):
        p = self.tmp / "big"
        p.write_bytes(b"x\r\n" * 100_000)
        reads = []
        real_open = Path.open

        def spy(self, *a, **kw):
            f = real_open(self, *a, **kw)
            real_read = f.read
            f.read = lambda n=-1: (reads.append(n), real_read(n))[1]
            return f

        with mock.patch.object(Path, "open", spy):
            got = core.proof_digest(p)
        self.assertEqual(got, self.sha(b"x\n" * 100_000))
        self.assertTrue(reads and all(0 < n <= CHUNK for n in reads), reads[:5])


class ProofTest(HermeticRoot):
    """status.Hasher, proof_inputs, proof_current (contract 4.5)."""

    def setUp(self):
        super().setUp()
        from bp import status
        self.status = status
        self.plan = {"files": [
            {"path": "a.py", "action": "create", "dependsOn": ["b.py"]},
            {"path": "b.py", "action": "create", "dependsOn": ["c.py", "a.py"]},   # a cycle a -> b -> a
            {"path": "c.py", "action": "create", "dependsOn": ["web-screen", "src/*.ts", "../outside.py"]},
            {"path": "d.py", "action": "create"}], "patterns": []}
        self.touch("a.py", "a\n")
        self.touch("b.py", "b\n")
        self.touch("d.py", "d\n")
        self.touch("x/inst.ts", "i\n")

    def test_hasher_hashes_each_version_of_a_file_once(self):
        h = self.status.Hasher()
        p = self.root / "a.py"
        with mock.patch.object(core, "_file_digests", wraps=core._file_digests) as m:
            d1 = h.digest(p)
            self.assertEqual(h.digest(p), d1)
            self.assertEqual(h.sha1(p), hashlib.sha1(b"a\n").hexdigest())
            self.assertEqual(m.call_count, 1)
            p.write_bytes(b"changed\n")
            self.assertNotEqual(h.digest(p), d1)
            self.assertEqual(m.call_count, 2)
        self.assertEqual(d1, core.proof_digest(self.touch("other", "a\r\n")))

    def test_proof_inputs_closure(self):
        got = self.status.proof_inputs(self.root, self.plan, ["a.py"])
        self.assertEqual(list(got), ["a.py", "b.py"])  # c.py is planned but not on disk; d.py is unrelated
        self.assertEqual(got["b.py"], core.proof_digest(self.root / "b.py"))
        self.touch("c.py", "c\n")
        self.assertEqual(list(self.status.proof_inputs(self.root, self.plan, ["a.py"])), ["a.py", "b.py", "c.py"])
        self.assertEqual(list(self.status.proof_inputs(self.root, self.plan, ["x/inst.ts", "d.py"])), ["d.py", "x/inst.ts"])
        self.assertEqual(self.status.proof_inputs(self.root, self.plan, ["/etc/passwd", "../x", "C:/x"]), {})
        self.touch("app/[id]/page.tsx", "p\n")  # review B1: bracket names are real files
        plan = {"files": [{"path": "app/[id]/page.tsx", "dependsOn": ["src/*.ts", "web-screen"]}]}
        self.assertEqual(list(self.status.proof_inputs(self.root, plan, ["app/[id]/page.tsx"])), ["app/[id]/page.tsx"])

    def test_proof_current(self):
        st = self.status
        proof = {"ok": True, "cmd": "c", "at": "t", "sha": "s", "inputs": st.proof_inputs(self.root, self.plan, ["a.py"])}
        self.assertTrue(st.proof_current(self.root, self.plan, proof, paths=["a.py"]))
        self.assertTrue(st.proof_current(self.root, self.plan, proof))  # without paths: its own closure
        self.touch("b.py", "edited\n")
        self.assertFalse(st.proof_current(self.root, self.plan, proof, paths=["a.py"]), "an edited dependency")
        self.touch("b.py", "b\r\n")
        self.assertTrue(st.proof_current(self.root, self.plan, proof, paths=["a.py"]), "CRLF of the same text")
        plan = copy.deepcopy(self.plan)
        plan["files"][0]["dependsOn"].append("d.py")
        self.assertFalse(st.proof_current(self.root, plan, proof, paths=["a.py"]), "a dependency added")
        self.assertFalse(st.proof_current(self.root, plan, proof), "a dependency added (closure)")
        plan = copy.deepcopy(self.plan)
        plan["files"][0]["dependsOn"] = []
        self.assertFalse(st.proof_current(self.root, plan, proof, paths=["a.py"]), "a dependency removed")
        (self.root / "b.py").unlink()
        self.assertFalse(st.proof_current(self.root, self.plan, proof, paths=["a.py"]), "an input deleted")

    def test_proof_current_fails_closed(self):
        st = self.status
        good = st.proof_inputs(self.root, self.plan, ["a.py"])
        for proof in (None, "x", [], {"ok": False, "inputs": good}, {"ok": 1, "inputs": good}, {"ok": "true", "inputs": good},
                      {"ok": True, "inputs": "x"}, {"ok": True, "inputs": {"a.py": 5}}, {"ok": True, "inputs": {}},
                      {"ok": True}, {"ok": True, "sha": None}, {"ok": True, "sha": 5},
                      {"inputs": good}, {"cmd": "c", "inputs": good}):  # review N4: a file proof needs "ok": true
            with self.subTest(proof=proof):
                self.assertFalse(st.proof_current(self.root, self.plan, proof, paths=["a.py"]))
        self.assertTrue(st.proof_current(self.root, self.plan, {"files": good}), "a feature proof (4.5)")

    def test_1x_proofs(self):
        st = self.status
        self.touch("a.py", "line\n")
        sha_lf = hashlib.sha1(b"line\n").hexdigest()
        self.assertTrue(st.proof_current(self.root, self.plan, {"ok": True, "cmd": "c", "sha": sha_lf}, paths=["a.py"]))
        self.assertFalse(st.proof_current(self.root, self.plan, {"ok": True, "sha": sha_lf}), "needs the path")
        self.touch("a.py", "line\r\n")
        self.assertTrue(st.proof_current(self.root, self.plan, {"ok": True, "sha": sha_lf}, paths=["a.py"]))
        sha_crlf = hashlib.sha1(b"line\r\n").hexdigest()  # what 1.x stored for a CRLF file: stale once (1.5 row h)
        self.assertFalse(st.proof_current(self.root, self.plan, {"ok": True, "sha": sha_crlf}, paths=["a.py"]))


OK_CMD = f'"{S.PY}" -c "pass"'
FAIL_CMD = f'"{S.PY}" -c "import sys; sys.exit(3)"'


class VerifyProofTest(HermeticRoot):
    """verify --run writes 2.0 proofs; an edited dependency makes its dependents stale; verify --stale (4.5, 4.7)."""

    def plan(self, cmd=OK_CMD):
        p = v2_plan()
        for m in p["modules"]:
            m["verify"] = cmd
        return p

    def rows(self):
        r = self.cli("status", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        return {x["path"]: x for x in json.loads(r.stdout)["rows"]}

    def test_proofs_staleness_and_verify_stale(self):
        self.write_plan(self.plan())
        main = self.touch("crates/snag-api/src/main.rs", "fn main() {}\r\n")
        self.touch("crates/snag-api/tests/snags.rs", "#[test] fn t() {}\n")
        self.touch("Cargo.toml", "[workspace]\n")
        r = self.cli("verify", "--run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        state = json.loads((self.root / ".blueprint" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stateVersion"], 2)
        ver = state["files"]["crates/snag-api/tests/snags.rs"]["verified"]
        self.assertEqual(set(ver), {"ok", "cmd", "at", "sha", "inputs"})
        self.assertIs(ver["ok"], True)
        self.assertEqual(list(ver["inputs"]), ["crates/snag-api/src/main.rs", "crates/snag-api/tests/snags.rs"])
        self.assertEqual(ver["inputs"]["crates/snag-api/src/main.rs"], core.proof_digest(main))
        self.assertEqual(state["files"]["crates/snag-api/src/main.rs"]["verified"]["sha"],
                         hashlib.sha1(b"fn main() {}\n").hexdigest(), "sha is the sha1 of the normalised text")
        rows = self.rows()
        self.assertEqual(rows["crates/snag-api/tests/snags.rs"]["status"], "verified")
        self.assertEqual((rows["crates/snag-api/tests/snags.rs"]["features"], rows["Cargo.toml"]["infrastructure"]),
                         (["F-log-snag"], True))
        self.assertEqual({p: r["stale"] for p, r in rows.items()}, {p: False for p in rows})

        self.touch("crates/snag-api/src/main.rs", "fn main() { changed }\n")
        rows = self.rows()
        stale = sorted(p for p, r in rows.items() if r["stale"])
        self.assertEqual(stale, ["crates/snag-api/src/main.rs", "crates/snag-api/tests/snags.rs"])
        self.assertEqual(rows["crates/snag-api/tests/snags.rs"]["status"], "ondisk")
        self.assertEqual(rows["Cargo.toml"]["status"], "verified")
        r = self.cli("status")
        self.assertIn("↻ 2 file(s) need re-verifying (inputs changed): verify --stale --run", r.stdout)
        r = self.cli("verify", "--stale")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("# 2 file(s)", r.stdout)
        self.assertNotIn("cargo metadata", r.stdout)
        r = self.cli("verify", "--stale", "--run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(any(x["stale"] for x in self.rows().values()))
        r = self.cli("verify", "--stale")
        self.assertIn("No verify commands in scope.", r.stdout)

    def test_route_style_file_names_are_verified(self):
        """Review B1: a real file whose name has [ ] ( ) * or ? is an input of its own proof and of its dependents'."""
        names = ["app/[id]/page.tsx", "app/[...slug]/page.tsx", "app/(group)/layout.tsx"]
        if os.name != "nt":  # Windows forbids * and ? in file names
            names += ["lib/a*b.ts", "lib/what?.ts"]
        for version in (1, 2):
            with self.subTest(version=version):
                plan = v1_plan() if version == 1 else v2_plan()
                plan["modules"] = [{"id": "app", "path": "app", "purpose": "routes", "verify": OK_CMD}]
                plan["files"] = [{"path": n, "action": "create", "module": "app", "phase": "P1", "purpose": "route",
                                  "dependsOn": [names[0]] if i else [], **({"infrastructure": True} if version == 2 else {})}
                                 for i, n in enumerate(names)]
                plan["patterns"], plan["owners"] = [], [{"pattern": "*", "owner": plan["roster"][0]["id"]}]
                plan["stores"], plan["resources"] = [], []
                self.write_plan(plan)
                for n in names:
                    self.touch(n, n + "\n")
                r = self.cli("verify", "--run")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                rows = self.rows()
                self.assertEqual({n: (rows[n]["status"], rows[n]["stale"]) for n in names},
                                 {n: ("verified", False) for n in names})
                state = json.loads((self.root / ".blueprint" / "state.json").read_text(encoding="utf-8"))
                self.assertEqual(list(state["files"][names[1]]["verified"]["inputs"]), sorted([names[0], names[1]]))
                self.touch(names[0], "changed\n")
                rows = self.rows()
                self.assertTrue(rows[names[1]]["stale"], "a dependency with brackets is an input")
                (self.root / ".blueprint" / "state.json").unlink()

    def test_a_failing_command_records_its_result(self):
        self.write_plan(self.plan(FAIL_CMD))
        self.touch("Cargo.toml", "[workspace]\n")
        r = self.cli("verify", "--run")
        self.assertEqual(r.returncode, 1)
        ver = json.loads((self.root / ".blueprint" / "state.json").read_text(encoding="utf-8"))["files"]["Cargo.toml"]["verified"]
        self.assertEqual({k: ver[k] for k in ("ok", "sha", "result")}, {"ok": False, "sha": None, "result": "fail"})
        self.assertNotIn("inputs", ver)

    def test_v1_plan_rows_and_no_stale_line(self):
        plan = v1_plan()
        for m in plan["modules"]:
            m["verify"] = OK_CMD
        for f in plan["files"]:
            f.pop("verify", None)
        self.write_plan(plan)
        self.touch("crates/snag-core/src/lib.rs", "x\n")
        self.assertEqual(self.cli("verify", "--run", "--owner", "backend-lead").returncode, 0)
        self.touch("crates/snag-core/src/lib.rs", "y\n")
        rows = self.rows()
        self.assertTrue(rows["crates/snag-core/src/lib.rs"]["stale"])
        self.assertEqual({(tuple(r["features"]), r["infrastructure"]) for r in rows.values()}, {((), False)})
        self.assertNotIn("↻", self.cli("status").stdout, "status output of a version 1 plan stays 1.3.0's (W0 rule)")


# ---------------------------------------------------------------------------------------------------- findings and codes

class FindingsTest(HermeticRoot):
    def setUp(self):
        super().setUp()
        from bp import validate
        self.v = validate

    def load(self, plan):
        self.write_plan(plan)
        return quiet(bplan.load_plan, self.root)[0]

    def test_finding_shape_and_1x_messages(self):
        plan = v2_plan()
        plan["app"]["name"] = ""
        plan["files"][0]["module"] = "nope"
        found = self.v.validate_findings(self.load(plan))
        for f in found:
            self.assertEqual(set(f), {"level", "code", "entry", "message", "next"})
        self.assertEqual([(f["level"], f["code"], f["message"]) for f in found],
                         [("error", "app-name", "app.name is empty"),
                          ("error", "unknown-module", "file Cargo.toml: unknown module 'nope'"),
                          ("error", "missing-verify", "file Cargo.toml: no verify command (set it on the file or its module)")])
        errors, warnings = self.v.validate_plan(self.load(plan))
        self.assertEqual((errors, warnings), ([f["message"] for f in found], []))

    def test_schema_v1_warning(self):
        for name, plan in (("loaded", self.load(v1_plan())), ("raw dict", v1_plan())):
            with self.subTest(plan=name):
                found = self.v.validate_findings(plan)
                first = found[0]
                self.assertEqual((first["level"], first["code"], first["entry"]), ("warning", "schema-v1", "blueprint.json"))
                self.assertTrue(first["message"].startswith("blueprint.json is schema version 1; it still works"))
                self.assertEqual("migrate" in first["message"], registry.command_built("migrate"))  # 1.5 row d (hermetic: no)
                self.assertEqual([f for f in found if f["level"] == "error"], [])
        self.assertEqual(self.v.validate_findings(v2_plan()), [])
        self.assertEqual(self.v.validate_findings(self.load(v2_plan())), [])

    def test_raw_dict_versions(self):
        raw = v1_plan()
        before = copy.deepcopy(raw)
        self.v.validate_plan(raw)
        self.assertEqual(raw, before, "a raw version 1 dict is upgraded as a copy")
        for bad in ("1", 3, None, True):
            with self.subTest(version=bad):
                raw = v1_plan()
                raw["version"] = bad
                errs = [f for f in self.v.validate_findings(raw) if f["level"] == "error"]
                self.assertEqual((errs[0]["message"], errs[0]["code"]), ("version must be 1 or 2", "version"))

    def test_windows_alias_paths(self):
        """file-path-alias errors (version 2 only): names Windows cannot store as written, or stores as another name.
        Every message says what Windows really does (review of #16, N4)."""
        cases = {
            "apps/web/y.ts.": "Windows drops the trailing dots and spaces, so it names the same file as 'apps/web/y.ts'",
            "apps/web/y2.ts ..": "Windows drops the trailing dots and spaces, so it names the same file as 'apps/web/y2.ts'",
            "apps/web../a.ts": "the folder name 'web..' ends in a dot or a space, which Windows cannot store as written",
            "apps/web /b.ts": "the folder name 'web ' ends in a dot or a space, which Windows cannot store as written",
            "apps/z.ts::$DATA": "':' in 'z.ts::$DATA' starts a stream name on Windows",
            "src:x/a.ts": "':' in 'src:x' starts a stream name on Windows",
        }
        plan = v2_plan()
        for path in cases:
            plan["files"].append(dict(plan["files"][1], path=path, dependsOn=[]))
        found = [f for f in self.v.validate_findings(self.load(plan)) if f["code"] == "file-path-alias"]
        self.assertEqual([(f["level"], f["entry"]) for f in found], [("error", f"file {p}") for p in cases])
        for f, (path, why) in zip(found, cases.items()):
            with self.subTest(path=path):
                self.assertEqual(f["message"], f"file {path!r}: not a portable name on Windows: {why}")
                self.assertEqual(self.v.classify_error(f["message"]), "file-path-alias")
        self.assertEqual([f["code"] for f in self.v.validate_findings(self.load(v2_plan()))], [])
        fine = v2_plan()  # names close to the rules that are fine
        for path in ("src/console.ts", "src/com10.ts", "src/.env", "src/a.b.c", "src/com.ts", "src/nul1.ts"):
            fine["files"].append(dict(fine["files"][1], path=path, dependsOn=[]))
        self.assertEqual([f["message"] for f in self.v.validate_findings(self.load(fine))], [])
        v1 = v1_plan()
        v1["files"].append(dict(v1["files"][1], path="apps/web/y.ts.", dependsOn=[]))
        v1["files"].append(dict(v1["files"][1], path="src/con.ts", dependsOn=[]))
        self.assertNotIn("file-path-alias", [f["code"] for f in self.v.validate_findings(self.load(v1))],
                         "a version 1 plan validates as in 1.3.0")

    def test_device_names_and_case_twins_are_warnings(self):
        """file-path-alias warnings (version 2 only, never blocking; review of #16, R2-2): device names and case-only
        twins are legitimate on a Linux-only project, but such a plan cannot be checked out on Windows (or macOS)."""
        plan = v2_plan()
        first = plan["files"][1]["path"]
        twin = first.upper()
        for path in (twin, "src/con.ts", "lpt9/a.ts", "src/Aux"):
            plan["files"].append(dict(plan["files"][1], path=path, dependsOn=[]))
        plan["patterns"].append({"id": "pat-alias", "glob": "src/aux/*.ts", "module": plan["modules"][0]["id"],
                                 "phase": plan["phases"][0]["id"], "purpose": "p", "instance": "one file", "verify": "true"})
        found = [f for f in self.v.validate_findings(self.load(plan)) if f["code"] == "file-path-alias"]
        dev = "the plan cannot be checked out on Windows as written: rename it"
        self.assertEqual([(f["level"], f["entry"], f["message"]) for f in found], [
            ("warning", f"file {twin}", f"file {twin!r}: differs from file {first!r} only by case; the plan cannot be "
                                        "checked out on Windows or macOS as written: rename one of them"),
            ("warning", "file src/con.ts", f"file 'src/con.ts': 'con.ts' is the Windows device name CON; {dev}"),
            ("warning", "file lpt9/a.ts", f"file 'lpt9/a.ts': 'lpt9' is the Windows device name LPT9; {dev}"),
            ("warning", "file src/Aux", f"file 'src/Aux': 'Aux' is the Windows device name AUX; {dev}"),
            ("warning", "pattern pat-alias", f"pattern 'pat-alias': 'aux' is the Windows device name AUX; {dev}"),
        ])
        for f in found:
            self.assertEqual(self.v.classify_error(f["message"]), "file-path-alias")

    def test_case_twins_are_simple_upper_case_pairs(self):
        """NTFS folds case one character at a time (simple upper case): A/a pair, but ß/ss and the Kelvin sign do not
        (review of #16, R2-1)."""
        plan = v2_plan()
        for path in ("src/A.ts", "src/a.ts", "src/straße.ts", "src/strasse.ts", "src/K.ts", "src/K.ts"):
            plan["files"].append(dict(plan["files"][1], path=path, dependsOn=[]))
        found = [f for f in self.v.validate_findings(self.load(plan)) if f["code"] == "file-path-alias"]
        self.assertEqual([f["entry"] for f in found], ["file src/a.ts"])

    def test_bad_id_is_registered_once_in_the_core(self):
        """Contract 7.4: bad-id is a cross-section code; the core registers it, domain modules only emit it."""
        self.assertIn((r"(?s)^[a-z ]+ id '.*' has the wrong form", "bad-id"), self.v.CODES)
        self.assertEqual(self.v.classify_error("task id 'T-a\nb' has the wrong form; use T-"), "bad-id")  # multi-line id
        self.assertIn("bad-id", self.v.CODE_EXAMPLES)
        for msg in ("milestone id 'beta' has the wrong form; use M then 1 to 3 digits",
                    "feature id 'Inbox' has the wrong form; use F- then lowercase letters",
                    "criterion id 'F-x/c1' has the wrong form; use C then 1 to 3 digits"):
            self.assertEqual(self.v.classify_error(msg), "bad-id")
        from test_registry_hooks import extension as ext
        with ext({"roadmap.py": "CODES = [(r'^[a-z ]+ id .* has the wrong form', 'bad-id')]\n"}):
            with self.assertRaisesRegex(RuntimeError, "bad-id.*roadmap.*validate|bad-id.*validate.*roadmap"):
                self.v.codes()
            with self.assertRaises(RuntimeError):
                self.v.classify_error("task id 'x' has the wrong form")

    def test_module_examples_are_checked_against_the_core_codes(self):
        """Review of #15, M2: a module's CODE_EXAMPLES message that a core regex claims first is refused, naming both
        owners (here a stand-in roadmap example that bad-id would silently take)."""
        from test_registry_hooks import extension as ext
        src = ("CODES = [(r'is late$', 'task-late')]\n"
               "CODE_EXAMPLES = {'task-late': \"task id 'T-a' has the wrong form and is late\"}\n")
        with ext({"roadmap.py": src}):
            with self.assertRaisesRegex(RuntimeError, "validate.*roadmap|roadmap.*validate"):
                self.v.codes()
        with ext({"roadmap.py": "CODES = [(r'^task \\S+ is late', 'task-late')]\n"
                                "CODE_EXAMPLES = {'task-late': 'task T-a is late'}\n"}):
            self.assertIn((r"^task \S+ is late", "task-late"), self.v.codes())

    def test_the_core_examples_are_checked(self):  # mutation m19: the core example check switched off
        with mock.patch.dict(self.v.CODE_EXAMPLES, {"bad-id": "nothing like an id error"}):
            with self.assertRaisesRegex(RuntimeError, "core example"):
                self.v.codes()

    def test_codes_lists(self):
        from bp import data, rules
        self.assertEqual(data.CODES + rules.CODES, frozen.VALIDATION_CODES)
        self.assertEqual(self.v.VALIDATION_CODES[0], (r"^blueprint\.json is schema version 1\b", "schema-v1"))
        self.assertEqual(self.v.VALIDATION_CODES[-len(frozen.VALIDATION_CODES):], frozen.VALIDATION_CODES)
        self.assertEqual(self.v.classify_error("blueprint.json is schema version 1; it still works."), "schema-v1")
        self.assertEqual(self.v.classify_error("resource r: owner 'y' is not in the roster"), "resource-invalid")

    def test_codes_classify(self):
        """Every finding the fixtures produce carries the code its message classifies as (contract 7.4)."""
        import test_plan_fuzz as tf
        plans = [v1_plan(), v2_plan()]
        for base in (v1_plan(), v2_plan()):
            for path in tf.mutation_paths(base):
                for value in ("", None, "nope", []):
                    plan = copy.deepcopy(base)
                    tf.apply(plan, path, value)
                    plans.append(plan)
        seen = set()
        for plan in plans:
            self.write_plan(plan)
            try:
                loaded = quiet(bplan.load_plan, self.root)[0]
            except SystemExit:
                continue
            for f in self.v.validate_findings(loaded):
                seen.add(f["code"])
                self.assertEqual(self.v.classify_error(f["message"]), f["code"], f["message"])
                if f["level"] == "error":
                    self.assertNotEqual(f["code"], "other", f["message"])
        self.assertGreater(len(seen), 10)

    def test_record_friction_takes_findings_or_messages(self):
        recs = []
        with mock.patch.object(self.v, "store_append", lambda name, rec: recs.append((name, rec))):
            self.v.record_friction(self.root, [{"level": "error", "code": "x-code", "message": "m"}, "app.name is empty"])
        self.assertEqual(recs[0][0], "friction.jsonl")
        self.assertEqual(recs[0][1]["codes"], {"x-code": 1, "app-name": 1})

    def test_validate_json_and_counts(self):
        self.write_plan(v2_plan())
        r = self.cli("validate")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OK: 5 files, 0 patterns, 3 modules, 2 phases, 5 owners, 2 features, 1 milestones, 1 tasks, "
                      "1 unknowns. 0 warning(s).", r.stdout)
        r = self.cli("validate", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out["ok"], out["command"], out["data"]["valid"], out["data"]["errors"], out["data"]["warnings"]),
                         (True, "validate", True, [], []))
        self.assertEqual(out["data"]["counts"], {"files": 5, "patterns": 0, "modules": 3, "phases": 2, "owners": 3,
                                                 "roster": 5, "resources": 0, "features": 2, "milestones": 1,
                                                 "tasks": 1, "unknowns": 1})  # review N9: owners = owner rules
        plan = v1_plan()
        plan["app"]["name"] = ""
        self.write_plan(plan)
        r = self.cli("validate", "--json")
        self.assertEqual(r.returncode, 1)
        out = json.loads(r.stdout)
        self.assertEqual((out["ok"], out["data"]["valid"]), (True, False))  # 7.1: ok = the command ran; exit 1
        data = out["data"]
        self.assertEqual([f["code"] for f in data["errors"]], ["app-name"])
        self.assertEqual([f["code"] for f in data["warnings"]], ["schema-v1", "other"])  # + the example's data warning
        r = self.cli("validate")
        self.assertIn("warning: blueprint.json is schema version 1", r.stdout)
        self.write_plan(v1_plan())
        r = self.cli("validate")
        self.assertIn("OK: 12 files, 1 patterns, 4 modules, 2 phases, 4 owners, 2 data resources. 2 warning(s).", r.stdout)

    def test_status_and_export_json_gain_the_new_keys(self):
        for plan in (v1_plan(), v2_plan()):
            self.write_plan(plan)
            for args in (["status", "--json"], ["export", "json"]):
                with self.subTest(version=plan["version"], cmd=args):
                    r = self.cli(*args)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    out = json.loads(r.stdout)
                    for k in NEW:
                        self.assertEqual(out[k], [])
                    self.assertIn("rows", out)


# ---------------------------------------------------------------------------------------------------- deep nesting

MARK = '"__DEEP__"'


# ------------------------------------------------------------------------------- values JSON cannot write back

def tree_bytes(root):
    """{relative path: bytes} of every file under .blueprint (what a refused command must leave as it was)."""
    d = root / ".blueprint"
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def _put(path, value):
    """A change to a plan dict: set the value at path (a list of keys and indexes)."""
    def change(plan):
        node = plan
        for k in path[:-1]:
            node = node[k]
        node[path[-1]] = value
    return change


def _key(plan):
    plan["app"]["x\udc80y"] = 1


UNUSABLE = [  # (label, change to the plan dict, the key path the error names, the message's description)
    ("lone surrogate", _put(["app", "name"], "Snag \ud800 Desk"), "app.name",
     "an unpaired surrogate (\\ud800), which is not valid Unicode"),
    ("low surrogate in a list", _put(["files", 1, "dependsOn"], ["\udfff"]), "files[1].dependsOn[0]",
     "an unpaired surrogate (\\udfff), which is not valid Unicode"),
    ("surrogate in a key", _key, "the key app.x\\udc80y", "an unpaired surrogate (\\udc80), which is not valid Unicode"),
    ("NaN", _put(["app", "extra"], [1, {"n": float("nan")}]), "app.extra[1].n",
     "a number that is not finite (NaN), which JSON does not have"),
    ("Infinity", _put(["app", "extra"], float("inf")), "app.extra",
     "a number that is not finite (Infinity), which JSON does not have"),
    ("-Infinity", _put(["app", "extra"], float("-inf")), "app.extra",
     "a number that is not finite (-Infinity), which JSON does not have"),
    ("1e400", _put(["app", "extra"], "@BIG@"), "app.extra",
     "a number that is not finite (Infinity), which JSON does not have"),
]


def unusable_text(version, change):
    """The plan text with the change: json.dumps writes a lone surrogate as an escape and NaN/Infinity as such."""
    plan = v1_plan() if version == 1 else v2_plan()
    change(plan)
    return json.dumps(plan, indent=2).replace('"@BIG@"', "1e400")


class UnusableJsonTest(TempRoot):
    """A plan holding a value JSON cannot write back (a lone surrogate, NaN, Infinity, or a number too large) is the
    bad-json load error naming its key path, for every command and both schema versions, migrate included; in
    state.json it is bad-state. Nothing is written."""

    COMMANDS = [(["validate"], 1), (["validate", "--json"], 1), (["status"], 1), (["status", "--json"], 1),
                (["status", "--next"], 1), (["sync"], 1), (["render"], 1), (["render", "--force"], 1),
                (["export", "json"], 1), (["export", "csv"], 1), (["claim", "Cargo.toml", "--by", "cto-architect"], 1),
                (["mark", "Cargo.toml", "built", "--by", "cto-architect"], 1), (["verify"], 1), (["migrate"], 1),
                (["migrate", "--dry-run"], 1), (["guard", "Cargo.toml"], 2)]

    def test_every_command_refuses_the_plan(self):
        for version in (1, 2):
            for label, put, where, what in UNUSABLE:
                text = unusable_text(version, put)
                (self.root / ".blueprint" / "blueprint.json").write_text(text, encoding="utf-8")
                before = tree_bytes(self.root)
                message = f"blueprint.json cannot be used: {where} holds {what}"
                for argv, code in self.COMMANDS:
                    with self.subTest(version=version, value=label, cmd=" ".join(argv)):
                        r = self.cli(*argv)
                        self.assertEqual(r.returncode, code, r.stdout + r.stderr)
                        self.assertNotIn("Traceback", r.stderr)
                        if "--json" in argv:
                            env = json.loads(r.stdout)
                            self.assertEqual((env["ok"], env["error"]["code"], env["error"]["message"]),
                                             (False, "bad-json", message))
                        else:
                            self.assertIn(message, r.stderr)
                        self.assertEqual(tree_bytes(self.root), before, "nothing is written")

    def test_load_raw_plan_is_the_one_place(self):
        from bp import plan as P
        text = unusable_text(2, UNUSABLE[0][1])
        (self.root / ".blueprint" / "blueprint.json").write_text(text, encoding="utf-8")
        for fn in (P.load_raw_plan, P.load_plan):
            with self.subTest(fn=fn.__name__):
                with self.assertRaises(SystemExit) as cm:
                    quiet(fn, self.root)
                self.assertEqual((cm.exception.code, cm.exception.error), (1, "bad-json"))

    def test_hook_pre_fails_closed(self):
        (self.root / ".blueprint" / "blueprint.json").write_text(unusable_text(2, UNUSABLE[3][1]), encoding="utf-8")
        payload = json.dumps({"cwd": str(self.root), "tool_input": {"file_path": str(self.root / "Cargo.toml")}})
        r = S.run_script(self.launcher(), ["hook", "pre"], self.root, stdin=payload, env=S.tool_env())
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("blueprint.json cannot be used: app.extra[1].n holds a number that is not finite (NaN)", r.stderr)

    def test_the_same_values_in_state_are_bad_state(self):
        self.write_plan(v2_plan())
        cases = [('{"files": {"Cargo.toml": {"state": "built", "note": "a\\ud800"}}}',
                  'files["Cargo.toml"].note', "an unpaired surrogate (\\ud800), which is not valid Unicode"),
                 ('{"files": {}, "syncedAt": NaN}', "syncedAt", "a number that is not finite (NaN), which JSON does not have"),
                 ('{"files": {}, "x": [1e999]}', "x[0]", "a number that is not finite (Infinity), which JSON does not have")]
        for text, where, what in cases:
            (self.root / ".blueprint" / "state.json").write_text(text, encoding="utf-8")
            self.touch("Cargo.toml")
            before = tree_bytes(self.root)
            for argv in (["status"], ["status", "--json"], ["sync"], ["mark", "Cargo.toml", "built", "--by", "cto-architect"]):
                with self.subTest(state=text, cmd=" ".join(argv)):
                    r = self.cli(*argv)
                    self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                    self.assertNotIn("Traceback", r.stderr)
                    shown = json.loads(r.stdout)["error"] if "--json" in argv else None
                    msg = f".blueprint/state.json is damaged: {where} holds {what}"
                    if shown:
                        self.assertEqual((shown["code"], shown["message"]), ("bad-state", msg))
                    else:
                        self.assertIn(msg, r.stderr)
                    # state.json and every other file are left as they are; taking the lock may add state.lock and
                    # its .gitignore lines (core.state_lock), which are not project content
                    lock = {"state.lock", ".gitignore"}
                    self.assertEqual({k: v for k, v in tree_bytes(self.root).items() if k not in lock},
                                     {k: v for k, v in before.items() if k not in lock}, "state.json is left as it is")

    def test_a_clean_plan_is_unchanged(self):  # the check costs nothing to a plan without such values
        self.write_plan(v2_plan())
        self.assertEqual(self.cli("validate").returncode, 0)
        plan = v2_plan()
        plan["app"]["name"] = "Snag \U0001F600 Desk \u2028"  # a real astral character and a separator are fine
        plan["app"]["extra"] = [1.5, -0.0, 1e308, 10 ** 30]
        self.write_plan(plan)
        self.assertEqual(self.cli("validate").returncode, 0)


class LoadNextTest(TempRoot):
    """Item 2: the load errors' and the schema-v1 warning's next run as printed. The schema-v1 finding is data
    (validate --json), so it holds nothing machine-specific: the copy's form when the copy has migrate, else the
    portable <skill> form."""

    def hint(self, args):
        return f'python "{S.SCRIPTS / "blueprint.py"}" {args} --root "{self.root.resolve()}"'

    def copy(self, *modules):
        bin_ = self.root / ".blueprint" / "bin"
        (bin_ / "bp" / "commands").mkdir(parents=True, exist_ok=True)
        (bin_ / "blueprint.py").write_text("# copy\n", encoding="utf-8")
        for m in modules:
            (bin_ / "bp" / (m + ".py")).write_text("# copy\n", encoding="utf-8")

    def test_load_errors(self):
        r = self.cli("status", "--json")
        self.assertEqual(json.loads(r.stdout)["error"]["next"], self.hint('init --name "App name"'))
        (self.root / ".blueprint" / "blueprint.json").write_text("{", encoding="utf-8")
        r = self.cli("status", "--json")
        self.assertEqual(json.loads(r.stdout)["error"]["next"], f"fix .blueprint/blueprint.json, then run: {self.hint('validate')}")
        self.write_plan({"version": 2, "files": 5})
        r = self.cli("status", "--json")
        self.assertEqual(json.loads(r.stdout)["error"]["code"], "bad-shape")
        self.assertEqual(json.loads(r.stdout)["error"]["next"], f"fix .blueprint/blueprint.json, then run: {self.hint('validate')}")
        self.copy()
        r = self.cli("status", "--json")
        self.assertEqual(json.loads(r.stdout)["error"]["next"],
                         "fix .blueprint/blueprint.json, then run: python .blueprint/bin/blueprint.py validate")

    def test_schema_v1_warning(self):
        self.write_plan(v1_plan())
        first = json.loads(self.cli("validate", "--json").stdout)["data"]["warnings"][0]
        self.assertEqual((first["code"], first["next"]), ("schema-v1", self.hint("migrate")))  # F3, F10: runnable
        self.copy("commands/migrate")
        first = json.loads(self.cli("validate", "--json").stdout)["data"]["warnings"][0]
        self.assertEqual(first["next"], "python .blueprint/bin/blueprint.py migrate")


# ---------------------------------------------------------------------------------- next texts that run as printed

PORTABLE_NOTE = " (run from the project folder)"
# folder names a shell would run or expand when pasted inside double quotes (bash, PowerShell, cmd)
HOSTILE_DIRS = ["a$(touch PWNED)b", "a`touch PWNED`b", "a%PATH%b", "a!b", "a&b", "a;b", "a'b", "a$HOME", "a^b"]
if os.name != "nt":
    HOSTILE_DIRS += ['a"b', "a\\"]
SAFE_DIRS = ["plain", "with space", "p(1)[2]", "a+b,c@d=e", "été 日本", "x-y_z.w",
             "RUNNER~1", "~start", "~user x"]  # ~ is literal inside double quotes in all three shells (CI: 8.3 names)


class RunHintTest(unittest.TestCase):
    """Review of #17, F8: run_hint never prints a command that runs something else when pasted. A path goes inside the
    command only when every character is literal inside double quotes in bash, PowerShell and cmd (letters and
    digits, non-ASCII ones too, space and - _ . / \\ : + , @ = ( ) [ ], not ending in a backslash). Otherwise: the copy
    form when the project's copy has the command, else the portable form with the <skill> placeholder."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="bp-hint-"))
        self.addCleanup(shutil.rmtree, self.base, True)

    def project(self, name, plan=True):
        root = self.base / name
        (root / ".blueprint").mkdir(parents=True, exist_ok=True)
        if plan:
            (root / ".blueprint" / "blueprint.json").write_text(json.dumps(v2_plan(), indent=2), encoding="utf-8")
        return root

    def test_the_rule(self):
        for name in SAFE_DIRS:
            with self.subTest(name=name):
                root = self.project(name)
                self.assertEqual(core.run_hint(root, "status"), f'python "{core.TOOL_HOME / "blueprint.py"}" status --root "{root}"')
        for name in HOSTILE_DIRS:
            with self.subTest(name=name):
                root = self.project(name)
                hint = core.run_hint(root, "status")
                self.assertEqual(hint, "python <skill>/scripts/blueprint.py status --root ." + PORTABLE_NOTE)
                bin_ = root / ".blueprint" / "bin"
                (bin_ / "bp").mkdir(parents=True)
                (bin_ / "blueprint.py").write_text("# copy\n", encoding="utf-8")
                self.assertEqual(core.run_hint(root, "status"), "python .blueprint/bin/blueprint.py status")
                self.assertEqual(core.run_hint(root, "migrate", "commands/migrate"),
                                 "python <skill>/scripts/blueprint.py migrate --root ." + PORTABLE_NOTE)
        root = self.project("plain2")
        with mock.patch.object(core, "TOOL_HOME", Path(str(self.base / "tool$(x)"))):
            self.assertEqual(core.run_hint(root, "status"), "python <skill>/scripts/blueprint.py status --root ." + PORTABLE_NOTE)
        with mock.patch.object(core, "TOOL_HOME", Path(str(self.base / "tool"))):  # use_copy=False: never the copy
            bin_ = root / ".blueprint" / "bin"
            (bin_ / "bp").mkdir(parents=True)
            (bin_ / "blueprint.py").write_text("# copy\n", encoding="utf-8")
            self.assertEqual(core.run_hint(root, "init --refresh-tool", use_copy=False),
                             f'python "{self.base / "tool" / "blueprint.py"}" init --refresh-tool --root "{root}"')

    def shells(self):
        """(name, how to run a pasted line) for the shells of this OS: the line is written to a script file, as pasting
        it into an interactive prompt would run it."""
        out = []
        if os.name == "nt":
            if shutil.which("powershell"):
                out.append(("powershell", ".ps1", lambda f: ["powershell", "-NoProfile", "-NonInteractive",
                                                             "-ExecutionPolicy", "Bypass", "-File", str(f)]))
            out.append(("cmd", ".cmd", lambda f: ["cmd", "/d", "/c", str(f)]))
        elif shutil.which("bash"):
            out.append(("bash", ".sh", lambda f: ["bash", "--norc", str(f)]))
        return out

    def paste(self, line, cwd, suffix, argv):
        script = self.base / ("pasted" + suffix)
        text = ("@echo off\r\nchcp 65001 >nul\r\n" + line + "\r\n") if suffix == ".cmd" else line + "\n"
        script.write_text(text, encoding="utf-8-sig" if suffix == ".ps1" else "utf-8")
        env = S.tool_env(PYTHONIOENCODING="utf-8")
        exe = os.path.dirname(sys.executable)
        env["PATH"] = exe + os.pathsep + env.get("PATH", "")
        if os.name != "nt":  # the printed command says `python`; a system with only python3 gets a shim, as a user would
            shim = self.base / "shim"
            if not (shim / "python").exists():
                shim.mkdir(exist_ok=True)
                os.symlink(sys.executable, shim / "python")
            env["PATH"] = str(shim) + os.pathsep + env["PATH"]
        if os.name == "nt" and not (Path(exe) / "python.exe").is_file():
            self.skipTest("no python.exe on PATH for the shells")
        return subprocess.run(argv(script), cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=env, timeout=120)

    def test_pasted_commands_run_and_run_nothing_else(self):
        shells = self.shells()
        if not shells:
            self.skipTest("no shell to paste into")
        if os.name == "nt" and not shutil.which("python"):
            self.skipTest("python is not on PATH")
        for name in SAFE_DIRS:  # the path form, pasted: it runs status on that project
            root = self.project(name)
            line = core.run_hint(root, "status")
            for shell, suffix, argv in shells:
                with self.subTest(name=name, shell=shell):
                    r = self.paste(line, self.base, suffix, argv)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertIn("Snag Desk: 5 planned files", r.stdout)
        scripts_dir = Path(os.path.abspath(os.sep)) / "scripts"
        had_scripts = scripts_dir.exists()
        for name in HOSTILE_DIRS:  # no copy: the placeholder form. It is not meant to run; pasted, it runs nothing
            root = self.project(name)
            line = core.run_hint(root, "status")
            self.assertEqual(line, "python <skill>/scripts/blueprint.py status --root ." + PORTABLE_NOTE)
            for shell, suffix, argv in shells:
                with self.subTest(name=name, shell=shell, form="placeholder"):
                    empty = Path(tempfile.mkdtemp(prefix="paste-", dir=self.base))
                    r = self.paste(line, empty, suffix, argv)
                    self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertEqual([x.name for x in empty.iterdir()], [], "nothing is created")
                    self.assertEqual(scripts_dir.exists(), had_scripts, "the > in <skill> creates nothing")
        for name in HOSTILE_DIRS:  # no path printed; the copy form, pasted in the project, runs the copy only
            root = self.project(name)
            r = S.run_script(S.LAUNCHER, ["init", "--refresh-tool", "--root", root], root)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            line = core.run_hint(root, "status")
            self.assertEqual(line, "python .blueprint/bin/blueprint.py status")
            for shell, suffix, argv in shells:
                with self.subTest(name=name, shell=shell):
                    r = self.paste(line, root, suffix, argv)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertIn("Snag Desk: 5 planned files", r.stdout)
        self.assertEqual([p for p in self.base.rglob("PWNED*")], [])


class SchemaV1ConsoleTest(TempRoot):
    def test_the_console_warning_runs_as_printed(self):
        """F10: the console schema-v1 warning names migrate in the runnable form, like the finding's next (F3: printed
        text may carry real paths; one form for the console and the envelope)."""
        self.write_plan(v1_plan())
        hint = f'python "{S.SCRIPTS / "blueprint.py"}" migrate --root "{self.root.resolve()}"'
        r = self.cli("validate")
        self.assertIn("warning: blueprint.json is schema version 1; it still works. To use milestones, features, tasks "
                      f"and unknowns, run: {hint}", r.stdout)
        first = json.loads(self.cli("validate", "--json").stdout)["data"]["warnings"][0]
        self.assertEqual(first["next"], hint)
        self.assertIn(f"run: {hint}", first["message"])


class WorkedByTest(TempRoot):
    """state.files[p].workedBy: every distinct id that ever claimed or marked the entry, in first-seen order. It only
    grows (status.set_file_state, the one writer); verify and sync never add to it; an older state reads as [by]."""

    PATH = "apps/web/src/Inbox.tsx"
    OWNER = "frontend-engineer"

    def setUp(self):
        super().setUp()
        self.write_plan(v2_plan())
        self.touch(self.PATH)

    def state(self):
        return json.loads((self.root / ".blueprint" / "state.json").read_text(encoding="utf-8"))

    def run_ok(self, *argv):
        r = self.cli(*argv)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def test_it_grows_and_never_shrinks(self):
        p, o = self.PATH, self.OWNER
        self.run_ok("claim", p, "--by", o)
        self.run_ok("mark", p, "built", "--by", o)
        self.run_ok("claim", p, "--by", "cto-architect", "--force")
        self.run_ok("mark", p, "built", "--by", o)
        self.run_ok("mark", p, "planned", "--by", "backend-lead", "--force")
        self.assertEqual(self.state()["files"][p]["workedBy"], [o, "cto-architect", "backend-lead"])
        self.assertEqual(self.state()["files"][p]["by"], "backend-lead")
        self.run_ok("sync")
        plan = v2_plan()
        for m in plan["modules"]:
            m["verify"] = f'"{sys.executable}" -c pass'
        for f in plan["files"]:
            f.pop("verify", None)
        self.write_plan(plan)
        self.run_ok("mark", p, "built", "--by", o)
        self.run_ok("verify", "--run")
        self.assertEqual(self.state()["files"][p]["workedBy"], [o, "cto-architect", "backend-lead"])

    def test_an_older_state_reads_as_its_last_by(self):
        from bp import status
        self.assertEqual(status.worked_by({"state": "claimed", "by": "x"}), ["x"])
        self.assertEqual(status.worked_by({"state": "planned"}), [])
        self.assertEqual(status.worked_by({"by": "x", "workedBy": ["a", "x"]}), ["a", "x"])
        self.assertEqual(status.worked_by(None), [])
        (self.root / ".blueprint" / "state.json").write_text(json.dumps(
            {"files": {self.PATH: {"state": "claimed", "by": "old-id", "at": "2026-01-01T00:00:00Z"}}}), encoding="utf-8")
        self.run_ok("mark", self.PATH, "built", "--by", self.OWNER)
        self.assertEqual(self.state()["files"][self.PATH]["workedBy"], ["old-id", self.OWNER])

    def test_a_damaged_list_is_bad_state(self):
        for bad in ("x", [1], {"a": 1}, ["ok", None]):
            with self.subTest(workedBy=bad):
                text = json.dumps({"files": {self.PATH: {"state": "claimed", "by": "a", "workedBy": bad}}})
                (self.root / ".blueprint" / "state.json").write_text(text, encoding="utf-8")
                for argv in (["status"], ["sync"], ["claim", self.PATH, "--by", self.OWNER]):
                    r = self.cli(*argv)
                    self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                    self.assertIn(f'.blueprint/state.json is damaged: files["{self.PATH}"].workedBy must be a list of ids',
                                  r.stderr)
                    self.assertEqual((self.root / ".blueprint" / "state.json").read_text(encoding="utf-8"), text)

    def test_a_by_an_older_copy_wrote_joins_the_list(self):
        """An older copy of the tool (a project copy not refreshed yet) sets by without growing workedBy: worked_by is
        the list, then by when it is not in it already; a missing or null by adds nothing."""
        from bp import status
        self.assertEqual(status.worked_by({"by": "newer", "workedBy": ["a", "b"]}), ["a", "b", "newer"])
        self.assertEqual(status.worked_by({"by": "a", "workedBy": ["a", "b"]}), ["a", "b"])
        self.assertEqual(status.worked_by({"by": None, "workedBy": ["a"]}), ["a"])
        self.assertEqual(status.worked_by({"workedBy": []}), [])
        (self.root / ".blueprint" / "state.json").write_text(json.dumps(
            {"files": {self.PATH: {"state": "built", "by": "old-copy", "workedBy": [self.OWNER]}}}), encoding="utf-8")
        self.run_ok("mark", self.PATH, "built", "--by", "cto-architect", "--force")
        self.assertEqual(self.state()["files"][self.PATH]["workedBy"], [self.OWNER, "old-copy", "cto-architect"])

    def test_a_damaged_raw_entry_is_never_nobody(self):
        """worked_by on a raw entry whose workedBy is damaged raises bad-state, like the state reader: it never answers
        an empty list, which a caller would read as nobody."""
        from bp import status
        from bp.core import BlueprintError
        for bad in ("x", [1], {"a": 1}, ["ok", None], 5):
            with self.subTest(workedBy=bad):
                with self.assertRaises(BlueprintError) as cm:
                    status.worked_by({"by": "a", "workedBy": bad})
                self.assertEqual(cm.exception.error, "bad-state")

    def test_parallel_claims_keep_every_id(self):
        ids = [f"agent-{i}" for i in range(6)]
        procs = [subprocess.Popen([S.PY, str(S.LAUNCHER), "claim", self.PATH, "--by", i, "--force", "--root", str(self.root)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=S.tool_env()) for i in ids]
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0, p.stderr.read())
        self.assertEqual(sorted(self.state()["files"][self.PATH]["workedBy"]), ids)


@unittest.skipUnless(os.name == "nt", "a junction is a Windows link")
class LockKeyJunctionTest(HermeticRoot):
    def test_a_junction_to_the_project_is_the_same_lock(self):
        """The lock is keyed by the project's real path: a junction (made without any privilege) names the same project
        (review of #14; without realpath this test fails on Windows)."""
        from bp import core
        self.write_plan(v2_plan())
        link = Path(tempfile.mkdtemp(prefix="bp-junction-")) / "j"
        self.addCleanup(shutil.rmtree, link.parent, True)
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(self.root)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.addCleanup(lambda: os.path.isdir(link) and os.rmdir(link))
        self.assertNotEqual(os.path.normcase(str(link)), os.path.normcase(str(self.root)))
        with core.state_lock(link):
            self.assertTrue(core._holding(self.root))
            with self.assertRaises(RuntimeError):
                with core.state_lock(self.root):
                    pass


def deep_json(depth):
    return "[" * depth + "1" + "]" * depth


def reader_limit():
    """The deepest list the JSON reader accepts, measured in a fresh process (the limit depends on the version)."""
    code = ("import json\nlo, hi = 50, 200000\nwhile lo < hi:\n    m = (lo + hi + 1) // 2\n    try:\n"
            "        json.loads('[' * m + ']' * m)\n        lo = m\n    except RecursionError:\n        hi = m - 1\nprint(lo)")
    return int(subprocess.run([S.PY, "-c", code], capture_output=True, text=True).stdout)


class DeepNestingTest(unittest.TestCase):
    """Migrate review R2-M3: a plan the JSON reader accepts and 1.3.0 loaded loads in 2.0 (upgrade_plan and
    upgrade_state copy without recursion), with the same STRUCTURE.md and ROSTER.md; a plan nested deeper than any
    step can take is the bad-json load error (exit 1, 2 for guard), never exit 70."""

    LIMIT = None

    @classmethod
    def setUpClass(cls):
        cls.LIMIT = reader_limit()
        cls.base = Path(tempfile.mkdtemp(prefix="bp deep "))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def project(self, tag, where, depth):
        root = self.base / f"{tag}-{where}-{depth}"
        (root / ".blueprint").mkdir(parents=True)
        plan = v1_plan()
        state = {"files": {}, "patterns": {}, "unplanned": [], "syncedAt": None, "head": None}
        if where == "top":
            plan["x"] = "__DEEP__"
        elif where == "file":
            plan["files"][0]["extra"] = "__DEEP__"
        else:
            state["x"] = "__DEEP__"
        (root / ".blueprint" / "blueprint.json").write_text(json.dumps(plan).replace(MARK, deep_json(depth)), encoding="utf-8")
        (root / ".blueprint" / "state.json").write_text(json.dumps(state).replace(MARK, deep_json(depth)), encoding="utf-8")
        return root

    def run_tool(self, tool, args, root):
        return S.run_script(tool, list(args) + ["--root", root], root).returncode

    def test_wherever_1_3_0_loads_2_0_loads(self):
        depths = [100, 300, 500, 900, max(100, self.LIMIT - 300)]
        for where in ("top", "file", "state"):
            for depth in depths:
                with self.subTest(where=where, depth=depth):
                    old, new = self.project("old", where, depth), self.project("new", where, depth)
                    for args in (["validate"], ["status"], ["render"]):
                        before = self.run_tool(S.FROZEN, args, old)
                        if before == 0:
                            self.assertEqual(self.run_tool(S.LAUNCHER, args, new), 0, args)
                    for view in ("STRUCTURE.md", "ROSTER.md"):
                        a, b = old / ".blueprint" / view, new / ".blueprint" / view
                        if a.exists():
                            self.assertEqual(b.read_bytes(), a.read_bytes(), view)

    def test_deeper_than_the_reader_is_a_load_error(self):
        root = self.project("over", "top", self.LIMIT + 500)
        self.assertEqual(self.run_tool(S.LAUNCHER, ["validate"], root), 1)
        self.assertEqual(self.run_tool(S.LAUNCHER, ["guard", "x.txt"], root), 2)
        r = S.run_script(S.LAUNCHER, ["validate", "--json", "--root", root], root)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(json.loads(r.stdout)["error"]["code"], "bad-json")

    def test_doctor_on_a_plan_too_deep_to_read(self):  # review P10-1
        root = self.project("doctor", "top", self.LIMIT + 500)
        r = S.run_script(S.LAUNCHER, ["doctor", "--root", root], root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("blueprint.json schema: unreadable", r.stdout)

    def test_a_deep_version_2_state_is_copied_without_recursion(self):  # review P10-5
        deep = 1
        for _ in range(3000):  # built directly: the 3.11 JSON reader stops near 1000 levels
            deep = [deep]
        st = migrate.upgrade_state({"stateVersion": 2, "files": {}, "x": deep})
        self.assertIsNot(st["x"], deep)
        self.assertEqual(st["tasks"], {})

    def test_copy_json_refuses_a_cycle_and_copies_shared_parts(self):  # review P10-2
        loop = [1]
        loop.append(loop)
        with self.assertRaisesRegex(ValueError, "cyclic value"):
            core.copy_json(loop)
        d = {"a": {"b": [loop]}}
        with self.assertRaisesRegex(ValueError, "cyclic value"):
            core.copy_json(d)
        shared = {"k": [1, 2]}
        out = core.copy_json({"one": shared, "two": [shared, shared]})
        self.assertEqual(out, {"one": shared, "two": [shared, shared]})
        self.assertIsNot(out["one"], out["two"][0])
        self.assertIsNot(out["two"][0], out["two"][1])
        self.assertIsNot(out["one"], shared)

    def test_recursion_in_a_load_step_is_mapped_only_for_a_deep_plan(self):  # review P10-3
        from bp import plan as bplan_mod

        def recurse(plan):
            return recurse(plan)

        shallow = self.project("shallow", "top", 5)
        deep = self.project("deep", "top", 400)
        with mock.patch.object(bplan_mod, "normalize_plan", recurse):
            with self.assertRaises(RecursionError):  # a bug on a shallow plan: an incident, never "nested too deeply"
                quiet(bplan_mod.load_plan, shallow)
            with self.assertRaises(core.BlueprintError) as cm:
                quiet(bplan_mod.load_plan, deep)
        self.assertEqual((cm.exception.error, cm.exception.message),
                         ("bad-json", "blueprint.json is nested too deeply to read"))
        self.assertEqual(core.json_depth(json.loads(deep_json(400))), 400)
        self.assertEqual(core.json_depth({"a": [1, {"b": []}]}), 4)
        self.assertEqual(core.json_depth(5), 0)

    def test_upgrade_is_iterative_and_pure(self):
        deep = json.loads(deep_json(500))
        raw = dict(v1_plan(), x=deep)
        up = migrate.upgrade_plan(raw)
        self.assertEqual(up["version"], 2)
        self.assertIs(raw["x"], deep, "the input is not changed")
        self.assertEqual((raw["version"], "features" in raw), (1, False))
        self.assertIsNot(up["x"], deep, "nothing is shared")
        node_a, node_b = up["x"], deep
        for _ in range(500):
            node_a, node_b = node_a[0], node_b[0]
        self.assertEqual(node_a, node_b)
        st = migrate.upgrade_state({"files": {}, "x": deep})
        self.assertEqual(st["stateVersion"], 2)
        self.assertIsNot(st["x"], deep)
        self.assertEqual(migrate.upgrade_plan(dict(v2_plan(), x=deep))["version"], 2)


if __name__ == "__main__":
    unittest.main()
