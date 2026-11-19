"""W1-U: unknowns[] (contract 2.6): validation codes, blocking, the claim refusal, the status --next skip, status and
the Unknowns tab of map.html.

Every validation code has a case that triggers it and the nearest valid plan that does not; every blocking rule has a
test that fails when the rule is removed. Commands run through the real launcher (subprocess).
"""
import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import support as S

from bp import registry, unknowns, validate
from bp import plan as bplan
from bp.views.context import blocked_by, build_context

from test_validate import TempRoot, quiet, v1_plan, v2_plan

OFFLINE = "apps/web/src/offline.ts"  # the only file of phase P2 in the example
EXPECTED_MESSAGE = ("apps/web/src/offline.ts is in phase P2, blocked by unknown U-offline (high, investigating). "
                    "Resolve it first, or use --force if the lead says so.")


def unknown(**kw):
    u = {"id": "U-x", "question": "Is it so?", "severity": "high", "status": "open", "blocks": ["P2"]}
    u.update(kw)
    return {k: v for k, v in u.items() if v is not DROP}


DROP = object()  # unknown(severity=DROP) leaves the key out


def with_unknowns(*items, **extra):
    plan = v2_plan()
    plan["unknowns"] = list(items)
    plan.update(extra)
    return plan


def findings(plan, **kw):
    return validate.validate_findings(plan, **kw)


def mine(found):
    return [f for f in found if f["code"].startswith("unknown-") or f["code"] == "decision-unknown"]


def codes_of(plan, level=None, **kw):
    return [f["code"] for f in mine(findings(plan, **kw)) if level in (None, f["level"])]


def decision(did="D-offline", **kw):
    return dict({"id": did, "title": "Decided", "decision": "Yes", "why": "Measured"}, **kw)


class ModuleTest(unittest.TestCase):
    def test_hooks_are_registered_and_codes_are_owned_once(self):
        self.assertIn("bp.unknowns", [m.__name__ for m in registry.modules("DOMAIN")])
        got = registry.codes()  # raises RuntimeError on a duplicate owner or an example another regex steals
        mine_ = {c for _, c in unknowns.CODES}
        self.assertEqual(mine_, set(unknowns.CODE_EXAMPLES))
        self.assertEqual(mine_, {"unknown-incomplete", "unknown-severity", "unknown-status", "unknown-blocks",
                                 "unknown-experiment", "unknown-resolved-no-decision", "unknown-dropped-no-note",
                                 "unknown-evidence", "unknown-open-blocker", "decision-unknown"})
        for code, example in unknowns.CODE_EXAMPLES.items():
            self.assertEqual(validate.classify_error(example), code)
        self.assertEqual([c for _, c in got if c in mine_], [c for _, c in unknowns.CODES])

    def test_python_3_10_grammar(self):
        import ast
        src = Path(unknowns.__file__).read_text(encoding="utf-8")
        ast.parse(src, feature_version=(3, 10))


