"""A malformed blueprint.json must produce validation-style errors, never a crash."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import test_blueprint as tb


class PlanShapeTest(unittest.TestCase):
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
