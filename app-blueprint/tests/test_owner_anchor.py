"""validate warns when a bare folder-name owner rule matches that folder at more than one depth."""
import importlib.util
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("blueprint_owner_anchor", SKILL / "scripts" / "blueprint.py")
bp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bp)


def plan_with(rule, paths):
    return {"version": 1, "app": {"name": "x"}, "stack": [], "decisions": [], "ignore": [], "approach": {},
            "stores": [], "resources": [], "patterns": [],
            "modules": [{"id": "m", "purpose": "p", "verify": "true"}],
            "phases": [{"id": "P1", "name": "one"}],
            "files": [{"path": p, "action": "create", "module": "m", "phase": "P1", "purpose": "p"} for p in paths],
            "owners": [{"pattern": "*", "owner": "lead"}, {"pattern": rule, "owner": "dev"}],
            "roster": [{"id": "lead", "kind": "agent"}, {"id": "dev", "kind": "agent"}]}


class OwnerAnchorTest(unittest.TestCase):
    PATHS = ["agents/a.md", "skills/demo/agents/openai.yaml", "README.md"]

    def warnings(self, rule, paths=None):
        errors, warnings = bp.validate_plan(plan_with(rule, paths or self.PATHS))
        self.assertEqual(errors, [])
        return [w for w in warnings if "at any depth" in w]

    def test_bare_folder_rule_matching_root_and_nested_warns(self):
        for rule in ("agents/", "agents"):
            w = self.warnings(rule)
            self.assertEqual(len(w), 1, (rule, w))
            self.assertEqual(w[0], f"owner rule 2 ({rule} -> dev) matches that folder name at any depth: it also matches "
                                   "skills/demo/agents/openai.yaml. Write /agents/ for the root folder only.")

    def test_anchored_or_single_location_rules_do_not_warn(self):
        self.assertEqual(self.warnings("/agents/"), [])
        self.assertEqual(self.warnings("agents/", ["skills/demo/agents/openai.yaml", "README.md"]), [])
        self.assertEqual(self.warnings("agents/", ["agents/a.md", "agents/b.md", "README.md"]), [])
        self.assertEqual(self.warnings("*.md"), [])
