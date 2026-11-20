"""W1-F: features and criteria (contract 2.5, 2.7 to 2.9, 4.3, 7.4, 8.2 to 8.5).

Validation codes (a bad and a good plan each, and the classify check), `feature_items`, one test per rule of 4.3 with
synthetic `checks` (plus the "not" path of each), the hooks (file_tags, roster_sections, map_payload, status_*), the
exact FEATURES.md of the version 2 example, and the map's Features tab and "by feature" mind map branch (run under node
with a small DOM stand-in when node is installed).
"""
import contextlib
import copy
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import support as S

from bp import features, plan as bplan, registry, status, validate
from bp.views.context import build_context
from bp.views.render import render_all, render_map

from test_registry_hooks import run_cli
from test_validate import TempRoot, extension, quiet, v1_plan, v2_plan



def own_modules():
    """This workstream's real module files, read from disk: dropped into a hermetic package they are the only
    non-core modules, so a test of features does not depend on which sibling modules (roadmap, unknowns, records) exist."""
    return {"features.py": (S.PACKAGE / "features.py").read_text(encoding="utf-8"),
            "views/features_md.py": (S.PACKAGE / "views" / "features_md.py").read_text(encoding="utf-8")}


class Own:
    """Mixin: every test runs in a hermetic package holding the core and the features modules only. Behaviour that
    depends on a sibling module has its own test (SiblingsTest) with both variants stated."""

    def setUp(self):
        super().setUp()
        stack = contextlib.ExitStack()
        stack.enter_context(extension(own_modules()))
        self.addCleanup(stack.close)


FILES = ("crates/snag-api/src/main.rs", "crates/snag-api/tests/snags.rs", "apps/web/src/Inbox.tsx", "apps/web/src/offline.ts")


def findings(plan, code=None):
    found = validate.validate_findings(plan)
    return [f for f in found if code is None or f["code"] == code]


def messages(plan, code):
    return [f["message"] for f in findings(plan, code)]


def feat(plan, fid):
    return next(f for f in plan["features"] if f.get("id") == fid)


def file_(plan, path):
    return next(f for f in plan["files"] if f["path"] == path)


class CodeTest(Own, unittest.TestCase):
    """Every validation code of 2.5, 2.7 and 2.8: the plan that triggers it, the nearest plan that does not."""

    def assertCode(self, plan, code, text=None, level="error"):
        found = findings(plan, code)
        self.assertTrue(found, f"{code} expected")
        for f in found:
            self.assertEqual((f["level"], validate.classify_error(f["message"])), (level, code), f["message"])
            self.assertTrue(f["next"], f"{code} names no next step: {f['message']}")
        if text:
            self.assertTrue(any(text in f["message"] for f in found), [f["message"] for f in found])

    def assertNoCode(self, plan, code):
        self.assertEqual(findings(plan, code), [])

    def test_the_example_is_clean(self):
        self.assertEqual([f["message"] for f in findings(v2_plan())], [])

    def test_feature_incomplete(self):
        for key in ("title", "statement", "kind", "owner"):
            p = v2_plan()
            del feat(p, "F-inbox")[key]
            self.assertCode(p, "feature-incomplete", f"(missing: {key})")
        p = v2_plan()
        feat(p, "F-inbox")["title"] = "   "
        self.assertCode(p, "feature-incomplete")
        self.assertNoCode(v2_plan(), "feature-incomplete")

    def test_feature_kind(self):
        p = v2_plan()
        feat(p, "F-inbox")["kind"] = "big"
        self.assertCode(p, "feature-kind", "'big'")
        for kind in features.KINDS:
            p = v2_plan()
            feat(p, "F-inbox")["kind"] = kind
            self.assertNoCode(p, "feature-kind")

    def test_feature_owner(self):
        p = v2_plan()
        feat(p, "F-inbox")["owner"] = "ghost"
        self.assertCode(p, "feature-owner", "'ghost'")
        self.assertNoCode(v2_plan(), "feature-owner")

    def test_feature_milestone(self):
        p = v2_plan()
        feat(p, "F-inbox")["milestone"] = "M9"
        self.assertCode(p, "feature-milestone", "'M9' does not exist")
        p = v2_plan()
        del feat(p, "F-inbox")["milestone"]
        self.assertCode(p, "feature-milestone", "set a milestone")
        self.assertNoCode(v2_plan(), "feature-milestone")
        p = v2_plan()  # a plan without milestones needs none ...
        p["milestones"] = []
        for f in p["features"]:
            del f["milestone"]
        p["tasks"] = []
        self.assertNoCode(p, "feature-milestone")
        feat(p, "F-inbox")["milestone"] = "M1"  # ... but one named must exist
        self.assertCode(p, "feature-milestone", "'M1' does not exist")

    def test_feature_no_files(self):
        p = v2_plan()
        for f in p["files"]:
            f["features"] = [x for x in f.get("features", []) if x != "F-inbox"]
            if not f.get("features"):
                f["infrastructure"] = True
        self.assertCode(p, "feature-no-files", "feature F-inbox")
        self.assertEqual(len(messages(p, "feature-no-files")), 1)
        # a draft is exempt, and only deletions do not serve a feature
        d = copy.deepcopy(p)
        feat(d, "F-inbox")["draft"] = True
        self.assertNoCode(d, "feature-no-files")
        gone = copy.deepcopy(p)
        gone["files"].append({"path": "apps/web/src/old.ts", "action": "delete", "module": "web", "phase": "P1",
                              "purpose": "old", "features": ["F-inbox"]})
        self.assertCode(gone, "feature-no-files", "feature F-inbox")
        pat = copy.deepcopy(p)
        pat["patterns"].append({"id": "screen", "glob": "apps/web/src/screens/*.tsx", "module": "web", "phase": "P1",
                                "purpose": "screens", "instance": "one screen", "features": ["F-inbox"]})
        self.assertNoCode(pat, "feature-no-files")
        self.assertNoCode(v2_plan(), "feature-no-files")

    def test_feature_no_criteria(self):
        p = v2_plan()
        feat(p, "F-inbox")["criteria"] = []
        self.assertCode(p, "feature-no-criteria", "feature F-inbox")
        p = v2_plan()
        del feat(p, "F-inbox")["criteria"]
        self.assertCode(p, "feature-no-criteria")
        feat(p, "F-inbox")["draft"] = True  # a draft is exempt
        self.assertNoCode(p, "feature-no-criteria")
        self.assertNoCode(v2_plan(), "feature-no-criteria")

    def test_feature_dependency(self):
        p = v2_plan()
        feat(p, "F-inbox")["dependsOn"] = ["F-nope"]
        self.assertCode(p, "feature-dependency", "'F-nope' is not a feature")
        p = v2_plan()
        feat(p, "F-log-snag")["dependsOn"] = ["F-inbox"]  # F-inbox already depends on F-log-snag
        self.assertCode(p, "feature-dependency", "feature dependency loop: ")
        p = v2_plan()
        feat(p, "F-inbox")["dependsOn"] = ["F-inbox"]
        self.assertCode(p, "feature-dependency", "feature dependency loop: F-inbox -> F-inbox")
        self.assertNoCode(v2_plan(), "feature-dependency")

    def test_feature_draft_is_a_warning(self):
        p = v2_plan()
        feat(p, "F-inbox")["draft"] = True
        self.assertCode(p, "feature-draft", "feature F-inbox is a draft;", level="warning")
        feat(p, "F-inbox")["source"] = {"study": "S-acme", "behaviours": ["B-1"]}
        self.assertCode(p, "feature-draft", "feature F-inbox is a draft from study S-acme;", level="warning")
        self.assertEqual([f for f in findings(p) if f["level"] == "error"], [])
        self.assertNoCode(v2_plan(), "feature-draft")

    def test_criterion_incomplete(self):
        for key in ("scenario", "action", "expected"):
            p = v2_plan()
            del feat(p, "F-inbox")["criteria"][0][key]
            self.assertCode(p, "criterion-incomplete", f"criterion F-inbox/C1: needs scenario, action and expected (missing: {key})")
        p = v2_plan()
        feat(p, "F-inbox")["criteria"][0]["action"] = ""
        self.assertCode(p, "criterion-incomplete")
        self.assertNoCode(v2_plan(), "criterion-incomplete")

    def test_criterion_level(self):
        p = v2_plan()
        feat(p, "F-inbox")["criteria"][0]["level"] = "huge"
        self.assertCode(p, "criterion-level", "criterion F-inbox/C1: level must be unit, integration, e2e or manual")
        p = v2_plan()
        feat(p, "F-inbox")["level"] = "huge"
        self.assertCode(p, "criterion-level", "feature F-inbox: level must be unit, integration, e2e or manual")
        p = v2_plan()
        c = feat(p, "F-log-snag")["criteria"][0]
        c["verifyLevel"] = "manual"  # a command never exercises manual (2.9)
        self.assertCode(p, "criterion-level", "verifyLevel must be unit, integration or e2e")
        for lv in features.COMMAND_LEVELS:
            c["verifyLevel"] = lv
            self.assertNoCode(p, "criterion-level")
        for lv in features.LEVELS:
            feat(p, "F-inbox")["criteria"][0]["level"] = lv
            self.assertNoCode(p, "criterion-level")
        self.assertNoCode(v2_plan(), "criterion-level")

    def test_criterion_test(self):
        p = v2_plan()
        feat(p, "F-log-snag")["criteria"][0]["test"] = "crates/snag-api/tests/nope.rs"
        self.assertCode(p, "criterion-test", "'crates/snag-api/tests/nope.rs' is not a planned file")
        feat(p, "F-log-snag")["criteria"][0]["test"] = "crates\\snag-api\\tests\\snags.rs"  # spelling does not matter
        self.assertNoCode(p, "criterion-test")
        p["patterns"].append({"id": "t", "glob": "crates/snag-api/tests/extra_*.rs", "module": "api", "phase": "P1",
                              "purpose": "tests", "instance": "a test", "features": ["F-log-snag"]})
        feat(p, "F-log-snag")["criteria"][0]["test"] = "crates/snag-api/tests/extra_one.rs"
        self.assertNoCode(p, "criterion-test")
        self.assertNoCode(v2_plan(), "criterion-test")

    def test_file_unknown_feature(self):
        p = v2_plan()
        file_(p, "apps/web/src/Inbox.tsx")["features"] = ["F-ghost"]
        self.assertCode(p, "file-unknown-feature", "file apps/web/src/Inbox.tsx: serves feature 'F-ghost', which does not exist")
        p = v2_plan()
        p["patterns"].append({"id": "screen", "glob": "apps/web/src/screens/*.tsx", "module": "web", "phase": "P1",
                              "purpose": "screens", "instance": "one screen", "features": ["F-ghost"]})
        self.assertCode(p, "file-unknown-feature", "pattern screen: serves feature 'F-ghost'")
        p = v2_plan()  # also in a plan that has no features at all
        p["features"], p["milestones"], p["tasks"], p["unknowns"] = [], [], [], []
        self.assertCode(p, "file-unknown-feature")
        self.assertNoCode(v2_plan(), "file-unknown-feature")

    def test_file_no_feature(self):
        p = v2_plan()
        f = file_(p, "Cargo.toml")
        del f["infrastructure"]
        self.assertCode(p, "file-no-feature", "file Cargo.toml: serves no feature")
        f["infrastructure"] = True  # fixed one way ...
        self.assertNoCode(p, "file-no-feature")
        del f["infrastructure"]
        f["features"] = ["F-inbox"]  # ... or the other
        self.assertNoCode(p, "file-no-feature")
        # patterns follow the same rule; deletions are exempt
        p = v2_plan()
        p["patterns"].append({"id": "screen", "glob": "apps/web/src/screens/*.tsx", "module": "web", "phase": "P1",
                              "purpose": "screens", "instance": "one screen"})
        self.assertCode(p, "file-no-feature", "pattern screen: serves no feature")
        p["patterns"][0]["infrastructure"] = True
        self.assertNoCode(p, "file-no-feature")
        p = v2_plan()
        p["files"].append({"path": "old.txt", "action": "delete", "module": "repo", "phase": "P1", "purpose": "old"})
        self.assertNoCode(p, "file-no-feature")
        # the rule applies only when the plan has features
        p = v2_plan()
        p["features"] = []
        p["milestones"], p["tasks"], p["unknowns"] = [], [], []
        for f in p["files"]:
            f.pop("features", None)
            f.pop("infrastructure", None)
        self.assertNoCode(p, "file-no-feature")
        self.assertNoCode(v2_plan(), "file-no-feature")

    def test_file_infra_and_feature(self):
        p = v2_plan()
        file_(p, "apps/web/src/Inbox.tsx")["infrastructure"] = True
        self.assertCode(p, "file-infra-and-feature", "file apps/web/src/Inbox.tsx: has features and infrastructure: true")
        p["patterns"].append({"id": "screen", "glob": "apps/web/src/screens/*.tsx", "module": "web", "phase": "P1",
                              "purpose": "screens", "instance": "one screen", "features": ["F-inbox"], "infrastructure": True})
        self.assertCode(p, "file-infra-and-feature", "pattern screen: has features and infrastructure: true")
        file_(p, "apps/web/src/Inbox.tsx")["infrastructure"] = False  # false is not infrastructure
        self.assertEqual(len(findings(p, "file-infra-and-feature")), 1)
        self.assertNoCode(v2_plan(), "file-infra-and-feature")

    def test_ids(self):
        p = v2_plan()
        feat(p, "F-inbox")["id"] = "Inbox"
        file_(p, "apps/web/src/Inbox.tsx")["features"] = ["Inbox"]
        file_(p, "apps/web/src/offline.ts")["features"] = ["Inbox"]
        self.assertCode(p, "bad-id", "feature id 'Inbox' has the wrong form")
        p = v2_plan()
        feat(p, "F-inbox")["criteria"][0]["id"] = "c-1"
        self.assertCode(p, "bad-id", "criterion id 'F-inbox/c-1' has the wrong form")
        for good in ("F-a", "F-" + "a" * 48):
            p = v2_plan()
            feat(p, "F-inbox")["id"] = good
            file_(p, "apps/web/src/Inbox.tsx")["features"] = [good]
            file_(p, "apps/web/src/offline.ts")["features"] = [good]
            self.assertNoCode(p, "bad-id")
        p = v2_plan()
        feat(p, "F-inbox")["id"] = "F-" + "a" * 49  # one too long
        self.assertTrue(findings(p, "bad-id"))
        p = v2_plan()
        feat(p, "F-inbox")["id"] = "F-x\n"  # a trailing newline is not a valid id
        self.assertTrue(findings(p, "bad-id"))
        self.assertNoCode(v2_plan(), "bad-id")

    def test_duplicate_and_missing_ids(self):
        p = v2_plan()
        p["features"].append(copy.deepcopy(feat(p, "F-inbox")))
        self.assertEqual(messages(p, "duplicate-id"), ["duplicate feature id: F-inbox"])
        p = v2_plan()
        feat(p, "F-inbox")["criteria"].append(copy.deepcopy(feat(p, "F-inbox")["criteria"][0]))
        self.assertEqual(messages(p, "duplicate-id"), ["duplicate criterion id: F-inbox/C1"])
        p = v2_plan()
        del feat(p, "F-inbox")["id"]
        self.assertEqual(len(findings(p, "missing-id")), 1)
        self.assertIn("feature without id:", messages(p, "missing-id")[0])
        p = v2_plan()
        del feat(p, "F-inbox")["criteria"][0]["id"]
        self.assertIn("criterion without id:", messages(p, "missing-id")[0])
        self.assertEqual(findings(v2_plan(), "duplicate-id") + findings(v2_plan(), "missing-id"), [])

    def test_code_examples_classify_to_their_own_code(self):
        registry.codes()  # raises when an example is claimed by an earlier regex or a code has two owners
        for code, msg in features.CODE_EXAMPLES.items():
            self.assertEqual(validate.classify_error(msg), code)
        owned = {code for _, code in features.CODES}
        self.assertEqual(owned, set(features.CODE_EXAMPLES))
        self.assertTrue({"feature-no-files", "criterion-test", "file-no-feature"} <= owned)

    def test_every_message_the_fixtures_produce_classifies(self):
        bad = v2_plan()
        feat(bad, "F-inbox").update(kind="big", owner="ghost", milestone="M9", level="huge", dependsOn=["F-nope", "F-inbox"])
        feat(bad, "F-inbox")["criteria"][0].update(level="huge", test="nope.rs", verifyLevel="manual")
        del feat(bad, "F-log-snag")["criteria"][1]["expected"]
        file_(bad, "Cargo.toml")["features"] = ["F-ghost"]
        for f in findings(bad):
            self.assertEqual(validate.classify_error(f["message"]), f["code"], f["message"])

    def test_never_crashes_on_odd_values(self):
        p = v2_plan()
        p["features"] = [None, 5, "F-x", {"id": ["F-a"], "criteria": "x", "dependsOn": 3, "level": [], "milestone": {},
                                          "owner": 1, "kind": [], "source": 4, "draft": "yes"},
                         {"id": "F-b", "criteria": [None, 5, {"id": 7}, {"id": "C1", "test": 5, "level": {}}]}]
        p["files"][1]["features"] = [None, 3, ["x"], "F-b"]
        p["patterns"] = [{"id": "p", "glob": 7, "features": "F-b"}]
        findings(p)  # must not raise


