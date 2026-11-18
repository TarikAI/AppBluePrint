"""A malformed blueprint.json must produce validation-style errors, never a crash."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import test_blueprint as tb


class ShapeBase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bp-shape-"))
        r = tb.run(["init", "--name", "Shapes"], self.dir)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.path = self.dir / ".blueprint" / "blueprint.json"
        self.plan = json.loads(self.path.read_text(encoding="utf-8"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_with(self, **changes):
        self.plan.update(changes)
        self.path.write_text(json.dumps(self.plan), encoding="utf-8")
        return tb.run(["validate"], self.dir)


class PlanShapeTest(ShapeBase):
    def test_non_object_file_entry_is_a_clear_error(self):
        r = self.run_with(files=["oops"])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("blueprint.json: files[0] must be an object (got string)", r.stdout + r.stderr)
        self.assertIn("Fix it, then run: python .blueprint/bin/blueprint.py validate", r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_every_bad_shape_is_reported_at_once(self):
        r = self.run_with(files=[{"path": "a", "uses": "x"}, 3], modules="nope", ignore=[1], approach=[], app=[],
                          owners=[None])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        out = r.stdout + r.stderr
        for needle in ("files[1] must be an object (got number)", "modules must be a list (got string)",
                       "ignore[0] must be a string (got number)", "approach must be an object (got list)",
                       "app must be an object (got list)", "owners[0] must be an object (got null)",
                       "files[0].uses must be a list (got string)"):
            self.assertIn(needle, out)

    def test_other_commands_also_exit_cleanly(self):
        self.run_with(files=["oops"])
        for cmd in (["sync"], ["status"], ["render"]):
            r = tb.run(cmd, self.dir)
            self.assertEqual(r.returncode, 1, (cmd, r.stdout, r.stderr))
            self.assertIn("files[0] must be an object", r.stdout + r.stderr)


class ScalarShapeTest(ShapeBase):
    def test_scalar_fields_of_the_wrong_type_are_reported_at_once(self):
        self.plan["modules"] = [{"id": ["m1"], "purpose": 5}]
        self.plan["files"] = [{"path": "a", "module": ["m1", "m2"], "phase": {}, "action": 1, "dependsOn": [3],
                               "uses": ["x"], "verify": []}]
        self.plan["owners"] = [{"pattern": 7, "owner": {}}]
        self.plan["stores"] = [{"id": "s", "schema": "one.sql", "kind": 1}]
        self.plan["resources"] = [{"id": "r", "aliases": [1], "definedIn": [], "store": 2, "name": 3}]
        self.plan["patterns"] = [{"id": "p", "glob": ["x"], "examples": "e"}]
        r = self.run_with()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        out = r.stdout + r.stderr
        for needle in ("modules[0].id must be a string (got list)", "modules[0].purpose must be a string (got number)",
                       "files[0].module must be a string (got list)", "files[0].phase must be a string (got object)",
                       "files[0].action must be a string (got number)", "files[0].dependsOn[0] must be a string (got number)",
                       "files[0].uses[0] must be an object (got string)", "files[0].verify must be a string (got list)",
                       "owners[0].pattern must be a string (got number)", "owners[0].owner must be a string (got object)",
                       "stores[0].schema must be a list (got string)", "stores[0].kind must be a string (got number)",
                       "resources[0].aliases[0] must be a string (got number)", "resources[0].definedIn must be a string (got list)",
                       "resources[0].store must be a string (got number)", "patterns[0].glob must be a string (got list)",
                       "patterns[0].examples must be a list (got string)"):
            self.assertIn(needle, out)
        self.assertNotIn("Traceback", r.stderr)

    def test_every_command_exits_1_on_a_bad_scalar(self):
        self.plan["files"] = [{"path": "a", "module": ["m1", "m2"], "phase": {}}]
        self.plan["modules"] = [{"id": ["m"]}]
        self.run_with()
        for cmd in (["validate"], ["sync"], ["status"], ["status", "--next"], ["render"], ["owner", "a"],
                    ["export", "csv"], ["export", "json"], ["export", "codeowners"], ["export", "agenticteam"],
                    ["contracts"], ["guard", "a", "--by", "x"], ["claim", "a", "--by", "x"], ["verify"]):
            r = tb.run(cmd, self.dir)
            self.assertEqual(r.returncode, 1, (cmd, r.stdout, r.stderr))
            self.assertNotIn("Traceback", r.stderr)
