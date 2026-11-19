"""W0-A2: the registry and its hooks (contract 1.4) let a later workstream add behaviour without editing any file of
W0-A2. Every test here adds throwaway modules under the names the contract gives the later workstreams (bp/unknowns.py,
bp/features.py, bp/views/roadmap_md.py, bp/commands/migrate.py, assets/tab-*.js, ...) and checks they are picked up.

In process, the modules are written to a temp folder that is appended to the package path (bp.__path__ and its
subpackages'), so `import bp.<name>` finds them exactly as it would a file in scripts/bp, and nothing under scripts/
changes. End to end, a temp copy of the skill gets the files and the real CLI runs from it.
"""
import contextlib
import importlib
import io
import json
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import support as S

import bp
import bp.commands
import bp.views
from bp import plan as bplan, registry, validate

from test_validate import TempRoot, quiet, v1_plan, v2_plan


@contextlib.contextmanager
def extension(files):
    """Make bp.<name> importable for each "rel/path.py": source pair, without touching scripts/bp."""
    tmp = Path(tempfile.mkdtemp(prefix="bp-ext-"))
    names, added = [], []
    try:
        for rel, src in files.items():
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(src), encoding="utf-8")
            names.append("bp." + ".".join(Path(rel).with_suffix("").parts))
        for pkg, folder in ((bp, tmp), (bp.views, tmp / "views"), (bp.commands, tmp / "commands")):
            if folder.is_dir():
                pkg.__path__.append(str(folder))
                added.append((pkg, str(folder)))
        importlib.invalidate_caches()
        yield tmp
    finally:
        for pkg, folder in added:
            pkg.__path__.remove(folder)
        for n in names:
            sys.modules.pop(n, None)
        importlib.invalidate_caches()
        shutil.rmtree(tmp, ignore_errors=True)


class RegistryTest(unittest.TestCase):
    def test_lists_are_the_contracts(self):
        self.assertEqual(registry.DOMAIN, ("rules", "data", "unknowns", "records", "features", "roadmap", "evidence"))
        self.assertEqual(registry.VIEWS, ("views.roadmap_md", "views.features_md"))
        self.assertEqual(set(registry.COMMAND_TABLE), {
            "init", "validate", "render", "contracts", "export", "sync", "status", "owner", "claim", "mark", "verify",
            "guard", "hook", "db-check", "feedback", "report", "doctor", "migrate", "task", "accept", "evidence", "study"})
        self.assertEqual(registry.COMMAND_TABLE["study"], "commands.study")

    def test_modules_in_order_skipping_missing(self):
        self.assertEqual([m.__name__ for m in registry.modules("DOMAIN")], ["bp.rules", "bp.data"])
        self.assertEqual(registry.modules("VIEWS"), [])
        with extension({"roadmap.py": "", "unknowns.py": "", "views/features_md.py": ""}):
            self.assertEqual([m.__name__ for m in registry.modules("DOMAIN")],
                             ["bp.rules", "bp.data", "bp.unknowns", "bp.roadmap"])
            self.assertEqual([m.__name__ for m in registry.modules("VIEWS")], ["bp.views.features_md"])

    def test_a_broken_module_is_a_bug_not_a_missing_one(self):
        with extension({"features.py": "import bp.not_there\n"}):
            with self.assertRaises(ModuleNotFoundError) as cm:
                registry.modules("DOMAIN")
            self.assertEqual(cm.exception.name, "bp.not_there")
        with extension({"records.py": "def broken(:\n"}):
            with self.assertRaises(SyntaxError):
                registry.modules("DOMAIN")

    def test_hook_has_a_single_owner(self):
        with extension({"features.py": "def status_card(ctx, fid):\n    return ['card ' + fid]\n",
                        "records.py": "def verify_feature(args, ctx):\n    return [], 0\n"}):
            self.assertEqual(registry.hook("status_card")({}, "F-x"), ["card F-x"])
            self.assertEqual(registry.hook("verify_feature")(None, {}), ([], 0))
            self.assertIsNone(registry.hook("no_such_hook"))
        with extension({"features.py": "def status_card(ctx, fid):\n    return []\n",
                        "roadmap.py": "def status_card(ctx, fid):\n    return []\n"}):
            with self.assertRaises(RuntimeError):
                registry.hook("status_card")

    def test_collect_in_domain_order(self):
        src = "def status_lines(ctx, args):\n    return [__name__]\n"
        with extension({"roadmap.py": src, "unknowns.py": src, "features.py": src}):
            self.assertEqual(registry.collect("status_lines", {}, None),
                             [("unknowns", ["bp.unknowns"]), ("features", ["bp.features"]), ("roadmap", ["bp.roadmap"])])
        self.assertEqual(registry.collect("status_lines", {}, None), [])

    def test_refusal_first_message_wins(self):
        with extension({"unknowns.py": "def refuse(action, root, plan, path, by):\n"
                                       "    return 'blocked: ' + path if action == 'claim' else None\n",
                        "records.py": "def refuse(action, root, plan, path, by):\n"
                                      "    return 'protected: ' + path if path.startswith('.blueprint/') else None\n"}):
            self.assertEqual(registry.refusal("claim", None, {}, "a.py", "x"), "blocked: a.py")
            self.assertEqual(registry.refusal("write", None, {}, ".blueprint/state.json", "x"), "protected: .blueprint/state.json")
            self.assertIsNone(registry.refusal("write", None, {}, "a.py", "x"))
        self.assertIsNone(registry.refusal("write", None, {}, ".blueprint/state.json", "x"))

    def test_signal_is_optional_and_never_raises(self):
        registry.signal(None, "guard", "guard-unplanned")  # no bp.signals yet: nothing happens
        calls = []
        with extension({"signals.py": "CALLS = []\ndef record(root, signal, code):\n    CALLS.append((root, signal, code))\n"}):
            registry.signal("R", "verify", "verify-fail")
            self.assertEqual(sys.modules["bp.signals"].CALLS, [("R", "verify", "verify-fail")])
        with extension({"signals.py": "def record(root, signal, code):\n    raise OSError('disk full')\n"}):
            registry.signal("R", "guard", "guard-protected")  # capture never breaks a command
        self.assertEqual(calls, [])

    def test_codes_order(self):
        with extension({"roadmap.py": "CODES = [(r'^task \\S+: needs title', 'task-incomplete')]\n",
                        "unknowns.py": "CODES = [(r'^unknown \\S+: needs question', 'unknown-incomplete')]\n"}):
            got = registry.codes()
            from bp import data, rules
            self.assertEqual(got, [(r"^unknown \S+: needs question", "unknown-incomplete"),
                                   (r"^task \S+: needs title", "task-incomplete")] + data.CODES + rules.CODES)
            self.assertEqual(validate.codes()[1:], got)