# ---------------------------------------------------------------------------------------------------- feature items

class ItemsTest(TempRoot):
    def setUp(self):
        super().setUp()
        self.plan = v2_plan()
        self.plan["patterns"].append({"id": "screen", "glob": "apps/web/src/screens/*.tsx", "module": "web", "phase": "P1",
                                      "purpose": "screens", "instance": "one screen", "features": ["F-inbox"]})
        self.write_plan(self.plan)

    def ctx(self, state=None):
        plan = quiet(bplan.load_plan, self.root)[0]
        st = state or bplan.load_state(self.root)
        return plan, st, status.compute(self.root, plan, st)

    def test_files_and_a_pattern_with_no_instance(self):
        plan, st, comp = self.ctx()
        self.assertEqual(features.feature_items(self.root, plan, st, comp, "F-log-snag"),
                         [{"path": "crates/snag-api/src/main.rs", "status": "planned", "from": "file"},
                          {"path": "crates/snag-api/tests/snags.rs", "status": "planned", "from": "file"}])
        self.assertEqual(features.feature_items(self.root, plan, st, comp, "F-inbox"),
                         [{"path": "apps/web/src/Inbox.tsx", "status": "planned", "from": "file"},
                          {"path": "apps/web/src/offline.ts", "status": "planned", "from": "file"},
                          {"path": "apps/web/src/screens/*.tsx", "status": "planned", "from": "pattern:screen"}])
        self.assertEqual(features.feature_items(self.root, plan, st, comp, "F-nope"), [])

    def test_pattern_instances_take_the_status_of_each_instance(self):
        self.touch("apps/web/src/screens/A.tsx")
        self.touch("apps/web/src/screens/B.tsx")
        self.touch("apps/web/src/Inbox.tsx")
        plan, st, comp = self.ctx()
        st["patterns"] = {"screen": {"instances": ["apps/web/src/screens/B.tsx", "apps/web/src/screens/A.tsx"]}}
        st["files"]["apps/web/src/screens/B.tsx"] = {"state": "built"}
        comp = status.compute(self.root, plan, st)
        items = features.feature_items(self.root, plan, st, comp, "F-inbox")
        self.assertEqual([(i["path"], i["status"], i["from"]) for i in items],
                         [("apps/web/src/Inbox.tsx", "ondisk", "file"), ("apps/web/src/offline.ts", "planned", "file"),
                          ("apps/web/src/screens/A.tsx", "ondisk", "pattern:screen"),
                          ("apps/web/src/screens/B.tsx", "built", "pattern:screen")])

    def test_deletions_are_not_items_and_the_file_decides(self):
        self.plan["files"].append({"path": "apps/web/src/old.ts", "action": "delete", "module": "web", "phase": "P1",
                                   "purpose": "old", "features": ["F-inbox"]})
        self.write_plan(self.plan)
        plan, st, comp = self.ctx()
        paths = [i["path"] for i in features.feature_items(self.root, plan, st, comp, "F-inbox")]
        self.assertNotIn("apps/web/src/old.ts", paths)
        # the link is stored on the file only: features have no files field and the example's Cargo.toml serves none
        self.assertNotIn("Cargo.toml", [i["path"] for i in features.feature_items(self.root, plan, st, comp, "F-log-snag")])

    def test_documents_that_verify_run_never_covers_pattern_instances(self):
        """Today's behaviour (core gap): verify_groups walks planned files only, so a pattern instance is never proven
        and a feature served only by pattern instances can reach built but not verified. When the core changes, this
        test changes with it."""
        from bp.verify import verify_groups
        plan, st, comp = self.ctx()
        listed = [p for paths in verify_groups(plan).values() for p in paths]
        self.assertFalse([p for p in listed if p.startswith("apps/web/src/screens/")])
        only = {"id": "F-x", "criteria": [crit("C1")]}
        built = [{"path": "apps/web/src/screens/A.tsx", "status": "built", "from": "pattern:screen"}]
        self.assertEqual(features.derive_status(only, built, CMD_PASS, False), "built")

    def test_blocked_and_verified_rows(self):
        self.touch("crates/snag-api/src/main.rs")
        plan, st, comp = self.ctx()
        st["files"]["crates/snag-api/src/main.rs"] = {"state": "blocked"}
        comp = status.compute(self.root, plan, st)
        items = features.feature_items(self.root, plan, st, comp, "F-log-snag")
        self.assertEqual(items[0]["status"], "blocked")


# ------------------------------------------------------------------------------------------------ rules of 4.3

def F(**kw):
    """A feature with one command criterion; keyword arguments replace fields."""
    f = {"id": "F-x", "level": "integration",
         "criteria": [{"id": "C1", "scenario": "s", "action": "a", "expected": "e", "verify": "cmd"}]}
    f.update(kw)
    return f


def crit(cid, verify=True, **kw):
    c = {"id": cid, "scenario": "s", "action": "a", "expected": "e"}
    if verify:
        c["verify"] = "cmd " + cid
    c.update(kw)
    return c


def items(*states):
    return [{"path": f"f{i}.rs", "status": s, "from": "file"} for i, s in enumerate(states)]


def entry(result="pass", via="command", level="integration", stale=False, **kw):
    e = {"result": result, "level": level, "at": "2027-01-01T00:00:00Z", "stale": stale, "recordId": "vr-000000000000",
         "via": via}
    e.update(kw)
    return e


def checks(criteria=None, command_pass=True, ev=None):
    return {"criteria": criteria or {}, "commandPass": command_pass, "evaluation": ev}


def evaluation(results=None, level="e2e", verdict="pass"):
    return {"verdict": verdict, "by": "evaluator", "mode": "browser", "level": level, "at": "2027-01-02T00:00:00Z",
            "criteria": {cid: {"result": r, "note": "", "digest": "sha256:" + "0" * 64} for cid, r in (results or {}).items()}}


CMD_PASS = checks({"C1": entry()})
V = ["verified"]