class ValidationTest(unittest.TestCase):
    """Each code: the plan that triggers it, the nearest valid plan that does not, and the classify check."""

    def assert_code(self, plan, code, level="error", **kw):
        found = [f for f in findings(plan, **kw) if f["code"] == code]
        self.assertTrue(found, f"{code} not reported: {[f['message'] for f in findings(plan, **kw)]}")
        for f in found:
            self.assertEqual(f["level"], level)
            self.assertEqual(validate.classify_error(f["message"]), code, f["message"])
            self.assertTrue(f["next"], f"{code} says nothing about what to do next")
            self.assertTrue(f["entry"])
        return found

    def assert_clean(self, plan, code, **kw):
        self.assertNotIn(code, codes_of(plan, **kw))

    def test_the_example_is_valid_and_warns_about_its_blocker(self):
        found = findings(v2_plan())
        self.assertEqual([f["level"] for f in found], ["warning"])
        self.assertEqual(found[0]["code"], "unknown-open-blocker")
        self.assertEqual(found[0]["message"], "unknown U-offline (high) is open and blocks P2; claim and status "
                                              "--next skip them until it is resolved")

    def test_unknown_incomplete(self):
        for missing in ("question", "severity", "status"):
            with self.subTest(missing=missing):
                self.assert_code(with_unknowns(unknown(**{missing: DROP})), "unknown-incomplete")
        self.assert_code(with_unknowns(unknown(question="   ")), "unknown-incomplete")
        self.assert_code(with_unknowns(unknown(severity="")), "unknown-incomplete")
        self.assert_clean(with_unknowns(unknown()), "unknown-incomplete")

    def test_unknown_severity(self):
        for bad in ("huge", "High", 5, ["high"]):
            with self.subTest(bad=bad):
                self.assert_code(with_unknowns(unknown(severity=bad)), "unknown-severity")
        for ok in ("low", "medium", "high", "critical"):
            self.assert_clean(with_unknowns(unknown(severity=ok)), "unknown-severity")
        self.assertNotIn("unknown-severity", codes_of(with_unknowns(unknown(severity=DROP))), "missing is incomplete")

    def test_unknown_status(self):
        for bad in ("done", "Open", 0, {}):
            with self.subTest(bad=bad):
                self.assert_code(with_unknowns(unknown(status=bad)), "unknown-status")
        for ok in ("open", "investigating", "resolved", "dropped"):
            plan = with_unknowns(unknown(status=ok, decision="D-offline", notes="n",
                                         experiment={"plan": "p", "timebox": "2d", "exit": "e"}),
                                 decisions=[decision()])
            self.assert_clean(plan, "unknown-status")

    def test_unknown_blocks(self):
        found = self.assert_code(with_unknowns(unknown(blocks=["P9"])), "unknown-blocks")
        self.assertIn("blocks 'P9', which is no phase, milestone or feature", found[0]["message"])
        self.assert_code(with_unknowns(unknown(blocks=[5])), "unknown-blocks")
        self.assert_code(with_unknowns(unknown(blocks=["phase:M1"])), "unknown-blocks")  # no such phase
        for ok in (["P1", "P2"], ["phase:P2"], ["M1"], ["milestone:M1"], ["F-inbox"], ["feature:F-inbox"], []):
            with self.subTest(ok=ok):
                self.assert_clean(with_unknowns(unknown(blocks=ok)), "unknown-blocks")
        self.assert_clean(with_unknowns(unknown(blocks=DROP)), "unknown-blocks")

    def test_unknown_blocks_that_names_two_kinds(self):
        plan = with_unknowns(unknown(blocks=["M1"]))
        plan["phases"].append({"id": "M1", "name": "Same id as the milestone", "goal": "g", "exit": "e"})
        found = self.assert_code(plan, "unknown-blocks")
        self.assertIn("blocks 'M1', which names both a phase and a milestone; write phase:M1 or milestone:M1",
                      found[0]["message"])
        for fixed in ("phase:M1", "milestone:M1"):
            plan["unknowns"][0]["blocks"] = [fixed]
            self.assert_clean(plan, "unknown-blocks")

    def test_unknown_experiment(self):
        good = {"plan": "ask three people", "timebox": "2d", "exit": "two say yes"}
        self.assert_clean(with_unknowns(unknown(status="investigating", experiment=good)), "unknown-experiment")
        self.assert_clean(with_unknowns(unknown(status="open")), "unknown-experiment")
        self.assert_clean(with_unknowns(unknown(status="open", experiment=good)), "unknown-experiment")
        for w in ("2h", "10d", "999w", "0d"):
            self.assert_clean(with_unknowns(unknown(status="investigating", experiment=dict(good, timebox=w))),
                              "unknown-experiment")
        found = self.assert_code(with_unknowns(unknown(status="investigating")), "unknown-experiment")
        self.assertIn("investigating needs experiment.plan, experiment.timebox (e.g. 2d) and experiment.exit",
                      found[0]["message"])
        bad = {"timebox": ("3x", "d", "1000d", "2 d", "2d\n", "", 5, DROP), "plan": ("", "  ", DROP, 5),
               "exit": ("", DROP, 5), "owner": ("nobody", 5, "")}
        for field, values in bad.items():
            for v in values:
                with self.subTest(field=field, value=v):
                    ex = {k: x for k, x in dict(good, **{field: v}).items() if x is not DROP}
                    self.assert_code(with_unknowns(unknown(status="investigating", experiment=ex)), "unknown-experiment")
                    self.assert_code(with_unknowns(unknown(status="open", experiment=ex)), "unknown-experiment")
        self.assert_clean(with_unknowns(unknown(status="investigating", experiment=dict(good, owner="owner"))),
                          "unknown-experiment")
        self.assert_code(with_unknowns(unknown(status="investigating", experiment="2d")), "unknown-experiment")
        self.assert_code(with_unknowns(unknown(status="investigating", experiment=[])), "unknown-experiment")

    def test_unknown_resolved_no_decision(self):
        plan = with_unknowns(unknown(status="resolved"), decisions=[decision()])
        self.assert_code(plan, "unknown-resolved-no-decision")
        plan["unknowns"][0]["decision"] = "D-other"
        found = self.assert_code(plan, "unknown-resolved-no-decision")
        self.assertIn("'D-other'", found[0]["message"])
        plan["unknowns"][0]["decision"] = "D-offline"
        self.assert_clean(plan, "unknown-resolved-no-decision")
        self.assert_clean(with_unknowns(unknown(status="open")), "unknown-resolved-no-decision")

    def test_unknown_dropped_no_note(self):
        self.assert_code(with_unknowns(unknown(status="dropped")), "unknown-dropped-no-note")
        self.assert_code(with_unknowns(unknown(status="dropped", notes="  ")), "unknown-dropped-no-note")
        self.assert_clean(with_unknowns(unknown(status="dropped", notes="We cut offline")), "unknown-dropped-no-note")
        self.assert_clean(with_unknowns(unknown(status="open")), "unknown-dropped-no-note")

    def test_unknown_evidence(self):
        plan = with_unknowns(unknown(evidenceFor=["ev-1111111111111111"], evidenceAgainst=["ev-2222222222222222"]))
        found = self.assert_code(plan, "unknown-evidence", level="warning", evidence_ids={"ev-3333333333333333"})
        self.assertEqual(len(found), 2)
        self.assertIn("evidence ev-1111111111111111 is not in .blueprint/studies", found[0]["message"])
        self.assert_clean(plan, "unknown-evidence", evidence_ids={"ev-1111111111111111", "ev-2222222222222222"})
        self.assert_clean(plan, "unknown-evidence")  # no store was read: nothing to compare with

    def test_unknown_open_blocker(self):
        plan = with_unknowns(unknown(status="investigating", blocks=["P2", "F-inbox"], severity="critical",
                                     experiment={"plan": "p", "timebox": "1d", "exit": "e"}))
        found = self.assert_code(plan, "unknown-open-blocker", level="warning")
        self.assertEqual(found[0]["message"], "unknown U-x (critical) is open and blocks P2, F-inbox; claim and status "
                                              "--next skip them until it is resolved")
        self.assertEqual([f for f in findings(plan) if f["level"] == "error"], [], "open questions are not errors")
        for label, u in (("low", unknown(severity="low")), ("medium", unknown(severity="medium")),
                         ("resolved", unknown(status="resolved", decision="D-offline")),
                         ("dropped", unknown(status="dropped", notes="n")), ("blocks nothing", unknown(blocks=[])),
                         ("blocks an unknown id", unknown(blocks=["P9"]))):
            with self.subTest(label):
                self.assert_clean(with_unknowns(u, decisions=[decision()]), "unknown-open-blocker")

    def test_decision_unknown(self):
        plan = with_unknowns(unknown(), decisions=[decision(unknown="U-nope")])
        found = self.assert_code(plan, "decision-unknown")
        self.assertIn("decision D-offline: unknown 'U-nope' does not exist", found[0]["message"])
        self.assert_clean(with_unknowns(unknown(), decisions=[decision(unknown="U-x")]), "decision-unknown")
        self.assert_clean(with_unknowns(unknown(), decisions=[decision()]), "decision-unknown")

    def test_bad_id_is_emitted_but_not_registered(self):
        for bad in ("nope", "U-Upper", "U-", "u-x", "U-" + "a" * 49):
            found = [f for f in findings(with_unknowns(unknown(id=bad))) if f["code"] == "bad-id"]
            self.assertEqual(len(found), 1, bad)
            self.assertEqual(found[0]["message"].split(";")[0], f"unknown id '{bad}' has the wrong form")
            self.assertEqual(found[0]["level"], "error")
            self.assertTrue(found[0]["next"])
        self.assertNotIn("bad-id", [f["code"] for f in findings(with_unknowns(unknown(id="U-ok-1")))])
        self.assertNotIn("bad-id", [c for _, c in unknowns.CODES])

    def test_whole_value_patterns(self):
        for bad in ("U-offline\n", "U-offline\r\n", " U-offline", "U-offline "):
            found = [f for f in findings(with_unknowns(unknown(id=bad))) if f["code"] == "bad-id"]
            self.assertEqual(len(found), 1, repr(bad))
            self.assertNotIn("\n", found[0]["message"])
        good = {"plan": "p", "timebox": "2d", "exit": "e"}
        for bad in ("2d\n", "2d\r\n", " 2d", "2d "):
            self.assertIn("unknown-experiment", codes_of(with_unknowns(
                unknown(status="investigating", experiment=dict(good, timebox=bad)))), repr(bad))
        for field, bad in (("severity", "high\n"), ("severity", " high"), ("status", "open\r\n"), ("status", " open")):
            self.assertIn("unknown-" + field, codes_of(with_unknowns(unknown(**{field: bad}))), repr(bad))
            self.assertEqual(unknowns.blocking(with_unknowns(unknown(**{field: bad})))["phases"], {"P2"})
        for status in ("resolved\n", " dropped", "dropped "):
            self.assertEqual(unknowns.blocking(with_unknowns(unknown(status=status)))["phases"], {"P2"}, repr(status))
        for sev in ("low\n", " medium", "medium "):
            self.assertEqual(unknowns.blocking(with_unknowns(unknown(severity=sev)))["phases"], {"P2"}, repr(sev))

    def test_duplicate_roster_and_decision_ids_fail_closed(self):
        ex = {"plan": "p", "timebox": "2d", "exit": "e", "owner": "owner"}
        plan = with_unknowns(unknown(status="investigating", experiment=ex))
        self.assertNotIn("unknown-experiment", codes_of(plan))
        plan["roster"].append(dict(plan["roster"][-1]))
        self.assertEqual(plan["roster"][-1]["id"], "owner")
        self.assertIn("unknown-experiment", codes_of(plan))
        plan = with_unknowns(unknown(status="resolved", decision="D-offline"), decisions=[decision(), decision()])
        self.assertIn("unknown-resolved-no-decision", codes_of(plan))

    def test_errors_name_commands_that_run(self):
        plan = with_unknowns(unknown(severity="huge", status="open"), decisions=[decision(unknown="U-nope")])
        for f in mine(findings(plan)):
            if f["next"] and "blueprint.py" in f["next"]:
                self.assertTrue(f["next"].startswith("python .blueprint/bin/blueprint.py "), f["next"])
        found = findings(with_unknowns(unknown()))
        self.assertEqual([f["next"] for f in found if f["code"] == "unknown-open-blocker"],
                         ["python .blueprint/bin/blueprint.py status --unknowns"])
        v1 = v1_plan()
        v1["decisions"] = [decision(unknown="U-nope")]
        with TempProject(v1) as p:  # loaded from disk, so the plan knows it came from version 1
            self.assertEqual([f["next"] for f in findings(p.loaded()) if f["code"] == "decision-unknown"],
                             ["python .blueprint/bin/blueprint.py migrate"])

    def test_status_text_is_one_line(self):
        plan = with_unknowns(unknown(question="Is it\nso?\r\n  Really", id="U-x", blocks=["P2"], status="investigating",
                                     experiment={"plan": "a\nb", "timebox": "1d", "exit": "c\n\nd"}, decision="D-1\nx"))
        ctx = {}
        unknowns.context(None, plan, {}, ctx)
        for flag in (True, False):
            for line in unknowns.status_lines(ctx, type("A", (), {"unknowns": flag})()):
                self.assertNotIn("\n", line)
                self.assertNotIn("\r", line)
        lines = "\n".join(unknowns.status_lines(ctx, type("A", (), {"unknowns": True})()))
        self.assertIn("Is it so? Really", lines)
        self.assertIn("a b; ends when: c d", lines)
        msg = unknowns.refuse("claim", None, with_unknowns(unknown(id="U-x\ny")), OFFLINE, "x")[0]
        self.assertNotIn("\n", msg)

    def test_ids_use_the_1x_messages(self):
        found = findings(with_unknowns(unknown(), unknown()))
        self.assertIn(("duplicate-id", "duplicate unknown id: U-x"), [(f["code"], f["message"]) for f in found])
        found = findings(with_unknowns(unknown(id=DROP)))
        self.assertIn("missing-id", [f["code"] for f in found])
        self.assertIn("unknown without id", " ".join(f["message"] for f in found))

    def test_every_finding_classifies_as_its_code(self):
        base = with_unknowns(unknown(status="investigating", experiment={"plan": "p", "timebox": "1d", "exit": "e"},
                                     evidenceFor=["ev-aaaaaaaaaaaaaaaa"]), decisions=[decision(unknown="U-x")])
        seen = set()
        for path in ("severity", "status", "question", "blocks", "experiment", "decision", "notes", "evidenceFor", "id"):
            for value in ("", None, "nope", [], ["P9"], 5, {}, DROP):
                plan = copy.deepcopy(base)
                if value is DROP:
                    plan["unknowns"][0].pop(path, None)
                else:
                    plan["unknowns"][0][path] = value
                for f in findings(plan, evidence_ids=set()):
                    seen.add(f["code"])
                    if f["code"] != "bad-id":  # the core registers bad-id
                        self.assertEqual(validate.classify_error(f["message"]), f["code"], f["message"])
        self.assertTrue({"unknown-incomplete", "unknown-severity", "unknown-status", "unknown-blocks",
                         "unknown-experiment", "unknown-evidence", "missing-id"} <= seen)

    def test_validate_command_reports_a_blocker_as_a_warning_with_exit_0(self):
        with TempProject() as p:
            r = p.cli("validate")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("warning: unknown U-offline (high) is open and blocks P2", r.stdout)
            self.assertIn("1 unknowns", r.stdout)
            data = json.loads(p.cli("validate", "--json").stdout)["data"]
            self.assertEqual([w["code"] for w in data["warnings"]], ["unknown-open-blocker"])
            self.assertEqual(data["counts"]["unknowns"], 1)

    def test_validate_command_fails_on_a_bad_unknown(self):
        with TempProject(with_unknowns(unknown(severity="huge"))) as p:
            r = p.cli("validate")
            self.assertEqual(r.returncode, 1)
            self.assertIn("error: unknown U-x: severity must be low, medium, high or critical (got 'huge')", r.stderr + r.stdout)


