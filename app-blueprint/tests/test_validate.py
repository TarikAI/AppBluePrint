"""W0-A2: schema version 2 and the in-memory upgrade (contract 3), state version 2 (4.1), proofs (4.5), findings and
codes (1.4, 7.4), BlueprintError and the output envelope (7.1, 7.3).

Run: python -m unittest discover -s app-blueprint/tests
"""
import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import support as S

from bp import core, migrate, plan as bplan, version

frozen = S.load_frozen("blueprint_frozen_for_validate")

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
    def setUp(self):
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
        return S.run_script(S.LAUNCHER, argv, self.root, stdin=stdin, env=S.tool_env(**env))

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


# ---------------------------------------------------------------------------------------------------- versions

class VersionTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(version.SCHEMA_VERSION, 2)
        self.assertEqual(version.SUPPORTED_SCHEMAS, (1, 2))
        self.assertEqual(version.STATE_VERSION, 2)
        self.assertEqual(version.MIN_SCHEMA2_TOOL, "1.9.1")
        self.assertEqual(version.TOOL_VERSION, "1.9.1")

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

    def test_v2_state_is_kept(self):
        raw = dict(copy.deepcopy(self.V1), stateVersion=2, tasks={"T-x": {"status": "done"}})
        self.assertEqual(migrate.upgrade_state(raw), raw)


# ---------------------------------------------------------------------------------------------------- load_plan

class LoadPlanTest(TempRoot):
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

class StateVersionTest(TempRoot):
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

    def init(self, root, script=S.LAUNCHER):
        root.mkdir(parents=True, exist_ok=True)
        r = S.run_script(script, ["init", "--name", "New", "--root", root], self.base, env=self.env)
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
        r = S.run_script(S.LAUNCHER, ["status", "--root", root], self.base, env=self.env)
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
        r = S.run_script(S.LAUNCHER, ["init", "--refresh-tool", "--root", root], self.base, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Your blueprint is schema version 1. It keeps working.", r.stdout)
        self.assertNotIn("migrate", r.stdout)  # the hint appears once bp/commands/migrate.py exists (W0-B)
        r = S.run_script(S.LAUNCHER, ["status", "--root", root], self.base, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        last = r.stdout.rstrip().splitlines()[-1]
        self.assertTrue(last.startswith("Schema version 1"), last)
        self.assertNotIn("migrate", last)

    def test_doctor_compares_the_copy_with_min_schema2_tool(self):
        root = self.base / "doc"
        self.init(root)
        (root / ".blueprint" / "bin" / "VERSION").write_text("1.9.0\n", encoding="utf-8")
        r = S.run_script(S.LAUNCHER, ["doctor", "--root", root], self.base, env=self.env)
        self.assertIn("blueprint.json schema: 2", r.stdout)
        self.assertIn(f"older than {version.MIN_SCHEMA2_TOOL} and cannot read schema version 2", r.stdout)
        (root / ".blueprint" / "bin" / "VERSION").write_text(version.TOOL_VERSION + "\n", encoding="utf-8")
        r = S.run_script(S.LAUNCHER, ["doctor", "--strict", "--root", root], self.base, env=self.env)
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


class ProofTest(TempRoot):
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


class VerifyProofTest(TempRoot):
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

class FindingsTest(TempRoot):
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
                self.assertNotIn("migrate", first["message"])  # until bp/commands/migrate.py exists (1.5 row d)
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


if __name__ == "__main__":
    unittest.main()