class DeriveStatusTest(unittest.TestCase):
    """One test per rule of 4.3 with synthetic checks; each also tries the nearest input that does not match."""

    def st(self, feature, its, chk=None, blocked=False):
        return features.derive_status(feature, its, chk if chk is not None else {}, blocked)

    def test_rule_1_draft(self):
        self.assertEqual(self.st(F(draft=True), items(*V), CMD_PASS), "draft")
        self.assertEqual(self.st(F(draft=True), items("blocked"), CMD_PASS, blocked=True), "draft")
        self.assertEqual(self.st(F(draft=True), [], {}), "draft")
        self.assertEqual(self.st(F(draft=False), [], {}), "planned")
        self.assertEqual(self.st(F(draft="yes"), [], {}), "planned", "only the boolean true is a draft")

    def test_rule_2_blocked(self):
        self.assertEqual(self.st(F(), items(*V), CMD_PASS, blocked=True), "blocked")  # unknown blocks feature, milestone or phase
        self.assertEqual(self.st(F(), items("verified", "blocked"), CMD_PASS), "blocked")  # one blocked item
        self.assertEqual(self.st(F(), items("verified", "built"), CMD_PASS), "built")
        # blocked comes before failed
        ev = evaluation({"C1": "fail"})
        self.assertEqual(self.st(F(), items(*V), checks({"C1": entry()}, True, ev), blocked=True), "blocked")
        self.assertEqual(self.st(F(), items(*V), checks({"C1": entry()}, True, ev)), "failed")

    def test_rule_3_failed(self):
        ev = evaluation({"C1": "fail"})
        for its in ([], items("planned"), items("built"), items(*V)):
            self.assertEqual(self.st(F(), its, checks({"C1": entry()}, True, ev)), "failed")
        # an evaluation fail on a criterion that has a command also fails the feature (4.3)
        two = F(criteria=[crit("C1"), crit("C2", verify=False)])
        ev2 = evaluation({"C1": "fail", "C2": "pass"})
        self.assertEqual(self.st(two, items(*V), checks({"C1": entry(), "C2": entry(via="evaluation", level="e2e")}, True, ev2)), "failed")
        # a failing command check is not a failure of the feature: it is just not verified
        self.assertEqual(self.st(F(), items(*V), checks({"C1": entry("fail")}, False)), "built")
        # pass, skipped and unknown in an evaluation never fail it
        for r in ("pass", "skipped", "unknown", "error", "timeout"):
            self.assertNotEqual(self.st(F(), items(*V), checks({"C1": entry()}, True, evaluation({"C1": r}))), "failed", r)
        self.assertEqual(self.st(F(), items(*V), checks({"C1": entry("fail", via="evaluation")}, True, None)), "failed")

    def test_rule_4_ranks_and_7_built(self):
        self.assertEqual(self.st(F(), items("built", "built"), {}), "built")
        self.assertEqual(self.st(F(), items("built", "verified"), {}), "built")  # no checks: never verified
        self.assertEqual(self.st(F(), items("built", "ondisk"), {}), "building")  # lo < 3
        self.assertEqual(self.st(F(), items("built", "planned"), {}), "building")

    def test_rule_8_building_and_9_planned(self):
        for s in ("claimed", "ondisk"):
            self.assertEqual(self.st(F(), items(s), {}), "building")
            self.assertEqual(self.st(F(), items("planned", s), {}), "building")
        self.assertEqual(self.st(F(), items("planned", "planned"), {}), "planned")
        self.assertEqual(self.st(F(), [], {}), "planned", "no items: lo = hi = 0")
        self.assertEqual(self.st(F(), items("nonsense"), {}), "planned", "an unknown file status ranks 0")

    def test_rule_5_6_verified_needs_every_command_criterion_to_pass_via_command(self):
        self.assertEqual(self.st(F(), items(*V), CMD_PASS), "accepted" if False else "verified")
        self.assertEqual(self.st(F(), items("verified", "verified"), CMD_PASS), "verified")
        self.assertEqual(self.st(F(), items("verified", "built"), CMD_PASS), "built", "lo must be 4")
        two = F(criteria=[crit("C1"), crit("C2")])
        both = checks({"C1": entry(), "C2": entry()})
        self.assertEqual(self.st(two, items(*V), both), "verified")
        self.assertEqual(self.st(two, items(*V), checks({"C1": entry()}, False)), "built", "C2 has no check")
        for bad in ("fail", "timeout", "skipped", "error", "unknown", "nonsense", None, 5):
            with self.subTest(result=bad):
                self.assertEqual(self.st(two, items(*V), checks({"C1": entry(), "C2": entry(bad)}, True)), "built")
        self.assertEqual(self.st(two, items(*V), checks({"C1": entry(), "C2": entry(stale=True)}, True)), "built")
        self.assertEqual(self.st(two, items(*V), checks({"C1": entry(), "C2": entry(via="evaluation")}, True)), "built")
        self.assertEqual(self.st(two, items(*V), checks({"C1": entry(), "C2": entry(via=None)}, True)), "built")
        self.assertEqual(self.st(F(), items(*V), checks({"C1": entry()}, False)), "built", "the commandPass flag must hold too")
        self.assertEqual(self.st(F(), items(*V), checks({"C1": entry()}, "yes")), "built")
        self.assertEqual(self.st(F(), items(*V), {}), "built", "no checks: before records exist nothing is verified")
        self.assertEqual(self.st(F(criteria=[]), items(*V), checks({}, True)), "built", "needs at least one criterion")
        self.assertEqual(self.st(F(), items(*V), "junk"), "built")
        self.assertEqual(self.st(F(), items(*V), checks("junk", True)), "built")

    def test_rule_6_accepted_when_every_criterion_without_verify_passes_through_the_evaluation(self):
        f = F(criteria=[crit("C1"), crit("C2", verify=False), crit("C3", verify=False, level="unit")])
        good = {"C1": entry(), "C2": entry(via="evaluation", level="e2e"), "C3": entry(via="evaluation", level="e2e")}
        ev = evaluation({"C2": "pass", "C3": "pass"})
        self.assertEqual(self.st(f, items(*V), checks(good, True, ev)), "accepted")
        # not accepted: no current evaluation, an unchecked criterion, a failed one, a stale one, a level too low
        self.assertEqual(self.st(f, items(*V), checks(good, True, None)), "verified")
        self.assertEqual(self.st(f, items(*V), checks(dict(good, C3=entry("unknown", via=None)), True, ev)), "verified")
        self.assertEqual(self.st(f, items(*V), checks(dict(good, C3=entry("skipped", via="evaluation")), True, ev)), "verified")
        self.assertEqual(self.st(f, items(*V), checks(dict(good, C3=entry(via="evaluation", stale=True)), True, ev)), "verified")
        self.assertEqual(self.st(f, items(*V), checks(dict(good, C2=entry(via="evaluation", level="unit")), True, ev)), "verified",
                         "C2 needs the feature's integration level")
        self.assertEqual(self.st(f, items(*V), checks(dict(good, C2=entry(via="evaluation", level=None)), True, ev)), "verified")
        self.assertEqual(self.st(f, items(*V), checks(dict(good, C2=entry(via="command", level="e2e")), True, ev)), "verified",
                         "a command never passes a criterion that has no verify")
        self.assertEqual(self.st(f, items("verified", "built"), checks(good, True, ev)), "built")
        self.assertEqual(self.st(f, items(*V), checks(dict(good), False, ev)), "built")

    def test_rule_6_levels(self):
        f = F(level="e2e", criteria=[crit("C1", verify=False)])
        for have, want in (("unit", "verified"), ("integration", "verified"), ("e2e", "accepted"), ("manual", "accepted")):
            with self.subTest(have=have):
                self.assertEqual(self.st(f, items(*V), checks({"C1": entry(via="evaluation", level=have)}, True, evaluation({"C1": "pass"}))), want)
        m = F(level="manual", criteria=[crit("C1", verify=False)])
        self.assertEqual(self.st(m, items(*V), checks({"C1": entry(via="evaluation", level="e2e")}, True, evaluation({"C1": "pass"}))),
                         "accepted", "rank(e2e) >= rank(manual) (2.9)")
        self.assertEqual(self.st(m, items(*V), checks({"C1": entry(via="evaluation", level="integration")}, True, evaluation({"C1": "pass"}))),
                         "verified")
        own = F(level="unit", criteria=[crit("C1", verify=False, level="manual")])  # the criterion's own level wins
        self.assertEqual(self.st(own, items(*V), checks({"C1": entry(via="evaluation", level="unit")}, True, evaluation({"C1": "pass"}))),
                         "verified")
        odd = F(level="nonsense", criteria=[crit("C1", verify=False)])  # an unknown required level is the strictest
        self.assertEqual(self.st(odd, items(*V), checks({"C1": entry(via="evaluation", level="e2e")}, True, evaluation({"C1": "pass"}))),
                         "accepted")
        self.assertEqual(self.st(odd, items(*V), checks({"C1": entry(via="evaluation", level="unit")}, True, evaluation({"C1": "pass"}))),
                         "verified")

    def test_rule_6_when_all_criteria_have_verify_the_evaluation_must_name_a_pass(self):
        f = F(level="integration")
        pass_ev = evaluation({"C1": "pass"}, level="e2e")
        self.assertEqual(self.st(f, items(*V), checks({"C1": entry()}, True, pass_ev)), "accepted")
        self.assertEqual(self.st(f, items(*V), CMD_PASS), "verified", "no evaluation")
        for bad in (evaluation({}), evaluation({"C1": "unknown"}), evaluation({"C1": "skipped"}),
                    evaluation({"C9": "pass"}), evaluation({"C1": "pass"}, level="unit"), evaluation({"C1": "pass"}, level=None)):
            with self.subTest(ev=bad):
                self.assertEqual(self.st(f, items(*V), checks({"C1": entry()}, True, bad)), "verified")
        self.assertEqual(self.st(f, items("verified", "built"), checks({"C1": entry()}, True, pass_ev)), "built")
        self.assertEqual(self.st(f, items(*V), checks({"C1": entry("fail")}, False, pass_ev)), "built",
                         "an evaluation pass does not change a command criterion's own result")
        self.assertEqual(self.st(f, items(*V), checks({"C1": entry()}, True, "junk")), "verified")

    def test_malformed_input_never_looks_more_done(self):
        for chk in (None, 5, "x", [], {"criteria": []}, {"criteria": {"C1": "pass"}}, {"criteria": {"C1": []}},
                    {"commandPass": True}, {"commandPass": True, "criteria": None, "evaluation": 3}):
            with self.subTest(checks=chk):
                self.assertIn(self.st(F(), items(*V), chk), ("built",))
        for feature in (None, [], "F-x", {}):
            self.assertEqual(features.derive_status(feature, items(*V), CMD_PASS, False), "built")
        self.assertEqual(features.derive_status(F(), None, CMD_PASS, False), "planned")
        self.assertEqual(features.derive_status(F(), [None, 3, {"status": "verified"}], CMD_PASS, False), "verified")

    def test_status_text_counts_the_criteria_that_await_evaluation(self):
        self.assertEqual(features.status_text("verified", 2, 3, False), "verified, 2 criteria await evaluation")
        self.assertEqual(features.status_text("verified", 1, 3, False), "verified, 1 criterion awaits evaluation")
        self.assertEqual(features.status_text("verified", 3, 3, True), "verified, 3 of 3 criteria await evaluation")
        self.assertEqual(features.status_text("verified", 0, 3, False), "verified")
        self.assertEqual(features.status_text("built", 2, 3, False), "built")
        self.assertEqual(features.status_text("accepted", 0, 3, False), "accepted")


# ------------------------------------------------------------------------------------------- context and views

class ProjectBase(Own, TempRoot):
    """A temp project holding the version 2 example, with the four planned files on disk when asked."""

    def setUp(self):
        super().setUp()
        self.plan = v2_plan()
        self.write_plan(self.plan)

    def run_(self, *argv, **kw):
        return run_cli(*argv, "--root", self.root, **kw)

    def load(self):
        plan = quiet(bplan.load_plan, self.root)[0]
        state = bplan.load_state(self.root)
        return plan, state

    def ctx(self, checks=None, blocked=None, state=None):
        plan, st = self.load()
        ctx = build_context(self.root, plan, state or st)
        if checks is not None or blocked is not None:
            if checks is not None:
                ctx["checks"] = checks
            if blocked is not None:
                ctx["blocked"] = blocked
            features.context(self.root, plan, state or st, ctx)
        return ctx

    def verified_state(self, paths):
        plan, st = self.load()
        for p in paths:
            self.touch(p, "// " + p + "\n")
            st["files"][p] = {"state": "built", "verified": {"ok": True, "cmd": "c", "at": "t", "sha": "x",
                                                            "inputs": status.proof_inputs(self.root, plan, [p])}}
        (self.root / ".blueprint" / "state.json").write_text(json.dumps(st), encoding="utf-8")
        return st