class TempProject:
    """A temporary project holding a plan; files named in the plan exist on disk (empty)."""

    def __init__(self, plan=None):
        self.plan = plan or v2_plan()

    def __enter__(self):
        self.root = Path(tempfile.mkdtemp(prefix="bp unknowns "))
        (self.root / ".blueprint").mkdir()
        self.write(self.plan)
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, plan):
        self.plan = plan
        (self.root / ".blueprint" / "blueprint.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")

    def cli(self, *args, stdin=None, **env):
        argv = list(args) if args[0] == "hook" else list(args) + ["--root", self.root]
        return S.run_script(S.LAUNCHER, argv, self.root, stdin=stdin, env=S.tool_env(**env))

    def loaded(self):
        return quiet(bplan.load_plan, self.root)[0]


class BlockingTest(unittest.TestCase):
    def blocked(self, *items, **extra):
        return unknowns.blocking(with_unknowns(*items, **extra))

    def assert_blocks_nothing(self, *items, **extra):
        b = self.blocked(*items, **extra)
        self.assertEqual((b["phases"], b["milestones"], b["features"], b["by"]), (set(), set(), set(), {}))

    def test_a_phase_is_blocked_by_an_open_severe_unknown(self):
        b = self.blocked(unknown(status="investigating", blocks=["P2"]))
        self.assertEqual((b["phases"], b["milestones"], b["features"]), ({"P2"}, set(), set()))
        self.assertEqual(b["by"], {"phase:P2": ["U-x"]})

    def test_a_milestone_blocks_its_features(self):
        b = self.blocked(unknown(blocks=["M1"]))
        self.assertEqual(b["milestones"], {"M1"})
        self.assertEqual(b["features"], {"F-log-snag", "F-inbox"}, "both example features are in M1")
        self.assertEqual(b["by"]["milestone:M1"], ["U-x"])
        self.assertEqual(b["by"]["feature:F-inbox"], ["U-x"])
        self.assertEqual(b["phases"], set())

    def test_a_feature_is_blocked_directly(self):
        b = self.blocked(unknown(blocks=["F-inbox"]))
        self.assertEqual((b["features"], b["phases"], b["milestones"]), ({"F-inbox"}, set(), set()))
        self.assertEqual(b["by"], {"feature:F-inbox": ["U-x"]})

    def test_a_feature_in_a_blocked_milestone_lists_both_unknowns(self):
        b = self.blocked(unknown(id="U-a", blocks=["F-inbox"]), unknown(id="U-b", blocks=["milestone:M1"]),
                         unknown(id="U-a", blocks=["F-inbox"]))
        self.assertEqual(b["by"]["feature:F-inbox"], ["U-a", "U-b"])
        self.assertEqual(b["by"]["feature:F-log-snag"], ["U-b"])

    def test_one_unknown_can_block_several_kinds(self):
        b = self.blocked(unknown(blocks=["P1", "milestone:M1", "F-inbox"]))
        self.assertEqual((b["phases"], b["milestones"], b["features"]), ({"P1"}, {"M1"}, {"F-inbox", "F-log-snag"}))

    def test_open_and_investigating_both_block_and_critical_counts(self):
        for status in ("open", "investigating"):
            for severity in ("high", "critical"):
                with self.subTest(status=status, severity=severity):
                    self.assertEqual(self.blocked(unknown(status=status, severity=severity))["phases"], {"P2"})

    def test_low_and_medium_severity_block_nothing(self):
        for severity in ("low", "medium"):
            with self.subTest(severity=severity):
                self.assert_blocks_nothing(unknown(severity=severity))
                self.assert_blocks_nothing(unknown(severity=severity, status="investigating", blocks=["M1", "F-inbox"]))

    def test_resolved_and_dropped_block_nothing(self):
        for status in ("resolved", "dropped"):
            with self.subTest(status=status):
                self.assert_blocks_nothing(unknown(status=status, severity="critical", blocks=["P1", "M1", "F-inbox"]))

    def test_an_unknown_that_blocks_an_id_the_plan_lacks_blocks_nothing(self):
        self.assert_blocks_nothing(unknown(blocks=["P9", "milestone:P1", "phase:M1", "feature:F-x", "nope:P1", ""]))

    def test_fail_closed_on_a_malformed_severity_or_status(self):
        for field, bad in (("severity", DROP), ("severity", ""), ("severity", "HIGH"), ("severity", "Low"),
                           ("severity", "urgent"), ("severity", 5), ("severity", ["low"]), ("severity", {}),
                           ("status", DROP), ("status", ""), ("status", "Resolved"), ("status", "done"),
                           ("status", ["resolved"]), ("status", 7)):
            with self.subTest(field=field, value=bad):
                self.assertEqual(self.blocked(unknown(**{field: bad}))["phases"], {"P2"}, "must not unblock work")

    def test_a_lone_string_in_blocks_still_blocks(self):
        self.assertEqual(self.blocked(unknown(blocks="P2"))["phases"], {"P2"})

    def test_an_ambiguous_id_blocks_every_kind_that_has_it(self):
        plan = with_unknowns(unknown(blocks=["M1"]))
        plan["phases"].append({"id": "M1", "name": "n", "goal": "g", "exit": "e"})
        b = unknowns.blocking(plan)
        self.assertEqual((b["phases"], b["milestones"]), ({"M1"}, {"M1"}))
        plan["unknowns"][0]["blocks"] = ["phase:M1"]
        b = unknowns.blocking(plan)
        self.assertEqual((b["phases"], b["milestones"]), ({"M1"}, set()))

    def test_blocking_survives_garbage(self):
        for plan in ({}, {"unknowns": "x"}, {"unknowns": [None, 5, "x", [], {}]}, {"unknowns": [{"blocks": [[]]}],
                                                                                    "phases": [None, {"id": []}]},
                     {"unknowns": [unknown()], "features": [5, {"id": 5, "milestone": []}], "milestones": "m"}):
            b = unknowns.blocking(plan)
            self.assertEqual(set(b), {"phases", "milestones", "features", "by"})

    def test_context_fills_unknowns_and_blocked(self):
        with TempProject(with_unknowns(unknown(status="investigating", blocks=["P2", "M1"]),
                                       unknown(id="U-low", severity="low"),
                                       unknown(id="U-done", status="resolved", decision="D-offline"),
                                       decisions=[decision()])) as p:
            plan = p.loaded()
            ctx = build_context(p.root, plan, bplan.load_state(p.root))
        self.assertEqual([u["id"] for u in ctx["unknowns"]], ["U-x", "U-low", "U-done"])
        self.assertEqual([u["blocking"] for u in ctx["unknowns"]], [True, False, False])
        self.assertEqual(set(ctx["unknowns"][0]), {"id", "question", "severity", "status", "blocks", "evidenceFor",
                                                   "evidenceAgainst", "experiment", "decision", "blocking"})
        self.assertEqual(ctx["unknowns"][2]["decision"], "D-offline")
        self.assertEqual(ctx["blocked"]["phases"], {"P2"})
        self.assertEqual(blocked_by(ctx, "phase", "P2"), ["U-x"])
        self.assertEqual(blocked_by(ctx, "milestone", "M1"), ["U-x"])
        self.assertEqual(blocked_by(ctx, "feature", "F-inbox"), ["U-x"])
        json.dumps(ctx["unknowns"])  # the payload is plain JSON

    def test_an_open_severe_unknown_that_blocks_nothing_is_not_marked_blocking(self):
        ctx = {}
        unknowns.context(None, with_unknowns(unknown(blocks=[])), {}, ctx)
        self.assertEqual(ctx["unknowns"][0]["blocking"], False)

    def test_context_without_unknowns(self):
        ctx = {}
        unknowns.context(None, v1_plan() | {"unknowns": []}, {}, ctx)
        self.assertEqual((ctx["unknowns"], ctx["blocked"]["by"]), ([], {}))