class ValidationRuleExtensionTest(TempRoot):
    """A later workstream adds a validation rule with a new code (contract 1.4, 7.4) by adding its module."""

    UNKNOWNS = '''
        CODES = [(r"^unknown \\S+: needs question, severity and status", "unknown-incomplete"),
                 (r"^unknown \\S+: severity must be", "unknown-severity")]
        SEEN = []

        def validate(plan, E, W, vctx):
            SEEN.append(dict(vctx))
            for u in plan.get("unknowns", []):
                if u.get("severity") not in ("low", "medium", "high", "critical"):
                    E(f"unknown {u['id']}: severity must be low, medium, high or critical", code="unknown-severity",
                      entry=f"unknown {u['id']}", next="set severity in blueprint.json")
                if not u.get("question"):
                    E(f"unknown {u['id']}: needs question, severity and status")  # no code: classified by CODES
                if u.get("status") == "open" and u.get("severity") == "high":
                    W(f"unknown {u['id']} (high) is open and blocks {', '.join(u.get('blocks', []))}")
    '''

    def test_rule_findings_codes_and_order(self):
        plan = v1_plan()
        plan["unknowns"] = [{"id": "U-a", "question": "", "severity": "huge", "status": "open"},
                            {"id": "U-b", "question": "q", "severity": "high", "status": "open", "blocks": ["P2"]}]
        self.write_plan(plan)
        loaded = quiet(bplan.load_plan, self.root)[0]
        with extension({"unknowns.py": self.UNKNOWNS}):
            found = validate.validate_findings(loaded, team=None, evidence_ids={"ev-1"}, root=self.root)
            mod = sys.modules["bp.unknowns"]
            self.assertEqual(mod.SEEN, [{"team": None, "evidenceIds": {"ev-1"}, "root": self.root}])
            self.assertEqual(validate.classify_error("unknown U-z: severity must be low"), "unknown-severity")
        self.assertEqual(validate.classify_error("unknown U-z: severity must be low"), "other")
        got = [(f["level"], f["code"], f["entry"], f["next"]) for f in found]
        self.assertEqual(got[0][:2], ("warning", "schema-v1"))
        self.assertEqual(got[1][:2], ("warning", "other"))  # the 1.x data warning comes before the new rules
        self.assertEqual(got[2:], [("error", "unknown-severity", "unknown U-a", "set severity in blueprint.json"),
                                   ("error", "unknown-incomplete", None, None),
                                   ("warning", "other", None, None)])
        errors, warnings = None, None
        with extension({"unknowns.py": self.UNKNOWNS}):
            errors, warnings = validate.validate_plan(loaded)
        self.assertEqual(errors, ["unknown U-a: severity must be low, medium, high or critical",
                                  "unknown U-a: needs question, severity and status"])

    def test_team_check_still_runs_last(self):
        plan = v1_plan()
        plan["unknowns"] = [{"id": "U-a", "question": "", "severity": "low", "status": "open"}]
        with extension({"unknowns.py": self.UNKNOWNS}):
            errors, _ = validate.validate_plan(plan, team={"agents": []})
        team = [f"roster {r['id']}: not an agent in team.json" for r in plan["roster"]]
        self.assertEqual(errors, ["unknown U-a: needs question, severity and status"] + team)


if __name__ == "__main__":
    unittest.main()