class ContextTest(ProjectBase):
    def test_a_fresh_project(self):
        ctx = self.ctx()
        fs = {f["id"]: f for f in ctx["features"]}
        self.assertEqual(list(fs), ["F-log-snag", "F-inbox"])
        f = fs["F-log-snag"]
        self.assertEqual((f["status"], f["statusText"], f["draft"], f["milestone"], f["owner"], f["level"], f["kind"]),
                         ("planned", "planned", False, "M1", "backend-lead", "integration", "add"))
        self.assertEqual([i["path"] for i in f["items"]], ["crates/snag-api/src/main.rs", "crates/snag-api/tests/snags.rs"])
        self.assertEqual(f["itemCounts"], {"planned": 2, "claimed": 0, "ondisk": 0, "built": 0, "verified": 0, "blocked": 0})
        self.assertEqual([c["id"] for c in f["criteria"]], ["C1", "C2"])
        c1, c2 = f["criteria"]
        self.assertEqual((c1["level"], c2["level"], c1["result"], c1["resultLevel"], c1["resultAt"], c1["stale"], c1["evaluation"]),
                         ("integration", "unit", "unknown", None, None, False, None))
        self.assertEqual(c1["mustNot"][:6], "Create")
        self.assertEqual((f["awaitingEvaluation"], f["evaluation"], f["blockedBy"], f["dependsOn"], f["source"]), (0, None, [], [], None))
        self.assertEqual(fs["F-inbox"]["dependsOn"], ["F-log-snag"])
        self.assertEqual(fs["F-inbox"]["awaitingEvaluation"], 1)
        self.assertEqual(fs["F-inbox"]["criteria"][0]["level"], "manual")
        json.dumps(ctx["features"])  # serialisable: it goes to status --json and into map.html

    def test_status_follows_the_files_and_the_checks(self):
        st = self.verified_state(FILES[:2])
        ctx = self.ctx(state=st)
        by = {f["id"]: f for f in ctx["features"]}
        self.assertEqual(by["F-log-snag"]["status"], "built", "files verified but no checks: never verified")
        chk = {"F-log-snag": checks({"C1": entry(), "C2": entry(level="unit")})}
        ctx = self.ctx(checks=chk, state=st)
        self.assertEqual({f["id"]: f["status"] for f in ctx["features"]}, {"F-log-snag": "verified", "F-inbox": "planned"})
        row = next(f for f in ctx["features"] if f["id"] == "F-log-snag")["criteria"][0]
        self.assertEqual((row["result"], row["resultLevel"], row["resultAt"], row["via"]),
                         ("pass", "integration", "2027-01-01T00:00:00Z", "command"))

    def test_awaiting_evaluation_is_counted_for_verified_features(self):
        self.plan["features"][0]["criteria"].append({"id": "C3", "scenario": "s", "action": "a", "expected": "e"})
        self.plan["features"][0]["criteria"].append({"id": "C4", "scenario": "s", "action": "a", "expected": "e"})
        self.write_plan(self.plan)
        st = self.verified_state(FILES[:2])
        chk = {"F-log-snag": checks({"C1": entry(), "C2": entry(level="unit")})}
        f = next(x for x in self.ctx(checks=chk, state=st)["features"] if x["id"] == "F-log-snag")
        self.assertEqual((f["status"], f["statusText"], f["awaitingEvaluation"]), ("verified", "verified, 2 criteria await evaluation", 2))
        chk["F-log-snag"]["criteria"]["C3"] = entry(via="evaluation", level="e2e")
        chk["F-log-snag"]["evaluation"] = evaluation({"C3": "pass"})
        f = next(x for x in self.ctx(checks=chk, state=st)["features"] if x["id"] == "F-log-snag")
        self.assertEqual((f["statusText"], f["awaitingEvaluation"]), ("verified, 1 criterion awaits evaluation", 1))
        chk["F-log-snag"]["criteria"]["C4"] = entry(via="evaluation", level="e2e")
        f = next(x for x in self.ctx(checks=chk, state=st)["features"] if x["id"] == "F-log-snag")
        self.assertEqual((f["status"], f["statusText"], f["awaitingEvaluation"], f["evaluation"]["by"]), ("accepted", "accepted", 0, "evaluator"))

    def test_all_criteria_without_verify(self):
        f0 = self.plan["features"][0]
        for c in f0["criteria"]:
            del c["verify"]
        self.write_plan(self.plan)
        st = self.verified_state(FILES[:2])
        f = next(x for x in self.ctx(checks={"F-log-snag": checks({}, True)}, state=st)["features"] if x["id"] == "F-log-snag")
        self.assertEqual((f["status"], f["statusText"]), ("verified", "verified, 2 of 2 criteria await evaluation"))

    def test_blocked_by_the_feature_its_milestone_or_the_phase_of_a_file(self):
        base = {"phases": set(), "milestones": set(), "features": set(), "by": {}}
        for blocked, who in (({"phases": {"P2"}, "by": {"phase:P2": ["U-offline"]}}, {"F-inbox"}),
                             ({"milestones": {"M1"}, "by": {"milestone:M1": ["U-a"]}}, {"F-inbox", "F-log-snag"}),
                             ({"features": {"F-log-snag"}, "by": {"feature:F-log-snag": ["U-b"]}}, {"F-log-snag"}),
                             ({"phases": {"P3"}}, set())):
            with self.subTest(blocked=blocked):
                ctx = self.ctx(checks={}, blocked=dict(base, **blocked))
                self.assertEqual({f["id"] for f in ctx["features"] if f["status"] == "blocked"}, who)
        ctx = self.ctx(checks={}, blocked=dict(base, phases={"P2"}, milestones={"M1"},
                                               by={"phase:P2": ["U-offline"], "milestone:M1": ["U-a", "U-offline"]}))
        inbox = next(f for f in ctx["features"] if f["id"] == "F-inbox")
        self.assertEqual(inbox["blockedBy"], ["U-a", "U-offline"])
        for junk in (None, 5, [], {"phases": 5, "by": []}, {"phases": [None], "milestones": "M1", "by": {"phase:P2": 5}}):
            self.ctx(checks={}, blocked=junk)  # never raises

    def test_drafts_and_duplicates(self):
        self.plan["features"][1]["draft"] = True
        self.plan["features"].append(copy.deepcopy(self.plan["features"][0]))
        self.write_plan(self.plan)
        ctx = self.ctx()
        self.assertEqual([(f["id"], f["status"]) for f in ctx["features"]], [("F-log-snag", "planned"), ("F-inbox", "draft")])

    def test_a_plan_without_features(self):
        self.write_plan(v1_plan())
        ctx = self.ctx()
        self.assertEqual(ctx["features"], [])
        self.assertEqual(features.file_tags(ctx), {})
        self.assertEqual(features.roster_sections(ctx), [])
        self.assertEqual(features.status_lines(ctx, type("A", (), {"features": True})()), [])
        self.assertEqual(features.status_json(ctx), {"features": []})
        self.assertEqual(features.map_payload(ctx)["features"], [])
        from bp.views import features_md
        self.assertEqual(features_md.views(ctx), {})


class HooksTest(ProjectBase):
    def test_file_tags(self):
        self.plan["patterns"].append({"id": "screen", "glob": "apps/web/src/screens/*.tsx", "module": "web", "phase": "P1",
                                      "purpose": "screens", "instance": "one screen", "features": ["F-inbox"]})
        self.write_plan(self.plan)
        tags = features.file_tags(self.ctx())
        self.assertEqual(tags["crates/snag-api/src/main.rs"], ["F-log-snag"])
        self.assertEqual(tags["Cargo.toml"], ["infra"])
        self.assertEqual(tags["apps/web/src/screens/*.tsx"], ["F-inbox"])

    def test_structure_md_carries_the_tags_and_a_plan_without_features_is_unchanged(self):
        self.assertEqual(self.run_("render")[0], 0)
        text = (self.root / ".blueprint" / "STRUCTURE.md").read_text(encoding="utf-8")
        self.assertIn("main.rs  ○ POST /snags and GET /snags (backend-lead, P1) [F-log-snag]", text)
        self.assertIn("Cargo.toml  ○ Workspace manifest (cto-architect, P1) [infra]", text)
        self.write_plan(v1_plan())
        self.assertEqual(self.run_("render")[0], 0)
        text = (self.root / ".blueprint" / "STRUCTURE.md").read_text(encoding="utf-8")
        self.assertNotIn("[F-", text)
        self.assertNotIn("[infra]", text)

    def test_roster_sections(self):
        sections = dict(features.roster_sections(self.ctx()))
        self.assertEqual(list(sections), ["Features owned", "Who may accept what"])
        self.assertEqual(sections["Features owned"],
                         "| Owner | Features owned |\n|---|---|\n| backend-lead | F-log-snag |\n| frontend-engineer | F-inbox |")
        who = sections["Who may accept what"].splitlines()
        self.assertEqual(who[-2:], ["| F-log-snag | cto-architect, frontend-engineer, evaluator, owner |",
                                    "| F-inbox | cto-architect, backend-lead, evaluator, owner |"])
        self.plan["features"][1]["draft"] = True  # a draft is never accepted: not listed
        self.write_plan(self.plan)
        self.assertNotIn("F-inbox", dict(features.roster_sections(self.ctx()))["Who may accept what"])
        # nobody is independent when one id owns the feature and every file
        p = v2_plan()
        p["roster"] = [r for r in p["roster"] if r["id"] in ("backend-lead",)]
        p["owners"] = [{"pattern": "*", "owner": "backend-lead"}]
        p["features"][1]["owner"] = "backend-lead"
        p["tasks"] = []
        p["unknowns"] = []
        self.write_plan(p)
        self.assertIn("| F-inbox | (nobody: add an evaluator to the roster) |", dict(features.roster_sections(self.ctx()))["Who may accept what"])

    def test_roster_md_has_the_sections(self):
        self.assertEqual(self.run_("render")[0], 0)
        text = (self.root / ".blueprint" / "ROSTER.md").read_text(encoding="utf-8")
        self.assertIn("## Features owned\n\n| Owner | Features owned |", text)
        self.assertIn("## Who may accept what\n\nA feature is accepted by a roster id that is not its owner", text)

    def test_map_payload_and_status_json(self):
        pay = features.map_payload(self.ctx())
        self.assertEqual(set(pay), {"features", "featureStates", "featureGlyph", "featureLabel", "levels", "featureNext"})
        self.assertEqual(pay["featureStates"], ["draft", "planned", "building", "built", "verified", "accepted", "failed", "blocked"])
        self.assertEqual(pay["featureGlyph"], {"draft": "◇", "planned": "○", "building": "◑", "built": "●", "verified": "✔",
                                               "accepted": "◉", "failed": "✖", "blocked": "▲"})
        self.assertEqual(pay["levels"], ["unit", "integration", "e2e", "manual"])
        for key in ("id", "title", "statement", "kind", "milestone", "owner", "level", "draft", "status", "items", "itemCounts",
                    "criteria", "awaitingEvaluation", "evaluation", "blockedBy", "dependsOn", "source"):
            self.assertIn(key, pay["features"][0])
        for key in ("id", "scenario", "action", "expected", "mustNot", "level", "result", "resultLevel", "resultAt", "stale", "evaluation"):
            self.assertIn(key, pay["features"][0]["criteria"][0])
        code, out, err = self.run_("status", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual([f["id"] for f in data["features"]], ["F-log-snag", "F-inbox"])
        self.assertEqual(data["features"], json.loads(json.dumps(pay["features"])))
        code, out, _ = self.run_("export", "json")
        self.assertEqual([f["status"] for f in json.loads(out)["features"]], ["planned", "planned"])

    def test_status_lines(self):
        code, out, err = self.run_("status")
        self.assertEqual(code, 0, err)
        self.assertIn("\n  Features: 2: ○ 2 planned\n", out)
        self.assertNotIn("F-log-snag", out.split("Features: 2")[1])
        code, out, _ = self.run_("status", "--features")
        lines = out.rstrip("\n").splitlines()
        self.assertEqual(lines[-3], "  Features: 2: ○ 2 planned")
        self.assertEqual(lines[-2], "    ○ F-log-snag               planned  (backend-lead) Log a snag")
        self.assertEqual(lines[-1], "    ○ F-inbox                  planned  (frontend-engineer) Review snags in the inbox")
        self.write_plan(v1_plan())  # a plan without features: the 1.x output only
        code, out, _ = self.run_("status")
        self.assertNotIn("Features", out)

    def test_status_card(self):
        code, out, err = self.run_("status", "--feature", "F-log-snag")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[:2], ["F-log-snag  Log a snag", "  Status: ○ planned"])
        self.assertIn("  add · M1 · backend-lead · integration", lines)
        self.assertIn("  Depends on: none", lines)
        self.assertIn("    ○ crates/snag-api/src/main.rs", lines)
        self.assertIn("    C1 [integration] unknown", lines)
        self.assertIn("    C2 [unit] unknown", lines)
        self.assertIn("      Check: $ cargo test -p snag-api validation", lines)
        self.assertTrue(lines[-1].startswith("  Latest evaluation: none"))
        code, out, _ = self.run_("status", "--feature", "F-inbox")
        self.assertIn("      Check: passed only by an independent evaluation (accept)", out.splitlines())
        self.assertIn("  Depends on: F-log-snag", out.splitlines())
        code, out, err = self.run_("status", "--feature", "F-ghost")
        self.assertEqual(code, 1)
        self.assertIn("error: feature F-ghost is not in the plan", err)
        self.assertRegex(err, r'next: python ".*blueprint\.py" status --features --root ".*"')  # runs as printed

    def test_status_card_shows_results_evaluation_and_blockers(self):
        chk = {"F-log-snag": checks({"C1": entry(repeat={"n": 3, "passed": 3}), "C2": entry("fail", level="unit"),},
                                    False, evaluation({"C2": "fail"}))}
        ctx = self.ctx(checks=chk, blocked={"phases": {"P1"}, "milestones": set(), "features": set(),
                                            "by": {"phase:P1": ["U-x"]}})
        card = "\n".join(features.status_card(ctx, "F-log-snag"))
        self.assertIn("Blocked by: U-x", card)
        self.assertIn("C1 [integration] pass (integration, 3/3)", card)
        self.assertIn("C2 [unit] fail", card)
        self.assertIn("Evaluation: fail", card)
        self.assertIn("Latest evaluation: evaluator · browser · pass · 2027-01-02", card)

    def test_the_hooks_have_one_owner_each(self):
        card = registry.hook("status_card")
        self.assertEqual((card.__module__, card.__name__), ("bp.features", "status_card"))
        self.assertTrue(registry.exists("features") and registry.exists("views.features_md"))
        mods = [n for n, _ in registry.collect("map_payload", self.ctx())]
        self.assertIn("features", mods)


# ----------------------------------------------------------------------------------------------------- FEATURES.md

EXAMPLE_FEATURES_MD = """# Snag Desk features
<!-- Generated by blueprint.py render. Do not edit. -->
Features: accepted 0 of 2 · ○ 2 planned

## ○ F-log-snag Log a snag · planned

A client records a snag through the API and gets its id back.

add · M1 · backend-lead · integration

Depends on: none

Files:

- ○ `crates/snag-api/src/main.rs`
- ○ `crates/snag-api/tests/snags.rs`

| ID | Given | When | Then | Must not | Level | Check | Evaluation |
|---|---|---|---|---|---|---|---|
| C1 | The API runs on an empty database | POST /snags with a title | 201 with the new id; GET /snags lists it | Create a second row when the request is retried with the same Idempotency-Key | integration | unknown | - |
| C2 | The API runs | POST /snags without a title | 422 naming the title field |  | unit | unknown | - |

Latest evaluation: none

## ○ F-inbox Review snags in the inbox · planned

A site lead sees open snags, newest first.

add · M1 · frontend-engineer · e2e

Depends on: F-log-snag

Files:

- ○ `apps/web/src/Inbox.tsx`
- ○ `apps/web/src/offline.ts`

| ID | Given | When | Then | Must not | Level | Check | Evaluation |
|---|---|---|---|---|---|---|---|
| C1 | Two snags exist | Open the inbox | Both are listed, newest first |  | manual | - | unknown |

Latest evaluation: none
"""