class ClaimRefusalTest(unittest.TestCase):
    def refuse(self, plan, path=OFFLINE, root=None, action="claim"):
        return unknowns.refuse(action, root, plan, path, "frontend-engineer")

    def test_message_error_and_next(self):
        got = self.refuse(v2_plan())
        self.assertEqual(got, (EXPECTED_MESSAGE, "blocked-by-unknown",
                               "python .blueprint/bin/blueprint.py status --unknowns"))

    def test_only_claims_on_blocked_phases_are_refused(self):
        plan = v2_plan()
        self.assertIsNone(self.refuse(plan, "apps/web/src/Inbox.tsx"), "phase P1 is not blocked")
        self.assertIsNone(self.refuse(plan, OFFLINE, action="write"))
        self.assertIsNone(self.refuse(plan, "not/in/the/plan.ts"))
        self.assertIsNone(self.refuse({}, OFFLINE))
        self.assertIsNone(self.refuse(with_unknowns(), OFFLINE))

    def test_every_spelling_of_the_path_is_refused(self):
        with TempProject() as p:
            plan = p.loaded()
            spellings = [OFFLINE, "./" + OFFLINE, ".\\" + OFFLINE.replace("/", "\\"), OFFLINE.replace("/", "\\"),
                         "apps//web/src/offline.ts", "apps/web/../web/src/offline.ts", "apps/./web/src/offline.ts",
                         str(p.root / OFFLINE), str(p.root / "apps" / "web" / ".." / "web" / "src" / "offline.ts")]
            if os.path.normcase("A") == "a":  # Windows: the file system ignores case
                spellings += [OFFLINE.upper(), "Apps/Web/Src/Offline.ts"]
            for raw in spellings:
                with self.subTest(path=raw):
                    got = self.refuse(plan, raw, root=p.root)
                    self.assertIsNotNone(got)
                    self.assertEqual(got[1], "blocked-by-unknown")

    def test_a_pattern_instance_in_a_blocked_phase_is_refused(self):
        plan = v2_plan()
        plan["patterns"] = [{"id": "screens", "glob": "apps/web/src/screens/*Screen.tsx", "module": "web", "phase": "P2",
                             "purpose": "screens"}]
        self.assertIsNotNone(self.refuse(plan, "apps/web/src/screens/DetailScreen.tsx"))
        plan["patterns"][0]["phase"] = "P1"
        self.assertIsNone(self.refuse(plan, "apps/web/src/screens/DetailScreen.tsx"))

    def test_the_message_names_every_blocking_unknown(self):
        plan = with_unknowns(unknown(id="U-a", status="investigating", severity="critical"), unknown(id="U-b", blocks=["P2"]),
                             unknown(id="U-c", severity="low"))
        msg = self.refuse(plan)[0]
        self.assertEqual(msg, f"{OFFLINE} is in phase P2, blocked by unknowns U-a (critical, investigating), U-b (high, "
                              "open). Resolve it first, or use --force if the lead says so.")

    def test_nothing_is_refused_once_the_unknown_stops_blocking(self):
        for change in ({"status": "resolved", "decision": "D-offline"}, {"status": "dropped", "notes": "n"},
                       {"severity": "medium"}, {"severity": "low"}, {"blocks": ["P1"]}, {"blocks": []}):
            with self.subTest(change=change):
                plan = v2_plan()
                plan["unknowns"][0].update(change)
                if change.get("blocks") == ["P1"]:
                    self.assertIsNone(self.refuse(plan))
                    self.assertIsNotNone(self.refuse(plan, "apps/web/src/Inbox.tsx"))
                else:
                    self.assertIsNone(self.refuse(plan))

    def test_fail_closed_for_a_malformed_unknown(self):
        plan = v2_plan()
        plan["unknowns"][0]["severity"] = "urgent"
        self.assertIsNotNone(self.refuse(plan))
        plan["unknowns"][0].pop("status")
        got = self.refuse(plan)
        self.assertIn("(urgent, no status)", got[0])

    def test_refuse_survives_garbage(self):
        for plan in ({"files": [None, {"path": 5}, {"path": OFFLINE, "phase": []}], "unknowns": [None, {}]},
                     {"files": [{"path": OFFLINE, "phase": "P2"}], "unknowns": [unknown(blocks=[None])]},
                     {"patterns": [{"glob": 5}, {"glob": ""}, None]}, {"files": "x"}):
            self.assertIsNone(self.refuse(plan))
        self.assertIsNone(unknowns.refuse("claim", None, None, OFFLINE, None))


