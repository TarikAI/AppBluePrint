"""contracts --module limits which module READMEs are listed or written."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import test_blueprint as tb

WARNING = ("Run the project's own tests now: a new README can break tooling that treats every entry in a folder "
           "as an item. Remove the README for that module if it does.")


class ContractsModuleTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bp-contracts-"))
        self.assertEqual(tb.run(["init", "--name", "Contracts"], self.dir).returncode, 0)
        plan = json.loads(tb.EXAMPLE.read_text(encoding="utf-8"))
        (self.dir / ".blueprint" / "blueprint.json").write_text(json.dumps(plan), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_module_limits_the_listing_and_the_write(self):
        r = tb.run(["contracts", "--module", "core"], self.dir)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("would write crates/snag-core/README.md", r.stdout)
        self.assertNotIn("snag-api", r.stdout)
        self.assertNotIn(WARNING, r.stdout)
        r = tb.run(["contracts", "--write", "--module", "core", "--module", "web"], self.dir)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.dir / "crates" / "snag-core" / "README.md").exists())
        self.assertTrue((self.dir / "apps" / "web" / "README.md").exists())
        self.assertFalse((self.dir / "crates" / "snag-api" / "README.md").exists())
        self.assertIn(WARNING, r.stdout)

    def test_unknown_module_is_a_usage_error(self):
        r = tb.run(["contracts", "--module", "nope"], self.dir)
        self.assertEqual(r.returncode, 64, r.stdout + r.stderr)
        self.assertIn("nope", r.stderr)