class FeaturesMdTest(ProjectBase):
    def md(self):
        self.assertEqual(self.run_("render")[0], 0)
        return (self.root / ".blueprint" / "FEATURES.md").read_text(encoding="utf-8")

    def test_the_version_2_example(self):
        self.assertEqual(self.md(), EXAMPLE_FEATURES_MD)

    def test_header_and_tracking(self):
        text = self.md()
        self.assertTrue(any(l.startswith("<!-- Generated by blueprint.py render") for l in text.splitlines()[:5]))
        self.assertEqual(json.loads((self.root / ".blueprint" / "state.json").read_text(encoding="utf-8"))["generatedViews"], ["FEATURES.md"])
        self.assertEqual(self.run_("render", "--check")[0], 0)
        self.write_plan(v1_plan())  # no features: the file goes away, as the contract's section 8 says
        self.run_("render")
        self.assertFalse((self.root / ".blueprint" / "FEATURES.md").exists())

    def test_status_drafts_evaluation_blockers_and_source(self):
        self.plan["features"][1]["draft"] = True
        self.plan["features"][1]["source"] = {"study": "S-acme", "behaviours": ["B-1"]}
        self.plan["features"].append(copy.deepcopy(self.plan["features"][1]))
        self.plan["features"][2].update(id="F-sketch", title="A | sketch\nwith two lines", draft=True)
        self.plan["features"][2]["criteria"] = []
        self.write_plan(self.plan)
        st = self.verified_state(FILES[:2])
        chk = {"F-log-snag": checks({"C1": entry(repeat={"n": 3, "passed": 3}), "C2": entry(stale=True)}, False, evaluation({"C2": "fail"}))}
        ctx = self.ctx(checks=chk, blocked={"phases": set(), "milestones": set(), "features": {"F-log-snag"},
                                            "by": {"feature:F-log-snag": ["U-a"]}}, state=st)
        from bp.views import features_md
        text = features_md.views(ctx)["FEATURES.md"]
        self.assertTrue(text.startswith("# Snag Desk features\n<!-- Generated by blueprint.py render. Do not edit. -->\n"))
        self.assertIn("Features: accepted 0 of 1 · drafts: 2 · ◇ 2 draft, ▲ 1 blocked\n", text)
        self.assertIn("## ▲ F-log-snag Log a snag · blocked\n", text)
        self.assertIn("| C1 | The API runs on an empty database |", text)
        self.assertIn("| integration | pass (integration, 3/3) | - |", text)
        self.assertIn("| unit | stale | fail |", text)
        self.assertIn("Latest evaluation: evaluator · browser · pass · 2027-01-02\n", text)
        self.assertIn("Blocked by: U-a\n", text)
        self.assertIn("Source: study S-acme\n", text)
        self.assertIn("- ✔ `crates/snag-api/src/main.rs`", text)
        self.assertLess(text.index("## ▲ F-log-snag"), text.index("## Drafts"))  # drafts last
        self.assertIn("### ◇ F-inbox Review snags in the inbox · draft", text)
        self.assertIn("### ◇ F-sketch A | sketch with two lines · draft", text)
        self.assertIn("No acceptance criteria yet.", text)
        self.assertNotIn("\r", text)

    def test_awaiting_evaluation_in_the_heading(self):
        self.plan["features"][0]["criteria"].append({"id": "C3", "scenario": "s", "action": "a", "expected": "e"})
        self.write_plan(self.plan)
        st = self.verified_state(FILES[:2])
        chk = {"F-log-snag": checks({"C1": entry(), "C2": entry(level="unit")})}
        from bp.views import features_md
        text = features_md.views(self.ctx(checks=chk, state=st))["FEATURES.md"]
        self.assertIn("## ✔ F-log-snag Log a snag · verified, 1 criterion awaits evaluation\n", text)
        self.assertIn("| C3 | s | a | e |  | integration | - | unknown |", text)

    def test_patterns_are_listed_with_their_instances(self):
        self.plan["patterns"].append({"id": "screen", "glob": "apps/web/src/screens/*.tsx", "module": "web", "phase": "P1",
                                      "purpose": "screens", "instance": "one screen", "features": ["F-inbox"]})
        self.write_plan(self.plan)
        from bp.views import features_md
        self.assertIn("- ○ ⟨pattern screen⟩ `apps/web/src/screens/*.tsx` (0 on disk)", features_md.views(self.ctx())["FEATURES.md"])
        self.touch("apps/web/src/screens/A.tsx")
        plan, st = self.load()
        st["patterns"] = {"screen": {"instances": ["apps/web/src/screens/A.tsx"]}}
        self.assertIn("- ◑ ⟨pattern screen⟩ `apps/web/src/screens/*.tsx` (1 on disk)", features_md.views(self.ctx(state=st))["FEATURES.md"])

    def test_deterministic(self):
        self.assertEqual(self.md(), self.md())


# ----------------------------------------------------------------------------------------------- the map (JavaScript)

NODE = shutil.which("node")

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
function Node(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.style = {}; this.listeners = {}; this._text = ''; this.value = ''; this.className = ''; }
Node.prototype.appendChild = function (c) { this.children.push(c); return c; };
Node.prototype.setAttribute = function (k, v) { this.attrs[k] = v; };
Node.prototype.addEventListener = function (t, f) { this.listeners[t] = f; };
Object.defineProperty(Node.prototype, 'textContent', {
  get: function () { return this._text + this.children.map(function (c) { return c.textContent; }).join(' '); },
  set: function (v) { this._text = String(v); this.children = []; } });