class ClaimCommandTest(unittest.TestCase):
    def claim(self, p, path, *extra):
        return p.cli("claim", path, "--by", "frontend-engineer", *extra)

    def test_claim_in_a_blocked_phase_exits_3(self):
        with TempProject() as p:
            r = self.claim(p, OFFLINE)
            self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
            self.assertIn("error: " + EXPECTED_MESSAGE, r.stderr)
            self.assertIn("next: python .blueprint/bin/blueprint.py status --unknowns", r.stderr)
            self.assertNotIn("Traceback", r.stderr)
            self.assertFalse((p.root / ".blueprint" / "state.json").exists() and "claimed" in
                             (p.root / ".blueprint" / "state.json").read_text(encoding="utf-8"))

    def test_claim_refusal_for_every_spelling_of_the_path(self):
        with TempProject() as p:
            for raw in ("./" + OFFLINE, OFFLINE.replace("/", "\\"), ".\\" + OFFLINE.replace("/", "\\")):
                with self.subTest(path=raw):
                    r = self.claim(p, raw)
                    self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
                    self.assertIn("blocked by unknown U-offline", r.stderr)

    def test_force_claims_anyway_and_other_phases_are_free(self):
        with TempProject() as p:
            self.assertEqual(self.claim(p, "apps/web/src/Inbox.tsx").returncode, 0)
            r = self.claim(p, OFFLINE, "--force")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("claimed " + OFFLINE, r.stdout)

    def test_claim_is_allowed_once_the_unknown_is_resolved(self):
        with TempProject() as p:
            plan = copy.deepcopy(p.plan)
            plan["decisions"] = [decision()]
            plan["unknowns"][0].update(status="resolved", decision="D-offline")
            p.write(plan)
            r = self.claim(p, OFFLINE)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_low_severity_unknown_does_not_stop_a_claim(self):
        with TempProject() as p:
            plan = copy.deepcopy(p.plan)
            plan["unknowns"][0]["severity"] = "medium"
            p.write(plan)
            self.assertEqual(self.claim(p, OFFLINE).returncode, 0)

    def test_the_1x_checks_come_first(self):
        with TempProject() as p:
            r = p.cli("claim", OFFLINE, "--by", "backend-lead")
            self.assertEqual(r.returncode, 3)
            self.assertIn("is owned by frontend-engineer", r.stderr)
            r = p.cli("claim", "no/such.ts", "--by", "backend-lead")
            self.assertIn("is not in the blueprint", r.stderr)


INBOX = "apps/web/src/Inbox.tsx"          # serves F-inbox only (phase P1)
MAIN = "crates/snag-api/src/main.rs"     # serves F-log-snag only (phase P1)
CARGO = "Cargo.toml"                     # infrastructure


def feature_message(path, what):
    return f"{path} serves only {what}. Resolve it first, or use --force if the lead says so."


class FeatureBlockTest(unittest.TestCase):
    """2.6 as ruled: a claim is refused when the file serves at least one feature and every feature it serves is blocked."""

    def refuse(self, plan, path):
        return unknowns.refuse("claim", None, plan, path, "x")

    def plan(self, blocks, **kw):
        return with_unknowns(unknown(blocks=blocks, **kw))

    def test_the_three_refusal_texts(self):
        phase = self.refuse(v2_plan(), OFFLINE)
        self.assertEqual(phase[0], "apps/web/src/offline.ts is in phase P2, blocked by unknown U-offline (high, "
                                   "investigating). Resolve it first, or use --force if the lead says so.")
        feature = self.refuse(self.plan(["F-inbox"]), INBOX)
        self.assertEqual(feature[0], feature_message(INBOX, "feature F-inbox, blocked by unknown U-x (high, open)"))
        milestone = self.refuse(self.plan(["M1"]), INBOX)
        self.assertEqual(milestone[0], feature_message(
            INBOX, "feature F-inbox, whose milestone M1 is blocked by unknown U-x (high, open)"))
        for got in (phase, feature, milestone):
            self.assertEqual(got[1:], ("blocked-by-unknown", "python .blueprint/bin/blueprint.py status --unknowns"))

    def test_only_the_blocked_feature_files_are_refused(self):
        plan = self.plan(["F-inbox"])
        self.assertIsNotNone(self.refuse(plan, INBOX))
        self.assertIsNotNone(self.refuse(plan, OFFLINE), "offline.ts serves F-inbox only, and its phase is free here")
        self.assertIsNone(self.refuse(plan, MAIN))
        self.assertIsNone(self.refuse(plan, CARGO), "infrastructure")

    def test_a_milestone_blocks_the_files_of_its_features(self):
        plan = self.plan(["milestone:M1"])
        for path in (INBOX, MAIN, OFFLINE, "crates/snag-api/tests/snags.rs"):
            self.assertIsNotNone(self.refuse(plan, path), path)
        self.assertIsNone(self.refuse(plan, CARGO))

    def test_one_blocked_and_one_free_feature_is_claimable(self):
        plan = self.plan(["F-inbox"])
        next(f for f in plan["files"] if f["path"] == INBOX)["features"] = ["F-inbox", "F-log-snag"]
        self.assertIsNone(self.refuse(plan, INBOX))
        plan["unknowns"][0]["blocks"] = ["F-inbox", "F-log-snag"]
        got = self.refuse(plan, INBOX)
        self.assertEqual(got[0], feature_message(INBOX, "blocked features: F-inbox, blocked by unknown U-x (high, open); "
                                                        "F-log-snag, blocked by unknown U-x (high, open)"))

    def test_a_feature_blocked_both_ways_names_both(self):
        plan = with_unknowns(unknown(id="U-a", blocks=["F-inbox"]), unknown(id="U-b", severity="critical", blocks=["M1"]))
        self.assertEqual(self.refuse(plan, INBOX)[0], feature_message(
            INBOX, "feature F-inbox, blocked by unknown U-a (high, open) and whose milestone M1 is blocked by unknown "
                   "U-b (critical, open)"))

    def test_infrastructure_and_unlinked_files_are_claimable(self):
        plan = self.plan(["M1", "F-inbox", "F-log-snag"])
        inbox = next(f for f in plan["files"] if f["path"] == INBOX)
        inbox["infrastructure"] = True
        self.assertIsNone(self.refuse(plan, INBOX), "infrastructure wins")
        inbox.pop("infrastructure")
        inbox.pop("features")
        self.assertIsNone(self.refuse(plan, INBOX), "no feature link")
        inbox["features"] = ["F-nope"]
        self.assertIsNone(self.refuse(plan, INBOX), "a feature the plan lacks is not blocked")

    def test_a_phase_block_still_applies_to_infrastructure(self):
        plan = self.plan(["P1"])
        self.assertIn("is in phase P1", self.refuse(plan, CARGO)[0])
        plan = self.plan(["P1", "F-inbox"])
        self.assertIn("is in phase P1", self.refuse(plan, INBOX)[0], "the phase text comes first")

    def test_a_pattern_instance_follows_its_pattern(self):
        plan = self.plan(["F-inbox"])
        plan["patterns"] = [{"id": "screens", "glob": "apps/web/src/screens/*Screen.tsx", "module": "web", "phase": "P1",
                             "purpose": "screens", "features": ["F-inbox"]}]
        inst = "apps/web/src/screens/DetailScreen.tsx"
        self.assertEqual(self.refuse(plan, inst)[0], feature_message(inst, "feature F-inbox, blocked by unknown U-x "
                                                                          "(high, open)"))
        plan["patterns"][0]["features"] = ["F-inbox", "F-log-snag"]
        self.assertIsNone(self.refuse(plan, inst))
        plan["patterns"][0].pop("features")
        self.assertIsNone(self.refuse(plan, inst))
        plan["patterns"][0].update(features=["F-inbox"], infrastructure=True)
        self.assertIsNone(self.refuse(plan, inst))

    def test_unknowns_that_do_not_block_refuse_nothing(self):
        for change in ({"status": "resolved", "decision": "D-offline"}, {"status": "dropped", "notes": "n"},
                       {"severity": "low"}, {"severity": "medium"}):
            with self.subTest(change=change):
                for blocks in (["F-inbox"], ["M1"]):
                    plan = with_unknowns(unknown(blocks=blocks, **change), decisions=[decision()])
                    self.assertIsNone(self.refuse(plan, INBOX))
                    self.assertEqual(unknowns.blocked_files(plan), set())

    def test_malformed_severity_or_status_fails_closed(self):
        for change in ({"severity": "urgent"}, {"severity": DROP}, {"status": "Resolved"}, {"status": 5}, {"status": DROP}):
            with self.subTest(change=change):
                self.assertIsNotNone(self.refuse(self.plan(["F-inbox"], **change), INBOX))
                self.assertIsNotNone(self.refuse(self.plan(["M1"], **change), INBOX))

    def test_blocked_files_and_the_context(self):
        plan = self.plan(["F-inbox", "P2"])
        self.assertEqual(unknowns.blocked_files(plan), {INBOX, OFFLINE})
        ctx = {}
        unknowns.context(None, plan, {}, ctx)
        self.assertEqual(ctx["blocked"]["files"], {INBOX, OFFLINE})
        self.assertEqual(ctx["blocked"]["phases"], {"P2"})
        self.assertEqual(unknowns.blocking(plan).keys(), {"phases", "milestones", "features", "by"})

    def test_the_claim_command_refuses_and_force_overrides(self):
        with TempProject(self.plan(["F-inbox"])) as p:
            r = p.cli("claim", INBOX, "--by", "frontend-engineer")
            self.assertEqual(r.returncode, 3, r.stderr)
            self.assertIn("error: " + feature_message(INBOX, "feature F-inbox, blocked by unknown U-x (high, open)"), r.stderr)
            self.assertIn("next: python .blueprint/bin/blueprint.py status --unknowns", r.stderr)
            r = p.cli("claim", ".\\" + INBOX.replace("/", "\\"), "--by", "frontend-engineer")
            self.assertEqual(r.returncode, 3)
            self.assertEqual(p.cli("claim", INBOX, "--by", "frontend-engineer", "--force").returncode, 0)
            self.assertEqual(p.cli("claim", CARGO, "--by", "cto-architect").returncode, 0)
        with TempProject(self.plan(["M1"])) as p:
            r = p.cli("claim", MAIN, "--by", "backend-lead")
            self.assertEqual(r.returncode, 3, r.stderr)
            self.assertIn("whose milestone M1 is blocked by unknown U-x (high, open)", r.stderr)