global.document = { createElement: function (t) { return new Node(t); }, createTextNode: function (t) { var n = new Node('#text'); n._text = String(t); return n; } };
global.BP_TABS = []; global.BP_BRANCHES = [];
const args = JSON.parse(process.argv[2]);
vm.runInThisContext(fs.readFileSync(args.mindmap, 'utf8'));
vm.runInThisContext(fs.readFileSync(args.tab, 'utf8'));
function el(tag, a, kids) {
  var n = document.createElement(tag);
  if (a) for (var k in a) { if (k === 'text') n.textContent = a[k]; else if (k === 'cls') n.className = a[k]; else n.setAttribute(k, a[k]); }
  (kids || []).forEach(function (c) { if (c != null) n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
  return n;
}
const D = args.D, rows = D.rows || [], byPath = {}; rows.forEach(function (r) { byPath[r.path] = r; });
const H = { el: el, $: function () {}, glyph: function (s) { return el('span', { text: '[' + s + ']' }); }, rows: rows, byPath: byPath,
  labels: { planned: 'planned' }, colors: { planned: 'c' },
  fileLeaf: function (r) { return { label: r.path, row: r }; }, patLeaf: function (p) { return { label: '<' + p.glob + '>', pat: p }; } };
const out = { tabs: BP_TABS.map(function (t) { return t.id; }), branches: BP_BRANCHES.map(function (b) { return b.value; }) };
const tab = BP_TABS.filter(function (t) { return t.id === 'features'; })[0];
out.label = tab.label(D);
const host = el('section');
tab.render(host, D, H);
out.text = host.textContent;
const sel = (function find(n) { if (n.attrs && n.attrs.id === 'f-feat-status') return n; for (var c of n.children) { var r = find(c); if (r) return r; } return null; })(host);
if (sel && sel.listeners.change) { sel.value = 'blocked'; sel.listeners.change(); out.filtered = host.textContent; }
const mode = BP_BRANCHES.filter(function (b) { return b.value === 'feature'; })[0];
out.mode = mode.label;
out.build = JSON.parse(JSON.stringify(mode.build(D, H)));
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "node is not installed")
class MapScriptsTest(ProjectBase):
    @classmethod
    def setUpClass(cls):
        cls.harness = Path(tempfile.mkdtemp(prefix="bp-js-")) / "harness.js"
        cls.harness.write_text(HARNESS, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.harness.parent, ignore_errors=True)

    def js(self, D):
        a = {"D": D, "mindmap": str(S.SKILL / "assets" / "mindmap.js"), "tab": str(S.SKILL / "assets" / "tab-features.js")}
        r = subprocess.run([NODE, str(self.harness), json.dumps(a)], capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def payload(self, ctx=None):
        ctx = ctx or self.ctx()
        plan, st = self.load()
        data = {"rows": ctx["comp"]["rows"], "patterns": ctx["comp"]["patterns"], "glyph": bplan.GLYPH}
        data.update(json.loads(json.dumps(features.map_payload(ctx))))
        return data

    def test_syntax(self):
        for name in ("mindmap.js", "tab-features.js"):
            r = subprocess.run([NODE, "--check", str(S.SKILL / "assets" / name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_plan_without_features_draws_a_line_that_names_the_next_step(self):
        for D in ({"rows": []}, {"rows": [], "features": []}, {"rows": [], "features": None}, {}):
            out = self.js(D)
            self.assertEqual(out["tabs"], ["features"])
            self.assertEqual(out["branches"], ["feature"])
            self.assertEqual(out["label"], "Features")
            self.assertIn("No features yet", out["text"])
            self.assertIn("blueprint.py render", out["text"])
            self.assertEqual([n["label"] for n in out["build"]], ["No features yet"])

    def test_the_empty_state_uses_the_payloads_next_step(self):
        out = self.js({"rows": [], "features": [], "featureNext": "Run: blueprint.py migrate, then add features[]"})
        self.assertIn("Run: blueprint.py migrate", out["text"])
        self.assertEqual(out["build"][0]["title"], "Run: blueprint.py migrate, then add features[]")

    def test_the_tab_and_the_branch_mode(self):
        self.plan["patterns"].append({"id": "screen", "glob": "apps/web/src/screens/*.tsx", "module": "web", "phase": "P1",
                                      "purpose": "screens", "instance": "one screen", "features": ["F-inbox"]})
        self.write_plan(self.plan)
        D = self.payload()
        out = self.js(D)
        self.assertEqual(out["label"], "Features (2)")
        for needle in ("F-log-snag", "Log a snag", "A client records a snag", "crates/snag-api/src/main.rs", "Depends on: F-log-snag",
                       "Criteria (2)", "POST /snags with a title", "No feature has this status." if False else "Latest evaluation: none",
                       "features accepted"):
            self.assertIn(needle, out["text"])
        self.assertIn("No feature has this status.", out["filtered"])
        labels = [n["label"] for n in out["build"]]
        self.assertEqual(labels[:2], ["○ F-log-snag Log a snag · planned", "○ F-inbox Review snags in the inbox · planned"])
        self.assertIn("Infrastructure", labels)
        self.assertEqual([c["label"] for c in out["build"][0]["children"]], ["crates/snag-api/src/main.rs", "crates/snag-api/tests/snags.rs"])
        self.assertEqual([c["label"] for c in out["build"][1]["children"]][-1], "<apps/web/src/screens/*.tsx>")
        self.assertEqual(out["build"][labels.index("Infrastructure")]["children"][0]["label"], "Cargo.toml")

    def test_files_that_serve_no_feature_get_their_own_branch(self):
        D = self.payload()
        D["rows"].append(dict(D["rows"][0], path="loose.rs", features=[], infrastructure=False))
        labels = [n["label"] for n in self.js(D)["build"]]
        self.assertEqual(labels[-1], "No feature (1)")

    def test_a_feature_object_with_missing_fields_does_not_throw(self):
        D = self.payload()
        D["features"] = [{"id": "F-x", "title": "T", "status": "planned"}, {"id": "F-y", "status": "weird", "items": [{"path": "p"}],
                                                                              "criteria": [{"id": "C1"}]}]
        out = self.js(D)
        self.assertIn("F-x", out["text"])

    def test_the_inlined_map_has_the_tab_and_the_mode(self):
        plan, st = self.load()
        ctx = self.ctx()
        html = render_map(plan, st, ctx["comp"], ctx)
        self.assertIn("BP_TABS.push({\n  id: 'features'", html)
        self.assertIn("value: 'feature'", html)
        self.assertLess(html.index("value: 'feature'"), html.index("id: 'features'"))
        self.assertIn('"featureStates"', html)
        self.assertEqual(html.count("</script>"), html.count("<script"), "no script text closes a script early")


class MindMapSurfaceTest(unittest.TestCase):
    """The file is also inlined by other tooling: its public surface is still the one function, and the new mode only
    registers when the host declared BP_BRANCHES."""

    def test_surface(self):
        text = (S.SKILL / "assets" / "mindmap.js").read_text(encoding="utf-8")
        self.assertEqual(text.count("\nfunction mindMap(host, root, opts) {"), 1)
        self.assertTrue(text.startswith("/* app-blueprint mind map renderer."))
        self.assertIn("typeof BP_BRANCHES !== 'undefined'", text)
        for mode in ("'module'", "'owner'", "'phase'"):
            self.assertNotIn("value: " + mode, text, "built-in modes are not plug-ins")

    @unittest.skipUnless(NODE, "node is not installed")
    def test_inlined_without_the_template(self):
        script = ("const fs=require('fs'),vm=require('vm');vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));"
                  "console.log(typeof mindMap, typeof BP_BRANCHES);")
        r = subprocess.run([NODE, "-e", script, str(S.SKILL / "assets" / "mindmap.js")], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "function undefined"), r.stderr)


# ------------------------------------------------------------------------------------------ review round 1

def features_rows(feature, chk=None):
    """The criteria rows of the payload for one feature, with synthetic checks."""
    plan = {"features": [dict(feature, id="F-x", title="t", statement="s", kind="add", owner="o")], "files": [], "patterns": [],
            "roster": [{"id": "o"}], "owners": [], "milestones": []}
    ctx = {"plan": plan, "comp": {"rows": [], "patterns": []}, "checks": {"F-x": chk} if chk is not None else {}}
    features.context(Path("."), plan, {"files": {}, "patterns": {}}, ctx)
    return ctx["features"][0]["criteria"]


class ReviewValidationTest(Own, unittest.TestCase):
    def test_a_command_criterion_cannot_require_manual(self):
        p = v2_plan()
        feat(p, "F-log-snag")["criteria"][0]["level"] = "manual"
        found = findings(p, "criterion-level")
        self.assertEqual(len(found), 1)
        self.assertIn("criterion F-log-snag/C1: level manual needs an evaluation; remove verify or lower the level", found[0]["message"])
        self.assertEqual(validate.classify_error(found[0]["message"]), "criterion-level")
        self.assertTrue(found[0]["next"])
        p = v2_plan()  # inherited from the feature level
        feat(p, "F-log-snag")["level"] = "manual"  # C1 inherits it; C2 says unit itself
        self.assertEqual(len(findings(p, "criterion-level")), 1)
        feat(p, "F-log-snag")["criteria"][1].pop("level")
        self.assertEqual(len(findings(p, "criterion-level")), 2)
        p = v2_plan()  # a criterion without verify may require manual; the example's does
        self.assertEqual(findings(p, "criterion-level"), [])
        feat(p, "F-inbox")["criteria"][0]["verify"] = "   "  # a blank verify is no verify
        self.assertEqual(findings(p, "criterion-level"), [])
        feat(p, "F-inbox")["criteria"][0]["verify"] = "pnpm test"
        self.assertEqual(len(findings(p, "criterion-level")), 1)

    def test_an_id_that_is_the_old_placeholder_is_still_checked(self):
        p = v2_plan()
        feat(p, "F-inbox")["id"] = "?"
        self.assertIn("feature id '?' has the wrong form", messages(p, "bad-id")[0])

    def test_features_without_an_id_are_each_reported_and_do_not_hide_the_rest(self):
        p = v2_plan()
        a, b = copy.deepcopy(feat(p, "F-inbox")), copy.deepcopy(feat(p, "F-inbox"))
        del a["id"], b["id"]
        del feat(p, "F-log-snag")["kind"]  # a later feature's own problem is still found
        p["features"] = [a, b] + p["features"]
        self.assertEqual(len(findings(p, "missing-id")), 2)
        self.assertEqual(len(findings(p, "feature-incomplete")), 1)
        self.assertEqual(findings(p, "feature-no-files"), [], "no spurious 'no planned file' for an id-less feature")
        self.assertEqual(findings(p, "feature-no-criteria"), [])

    def test_source_study_has_the_study_id_form(self):
        p = v2_plan()
        feat(p, "F-inbox")["source"] = {"study": "nope", "behaviours": []}
        self.assertIn("study id 'nope' has the wrong form", messages(p, "bad-id")[0])
        self.assertEqual(validate.classify_error(messages(p, "bad-id")[0]), "bad-id")
        for good in ("S-acme", "S-" + "a" * 48):
            feat(p, "F-inbox")["source"] = {"study": good}
            self.assertEqual(findings(p, "bad-id"), [])
        feat(p, "F-inbox")["source"] = {"study": "S-" + "a" * 49}
        self.assertTrue(findings(p, "bad-id"))
        feat(p, "F-inbox")["source"] = {"behaviours": ["B-1"]}  # no study: nothing to check
        self.assertEqual(findings(p, "bad-id"), [])

    def test_every_message_classifies_to_its_own_code_and_none_is_other(self):
        import test_plan_fuzz as tf
        base = v2_plan()
        plans = []
        hostile = copy.deepcopy(base)
        hostile["features"][0]["id"] = "F-a b: needs title\n"
        hostile["features"][1]["id"] = "?"
        hostile["files"][1]["features"] = ["F-a b: needs title\n", "nope"]
        hostile["features"][0]["criteria"][0]["id"] = "C 1\n"
        plans.append(hostile)
        for path in tf.mutation_paths(base):
            for value in ("", None, "nope", [], "F-x y", 5, {}):
                plan = copy.deepcopy(base)
                tf.apply(plan, path, value)
                plans.append(plan)
        seen = set()
        mine = set(features.CODE_EXAMPLES) | {"missing-id", "duplicate-id"}
        for plan in plans:
            try:
                found = validate.validate_findings(plan) if plan.get("version") == 2 else []
            except (AttributeError, TypeError, KeyError):
                continue  # a plan that did not come through load_plan: rules.py stumbles first, not this module
            for f in found:
                if f["code"] not in mine or not f["entry"] or not str(f["entry"]).split(" ")[0] in ("feature", "criterion", "file", "pattern"):
                    continue
                seen.add(f["code"])
                self.assertNotEqual(validate.classify_error(f["message"]), "other", f["message"])
                self.assertEqual(validate.classify_error(f["message"]), f["code"], f["message"])
        self.assertGreater(len(seen), 8)

    def test_every_E_and_W_call_passes_its_code(self):
        import ast
        tree = ast.parse((S.SCRIPTS / "bp" / "features.py").read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("E", "W")]
        self.assertGreater(len(calls), 20)
        for n in calls:
            self.assertIn("code", [k.arg for k in n.keywords], f"line {n.lineno}: E/W without code=")


class ReviewDeriveTest(unittest.TestCase):
    def st(self, feature, its, chk, blocked=False):
        return features.derive_status(feature, its, chk, blocked)

    def accepted_fixture(self):
        f = F(criteria=[crit("C1"), crit("C2", verify=False)])
        chk = checks({"C1": entry(), "C2": entry(via="evaluation", level="e2e")}, True, evaluation({"C2": "pass"}))
        self.assertEqual(self.st(f, items(*V), chk), "accepted")
        return f, chk

    def test_m1_a_criterion_nobody_can_identify_keeps_the_feature_below_verified(self):
        f, chk = self.accepted_fixture()
        for bad in ({"scenario": "never evaluated"}, {"id": ""}, {"id": None}, {"id": 5}, "junk", None, {"id": "C2"}):
            with self.subTest(extra=bad):
                g = dict(f, criteria=f["criteria"] + [bad])
                self.assertEqual(self.st(g, items(*V), chk), "built")
        dup = F(criteria=[crit("C1"), crit("C1", verify=False)])
        self.assertEqual(self.st(dup, items(*V), checks({"C1": entry()}, True, evaluation({"C1": "pass"}))), "built")
        # the cap is "at most built": lower rungs are unchanged
        g = dict(f, criteria=f["criteria"] + [{"scenario": "x"}])
        self.assertEqual(self.st(g, items("ondisk", "built"), chk), "building")
        self.assertEqual(self.st(g, items("planned"), chk), "planned")

    def test_m1_the_payload_lists_such_a_criterion_with_a_question_mark_and_unknown(self):
        f = F(criteria=[crit("C1", verify=False), {"scenario": "no id", "action": "a", "expected": "e"},
                        crit("C1", verify=False), {"id": "", "scenario": "empty"}])
        rows = features_rows(f)
        self.assertEqual([r["id"] for r in rows], ["C1", "?", "C1", "?"])
        self.assertEqual([r["identified"] for r in rows], [False, False, False, False])
        self.assertEqual([r["result"] for r in rows], ["unknown"] * 4)
        self.assertTrue(all(r["checkText"] == "unknown" or not r["verify"] for r in rows))
        solo = features_rows(F(criteria=[crit("C1")]))[0]
        self.assertTrue(solo["identified"])

    def test_m2a_a_command_pass_needs_a_known_level_that_is_high_enough(self):
        f = F(level="integration")
        for level, want in (("unit", "built"), ("integration", "verified"), ("e2e", "verified"), (None, "built"), ("E2E", "built"),
                            ("bogus", "built"), ("manual", "built"), (5, "built")):
            with self.subTest(level=level):
                self.assertEqual(self.st(f, items(*V), checks({"C1": entry(level=level)})), want)
        # the criterion's own level and the verifyLevel cap (integration when absent, 2.9)
        e2e = F(criteria=[crit("C1", level="e2e")])
        self.assertEqual(self.st(e2e, items(*V), checks({"C1": entry(level="e2e")})), "built", "capped at integration")
        e2e_cap = F(criteria=[crit("C1", level="e2e", verifyLevel="e2e")])
        self.assertEqual(self.st(e2e_cap, items(*V), checks({"C1": entry(level="e2e")})), "verified")
        self.assertEqual(self.st(e2e_cap, items(*V), checks({"C1": entry(level="integration")})), "built")
        capped = F(criteria=[crit("C1", level="e2e", verifyLevel="integration")])
        self.assertEqual(self.st(capped, items(*V), checks({"C1": entry(level="e2e")})), "built")
        unit = F(level="unit", criteria=[crit("C1", verifyLevel="unit")])
        self.assertEqual(self.st(unit, items(*V), checks({"C1": entry(level="e2e")})), "verified")
        feature_e2e = F(level="e2e")  # no verifyLevel: the command counts as integration at most
        self.assertEqual(self.st(feature_e2e, items(*V), checks({"C1": entry(level="e2e")})), "built")
        bad_cap = F(criteria=[crit("C1", verifyLevel="huge")])
        self.assertEqual(self.st(bad_cap, items(*V), checks({"C1": entry(level="e2e")})), "built")
        # a command never satisfies manual
        manual = F(level="manual", criteria=[crit("C1", verifyLevel="e2e")])
        self.assertEqual(self.st(manual, items(*V), checks({"C1": entry(level="e2e")})), "built")
        self.assertEqual(self.st(manual, items(*V), checks({"C1": entry(level="e2e")}, True, evaluation({"C1": "pass"}))), "built")
        # with an evaluation present the same level rule gives the same answer
        self.assertEqual(self.st(f, items(*V), checks({"C1": entry(level="unit")}, True, evaluation({"C1": "pass"}))), "built")

    def test_m2b_stale_is_fresh_only_when_it_is_false(self):
        for stale, want in ((False, "verified"), (True, "built"), ("true", "built"), (1, "built"), (None, "built"), ("false", "built"),
                            (0, "built"), ([], "built")):
            with self.subTest(stale=stale):
                self.assertEqual(self.st(F(), items(*V), checks({"C1": entry(stale=stale)})), want)
        e = entry()
        del e["stale"]
        self.assertEqual(self.st(F(), items(*V), checks({"C1": e})), "built", "a missing stale key is stale")
        f = F(criteria=[crit("C1", verify=False)])
        for stale in ("true", 1, None):
            chk = checks({"C1": entry(via="evaluation", level="e2e", stale=stale)}, True, evaluation({"C1": "pass"}))
            self.assertEqual(self.st(f, items(*V), chk), "verified")

    def test_m2b_the_payload_reports_the_same_staleness(self):
        for stale, want in ((False, False), (True, True), ("true", True), (1, True), (None, True)):
            row = features_rows(F(criteria=[crit("C1")]), checks({"C1": entry(stale=stale)}))[0]
            self.assertIs(row["stale"], want, stale)
        self.assertIs(features_rows(F(), checks({}))[0]["stale"], False, "no record at all is not stale, it is unknown")

    def test_m2c_only_the_exact_string_pass_is_a_pass(self):
        for r in ("PASS", " pass", "pass ", "Pass", True, 1, None, ["pass"], {"pass": 1}):
            with self.subTest(result=r):
                self.assertEqual(self.st(F(), items(*V), checks({"C1": entry(r)})), "built")
                self.assertEqual(features_rows(F(), checks({"C1": entry(r)}))[0]["result"], "unknown")
        self.assertEqual(self.st(F(), items(*V), checks({"C1": entry("pass")})), "verified")


class ReviewViewTest(ProjectBase):
    def md(self, plan=None, chk=None, blocked=None):
        if plan is not None:
            self.write_plan(plan)
        from bp.views import features_md
        return features_md.views(self.ctx(checks=chk if chk is not None else {}, blocked=blocked))["FEATURES.md"]

    def test_hostile_values_stay_on_one_line_and_inert(self):
        p = v2_plan()
        f = feat(p, "F-log-snag")
        evil = "x\n## injected\n| a | b |\n<img src=x onerror=1> & more"
        f.update(title=evil, statement=evil, kind="add\n## kind", owner="backend-lead\n## owner", milestone="M1\n## ms",
                 dependsOn=["F-x\n## dep"], source={"study": "S-a\n## study", "behaviours": []})
        f["criteria"][0].update(scenario=evil, action=evil, expected=evil, mustNot=evil)
        file_(p, "crates/snag-api/src/main.rs")["path"] = "crates/snag`api\n## path.rs"
        file_(p, "crates/snag-api/tests/snags.rs")["dependsOn"] = []
        blocked = {"phases": set(), "milestones": set(), "features": {"F-log-snag"}, "by": {"feature:F-log-snag": ["U-a\n## unk"]}}
        text = self.md(p, {}, blocked)
        for line in text.splitlines():
            for bad in ("## injected", "## kind", "## owner", "## ms", "## dep", "## study", "## path", "## unk", "| a | b"):
                self.assertFalse(line.startswith(bad), line)
        self.assertNotIn("<img", text)
        self.assertIn("&lt;img src=x onerror=1>", text)
        self.assertIn("&amp; more", text)
        self.assertEqual(len([l for l in text.splitlines() if l.startswith("## ")]), 2, "only the two feature headings")
        self.assertIn("``crates/snag`api ## path.rs``", text)

    def test_a_pipe_in_a_cell_is_escaped(self):
        p = v2_plan()
        feat(p, "F-log-snag")["criteria"][0]["expected"] = "a | b"
        text = self.md(p)
        self.assertIn("| a \\| b |", text)
        self.assertNotIn("| a | b |", text)

    def test_a_statement_cannot_start_a_heading(self):
        p = v2_plan()
        feat(p, "F-log-snag")["statement"] = "# not a heading"
        self.assertFalse(any(l.startswith("# not a heading") for l in self.md(p).splitlines()))

    def test_the_check_column_shows_pass_only_for_a_command_result(self):
        chk = {"F-log-snag": checks({"C1": entry(via="evaluation"), "C2": entry(level="unit")})}
        text = self.md(v2_plan(), chk)
        row1 = next(l for l in text.splitlines() if l.startswith("| C1 | The API runs"))
        self.assertIn("| unknown |", row1)
        row2 = next(l for l in text.splitlines() if l.startswith("| C2 |"))
        self.assertIn("| pass (unit) |", row2)

    def test_the_evaluation_column_honours_via(self):
        p = v2_plan()
        chk = {"F-inbox": checks({"C1": entry(via="command", level="e2e")}, True, evaluation({}))}
        row = next(l for l in self.md(p, chk).splitlines() if l.startswith("| C1 | Two snags"))
        self.assertTrue(row.endswith("| manual | - | unknown |"), row)
        chk = {"F-inbox": checks({"C1": entry(via="evaluation", level="e2e")}, True, evaluation({"C1": "pass"}))}
        row = next(l for l in self.md(p, chk).splitlines() if l.startswith("| C1 | Two snags"))
        self.assertTrue(row.endswith("| manual | - | pass (e2e) |"), row)

    def test_a_pass_below_the_required_level_says_why(self):
        p = v2_plan()
        feat(p, "F-inbox")["level"] = "e2e"
        feat(p, "F-inbox")["criteria"][0]["level"] = "e2e"
        chk = {"F-inbox": checks({"C1": entry("unknown", via=None, level=None)}, True, evaluation({"C1": "pass"}, level="unit"))}
        self.assertIn("| e2e | - | below level (unit &lt; e2e) |", self.md(p, chk))  # "<" is escaped in Markdown
        card = "\n".join(features.status_card(self.ctx(checks=chk), "F-inbox"))
        self.assertIn("Evaluation: below level (unit < e2e)", card)
        chk = {"F-inbox": checks({"C1": entry("pass", via="evaluation", level="unit")}, True, evaluation({"C1": "pass"}, level="unit"))}
        self.assertIn("below level (unit &lt; e2e)", self.md(p, chk))

    def test_the_card_and_md_show_unidentifiable_criteria(self):
        p = v2_plan()
        feat(p, "F-inbox")["criteria"].append({"scenario": "orphan", "action": "a", "expected": "e"})
        self.assertIn("| ? | orphan | a | e |", self.md(p))
        card = "\n".join(features.status_card(self.ctx(), "F-inbox"))
        self.assertIn("    ? [e2e] unknown", card)

    def test_pattern_glyph_shows_blocked_in_preference_and_the_lowest_otherwise(self):
        self.plan["patterns"].append({"id": "screen", "glob": "apps/web/src/screens/*.tsx", "module": "web", "phase": "P1",
                                      "purpose": "screens", "instance": "one screen", "features": ["F-inbox"]})
        self.write_plan(self.plan)
        for rel in "ABC":
            self.touch(f"apps/web/src/screens/{rel}.tsx")
        plan, st = self.load()
        # 0.tsx is recorded but not on disk (planned) and sorts first: only the blocked preference can show the triangle
        st["patterns"] = {"screen": {"instances": [f"apps/web/src/screens/{x}.tsx" for x in "0ABC"]}}
        st["files"]["apps/web/src/screens/A.tsx"] = {"state": "built"}
        st["files"]["apps/web/src/screens/B.tsx"] = {"state": "blocked"}
        from bp.views import features_md
        self.assertIn("- ▲ ⟨pattern screen⟩", features_md.views(self.ctx(state=st))["FEATURES.md"])
        st["files"]["apps/web/src/screens/B.tsx"] = {"state": "built"}
        self.assertIn("- ○ ⟨pattern screen⟩", features_md.views(self.ctx(state=st))["FEATURES.md"],
                      "0 is only planned: the lowest wins, not the highest")


class ReviewHookTest(ProjectBase):
    def test_may_accept_excludes_the_feature_owner_even_when_the_owner_owns_no_item(self):
        p = v2_plan()
        p["roster"].append({"id": "lead", "kind": "agent"})
        feat(p, "F-inbox")["owner"] = "lead"  # lead owns no file: only the owner rule excludes it
        self.write_plan(p)
        who = dict(features.roster_sections(self.ctx()))["Who may accept what"].splitlines()[-1]
        self.assertEqual(who, "| F-inbox | cto-architect, backend-lead, evaluator, owner |")

    def test_may_accept_excludes_the_owners_of_items_who_are_not_the_feature_owner(self):
        p = v2_plan()
        feat(p, "F-inbox")["owner"] = "cto-architect"  # frontend-engineer owns the items, the owner owns none of them
        self.write_plan(p)
        who = dict(features.roster_sections(self.ctx()))["Who may accept what"].splitlines()[-1]
        self.assertEqual(who, "| F-inbox | backend-lead, evaluator, owner |")

    def test_file_tags_are_empty_when_the_plan_has_no_features(self):
        p = v2_plan()
        p["features"], p["milestones"], p["tasks"], p["unknowns"] = [], [], [], []
        for f in p["files"]:
            f.pop("features", None)
        file_(p, "crates/snag-api/src/main.rs")["infrastructure"] = True
        self.write_plan(p)
        self.assertEqual(features.file_tags(self.ctx()), {})

    def test_status_features_prints_the_blocked_by_line(self):
        base = {"phases": {"P2"}, "milestones": set(), "features": set(), "by": {"phase:P2": ["U-offline"]}}
        ctx = self.ctx(checks={}, blocked=base)
        lines = features.status_lines(ctx, type("A", (), {"features": True})())
        self.assertIn("        blocked by U-offline", lines)
        self.assertNotIn("        blocked by U-offline", features.status_lines(ctx, type("A", (), {"features": False})()))

    def test_a_planned_file_with_no_row_counts_as_planned(self):
        plan, st = self.load()
        got = features.feature_items(self.root, plan, st, {"rows": [], "patterns": []}, "F-log-snag")
        self.assertEqual([i["status"] for i in got], ["planned", "planned"])
        rows = [{"path": "crates/snag-api/src/main.rs", "status": "nonsense"}]
        got = features.feature_items(self.root, plan, st, {"rows": rows, "patterns": []}, "F-log-snag")
        self.assertEqual([i["status"] for i in got], ["planned", "planned"])

    def test_the_payload_does_not_alias_the_plan(self):
        p = v2_plan()
        feat(p, "F-inbox")["source"] = {"study": "S-a", "behaviours": ["B-1"]}
        self.write_plan(p)
        plan, st = self.load()
        ctx = build_context(self.root, plan, st)
        src = next(f for f in ctx["features"] if f["id"] == "F-inbox")["source"]
        self.assertEqual(src, {"study": "S-a", "behaviours": ["B-1"]})
        self.assertIsNot(src, feat(plan, "F-inbox")["source"])
        self.assertIsNot(src["behaviours"], feat(plan, "F-inbox")["source"]["behaviours"])
        src["study"] = "changed"
        src["behaviours"].append("B-2")
        self.assertEqual(feat(plan, "F-inbox")["source"], {"study": "S-a", "behaviours": ["B-1"]})

    def test_the_empty_state_names_the_next_step(self):
        self.assertIn("render", features.map_payload(self.ctx())["featureNext"])
        self.write_plan(v1_plan())
        nxt = features.map_payload(self.ctx())["featureNext"]
        self.assertIn("migrate", nxt)
        self.assertNotIn("render", nxt)

    def test_status_json_with_feature_is_the_cores_business(self):
        """`status --json --feature F` honours the selector as the text form does (core, PR #16): an unknown id is the
        unknown-feature failure envelope (exit 1) with this module's wording; a known id lists only that feature."""
        code, out, err = self.run_("status", "--json", "--feature", "F-ghost")
        self.assertEqual(code, 1, err)
        env = json.loads(out)
        self.assertEqual((env["ok"], env["error"]["code"], env["error"]["message"]),
                         (False, "unknown-feature", "feature F-ghost is not in the plan"))
        code, out, err = self.run_("status", "--json", "--feature", "F-inbox")
        self.assertEqual(code, 0, err)
        self.assertEqual([f["id"] for f in json.loads(out)["features"]], ["F-inbox"])


class PerformanceTest(TempRoot):
    def test_thousands_of_features_are_linear(self):
        import time
        n = 3000
        plan = v2_plan()
        plan["milestones"], plan["tasks"], plan["unknowns"] = [], [], []
        plan["features"] = [{"id": f"F-{i}", "title": "t", "statement": "s", "kind": "add", "owner": "backend-lead",
                             "criteria": [{"id": "C1", "scenario": "s", "action": "a", "expected": "e", "verify": "true"}]}
                            for i in range(n)]
        plan["files"] = [{"path": f"src/f{i}.rs", "action": "create", "module": "api", "phase": "P1", "purpose": "p",
                          "features": [f"F-{i}"]} for i in range(n)]
        self.write_plan(plan)
        loaded = quiet(bplan.load_plan, self.root)[0]
        st = bplan.load_state(self.root)
        comp = status.compute(self.root, loaded, st)
        ctx = {"root": self.root, "plan": loaded, "state": st, "comp": comp, "checks": {},
               "blocked": {"phases": set(), "milestones": set(), "features": set(), "by": {}}, "features": []}
        started = time.time()
        features.context(self.root, loaded, st, ctx)
        took = time.time() - started
        self.assertEqual(len(ctx["features"]), n)
        self.assertLess(took, 2.0, f"features.context took {took:.1f}s for {n} features")
        started = time.time()
        for hook in (features.file_tags, features.roster_sections, features.map_payload, features.status_json):
            hook(ctx)
        from bp.views import features_md
        features_md.views(ctx)
        self.assertLess(time.time() - started, 2.0)


class WholeValueTest(Own, unittest.TestCase):
    """Every id, level and kind matches the whole string: a trailing newline, a carriage return or a leading space is
    never accepted (re.fullmatch, not re.match with $)."""
    BAD = ("{}\n", "{}\r\n", " {}", "{} ", "{}\r")

    def test_feature_ids(self):
        for tpl in self.BAD:
            p = v2_plan()
            bad = tpl.format("F-inbox")
            feat(p, "F-inbox")["id"] = bad
            for f in p["files"]:
                f["features"] = [bad if x == "F-inbox" else x for x in f.get("features", [])] or None
                if f["features"] is None:
                    del f["features"]
            self.assertTrue(any("feature id" in m for m in messages(p, "bad-id")), repr(bad))

    def test_criterion_ids(self):
        for tpl in self.BAD:
            p = v2_plan()
            feat(p, "F-inbox")["criteria"][0]["id"] = tpl.format("C1")
            self.assertTrue(any("criterion id" in m for m in messages(p, "bad-id")), repr(tpl))

    def test_study_ids(self):
        for tpl in self.BAD:
            p = v2_plan()
            feat(p, "F-inbox")["source"] = {"study": tpl.format("S-acme")}
            self.assertTrue(any("study id" in m for m in messages(p, "bad-id")), repr(tpl))

    def test_levels_and_kinds(self):
        for tpl in self.BAD:
            p = v2_plan()
            feat(p, "F-inbox")["level"] = tpl.format("e2e")
            self.assertTrue(findings(p, "criterion-level"), repr(tpl))
            p = v2_plan()
            feat(p, "F-inbox")["criteria"][0]["level"] = tpl.format("unit")
            self.assertTrue(findings(p, "criterion-level"), repr(tpl))
            p = v2_plan()
            feat(p, "F-log-snag")["criteria"][0]["verifyLevel"] = tpl.format("e2e")
            self.assertTrue(findings(p, "criterion-level"), repr(tpl))
            p = v2_plan()
            feat(p, "F-inbox")["kind"] = tpl.format("add")
            self.assertTrue(findings(p, "feature-kind"), repr(tpl))

    def test_the_tab_script_has_no_id_patterns_of_its_own(self):
        text = (S.SKILL / "assets" / "tab-features.js").read_text(encoding="utf-8")
        self.assertNotIn("test(", text)
        self.assertNotIn(".match(", text)


class DuplicateRosterIdTest(ProjectBase):
    def test_a_roster_id_listed_twice_is_one_id_and_never_gains_a_property(self):
        p = v2_plan()
        p["roster"].append({"id": "evaluator", "kind": "human"})  # same id, a conflicting entry
        p["roster"].append({"id": "backend-lead", "kind": "human"})
        self.write_plan(p)
        sections = dict(features.roster_sections(self.ctx()))
        who = sections["Who may accept what"].splitlines()[-2:]
        self.assertEqual(who, ["| F-log-snag | cto-architect, frontend-engineer, evaluator, owner |",
                               "| F-inbox | cto-architect, backend-lead, evaluator, owner |"])
        self.assertEqual(sections["Features owned"].splitlines()[2:],
                         ["| backend-lead | F-log-snag |", "| frontend-engineer | F-inbox |"])
        self.assertEqual(features.roster_ids(p), ["cto-architect", "backend-lead", "frontend-engineer", "evaluator", "owner"])


class IndependenceTest(unittest.TestCase):
    """One rule for who may accept (4.4): the ROSTER.md table and `accept` both call it."""

    def setUp(self):
        self.plan = v2_plan()
        self.feature = self.plan["features"][0]  # F-log-snag: owner backend-lead, files under crates/ (backend-lead)
        self.items = [{"path": "crates/snag-api/src/main.rs"}, {"path": "crates/snag-api/tests/snags.rs"}]

    def who(self, state=None, plan=None):
        return features.independent_ids(plan or self.plan, self.feature, self.items, None, state)

    def test_owner_file_owner_and_claimer_are_not_independent(self):
        self.assertEqual(self.who(), ["cto-architect", "frontend-engineer", "evaluator", "owner"])
        self.plan["owners"].append({"pattern": "crates/snag-api/tests/snags.rs", "owner": "frontend-engineer"})
        self.assertNotIn("frontend-engineer", self.who())
        state = {"files": {"crates/snag-api/src/main.rs": {"state": "claimed", "by": "evaluator"}}}
        self.assertNotIn("evaluator", self.who(state))
        self.assertIn("evaluator", self.who({"files": {"other.txt": {"by": "evaluator"}}}))

    def test_damaged_state_is_read_as_no_claims(self):
        for state in (None, {}, {"files": None}, {"files": []}, {"files": {"crates/snag-api/src/main.rs": None}}, "x"):
            self.assertIn("evaluator", self.who(state))

    def test_spellings_that_fold_together_are_one_person_and_only_refuse(self):
        self.feature["owner"] = "Backend-Lead "
        self.assertNotIn("backend-lead", self.who())
        for by in ("evaluator ", "Evaluator", "", " ", None, 5):
            self.assertIsNotNone(features.independence_problem(self.plan, self.feature, self.items, by), repr(by))

    def test_a_blank_roster_id_is_nobody(self):
        self.plan["roster"].append({"id": " ", "kind": "agent"})
        self.assertNotIn(" ", self.who())


class ReviewRound2Test(ProjectBase):
    def test_r2_1_a_verify_level_that_is_not_a_command_level_never_passes(self):
        for vl in ("manual", "E2E", "", " unit", "unit\n", 5, ["unit"], True):
            with self.subTest(verifyLevel=vl):
                f = F(level="unit", criteria=[crit("C1", verifyLevel=vl)])
                self.assertEqual(features.derive_status(f, items(*V), checks({"C1": entry(level="e2e")}), False), "built")
                self.assertFalse(features_rows(f, checks({"C1": entry(level="e2e")}))[0]["counted"])
        for vl in ("unit", "integration", "e2e"):
            f = F(level="unit", criteria=[crit("C1", verifyLevel=vl)])
            self.assertEqual(features.derive_status(f, items(*V), checks({"C1": entry(level="e2e")}), False), "verified")

    def test_r2_2_the_kind_line_is_one_escaped_line(self):
        p = v2_plan()
        f = feat(p, "F-log-snag")
        f.update(kind="# kind", milestone="M1", owner="backend-lead")
        from bp.views import features_md
        self.write_plan(p)
        text = features_md.views(self.ctx(checks={}))["FEATURES.md"]
        self.assertFalse(any(l.startswith("# kind") for l in text.splitlines()))
        self.assertIn("\\# kind · M1 · backend-lead · integration", text)
        f.update(kind="<b>x</b>\n## h", owner="a\n# b")
        self.write_plan(p)
        text = features_md.views(self.ctx(checks={}))["FEATURES.md"]
        self.assertIn("&lt;b>x&lt;/b> ## h · M1 · a # b · integration", text)
        self.assertEqual(len([l for l in text.splitlines() if l.startswith("## ")]), 2)

    def test_r2_3_a_backslash_is_escaped_before_the_pipe(self):
        p = v2_plan()
        feat(p, "F-log-snag")["criteria"][0]["expected"] = "a\\|b"  # plan text: a\|b
        self.write_plan(p)
        from bp.views import features_md
        text = features_md.views(self.ctx(checks={}))["FEATURES.md"]
        row = next(l for l in text.splitlines() if l.startswith("| C1 | The API runs"))
        self.assertIn("| a\\\\\\|b |", row)  # a, escaped backslash, escaped pipe, b
        other = next(l for l in text.splitlines() if l.startswith("| C2 |"))
        cells = lambda r: len(r.replace("\\\\", "").replace("\\|", "").split("|"))
        self.assertEqual(cells(row), cells(other), "the row has as many cells as any other row")

    def test_r2_5_repeat_in_the_payload_is_a_copy(self):
        chk = {"F-log-snag": checks({"C1": entry(repeat={"n": 3, "passed": 3}), "C2": entry(level="unit")})}
        ctx = self.ctx(checks=chk)
        row = next(f for f in ctx["features"] if f["id"] == "F-log-snag")["criteria"][0]
        self.assertEqual(row["repeat"], {"n": 3, "passed": 3})
        row["repeat"]["n"] = 99
        self.assertEqual(chk["F-log-snag"]["criteria"]["C1"]["repeat"], {"n": 3, "passed": 3})


class NoMachineSpecificTextTest(ProjectBase):
    """Files written into a project are committed and shared: no absolute path, no temp folder, no launcher path."""

    def render(self):
        self.assertEqual(self.run_("render")[0], 0)
        out = {n: (self.root / ".blueprint" / n).read_text(encoding="utf-8") for n in ("map.html", "FEATURES.md")
               if (self.root / ".blueprint" / n).exists()}  # a plan without features has no FEATURES.md
        data = re.search(r'<script id="bp" type="application/json">(.*?)</script>', out["map.html"], re.S).group(1)
        return out, json.loads(data.replace("<\\/", "</"))

    def assertNoLeak(self, text, what):
        root = str(self.root)
        for spelling in {root, root.replace("\\", "/"), root.lower(), root.replace("\\", "/").lower(), root.replace("\\", "\\\\")}:
            self.assertNotIn(spelling, text, what)
        self.assertNotIn(str(S.SKILL), text, what)
        self.assertNotIn(str(S.SKILL).replace("\\", "/"), text, what)
        self.assertIsNone(re.search(r"(?<![A-Za-z])[A-Za-z]:[\\/](?!/)", text), what + ": a drive-letter path")
        self.assertIsNone(re.search(r"(?:^|[\s`\"'(])/(?:home|Users|tmp|var|usr|mnt|private|root|etc|opt|Volumes)/", text, re.M),
                          what + ": a leading-slash path")

    def test_a_project_without_a_copy_leaks_nothing(self):
        self.assertFalse((self.root / ".blueprint" / "bin").exists())
        for plan in (v2_plan(), v1_plan(), dict(v2_plan(), features=[], milestones=[], tasks=[], unknowns=[])):
            if "features" in plan and not plan.get("features"):
                for f in plan["files"]:
                    f.pop("features", None)
            self.write_plan(plan)
            out, payload = self.render()
            for name, text in out.items():
                self.assertNoLeak(text, name)
            self.assertNoLeak(json.dumps(payload, ensure_ascii=False), "payload")
            self.assertIn("<skill>/scripts/blueprint.py", payload["featureNext"])
            self.assertIn("--root .", payload["featureNext"])
            self.assertNotIn(".blueprint/bin", payload["featureNext"])

    def test_a_project_with_a_copy_names_the_copy(self):
        (self.root / ".blueprint" / "bin").mkdir()
        (self.root / ".blueprint" / "bin" / "blueprint.py").write_text("# copy\n", encoding="utf-8")
        for plan, verb in ((v2_plan(), "render"), (v1_plan(), "migrate")):
            self.write_plan(plan)
            ctx = self.ctx()
            nxt = features.map_payload(ctx)["featureNext"]
            self.assertIn("python .blueprint/bin/blueprint.py " + (verb if plan["version"] == 1 else "render"), nxt)
            self.assertNotIn("<skill>", nxt)
            self.assertNoLeak(nxt, "featureNext")

    def test_the_tab_and_the_branch_mode_carry_no_launcher_path(self):
        for name in ("tab-features.js", "mindmap.js"):
            text = (S.SKILL / "assets" / name).read_text(encoding="utf-8")
            self.assertNoLeak(text, name)


class RosterSectionSafetyTest(ProjectBase):
    def test_cells_escape_backslash_then_pipe_and_write_entities(self):
        p = v2_plan()
        evil = "a\\|b <i>&\n## x"
        p["roster"].append({"id": evil, "kind": "agent"})
        feat(p, "F-inbox")["owner"] = evil
        self.write_plan(p)
        sections = dict(features.roster_sections(self.ctx()))
        cell = "a\\\\\\|b &lt;i>&amp; ## x"  # backslash doubled, pipe escaped, < and & as entities, one line
        owned = sections["Features owned"].splitlines()
        self.assertIn(f"| {cell} | F-inbox |", owned)
        accept = sections["Who may accept what"].splitlines()
        self.assertTrue(any(line.startswith("| F-log-snag |") and cell in line for line in accept), accept)
        for body in sections.values():
            self.assertNotIn("<i>", body)
            self.assertFalse(any(line.startswith("##") for line in body.splitlines()))
        for line in owned[2:]:  # every table row has the same number of unescaped cells
            self.assertEqual(len(line.replace("\\\\", "").replace("\\|", "").split("|")), 4, line)

    def test_a_hostile_feature_id_in_the_may_accept_table(self):
        p = v2_plan()
        p["features"][0]["id"] = "F-a|b\\<c&"
        for f in p["files"]:
            f["features"] = [("F-a|b\\<c&" if x == "F-log-snag" else x) for x in f.get("features", [])] or None
            if f["features"] is None:
                del f["features"]
        self.write_plan(p)
        row = dict(features.roster_sections(self.ctx()))["Who may accept what"].splitlines()[4]
        self.assertTrue(row.startswith("| F-a\\|b\\\\&lt;c&amp; |"), row)


class SiblingsTest(TempRoot):
    """What depends on a sibling module, with both variants stated: without the unknowns module nothing is blocked; with
    it (a stand-in here, the real module in the third test) the example's F-inbox is blocked by U-offline, because its
    file offline.ts is in phase P2."""
    UNKNOWNS = '''
def context(root, plan, state, ctx):
    ctx["blocked"] = {"phases": {"P2"}, "milestones": set(), "features": set(), "by": {"phase:P2": ["U-offline"]}}
'''

    def statuses(self, files=None, hermetic=True):
        self.write_plan(v2_plan())
        plan = quiet(bplan.load_plan, self.root)[0]
        with extension(files if files is not None else own_modules(), hermetic=hermetic):
            ctx = build_context(self.root, plan, bplan.load_state(self.root))
        return {f["id"]: (f["status"], f["blockedBy"]) for f in ctx["features"]}

    def test_without_unknowns_nothing_is_blocked(self):
        self.assertEqual(self.statuses(), {"F-log-snag": ("planned", []), "F-inbox": ("planned", [])})

    def test_with_unknowns_the_feature_with_a_file_in_the_blocked_phase_is_blocked(self):
        got = self.statuses(dict(own_modules(), **{"unknowns.py": self.UNKNOWNS}))
        self.assertEqual(got, {"F-log-snag": ("planned", []), "F-inbox": ("blocked", ["U-offline"])})

    def test_in_the_real_tree_it_depends_on_whether_unknowns_exists(self):
        got = self.statuses({}, hermetic=False)
        if registry.exists("unknowns"):
            self.assertEqual(got["F-inbox"], ("blocked", ["U-offline"]))
        else:
            self.assertEqual(got["F-inbox"], ("planned", []))
        self.assertEqual(got["F-log-snag"], ("planned", []))


if __name__ == "__main__":
    unittest.main()