class StatusTest(unittest.TestCase):
    def ready(self, out):
        return out.split("Ready to build next:")[1]

    def test_next_skips_a_blocked_phase_and_says_why(self):
        with TempProject(with_unknowns(unknown(id="U-offline", status="investigating", blocks=["P1"],
                                               experiment={"plan": "p", "timebox": "1d", "exit": "e"}))) as p:
            r = p.cli("status", "--next")
            self.assertEqual(r.returncode, 0, r.stderr)
            ready = self.ready(r.stdout)
            self.assertIn("(phase P1 skipped: blocked by U-offline)", ready)
            self.assertIn(OFFLINE, ready, "the lowest open phase that is not blocked is offered")
            self.assertNotIn("Cargo.toml", ready)
            self.assertNotIn("Inbox.tsx", ready)

    def test_next_offers_the_phase_again_when_the_unknown_stops_blocking(self):
        with TempProject() as p:
            r = p.cli("status", "--next")
            ready = self.ready(r.stdout)
            self.assertIn("(phase P2 skipped: blocked by U-offline)", ready)
            self.assertIn("Cargo.toml", ready, "P1 is not blocked and still has open files")
            self.assertNotIn(OFFLINE, ready)
            plan = copy.deepcopy(p.plan)
            plan["unknowns"][0]["blocks"] = ["P1"]
            p.write(plan)
            self.assertIn("(phase P1 skipped: blocked by U-offline)", self.ready(p.cli("status", "--next").stdout))
            plan["unknowns"][0]["severity"] = "medium"
            p.write(plan)
            ready = self.ready(p.cli("status", "--next").stdout)
            self.assertIn("Cargo.toml", ready)
            self.assertNotIn("skipped", ready)

    def test_next_skips_a_phase_blocked_through_a_prefixed_id(self):
        with TempProject(with_unknowns(unknown(blocks=["phase:P1"]))) as p:
            self.assertIn("(phase P1 skipped: blocked by U-x)", self.ready(p.cli("status", "--next").stdout))

    def test_default_status_lists_the_open_blocking_unknowns(self):
        with TempProject() as p:
            out = p.cli("status").stdout
            self.assertIn("Open blocking unknowns: 1", out)
            self.assertIn("U-offline (high, investigating) blocks P2", out)
        with TempProject(with_unknowns(unknown(severity="low"))) as p:
            self.assertNotIn("blocking unknowns", p.cli("status").stdout)

    def test_status_unknowns_lists_every_unknown(self):
        plan = with_unknowns(
            unknown(id="U-offline", status="investigating", experiment={"plan": "Ask three site leads",
                                                                        "timebox": "3d", "exit": "Two of three say yes"}),
            unknown(id="U-low", severity="low", question="Which colour?"),
            unknown(id="U-done", status="resolved", decision="D-offline", question="Done?"), decisions=[decision()])
        with TempProject(plan) as p:
            r = p.cli("status", "--unknowns")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Unknowns: 2 open or investigating, 1 resolved or dropped", r.stdout)
            self.assertIn("U-offline  high  investigating  BLOCKING  blocks P2", r.stdout)
            self.assertIn("experiment 3d: Ask three site leads; ends when: Two of three say yes", r.stdout)
            self.assertIn("U-low  low  open", r.stdout)
            self.assertNotIn("U-low  low  open  BLOCKING", r.stdout)
            self.assertIn("decided in D-offline", r.stdout)
            self.assertEqual(r.stdout, p.cli("status", "--unknowns").stdout, "deterministic")

    def test_status_unknowns_without_any_names_the_next_step(self):
        with TempProject(with_unknowns()) as p:
            out = p.cli("status", "--unknowns").stdout
            self.assertIn("Unknowns: none", out)
            self.assertIn("unknowns[]", out)
            self.assertNotIn("Unknowns", p.cli("status").stdout)

    def test_status_json_carries_the_unknowns(self):
        with TempProject() as p:
            data = json.loads(p.cli("status", "--json").stdout)
            self.assertEqual([u["id"] for u in data["unknowns"]], ["U-offline"])
            self.assertTrue(data["unknowns"][0]["blocking"])
            self.assertEqual(data["unknowns"][0]["blocks"], ["P2"])
            exported = json.loads(p.cli("export", "json").stdout)
            self.assertEqual(exported["unknowns"], data["unknowns"])

    def test_a_version_1_plan_prints_exactly_what_1_3_0_printed(self):
        with TempProject(v1_plan()) as p:
            out = p.cli("status").stdout
            self.assertNotIn("Unknowns:", out)
            self.assertNotIn("blocking unknowns", out)
            self.assertEqual(json.loads(p.cli("status", "--json").stdout)["unknowns"], [])


class MapTest(unittest.TestCase):
    TAB = S.SKILL / "assets" / "tab-unknowns.js"

    def test_render_inlines_the_tab_and_the_payload(self):
        with TempProject() as p:
            r = p.cli("render")
            self.assertEqual(r.returncode, 0, r.stderr)
            html = (p.root / ".blueprint" / "map.html").read_text(encoding="utf-8")
            self.assertIn("BP_TABS.push({", html)
            self.assertIn("id: 'unknowns'", html)
            payload = json.loads(re.search(r'<script id="bp" type="application/json">(.*?)</script>', html, re.S)
                                 .group(1).replace("<\\/", "</"))
            self.assertEqual(payload["unknowns"][0]["id"], "U-offline")
            self.assertEqual(payload["unknowns"][0]["blocking"], True)

    def test_render_without_unknowns_still_has_the_key(self):
        with TempProject(v1_plan()) as p:
            self.assertEqual(p.cli("render").returncode, 0)
            html = (p.root / ".blueprint" / "map.html").read_text(encoding="utf-8")
            payload = json.loads(re.search(r'<script id="bp" type="application/json">(.*?)</script>', html, re.S)
                                 .group(1).replace("<\\/", "</"))
            self.assertEqual(payload["unknowns"], [])

    def test_the_asset_has_no_network_or_dependencies(self):
        text = self.TAB.read_text(encoding="utf-8")
        for banned in ("[str(", "CLOSED[", "RANK[", "fetch(", "XMLHttpRequest", "http://", "https://", "import ", "require(", "eval(", "innerHTML",
                       "document.write"):
            self.assertNotIn(banned, text)
        self.assertNotIn("</script", text.lower())

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_asset_parses(self):
        r = subprocess.run(["node", "--check", str(self.TAB)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    HARNESS = r"""
const fs = require('fs');
function node(tag, attrs, kids) {
  const n = { tag, attrs: {}, children: [], text: '', listeners: {}, checked: false, firstChild: null };
  for (const k in (attrs || {})) { if (k === 'text') n.text = attrs[k]; else n.attrs[k === 'cls' ? 'class' : k] = attrs[k]; }
  (kids || []).forEach(c => { if (c != null) n.children.push(typeof c === 'string' ? { tag: '#text', text: c, children: [] } : c); });
  n.appendChild = c => { n.children.push(c); n.firstChild = n.children[0]; return c; };
  n.removeChild = c => { n.children.splice(n.children.indexOf(c), 1); n.firstChild = n.children[0] || null; };
  n.addEventListener = (e, f) => { n.listeners[e] = f; };
  n.firstChild = n.children[0] || null;
  return n;
}
function text(n) { return (n.text || '') + (n.children || []).map(text).join(' '); }
const tabs = [];
global.BP_TABS = { push: t => tabs.push(t) };
require('vm').runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
const H = { el: node };
const out = [];
for (const D of JSON.parse(process.argv[2])) {
  const tab = tabs[0], host = node('section');
  const label = tab.label(D);
  tab.render(host, D, H);
  const res = { id: tab.id, label, text: text(host) };
  const box = (function find(n) { if (n.attrs && n.attrs.type === 'checkbox') return n; for (const c of n.children || []) { const f = find(c); if (f) return f; } return null; })(host);
  if (box) { box.checked = true; box.listeners.change(); res.afterTick = text(host); }
  out.push(res);
}
console.log(JSON.stringify(out));
"""

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_tab_draws_without_throwing(self):
        good = {"id": "U-offline", "question": "Must the inbox work offline?", "severity": "high", "status": "investigating",
                "blocks": ["P2"], "evidenceFor": ["ev-1"], "evidenceAgainst": [], "blocking": True, "decision": None,
                "experiment": {"plan": "Ask three site leads", "timebox": "3d", "exit": "Two of three say yes", "owner": "owner"}}
        closed = dict(good, id="U-done", status="resolved", blocking=False, decision="D-offline", experiment=None)
        datasets = [{}, {"unknowns": None}, {"unknowns": []}, {"unknowns": "x"}, {"unknowns": [None, 5, "x", [], {}]},
                    {"unknowns": [{"id": 5, "severity": 5, "status": [], "blocks": "P2", "experiment": 5,
                                   "evidenceFor": "x"}]},
                    {"unknowns": [good, closed]}, {"unknowns": [closed]}]
        r = subprocess.run(["node", "-e", self.HARNESS, str(self.TAB), json.dumps(datasets)], capture_output=True,
                           text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual({o["id"] for o in out}, {"unknowns"})
        labels = [o["label"] for o in out]
        self.assertEqual(labels[:4], ["Unknowns"] * 4)
        self.assertEqual(labels[6], "Unknowns (1)")
        for o in out[:4]:
            self.assertIn("No unknowns yet", o["text"])
            self.assertIn("blueprint.py render", o["text"], "the empty state names the next command")
        shown = out[6]["text"]
        for want in ("U-offline", "Must the inbox work offline?", "blocks work", "3d", "ends when: Two of three say yes",
                     "1 for", "0 against", "1 open or investigating", "1 blocking work", "1 resolved or dropped"):
            self.assertIn(want, shown)
        self.assertNotIn("U-done", shown, "resolved ones are hidden until asked for")
        self.assertIn("U-done", out[6]["afterTick"])
        self.assertIn("decided in D-offline", out[6]["afterTick"])
        self.assertIn("Nothing is open", out[7]["text"])
        odd = [{"unknowns": [{"id": "__proto__", "status": "constructor", "severity": "__proto__", "blocks": ["constructor"]},
                             {"id": "constructor", "status": "toString", "severity": "hasOwnProperty"}]}]
        r = subprocess.run(["node", "-e", self.HARNESS, str(self.TAB), json.dumps(odd)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)[0]["label"], "Unknowns (2)", "text is never read as a property name")

class RobustnessTest(unittest.TestCase):
    """Hooks called with raw, wrongly shaped plans (validate_plan accepts any dict) never raise."""
    VALUES = [None, 5, True, "x", "", [], {}, ["a"], [5], [{}], [None], "high", "open", ["P2"]]

    def test_every_hook_survives_every_value_in_every_field(self):
        base = with_unknowns(unknown(status="investigating", decision="D-offline", notes="n", evidenceFor=["ev-a"],
                                     experiment={"plan": "p", "timebox": "1d", "exit": "e", "owner": "owner"}),
                             decisions=[decision(unknown="U-x")])
        paths = [("unknowns",), ("decisions",), ("phases",), ("milestones",), ("features",), ("files",), ("patterns",),
                 ("roster",)]
        paths += [("unknowns", 0, k) for k in base["unknowns"][0]]
        paths += [("unknowns", 0, "experiment", k) for k in base["unknowns"][0]["experiment"]]
        paths += [("decisions", 0, "unknown"), ("decisions", 0, "id"), ("features", 0, "milestone"), ("features", 0, "id"),
                  ("phases", 0, "id"), ("files", 0, "phase"), ("files", 0, "path")]
        ran = 0
        for path in paths:
            for value in self.VALUES:
                plan = copy.deepcopy(base)
                node = plan
                for step in path[:-1]:
                    node = node[step]
                node[path[-1]] = value
                label = f"{path} = {value!r}"
                try:
                    unknowns.validate(plan, lambda *a, **k: None, lambda *a, **k: None, {"evidenceIds": {"ev-b"}})
                    unknowns.validate(plan, lambda *a, **k: None, lambda *a, **k: None, {})
                    unknowns.blocking(plan)
                    ctx = {}
                    unknowns.context(None, plan, {}, ctx)
                    unknowns.refuse("claim", None, plan, OFFLINE, "x")
                    unknowns.refuse("claim", None, plan, "apps/web/src/Inbox.tsx", "x")
                    unknowns.status_lines(ctx, type("A", (), {"unknowns": True})())
                    unknowns.status_lines(ctx, type("A", (), {})())
                    json.dumps(unknowns.map_payload(ctx))
                    json.dumps(unknowns.status_json(ctx))
                except Exception as e:  # noqa: BLE001
                    self.fail(f"{label}: {type(e).__name__}: {e}")
                ran += 1
        self.assertGreater(ran, 200)

    def test_output_is_deterministic(self):
        plan = with_unknowns(unknown(id="U-b"), unknown(id="U-a", blocks=["M1", "P1"]))
        first = (unknowns.blocking(plan), findings(plan))
        for _ in range(3):
            self.assertEqual((unknowns.blocking(plan), findings(plan)), first)
        self.assertEqual(list(unknowns.blocking(plan)["by"]), list(first[0]["by"]))


if __name__ == "__main__":
    unittest.main()
