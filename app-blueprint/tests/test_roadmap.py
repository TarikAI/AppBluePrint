"""W1-R: milestones, tasks, the task command, ROADMAP.md and the Roadmap tab (contract 2.3, 2.4, 4.2, 4.4, 7.2, 8.1, 8.5).

Every validation code of 2.3 and 2.4 has a case that triggers it and the nearest valid plan that does not; every rule
of the task ladder (who may set what, the owner-action rule, a note for blocked) has a test that goes red when the rule
is removed. The features module is stood in for by a stub ctx["features"] (the contract's W1-R acceptance), so these
tests do not depend on which other workstreams have merged.
"""
import contextlib
import copy
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import support as S

from bp import plan as bplan, registry, roadmap, validate
from bp.core import BlueprintError
from bp.views import roadmap_md
from bp.views.context import merged
from bp.views.render import render_all, render_map

from test_validate import V2_EXAMPLE, TempRoot, quiet, v2_plan

VALUES = [None, 5, True, "x", [], {}, ["a"], [5], [{}], [None]]


def findings(plan, level="error"):
    return [f for f in validate.validate_findings(plan) if f["level"] == level]


def codes(plan, level="error"):
    return [f["code"] for f in findings(plan, level)]


def mine(plan):
    """The findings of this module's codes only (a plan that also has features may trip other modules' rules)."""
    own = {c for _, c in roadmap.CODES} | {"bad-id", "duplicate-id", "missing-id"}
    return [f for f in findings(plan) if f["code"] in own and f["entry"] and f["entry"].split(" ")[0] in ("milestone", "task")]


def plan_with(**edits):
    """The v2 example with the first milestone and first task edited (a key set to None is removed)."""
    p = v2_plan()
    for key, value in edits.items():
        section, field = key.split("__")
        item = p[section][0]
        if value is None:
            item.pop(field, None)
        else:
            item[field] = value
    return p


def add_task(p, **fields):
    t = {"id": "T-extra", "title": "Extra", "owner": "owner", "milestone": "M1"}
    t.update(fields)
    p["tasks"].append(t)
    return t


class CodeTest(unittest.TestCase):
    """Each code of 2.3 and 2.4: a plan that triggers it and the nearest plan that does not."""

    def assertTriggers(self, code, plan, near):
        got = [f for f in findings(plan) if f["code"] == code]
        self.assertTrue(got, f"{code} not reported; got {codes(plan)}")
        self.assertEqual([f for f in findings(near) if f["code"] == code], [], f"{code} reported for the valid plan")
        for f in got:  # every message classifies as its code, and says what to do next (7.4)
            self.assertEqual(validate.classify_error(f["message"]), code, f["message"])
            self.assertTrue(f["next"], f"no next for {f['message']}")
            self.assertTrue(f["entry"], f"no entry for {f['message']}")
        return got

    def test_the_example_is_clean(self):
        self.assertEqual(mine(v2_plan()), [])

    def test_milestone_incomplete(self):
        for edit in ({"milestones__name": None}, {"milestones__goal": None}, {"milestones__name": "  "},
                     {"milestones__goal": ""}):
            got = self.assertTriggers("milestone-incomplete", plan_with(**edit), v2_plan())
            self.assertEqual(got[0]["message"], "milestone M1: needs name and goal")
            self.assertEqual(got[0]["entry"], "milestone M1")

    def test_milestone_due(self):
        for bad in ("2027-02-30", "tomorrow", "2027-1-5", "2027-13-01", "0000-01-01", "2027-01-15T10:00", "２０２７-01-15", 5):
            got = self.assertTriggers("milestone-due", plan_with(milestones__due=bad), v2_plan())
            self.assertIn("is not a date (YYYY-MM-DD)", got[0]["message"])
        for ok in ("2028-02-29", "2027-12-31", "", None):  # a leap day is a date; a blank or absent due is not set
            self.assertEqual([c for c in codes(plan_with(milestones__due=ok)) if c == "milestone-due"], [], repr(ok))

    def test_task_incomplete(self):
        for edit in ({"tasks__title": None}, {"tasks__owner": None}, {"tasks__title": ""}, {"tasks__owner": " "}):
            got = self.assertTriggers("task-incomplete", plan_with(**edit), v2_plan())
            self.assertEqual(got[0]["message"], "task T-domain: needs title and owner")
        # an empty owner is incomplete, not also "not a roster entry"
        self.assertNotIn("task-owner", codes(plan_with(tasks__owner=None)))

    def test_task_owner(self):
        got = self.assertTriggers("task-owner", plan_with(tasks__owner="nobody"), v2_plan())
        self.assertEqual(got[0]["message"], "task T-domain: owner 'nobody' is not a roster entry")

    def test_task_milestone(self):
        got = self.assertTriggers("task-milestone", plan_with(tasks__milestone="M9"), v2_plan())
        self.assertEqual(got[0]["message"], "task T-domain: milestone 'M9' does not exist")
        got = self.assertTriggers("task-milestone", plan_with(tasks__milestone=None), v2_plan())
        self.assertEqual(got[0]["message"], "task T-domain: set a milestone (the plan has milestones)")
        # without milestones a task needs none, and naming one is an error
        none = v2_plan()
        none["milestones"] = []
        for f in none["features"]:
            f.pop("milestone", None)
        none["tasks"][0].pop("milestone")
        self.assertEqual([c for c in codes(none) if c == "task-milestone"], [])
        none["tasks"][0]["milestone"] = "M1"
        self.assertIn("task-milestone", codes(none))

    def test_task_dependency(self):
        good = v2_plan()
        add_task(good, dependsOn=["T-domain", "F-inbox"])  # a task and a feature are both fine
        self.assertEqual([c for c in codes(good) if c == "task-dependency"], [])
        bad = v2_plan()
        add_task(bad, dependsOn=["T-nope"])
        got = self.assertTriggers("task-dependency", bad, good)
        self.assertEqual(got[0]["message"], "task T-extra: dependsOn 'T-nope' names no task or feature")
        # a milestone or an unknown is neither a task nor a feature
        other = v2_plan()
        add_task(other, dependsOn=["M1", "U-offline"])
        self.assertEqual(codes(other).count("task-dependency"), 2)

    def test_task_dependency_loops(self):
        loop = v2_plan()
        loop["tasks"][0]["dependsOn"] = ["T-b"]
        add_task(loop, id="T-b", dependsOn=["T-c"])
        add_task(loop, id="T-c", dependsOn=["T-domain"])
        got = [f for f in findings(loop) if f["code"] == "task-dependency"]
        self.assertEqual([f["message"] for f in got], ["task dependency loop: T-domain -> T-b -> T-c -> T-domain"])
        self.assertEqual(validate.classify_error(got[0]["message"]), "task-dependency")
        self.assertTrue(got[0]["next"])
        selfdep = v2_plan()
        selfdep["tasks"][0]["dependsOn"] = ["T-domain"]
        self.assertEqual([f["message"] for f in findings(selfdep) if f["code"] == "task-dependency"],
                         ["task dependency loop: T-domain -> T-domain"])
        # a diamond and a chain are not loops
        fine = v2_plan()
        add_task(fine, id="T-a", dependsOn=["T-domain"])
        add_task(fine, id="T-b", dependsOn=["T-domain"])
        add_task(fine, id="T-c", dependsOn=["T-a", "T-b"])
        add_task(fine, id="T-d", dependsOn=["T-c", "T-a"])
        self.assertEqual([c for c in codes(fine) if c == "task-dependency"], [])

    def test_two_loops_are_two_findings_and_a_long_chain_is_fine(self):
        p = v2_plan()
        p["tasks"] = [{"id": "T-a", "title": "a", "owner": "owner", "milestone": "M1", "dependsOn": ["T-b"]},
                      {"id": "T-b", "title": "b", "owner": "owner", "milestone": "M1", "dependsOn": ["T-a"]},
                      {"id": "T-c", "title": "c", "owner": "owner", "milestone": "M1", "dependsOn": ["T-d"]},
                      {"id": "T-d", "title": "d", "owner": "owner", "milestone": "M1", "dependsOn": ["T-c"]}]
        self.assertEqual(codes(p).count("task-dependency"), 2)
        chain = v2_plan()
        chain["tasks"] = [{"id": f"T-n{i}", "title": "n", "owner": "owner", "milestone": "M1",
                           "dependsOn": [f"T-n{i + 1}"] if i < 1500 else []} for i in range(1501)]
        self.assertEqual([c for c in codes(chain) if c == "task-dependency"], [])  # no recursion limit on a long chain

    def test_owner_action_human(self):
        agent = plan_with(tasks__owner="backend-lead")
        got = self.assertTriggers("owner-action-human", agent, v2_plan())
        self.assertEqual(got[0]["message"], "task T-domain: an owner action must be owned by a human roster entry, not backend-lead")
        self.assertEqual([c for c in codes(plan_with(tasks__owner="backend-lead", tasks__ownerAction=False))
                          if c == "owner-action-human"], [])
        # an owner that is no roster entry is task-owner, not also owner-action-human
        unknown = codes(plan_with(tasks__owner="nobody"))
        self.assertIn("task-owner", unknown)
        self.assertNotIn("owner-action-human", unknown)

    def test_ids(self):
        for section, bad in (("milestones", "m1"), ("milestones", "M1000"), ("milestones", "M"), ("tasks", "domain"),
                             ("tasks", "T-Domain"), ("tasks", "T-"), ("tasks", "T-" + "a" * 49)):
            p = v2_plan()
            p[section][0]["id"] = bad
            got = [f for f in findings(p) if f["code"] == "bad-id"]
            self.assertTrue(got, bad)
            self.assertIn(f"id '{bad}' has the wrong form", got[0]["message"])
            self.assertTrue(got[0]["next"])
        for section, ok in (("milestones", "M999"), ("tasks", "T-" + "a" * 48), ("tasks", "T-3d")):
            p = v2_plan()
            p[section][0]["id"] = ok
            self.assertEqual([f for f in findings(p) if f["code"] == "bad-id"], [], ok)

    def test_duplicate_and_missing_ids(self):
        p = v2_plan()
        p["milestones"].append(dict(p["milestones"][0]))
        p["tasks"].append(dict(p["tasks"][0]))
        p["tasks"].append({"title": "no id", "owner": "owner", "milestone": "M1"})
        p["milestones"].append({"name": "x", "goal": "y"})
        got = {f["message"]: f["code"] for f in findings(p)}
        self.assertEqual(got["duplicate milestone id: M1"], "duplicate-id")
        self.assertEqual(got["duplicate task id: T-domain"], "duplicate-id")
        self.assertEqual([c for m, c in got.items() if m.startswith("task without id:")], ["missing-id"])
        self.assertEqual([c for m, c in got.items() if m.startswith("milestone without id:")], ["missing-id"])

    def test_unusual_ids_never_break_classification(self):
        for bad_id in ("T x", "T:x", "", "  "):
            p = v2_plan()
            p["tasks"][0]["id"] = bad_id
            p["tasks"][0]["owner"] = "nobody"
            for f in findings(p):
                if f["entry"] and f["entry"].startswith("task"):
                    if f["code"] != "bad-id":
                        self.assertEqual(validate.classify_error(f["message"]), f["code"], f["message"])

    def test_the_codes_are_registered_in_order(self):
        listed = [c for _, c in validate.codes()]
        for _, code in roadmap.CODES:
            self.assertIn(code, listed)
        # new modules come before the 1.x list, whose broad regexes would otherwise take these messages
        self.assertLess(listed.index("task-incomplete"), listed.index("owner-not-in-roster"))
        for code, example in roadmap.CODE_EXAMPLES.items():
            self.assertEqual(validate.classify_error(example), code)

    def test_no_milestones_no_tasks_is_the_1x_plan(self):
        p = v2_plan()
        p["milestones"], p["tasks"] = [], []
        for f in p["features"]:
            f.pop("milestone", None)
        self.assertEqual(mine(p), [])
        v1 = json.loads(S.EXAMPLE.read_text(encoding="utf-8"))
        errors, _ = validate.validate_plan(v1)
        self.assertEqual(errors, [])


class MalformedTest(TempRoot):
    def test_wrong_types_are_load_errors_with_exit_1(self):
        base = v2_plan()
        cases = []
        for section in ("milestones", "tasks"):
            p = copy.deepcopy(base)
            p[section] = "M1"
            cases.append(p)
            p = copy.deepcopy(base)
            p[section] = [5]
            cases.append(p)
        p = copy.deepcopy(base)
        p["tasks"][0]["ownerAction"] = "yes"
        cases.append(p)
        p = copy.deepcopy(base)
        p["tasks"][0]["dependsOn"] = "T-x"
        cases.append(p)
        p = copy.deepcopy(base)
        p["milestones"][0]["due"] = 20270115
        cases.append(p)
        for plan in cases:
            code, err = self.load_exit(plan)
            self.assertEqual(code, 1, err)
            self.assertIn("blueprint.json:", err)

    def test_every_value_in_every_field_never_crashes_validation_context_or_views(self):
        base = v2_plan()
        fields = {"milestones": ["id", "name", "goal", "due", "exit", "extra"],
                  "tasks": ["id", "title", "owner", "milestone", "ownerAction", "dependsOn", "doneWhen", "notes", "extra"]}
        count = 0
        for section, names in fields.items():
            for name in names:
                for value in VALUES + [roadmap]:  # the last value is not JSON at all
                    for delete in (False, True):
                        plan = copy.deepcopy(base)
                        if delete:
                            plan[section][0].pop(name, None)
                        else:
                            plan[section][0][name] = value
                        count += 1
                        self.exercise(plan)
        for value in VALUES:
            for section in ("milestones", "tasks"):
                plan = copy.deepcopy(base)
                plan[section] = value
                self.exercise(plan)
                plan[section] = [value, base[section][0]]
                self.exercise(plan)
        self.assertGreater(count, 300)

    def exercise(self, plan):
        validate.validate_findings(plan)
        state = {"tasks": {"T-domain": "done", "T-x": {"status": ["done"]}}, "syncedAt": 5}
        ctx = {"root": self.root, "plan": plan, "state": state, "comp": {}, "unknowns": [5, {"id": "U-x", "blocks": 7}],
               "blocked": {"by": []}, "checks": {}, "features": [None, {"id": "F-a", "milestone": "M1", "criteria": 5}],
               "milestones": [], "tasks": [], "studies": []}
        roadmap.context(self.root, plan, state, ctx)
        roadmap_md.views(ctx)
        roadmap.map_payload(ctx)
        roadmap.status_json(ctx)
        roadmap.roster_sections(ctx)
        roadmap.status_lines(ctx, type("A", (), {"tasks": True, "milestone": None})())
        json.dumps(roadmap.map_payload(ctx), default=str)

    def test_state_that_is_not_what_the_tool_writes(self):
        plan = v2_plan()
        for state in ({}, {"tasks": []}, {"tasks": {"T-domain": []}}, {"tasks": {"T-domain": {"status": "finished"}}},
                      {"tasks": {"T-domain": {"status": "done", "by": 5, "at": [], "note": {}}}}):
            ctx = {"plan": plan, "state": state, "unknowns": [], "blocked": {}, "features": []}
            roadmap.context(self.root, plan, state, ctx)
            row = ctx["tasks"][0]
            self.assertEqual(row["status"], "done" if state.get("tasks", {}) == {"T-domain": {"status": "done", "by": 5, "at": [], "note": {}}} else "todo")
            self.assertTrue(row["by"] is None or isinstance(row["by"], str))


class TaskLadderTest(TempRoot):
    """set_task: who may set what (4.2, 4.4). Each test names the rule it guards."""

    def setUp(self):
        super().setUp()
        plan = v2_plan()
        plan["roster"].append({"id": "co-owner", "kind": "human", "role": "Second person"})
        add_task(plan, id="T-api-keys", title="Create the API keys", owner="backend-lead")
        add_task(plan, id="T-pay", title="Pay for the domain", owner="owner", ownerAction=True)
        self.write_plan(plan)
        self.plan = bplan.load_plan(self.root)

    def state_task(self, tid):
        return json.loads((self.root / ".blueprint" / "state.json").read_text(encoding="utf-8"))["tasks"][tid]

    def set(self, *a, **kw):
        return roadmap.set_task(self.root, self.plan, *a, **kw)

    def refused(self, error, code, *a, **kw):
        with self.assertRaises(BlueprintError) as cm:
            self.set(*a, **kw)
        self.assertEqual((cm.exception.error, cm.exception.code), (error, code))
        self.assertTrue(cm.exception.next, "a refusal says what to do next")
        self.assertTrue(cm.exception.entry)
        return cm.exception

    def test_the_owner_walks_the_ladder_and_it_is_recorded(self):
        for status in ("doing", "done", "todo", "blocked"):
            rec = self.set("T-api-keys", status, "backend-lead", note="waiting on a vendor" if status == "blocked" else None)
            self.assertEqual(rec, self.state_task("T-api-keys"))
            self.assertEqual((rec["status"], rec["by"]), (status, "backend-lead"))
            self.assertRegex(rec["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(self.state_task("T-api-keys")["note"], "waiting on a vendor")
        log = [json.loads(x) for x in (self.root / ".blueprint" / "log.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(e["event"], e["task"], e["status"]) for e in log],
                         [("task", "T-api-keys", s) for s in ("doing", "done", "todo", "blocked")])

    def test_tasks_are_kept_in_state_with_the_rest_of_it(self):
        self.set("T-api-keys", "doing", "backend-lead")
        self.set("T-pay", "done", "owner")
        state = json.loads((self.root / ".blueprint" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(state["tasks"]), ["T-api-keys", "T-pay"])
        self.assertEqual(state["stateVersion"], 2)

    def test_unknown_task_and_unknown_roster_id_change_nothing(self):
        e = self.refused("unknown-task", 1, "T-nope", "done", "owner")
        self.assertIn("status --tasks", e.next)
        e = self.refused("unknown-roster", 1, "T-api-keys", "done", "nobody")
        self.assertIn("backend-lead", e.next)
        self.refused("unknown-roster", 1, "T-api-keys", "done", "nobody", force=True)
        self.assertFalse((self.root / ".blueprint" / "state.json").exists())

    def test_a_status_outside_the_ladder_is_usage(self):
        self.refused("usage", 64, "T-api-keys", "finished", "backend-lead")

    def test_blocked_needs_a_note(self):
        self.refused("usage", 64, "T-api-keys", "blocked", "backend-lead")
        self.refused("usage", 64, "T-api-keys", "blocked", "backend-lead", note="   ")
        self.assertFalse((self.root / ".blueprint" / "state.json").exists())
        self.set("T-api-keys", "blocked", "backend-lead", note="waiting on DNS")
        self.set("T-api-keys", "doing", "backend-lead")  # the other statuses need no note

    def test_only_the_owner_sets_a_task(self):
        e = self.refused("wrong-owner", 3, "T-api-keys", "doing", "owner")
        self.assertIn("backend-lead", e.message)
        self.assertIn("--force", e.message)
        self.assertIn("task T-api-keys doing --by backend-lead", e.next)
        self.assertFalse((self.root / ".blueprint" / "state.json").exists())
        self.set("T-api-keys", "doing", "owner", force=True)  # the lead's call
        self.assertEqual(self.state_task("T-api-keys")["by"], "owner")

    def test_an_agent_never_marks_an_owner_action_done(self):
        for by in ("backend-lead", "evaluator"):
            e = self.refused("owner-action-human", 3, "T-pay", "done", by)
            self.assertIn("owner", e.next)
        # --force skips the owner check only; it does not make an agent a person
        self.refused("owner-action-human", 3, "T-pay", "done", "backend-lead", force=True)
        self.assertFalse((self.root / ".blueprint" / "state.json").exists())

    def test_owner_action_check_comes_before_the_owner_check(self):
        # an agent that is also not the owner is told the specific rule, whose answer --force cannot change
        self.assertEqual(self.refused("owner-action-human", 3, "T-pay", "done", "evaluator").error, "owner-action-human")

    def test_the_human_owner_marks_an_owner_action_done(self):
        self.set("T-pay", "done", "owner")
        self.assertEqual(self.state_task("T-pay")["status"], "done")

    def test_another_person_needs_force_to_mark_an_owner_action_done(self):
        self.refused("wrong-owner", 3, "T-pay", "done", "co-owner")
        self.set("T-pay", "done", "co-owner", force=True)
        self.assertEqual(self.state_task("T-pay")["by"], "co-owner")

    def test_the_owner_action_rule_is_about_done_only(self):
        # an agent still may not touch another's task (wrong-owner), but the owner may doing/blocked it, and an
        # owner action whose owner is an agent (a plan error validate reports) can be doing/todo but never done
        plan = v2_plan()
        plan["tasks"][0]["owner"] = "backend-lead"
        self.write_plan(plan)
        self.plan = bplan.load_plan(self.root)
        self.set("T-domain", "doing", "backend-lead")
        self.refused("owner-action-human", 3, "T-domain", "done", "backend-lead")

    def test_a_task_with_the_same_id_twice_is_the_first(self):
        plan = v2_plan()
        plan["tasks"].append({"id": "T-domain", "title": "again", "owner": "backend-lead", "milestone": "M1"})
        self.write_plan(plan)
        self.plan = bplan.load_plan(self.root)
        self.refused("wrong-owner", 3, "T-domain", "doing", "backend-lead")

    def test_a_state_with_a_wrong_tasks_value_is_replaced_not_crashed(self):
        (self.root / ".blueprint" / "state.json").write_text(json.dumps({"stateVersion": 2, "tasks": "x", "files": {}}), encoding="utf-8")
        self.set("T-api-keys", "doing", "backend-lead")
        self.assertEqual(self.state_task("T-api-keys")["status"], "doing")


class TaskCliTest(TempRoot):
    """The command through the real CLI: output, --json, exit codes (7.2)."""

    def setUp(self):
        super().setUp()
        plan = v2_plan()
        add_task(plan, id="T-api-keys", title="Create the API keys", owner="backend-lead")
        self.write_plan(plan)

    def run_json(self, *args):
        r = self.cli(*args, "--json")
        return r, json.loads(r.stdout)

    def test_done_by_the_owner(self):
        r = self.cli("task", "T-domain", "done", "--by", "owner")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "T-domain: done (by owner)\n", ""))
        r, out = self.run_json("task", "T-api-keys", "doing", "--by", "backend-lead")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(out, {"ok": True, "command": "task", "data": {"task": "T-api-keys", "status": "doing", "by": "backend-lead"}})

    def test_status_shows_it_and_a_second_call_overwrites(self):
        self.cli("task", "T-domain", "doing", "--by", "owner")
        self.cli("task", "T-domain", "done", "--by", "owner", "--note", "registered")
        out = json.loads(self.cli("status", "--json").stdout)
        self.assertEqual([(t["id"], t["status"], t["by"], t["note"]) for t in out["tasks"] if t["id"] == "T-domain"],
                         [("T-domain", "done", "owner", "registered")])
        m = out["milestones"][0]
        self.assertEqual(m["progress"]["tasks"], 2)
        self.assertEqual(m["progress"]["done"], 1)
        self.assertFalse(m["done"])

    def test_exit_codes_and_failure_envelopes(self):
        cases = [(("task", "T-nope", "done", "--by", "owner"), 1, "unknown-task"),
                 (("task", "T-domain", "done", "--by", "nobody"), 1, "unknown-roster"),
                 (("task", "T-api-keys", "blocked", "--by", "backend-lead"), 64, "usage"),
                 (("task", "T-api-keys", "done", "--by", "owner"), 3, "wrong-owner"),
                 (("task", "T-domain", "done", "--by", "backend-lead"), 3, "owner-action-human"),
                 (("task", "T-domain", "done", "--by", "backend-lead", "--force"), 3, "owner-action-human")]
        for args, exit_code, error in cases:
            r, out = self.run_json(*args)
            self.assertEqual((r.returncode, out["ok"], out["command"], out["error"]["code"]), (exit_code, False, "task", error), r.stderr)
            self.assertTrue(out["error"]["next"])
            self.assertTrue(out["error"]["entry"].startswith("task T-"))
            self.assertIn("error:", r.stderr)
            self.assertIn("next:", r.stderr)
        self.assertFalse((self.root / ".blueprint" / "state.json").exists())

    def test_argparse_usage_errors_keep_exit_2(self):
        self.assertEqual(self.cli("task", "T-domain", "finished", "--by", "owner").returncode, 2)
        self.assertEqual(self.cli("task", "T-domain", "done").returncode, 2)

    def test_the_blocked_note_is_kept(self):
        r = self.cli("task", "T-api-keys", "blocked", "--by", "backend-lead", "--note", "waiting on the vendor")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("blocked (waiting on the vendor)", self.cli("render").stdout + (self.root / ".blueprint" / "ROADMAP.md").read_text(encoding="utf-8"))

    def test_done_with_unmet_dependencies_warns_but_succeeds(self):
        plan = json.loads((self.root / ".blueprint" / "blueprint.json").read_text(encoding="utf-8"))
        plan["tasks"][0]["dependsOn"] = ["T-api-keys", "F-inbox", "T-nope"]
        self.write_plan(plan)
        r = self.cli("task", "T-domain", "done", "--by", "owner")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "T-domain: done (by owner)\n")
        self.assertEqual(r.stderr.count("warning:"), 1)
        for dep in ("T-api-keys", "F-inbox"):
            self.assertIn(dep, r.stderr)
        r, out = self.run_json("task", "T-domain", "done", "--by", "owner")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(out["data"]["warnings"]), 1)
        self.assertIn("T-api-keys", out["data"]["warnings"][0])
        # a dependency that is done leaves nothing to say, and other statuses never warn
        self.assertEqual(self.cli("task", "T-api-keys", "done", "--by", "backend-lead").stderr, "")
        r = self.cli("task", "T-domain", "doing", "--by", "owner")
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        plan["tasks"][0]["dependsOn"] = ["T-api-keys"]
        self.write_plan(plan)
        r, out = self.run_json("task", "T-domain", "done", "--by", "owner")
        self.assertNotIn("warnings", out["data"])
        self.assertEqual(r.stderr, "")

    def test_any_status_may_follow_any_status(self):
        for s in ("done", "todo", "blocked", "done", "doing", "todo"):
            extra = ["--note", "why"] if s == "blocked" else []
            self.assertEqual(self.cli("task", "T-api-keys", s, "--by", "backend-lead", *extra).returncode, 0, s)

    def test_force_for_the_lead(self):
        self.assertEqual(self.cli("task", "T-api-keys", "doing", "--by", "owner", "--force").returncode, 0)

    def test_a_version_1_plan_has_no_tasks(self):
        (self.root / ".blueprint" / "blueprint.json").write_text(S.EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        before = (self.root / ".blueprint" / "blueprint.json").read_bytes()
        r, out = self.run_json("task", "T-domain", "done", "--by", "owner")
        self.assertEqual((r.returncode, out["error"]["code"]), (1, "unknown-task"))
        self.assertEqual((self.root / ".blueprint" / "blueprint.json").read_bytes(), before)
        status = self.cli("status")
        self.assertNotIn("Roadmap", status.stdout)
        self.assertFalse(any((self.root / ".blueprint" / n).exists() for n in ("ROADMAP.md",)))

    def test_a_plan_that_does_not_load_is_a_load_error(self):
        (self.root / ".blueprint" / "blueprint.json").write_text("{", encoding="utf-8")
        r, out = self.run_json("task", "T-domain", "done", "--by", "owner")
        self.assertEqual((r.returncode, out["error"]["code"]), (1, "bad-json"))

    def test_status_flags(self):
        out = self.cli("status", "--tasks", "--milestone", "M1").stdout
        self.assertIn("Roadmap: 1 milestone(s) (done 0), tasks done 0 of 2", out)
        self.assertIn("T-domain  todo  owner (you)  Register snagdesk.example", out)
        self.assertIn("M1 Private beta · due 2027-01-15", out)
        self.assertIn("Waiting on a person: T-domain (owner)", out)
        r, out = self.run_json("status", "--milestone", "M1")  # status --json ignores the flags and carries the data
        r = self.cli("status", "--milestone", "M9")
        self.assertEqual(r.returncode, 1)
        self.assertIn("M9", r.stderr)
        self.assertIn("(milestones: M1)", r.stderr)
        self.assertIn("next: python .blueprint/bin/blueprint.py status", r.stderr)


def stub_features(**status):
    """What the features module would put in ctx["features"] (contract 8.5), for the example's two features."""
    def crit(cid, result):
        return {"id": cid, "scenario": "s", "action": "a", "expected": "e", "mustNot": "", "level": "integration",
                "result": result, "resultLevel": None, "resultAt": None, "stale": False, "evaluation": None}
    return [{"id": "F-log-snag", "title": "Log a snag", "milestone": "M1", "owner": "backend-lead", "draft": False,
             "status": status.get("log", "accepted"), "criteria": [crit("C1", "pass"), crit("C2", "pass")],
             "awaitingEvaluation": 0, "blockedBy": []},
            {"id": "F-inbox", "title": "Review snags in the inbox", "milestone": "M1", "owner": "frontend-engineer",
             "draft": False, "status": status.get("inbox", "verified"), "criteria": [crit("C1", "unknown")],
             "awaitingEvaluation": 1, "blockedBy": [],
             "statusText": "verified, 1 criterion awaits evaluation" if status.get("inbox", "verified") == "verified" else status.get("inbox")}]


class ViewTest(TempRoot):
    def setUp(self):
        super().setUp()
        self.plan_json = v2_plan()

    def ctx(self, plan_json=None, state=None, features=None, unknowns=None, blocked_by=None):
        self.write_plan(plan_json or self.plan_json)
        plan = bplan.load_plan(self.root)
        state = state if state is not None else {"syncedAt": "2027-01-10T09:00:00Z", "tasks": {}}
        ctx = {"root": self.root, "plan": plan, "state": state, "comp": {}, "unknowns": unknowns or [],
               "blocked": {"phases": set(), "milestones": set(), "features": set(), "by": blocked_by or {}},
               "checks": {}, "features": stub_features() if features is None else features, "milestones": [], "tasks": [],
               "studies": []}
        roadmap.context(self.root, plan, state, ctx)
        return ctx

    def test_roadmap_md_of_the_v2_example(self):
        ctx = self.ctx(state={"syncedAt": "2027-01-20T09:00:00Z", "tasks": {"T-domain": {"status": "doing", "by": "owner"}}},
                       blocked_by={"milestone:M1": ["U-offline"]},
                       unknowns=[{"id": "U-offline", "question": "Must the inbox work offline on site?", "severity": "high",
                                  "status": "investigating", "blocks": ["P2", "milestone:M1"], "evidenceFor": ["ev-1a2b3c4d5e6f7a8b"],
                                  "evidenceAgainst": [], "blocking": True,
                                  "experiment": {"plan": "Ask three site leads", "timebox": "3d", "exit": "Two of three need offline"}}])
        text = roadmap_md.views(ctx)["ROADMAP.md"]
        self.assertEqual(text, "\n".join([
            "# Snag Desk roadmap",
            "<!-- Generated by blueprint.py render. Do not edit. -->",
            "Milestones: 1 (done 0) · Features: accepted 1 of 2 · Tasks: done 0 of 1 · Open blocking questions: 1",
            "",
            "## M1 Private beta · due 2027-01-15 · 1 of 2 features accepted · 0 of 1 tasks done · overdue",
            "Goal: One site team logs and reviews snags · Exit: F-log-snag and F-inbox accepted",
            "",
            "| Feature | Status | Criteria passing | Owner |",
            "|---|---|---|---|",
            "| F-log-snag Log a snag | accepted | 2 of 2 | backend-lead |",
            "| F-inbox Review snags in the inbox | verified, 1 criterion awaits evaluation | 0 of 1 | frontend-engineer |",
            "",
            "| Task | Owner | Status |",
            "|---|---|---|",
            "| T-domain Register snagdesk.example and point it at the host (you) | owner | doing |",
            "",
            "Blocked by: U-offline (high, investigating)",
            "",
            "## Open questions",
            "",
            "| Unknown | Severity | Status | Blocks | Experiment | Evidence |",
            "|---|---|---|---|---|---|",
            "| U-offline Must the inbox work offline on site? | high | investigating | P2, milestone:M1 | 3d, exit: Two of three need offline | for 1 / against 0 |",
            "",
            "## Timeline",
            "",
            "| Due | Milestone | Status |",
            "|---|---|---|",
            "| 2027-01-15 | M1 Private beta | overdue |",
            ""]))

    def test_the_header_rule_of_section_8(self):
        for ctx in (self.ctx(), self.ctx(features=[])):
            text = roadmap_md.views(ctx)["ROADMAP.md"]
            self.assertTrue(any(l.startswith("<!-- Generated by blueprint.py render") for l in text.splitlines()[:5]))
            self.assertEqual(text.splitlines()[0], "# Snag Desk roadmap")
            self.assertTrue(text.endswith("\n") and not text.endswith("\n\n"))
            self.assertNotIn("\r", text)

    def test_deterministic(self):
        a = roadmap_md.views(self.ctx())
        b = roadmap_md.views(self.ctx())
        self.assertEqual(a, b)

    def test_nothing_for_a_plan_without_milestones_and_tasks(self):
        p = v2_plan()
        p["milestones"], p["tasks"] = [], []
        for f in p["features"]:
            f.pop("milestone")
        self.assertEqual(roadmap_md.views(self.ctx(p)), {})

    def test_tasks_only_and_milestones_only(self):
        p = v2_plan()
        p["milestones"] = []
        p["tasks"][0].pop("milestone")
        for f in p["features"]:
            f.pop("milestone")
        text = roadmap_md.views(self.ctx(p, features=[]))["ROADMAP.md"]
        self.assertIn("Milestones: 0 (done 0) · Features: accepted 0 of 2 · Tasks: done 0 of 1", text)
        self.assertIn("## Not in a milestone", text)
        self.assertIn("| F-log-snag Log a snag | planned | 0 of 2 | backend-lead |", text)  # the plan's features stand in
        self.assertNotIn("## M1", text)
        self.assertNotIn("## Timeline", text)
        q = v2_plan()
        q["tasks"], q["features"], q["files"], q["patterns"] = [], [], [], []
        q["milestones"].append({"id": "M2", "name": "GA", "goal": "Everyone"})
        text = roadmap_md.views(self.ctx(q, features=[]))["ROADMAP.md"]
        self.assertIn("## M2 GA · nothing in it yet", text)
        self.assertIn("## M1 Private beta · due 2027-01-15 · nothing in it yet", text)

    def test_a_milestone_is_done_only_when_every_feature_is_accepted_and_every_task_done(self):
        done = {"syncedAt": "2027-01-10T09:00:00Z", "tasks": {"T-domain": {"status": "done", "by": "owner"}}}
        ms = lambda **kw: self.ctx(state=done, features=stub_features(**kw))["milestones"][0]  # noqa: E731
        self.assertFalse(ms()["done"])  # F-inbox is verified, not accepted
        self.assertTrue(ms(inbox="accepted")["done"])
        for status in ("verified", "built", "failed", "blocked", "planned", "building"):
            self.assertFalse(ms(inbox=status)["done"], status)
        self.assertFalse(self.ctx(state={"tasks": {"T-domain": {"status": "doing"}}}, features=stub_features(inbox="accepted"))["milestones"][0]["done"])
        self.assertFalse(self.ctx(state={"tasks": {"T-domain": {"status": "blocked"}}}, features=stub_features(inbox="accepted"))["milestones"][0]["done"])

    def test_drafts_are_not_counted_and_never_make_a_milestone_wait(self):
        feats = stub_features(inbox="accepted")
        feats.append(dict(stub_features()[1], id="F-sketch", status="draft", draft=True, statusText="draft"))
        p = v2_plan()
        p["features"].append({"id": "F-sketch", "title": "Review snags in the inbox", "kind": "add", "owner": "frontend-engineer",
                              "milestone": "M1", "draft": True})
        ctx = self.ctx(p, state={"tasks": {"T-domain": {"status": "done"}}}, features=feats)
        m = ctx["milestones"][0]
        self.assertEqual(m["progress"], {"features": 2, "accepted": 2, "tasks": 1, "done": 1})
        self.assertTrue(m["done"])
        self.assertEqual(m["features"], ["F-log-snag", "F-inbox", "F-sketch"])
        text = roadmap_md.views(ctx)["ROADMAP.md"]
        self.assertIn("| F-sketch Review snags in the inbox | draft |", text)
        self.assertIn("Features: accepted 2 of 2", text)

    def test_an_empty_milestone_is_not_done(self):
        p = v2_plan()
        p["milestones"].append({"id": "M2", "name": "GA", "goal": "g"})
        self.assertFalse(self.ctx(p)["milestones"][1]["done"])

    def test_overdue_is_relative_to_syncedAt_not_the_clock(self):
        late = {"syncedAt": "2027-01-16T00:00:00Z", "tasks": {}}
        on_time = {"syncedAt": "2027-01-15T23:59:59Z", "tasks": {}}
        self.assertTrue(self.ctx(state=late)["milestones"][0]["overdue"])
        self.assertFalse(self.ctx(state=on_time)["milestones"][0]["overdue"])  # the due day itself is not late
        self.assertFalse(self.ctx(state={"syncedAt": None})["milestones"][0]["overdue"])
        self.assertFalse(self.ctx(state={})["milestones"][0]["overdue"])
        done = {"syncedAt": "2027-02-01T00:00:00Z", "tasks": {"T-domain": {"status": "done"}}}
        self.assertFalse(self.ctx(state=done, features=stub_features(inbox="accepted"))["milestones"][0]["overdue"])  # done is not overdue

    def test_the_status_text_is_the_features_modules_and_printed_as_given(self):
        f = stub_features()[1]
        self.assertEqual(roadmap.feature_status_text(f), "verified, 1 criterion awaits evaluation")
        f["statusText"] = "verified, 2 of 3 criteria await evaluation"
        self.assertEqual(roadmap.feature_status_text(f), "verified, 2 of 3 criteria await evaluation")
        del f["statusText"]  # a stub or an older features module: the bare status, no wording of our own
        self.assertEqual(roadmap.feature_status_text(f), "verified")
        for bad in (None, 5, ""):
            self.assertEqual(roadmap.feature_status_text(dict(f, statusText=bad)), "verified")
        self.assertEqual(roadmap.criteria_passing(stub_features()[0]), (2, 2))

    def test_the_message_texts_equal_the_cores_uniq(self):
        from bp import rules
        for section, items in (("milestone", [{"name": "x"}, {"id": "M1"}, {"id": "M1"}, {"id": ""}]),
                               ("task", [{"title": "t"}, {"id": "T-a"}, {"id": "T-a"}])):
            core = []
            rules.uniq(items, "id", section, lambda m, *a, **k: core.append(m))
            plan = {"milestones": [], "tasks": []}
            plan[section + "s"] = [dict(i) for i in items]
            mine_msgs = []
            roadmap.validate(plan, lambda m, **k: mine_msgs.append(m), lambda m, **k: None, {})
            ids = [m for m in mine_msgs if m.startswith(("duplicate ", f"{section} without id"))]
            self.assertEqual(ids, core)

    def test_features_without_a_milestone_and_tasks_without_one_come_last(self):
        p = v2_plan()
        add_task(p, id="T-loose", title="Loose", milestone=None)
        p["tasks"][-1].pop("milestone")
        p["milestones"] = [p["milestones"][0]]
        p["features"][1].pop("milestone")
        text = roadmap_md.views(self.ctx(p, features=[dict(f, milestone=None) if f["id"] == "F-inbox" else f for f in stub_features()]))["ROADMAP.md"]
        self.assertLess(text.index("## M1"), text.index("## Not in a milestone"))
        tail = text[text.index("## Not in a milestone"):]
        self.assertIn("F-inbox", tail)
        self.assertIn("T-loose", tail)
        self.assertNotIn("F-log-snag", tail)

    def test_milestones_keep_plan_order_and_the_timeline_is_in_date_order(self):
        p = v2_plan()
        p["milestones"] = [{"id": "M3", "name": "Later", "goal": "g", "due": "2027-06-01"},
                           {"id": "M1", "name": "Sooner", "goal": "g", "due": "2027-01-01"},
                           {"id": "M2", "name": "Undated", "goal": "g"},
                           {"id": "M4", "name": "Same day", "goal": "g", "due": "2027-01-01"}]
        for t in p["tasks"]:
            t["milestone"] = "M1"
        text = roadmap_md.views(self.ctx(p, features=[]))["ROADMAP.md"]
        heads = [l.split(" · ")[0] for l in text.splitlines() if l.startswith("## M")]
        self.assertEqual(heads, ["## M3 Later", "## M1 Sooner", "## M2 Undated", "## M4 Same day"])
        timeline = text[text.index("## Timeline"):].splitlines()
        self.assertEqual([l.split(" | ")[1].split(" ")[0] for l in timeline if l.startswith("| 2027")], ["M1", "M4", "M3"])

    def test_cells_survive_pipes_and_newlines(self):
        p = v2_plan()
        p["tasks"][0]["title"] = "Pay | then\nwait"
        text = roadmap_md.views(self.ctx(p))["ROADMAP.md"]
        self.assertIn("T-domain Pay \\| then wait (you)", text)

    def test_blocked_task_shows_its_note(self):
        ctx = self.ctx(state={"tasks": {"T-domain": {"status": "blocked", "note": "waiting on DNS"}}})
        self.assertIn("| owner | blocked (waiting on DNS) |", roadmap_md.views(ctx)["ROADMAP.md"])

    def test_open_questions_fall_back_to_the_plan_and_skip_closed_ones(self):
        p = v2_plan()
        p["unknowns"].append({"id": "U-done", "question": "q", "severity": "low", "status": "resolved"})
        p["unknowns"].append({"id": "U-low", "question": "q", "severity": "low", "status": "open", "blocks": ["P2"]})
        text = roadmap_md.views(self.ctx(p))["ROADMAP.md"]
        self.assertIn("U-offline", text)
        self.assertIn("U-low", text)
        self.assertNotIn("U-done", text)
        self.assertIn("Open blocking questions: 1", text)  # U-low is low severity: it never blocks
        crit = roadmap_md.views(self.ctx(p, unknowns=[]))["ROADMAP.md"]
        self.assertEqual(crit, text)

    def test_roster_section(self):
        sections = roadmap.roster_sections(self.ctx())
        self.assertEqual([t for t, _ in sections], ["Tasks"])
        self.assertIn("| owner | T-domain Register snagdesk.example and point it at the host (you) | todo |", sections[0][1])
        p = v2_plan()
        p["tasks"] = []
        self.assertEqual(roadmap.roster_sections(self.ctx(p)), [])

    def test_payload_shapes(self):
        ctx = self.ctx(state={"tasks": {"T-domain": {"status": "done", "by": "owner", "at": "2027-01-01T00:00:00Z", "note": "ok"}}})
        payload = roadmap.map_payload(ctx)
        self.assertEqual(set(payload), {"milestones", "tasks", "roadmap"})
        self.assertEqual(roadmap.status_json(ctx), {k: payload[k] for k in ("milestones", "tasks")})
        m, t = payload["milestones"][0], payload["tasks"][0]
        self.assertEqual(set(m), {"id", "name", "goal", "due", "exit", "features", "tasks", "progress", "done", "overdue", "blockedBy"})
        self.assertEqual(set(m["progress"]), {"features", "accepted", "tasks", "done"})
        self.assertEqual(set(t), {"id", "title", "owner", "milestone", "ownerAction", "dependsOn", "doneWhen", "status", "by", "at", "note"})
        self.assertEqual((t["status"], t["by"], t["note"], t["ownerAction"]), ("done", "owner", "ok", True))
        self.assertEqual(json.loads(json.dumps(payload)), payload)

    def test_the_map_payload_merges_without_a_key_collision(self):
        ctx = self.ctx()
        out = merged("map_payload", ctx, {"rows": []})  # raises RuntimeError on a key two modules set
        self.assertEqual(out["milestones"], ctx["milestones"])
        self.assertEqual(out["tasks"], ctx["tasks"])

    def test_status_lines_are_empty_for_a_plan_without_roadmap_and_tell_the_unknown_milestone(self):
        p = v2_plan()
        p["milestones"], p["tasks"] = [], []
        for f in p["features"]:
            f.pop("milestone")
        ctx = self.ctx(p)
        self.assertEqual(roadmap.status_lines(ctx, type("A", (), {})()), [])
        with self.assertRaises(BlueprintError) as cm:
            roadmap.status_lines(ctx, type("A", (), {"milestone": "M1"})())
        self.assertEqual(cm.exception.error, "unknown-milestone")

    def test_milestone_blocked_by_an_unknown_is_in_the_payload(self):
        ctx = self.ctx(blocked_by={"milestone:M1": ["U-offline"], "phase:P2": ["U-x"]})
        self.assertEqual(ctx["milestones"][0]["blockedBy"], ["U-offline"])


class RenderTest(TempRoot):
    """The view through render_all with the real registry (whatever other modules are built)."""

    def test_render_writes_and_tracks_roadmap_md(self):
        plan = v2_plan()
        self.write_plan(plan)
        loaded = bplan.load_plan(self.root)
        state = bplan.load_state(self.root)
        quiet(render_all, self.root, loaded, state, quiet=True)
        text = (self.root / ".blueprint" / "ROADMAP.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Snag Desk roadmap\n<!-- Generated by blueprint.py render"))
        self.assertIn("ROADMAP.md", bplan.load_state(self.root)["generatedViews"])
        self.assertEqual(quiet(render_all, self.root, loaded, bplan.load_state(self.root), quiet=True, check=True)[0], [])
        (self.root / ".blueprint" / "ROADMAP.md").write_text("edited\n", encoding="utf-8")
        self.assertIn("ROADMAP.md", quiet(render_all, self.root, loaded, bplan.load_state(self.root), quiet=True, check=True)[0])

    def test_the_file_goes_when_the_plan_has_nothing_for_it(self):
        self.write_plan(v2_plan())
        loaded = bplan.load_plan(self.root)
        quiet(render_all, self.root, loaded, bplan.load_state(self.root), quiet=True)
        plan = v2_plan()
        plan["milestones"], plan["tasks"] = [], []
        for f in plan["features"]:
            f.pop("milestone")
        self.write_plan(plan)
        quiet(render_all, self.root, bplan.load_plan(self.root), bplan.load_state(self.root), quiet=True)
        self.assertFalse((self.root / ".blueprint" / "ROADMAP.md").exists())

    def test_a_plan_without_the_new_sections_writes_no_view_and_no_key(self):
        plan = json.loads(S.EXAMPLE.read_text(encoding="utf-8"))
        self.write_plan(plan)
        quiet(render_all, self.root, bplan.load_plan(self.root), bplan.load_state(self.root), quiet=True)
        self.assertFalse((self.root / ".blueprint" / "ROADMAP.md").exists())
        state = json.loads((self.root / ".blueprint" / "state.json").read_text(encoding="utf-8")) if (self.root / ".blueprint" / "state.json").exists() else {}
        self.assertNotIn("generatedViews", state)

    def test_render_cli_prints_the_wrote_line(self):
        self.write_plan(v2_plan())
        self.assertIn("wrote .blueprint/ROADMAP.md", self.cli("render").stdout)

    def test_the_map_carries_the_data_and_the_tab(self):
        self.write_plan(v2_plan())
        loaded = bplan.load_plan(self.root)
        state = bplan.load_state(self.root)
        from bp.views.context import build_context
        ctx = build_context(self.root, loaded, state)
        html = render_map(loaded, state, ctx["comp"], ctx)
        self.assertIn("BP_TABS.push", html)
        self.assertIn("id: 'roadmap'", html)
        data = json.loads(html.split('<script id="bp" type="application/json">')[1].split("</script>")[0].replace("<\\/", "</"))
        self.assertEqual(data["milestones"][0]["id"], "M1")
        self.assertEqual(data["tasks"][0]["id"], "T-domain")


TAB = S.SKILL / "assets" / "tab-roadmap.js"
HARNESS = r"""
const fs = require('fs');
function node(tag) {
  return { tag: tag, children: [], attrs: {}, className: '', textContent: '',
    setAttribute: function (k, v) { this.attrs[k] = String(v); }, appendChild: function (c) { this.children.push(c); return c; } };
}
global.document = { createElement: node, createTextNode: function (t) { return { text: String(t) }; } };
function text(n) { return n.text !== undefined ? n.text : (n.textContent || '') + n.children.map(text).join(' '); }
global.BP_TABS = [];
require('vm').runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
const tabs = BP_TABS;
if (tabs.length !== 1 || tabs[0].id !== 'roadmap') throw new Error('the tab did not register once as roadmap');
const out = [];
for (const D of JSON.parse(fs.readFileSync(0, 'utf8'))) {
  const host = node('section');
  const label = tabs[0].label(D);
  tabs[0].render(host, D, process.argv[2] === 'helpers' ? { el: function (t, a, k) { const n = node(t); if (a) for (const x in a) { if (x === 'text') n.textContent = a[x]; else if (x === 'cls') n.className = a[x]; else n.setAttribute(x, a[x]); } (k || []).forEach(function (c) { if (c != null) n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); }); return n; } } : undefined);
  out.push({ label: label, text: text(host) });
}
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TabRunTest(unittest.TestCase):
    """Run the tab in node against a minimal fake DOM: it draws what the plan has and never throws."""

    def run_tab(self, payloads, helpers=False):
        args = ["node", "-e", HARNESS, "--", str(TAB)] + (["helpers"] if helpers else [])
        r = subprocess.run(args, input=json.dumps(payloads), capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def payload(self, **kw):
        ctx = TempRoot()
        ctx.setUp()
        try:
            ctx.write_plan(v2_plan())
            plan = bplan.load_plan(ctx.root)
            state = {"syncedAt": "2027-02-01T00:00:00Z", "tasks": {"T-domain": {"status": "blocked", "note": "waiting on DNS"}}}
            full = {"root": ctx.root, "plan": plan, "state": state, "comp": {}, "unknowns": roadmap.open_questions({"plan": plan}),
                    "blocked": {"by": {"milestone:M1": ["U-offline"]}}, "features": stub_features(), "milestones": [], "tasks": [], "studies": []}
            roadmap.context(ctx.root, plan, state, full)
            d = dict(roadmap.map_payload(full), features=full["features"], unknowns=full["unknowns"],
                     featureGlyph={"accepted": "\u25C9", "verified": "\u2714"})
            d.update(kw)
            return d
        finally:
            ctx.tearDown()

    def test_it_draws_the_plan(self):
        for helpers in (False, True):
            (out,) = self.run_tab([self.payload()], helpers)
            self.assertEqual(out["label"], "Roadmap (1 overdue)")
            for want in ("M1 Private beta", "due 2027-01-15", "overdue", "T-domain", "you", "blocked (waiting on DNS)",
                         "1 of 2 features accepted", "F-log-snag", "verified, 1 criterion awaits evaluation", "U-offline",
                         "Blocked by:", "Timeline", "Open questions", "milestones done", "open blocking questions"):
                self.assertIn(want, out["text"])

    def test_it_does_not_throw_without_milestones_or_with_nonsense(self):
        empty = {"milestones": [], "tasks": []}
        bad = [{}, empty, {"milestones": "x", "tasks": 5, "features": {}, "unknowns": None},
               {"milestones": [None, 5, {}, {"id": "M1", "progress": "x", "features": "F", "tasks": [1], "due": 5}],
                "tasks": [None, {"id": 3, "status": "weird"}, {"id": "T-a", "milestone": "M1", "status": "weird"}],
                "features": [None, {"id": "F-a", "criteria": 3, "awaitingEvaluation": "x", "status": "verified"}],
                "unknowns": [None, {"id": "U", "status": "open", "experiment": 5, "blocks": 7}]},
               {"milestones": [{"id": "M1", "name": "x", "due": "2027-13-45"}, {"id": "M2", "name": "y", "due": "2027-01-01"}]}]
        outs = self.run_tab(bad)
        for o in outs[:3]:
            self.assertIn("No milestones or tasks yet", o["text"])
        self.assertIn("blueprint.py render", outs[0]["text"])  # the empty state names the next command
        self.assertEqual(outs[1]["label"], "Roadmap")

    def test_tasks_only_and_milestones_only(self):
        only_tasks = self.payload(milestones=[], features=[])
        only_ms = self.payload(tasks=[])
        a, b = self.run_tab([only_tasks, only_ms])
        self.assertIn("Not in a milestone", a["text"])
        self.assertIn("T-domain", a["text"])
        self.assertIn("M1 Private beta", b["text"])


class TabSourceTest(unittest.TestCase):
    def test_plain_javascript_without_network_or_dependencies(self):
        src = TAB.read_text(encoding="utf-8")
        self.assertIn("BP_TABS", src)
        self.assertIn("id: 'roadmap'", src)
        for bad in ("http://", "https://", "fetch(", "XMLHttpRequest", "import ", "require(", "innerHTML", "eval(", "WebSocket", "sendBeacon"):
            self.assertNotIn(bad, src, bad)
        self.assertNotIn("</script", src.lower())

    def test_it_is_picked_up_by_the_template_loader(self):
        from bp.views.render import tab_scripts
        self.assertTrue(any("id: 'roadmap'" in t for t in tab_scripts()))


class ModuleShapeTest(unittest.TestCase):
    def test_hooks_and_imports(self):
        for name in ("CODES", "CODE_EXAMPLES", "validate", "context", "roster_sections", "map_payload", "status_lines", "status_json", "set_task"):
            self.assertTrue(hasattr(roadmap, name), name)
        self.assertTrue(callable(roadmap_md.views))
        src = (S.PACKAGE / "roadmap.py").read_text(encoding="utf-8")
        for forbidden in ("bp.registry", "bp.validate", "bp.views", "bp.commands", "bp.cli", "bp.features", "bp.unknowns",
                          "bp.records", "bp.rules", "bp.evidence", "import subprocess", "urllib", "socket"):
            self.assertNotIn(forbidden, src, forbidden)

    def test_the_command_is_registered_and_built(self):
        self.assertTrue(registry.command_built("task"))
        import argparse
        p = argparse.ArgumentParser()
        from bp.commands import roadmap as cmd
        cmd.register("task", p)
        ns = p.parse_args(["T-x", "done", "--by", "owner", "--force", "--json"])
        self.assertEqual((ns.task, ns.status, ns.by, ns.force, ns.json), ("T-x", "done", "owner", True, True))

    def test_help_lists_task(self):
        r = S.run_script(S.LAUNCHER, ["task", "--help"], Path.cwd(), env=S.tool_env())
        self.assertEqual(r.returncode, 0)
        self.assertIn("--by", r.stdout)
        self.assertIn("--force", r.stdout)

    def test_every_file_parses_with_the_python_3_10_grammar(self):
        import ast
        for path in (S.PACKAGE / "roadmap.py", S.PACKAGE / "commands" / "roadmap.py", S.PACKAGE / "views" / "roadmap_md.py"):
            ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 10))


PREFIX = "python .blueprint/bin/blueprint.py"


class ReviewRoundOneTest(TempRoot):
    """The fixes of review round 1 (M1 to M3, N1 to N7)."""
    ctx = ViewTest.ctx

    def setUp(self):
        super().setUp()
        self.plan_json = v2_plan()

    # M1: a value is validated whole, so a trailing newline (or any stray whitespace) is never part of an id or a date
    def test_ids_and_dates_are_matched_whole(self):
        for bad in ("M2\n", "M2\r\n", " M2", "M\n2", "M2 "):
            p = v2_plan()
            p["milestones"].append({"id": bad, "name": "x", "goal": "y"})
            self.assertIn("bad-id", [f["code"] for f in findings(p) if f["entry"] and f["entry"].startswith("milestone")], repr(bad))
        for bad in ("T-x\n", "T-x\r\n", " T-x", "T-\nx", "T-x "):
            p = v2_plan()
            add_task(p, id=bad)
            self.assertIn("bad-id", [f["code"] for f in findings(p) if f["entry"] and f["entry"].startswith("task")], repr(bad))
        for bad in ("2027-01-15\n", "2027-01-15\r\n", " 2027-01-15", "2027-01-\n15", "2027-01-15 "):
            self.assertIn("milestone-due", codes(plan_with(milestones__due=bad)), repr(bad))
            self.assertFalse(roadmap.is_date(bad), repr(bad))

    # M2: membership is the plan's; the payload only supplies statuses
    def test_a_feature_the_payload_does_not_report_is_planned_and_blocks_done(self):
        done = {"syncedAt": "2027-01-10T09:00:00Z", "tasks": {"T-domain": {"status": "done"}}}
        for feats in ([stub_features(inbox="accepted")[0]],  # F-inbox left out
                      [stub_features(inbox="accepted")[0], dict(stub_features(inbox="accepted")[1], milestone=None)]):
            ctx = self.ctx(state=done, features=feats)
            m = ctx["milestones"][0]
            self.assertFalse(m["done"])
            self.assertEqual(m["progress"], {"features": 2, "accepted": 1, "tasks": 1, "done": 1})
            self.assertEqual(m["features"], ["F-log-snag", "F-inbox"])
            text = roadmap_md.views(ctx)["ROADMAP.md"]
            self.assertIn("1 of 2 features accepted", text)
            self.assertIn("Features: accepted 1 of 2", text)  # the summary agrees with the milestone
            self.assertIn("| F-inbox Review snags in the inbox | planned |", text)
            self.assertEqual(roadmap.counts(ctx)["milestonesDone"], 0)

    def test_the_plans_milestone_wins_over_the_payloads(self):
        feats = [dict(f, milestone="M7") for f in stub_features(inbox="accepted")]  # the payload disagrees: not trusted
        ctx = self.ctx(state={"tasks": {"T-domain": {"status": "done"}}}, features=feats)
        self.assertFalse(ctx["milestones"][0]["done"])
        self.assertEqual(ctx["milestones"][0]["progress"]["accepted"], 0)
        self.assertEqual(ctx["milestones"][0]["features"], ["F-log-snag", "F-inbox"])
        extra = stub_features(inbox="accepted") + [dict(stub_features()[0], id="F-ghost", milestone="M1", status="accepted")]
        self.assertEqual(self.ctx(features=extra)["milestones"][0]["features"], ["F-log-snag", "F-inbox"])  # not in the plan

    # M3: a roster id is human only when every entry with that id is
    def duplicate_roster(self, first_kind, second_kind):
        plan = v2_plan()
        i = next(i for i, r in enumerate(plan["roster"]) if r["id"] == "owner")
        human = plan["roster"].pop(i)
        plan["roster"][i:i] = [dict(human, kind=first_kind), dict(human, kind=second_kind)]
        self.write_plan(plan)
        return plan

    def test_duplicate_roster_entries_with_conflicting_kinds_are_not_human(self):
        for first, second in (("agent", "human"), ("human", "agent")):
            plan = self.duplicate_roster(first, second)
            self.assertIn("owner-action-human", codes(plan))
            with self.assertRaises(BlueprintError) as cm:
                roadmap.set_task(self.root, bplan.load_plan(self.root), "T-domain", "done", "owner")
            self.assertEqual((cm.exception.error, cm.exception.code), ("owner-action-human", 3))
        plan = self.duplicate_roster("human", "human")  # the same kind twice is still a person
        self.assertNotIn("owner-action-human", codes(plan))
        roadmap.set_task(self.root, bplan.load_plan(self.root), "T-domain", "done", "owner")

    def test_a_repeated_task_id_with_an_owner_action_is_one(self):
        plan = v2_plan()
        plan["tasks"].insert(0, {"id": "T-domain", "title": "x", "owner": "backend-lead", "milestone": "M1"})
        self.write_plan(plan)
        with self.assertRaises(BlueprintError) as cm:
            roadmap.set_task(self.root, bplan.load_plan(self.root), "T-domain", "done", "backend-lead")
        self.assertEqual(cm.exception.error, "owner-action-human")

    # N6: a person's done owner action is not for an agent to move
    def test_a_non_human_cannot_change_a_done_owner_action(self):
        plan = v2_plan()
        plan["roster"].append({"id": "co-owner", "kind": "human"})
        self.write_plan(plan)
        loaded = bplan.load_plan(self.root)
        roadmap.set_task(self.root, loaded, "T-domain", "done", "owner")
        for status in ("todo", "doing", "blocked", "done"):
            for force in (False, True):
                with self.assertRaises(BlueprintError) as cm:
                    roadmap.set_task(self.root, loaded, "T-domain", status, "backend-lead", note="n", force=force)
                self.assertEqual((cm.exception.error, cm.exception.code), ("owner-action-human", 3), (status, force))
        state = json.loads((self.root / ".blueprint" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual((state["tasks"]["T-domain"]["status"], state["tasks"]["T-domain"]["by"]), ("done", "owner"))
        roadmap.set_task(self.root, loaded, "T-domain", "todo", "co-owner", force=True)  # a person may

    # N1: every next runs as printed
    def refused_next(self, loaded, *args):
        with self.assertRaises(BlueprintError) as cm:
            roadmap.set_task(self.root, loaded, *args)
        return cm.exception.next

    def test_next_steps_are_commands_that_run(self):
        plan = v2_plan()
        plan["roster"].append({"id": "co-owner", "kind": "human"})
        add_task(plan, id="T-api", owner="backend-lead")
        self.write_plan(plan)
        loaded = bplan.load_plan(self.root)
        for args in (("T-nope", "done", "owner"), ("T-api", "done", "nobody"), ("T-api", "finished", "backend-lead"),
                     ("T-api", "blocked", "backend-lead"), ("T-api", "done", "owner"), ("T-domain", "done", "backend-lead")):
            nxt = self.refused_next(loaded, *args)
            self.assertTrue(nxt.startswith(PREFIX + " "), nxt)
            self.assertNotIn("<", nxt)
        self.assertIn("task T-domain done --by owner", self.refused_next(loaded, "T-domain", "done", "backend-lead"))
        self.assertIn("--by backend-lead", self.refused_next(loaded, "T-api", "blocked", "backend-lead"))
        self.assertIn("--by backend-lead", self.refused_next(loaded, "T-api", "done", "owner"))

    def test_version_1_next_is_migrate(self):
        (self.root / ".blueprint" / "blueprint.json").write_text(S.EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        out = json.loads(self.cli("task", "T-domain", "done", "--by", "owner", "--json").stdout)
        self.assertEqual(out["error"]["next"], PREFIX + " migrate")
        ctx = {"plan": bplan.load_plan(self.root), "milestones": [], "tasks": []}
        with self.assertRaises(BlueprintError) as cm:
            roadmap.status_lines(ctx, type("A", (), {"milestone": "M1"})())
        self.assertEqual(cm.exception.next, PREFIX + " migrate")

    def test_unknown_milestone_next_runs(self):
        with self.assertRaises(BlueprintError) as cm:
            roadmap.status_lines(self.ctx(), type("A", (), {"milestone": "M9"})())
        self.assertEqual(cm.exception.next, PREFIX + " status")

    # N2 and N3: one line per value; no raw HTML
    def test_status_tasks_prints_one_line_per_task(self):
        ctx = self.ctx(state={"tasks": {"T-domain": {"status": "blocked", "note": "waiting\n  on\r\nDNS\t now"}}})
        lines = roadmap.status_lines(ctx, type("A", (), {"tasks": True, "milestone": None})())
        task_lines = [l for l in lines if "T-domain" in l and "blocked" in l]
        self.assertEqual(len(task_lines), 1)
        self.assertIn("[waiting on DNS now]", task_lines[0])
        self.assertFalse(any("\n" in l or "\r" in l for l in lines))

    def test_plan_text_in_roadmap_md_is_escaped_and_on_one_line(self):
        p = v2_plan()
        p["app"]["name"] = "<b>Snag</b> & Co"
        p["milestones"][0]["name"] = "Beta <img src=x onerror=alert(1)>\n## Injected"
        p["milestones"][0]["goal"] = "a\n# Heading\n| x | y |"
        p["tasks"][0]["title"] = "Pay <script>x</script> & go\n## Fake | cell"
        text = roadmap_md.views(self.ctx(p, state={"tasks": {"T-domain": {"status": "blocked", "note": "<i>n</i>\n| z"}}}))["ROADMAP.md"]
        for raw in ("<b", "<img", "<script", "<i>"):
            self.assertNotIn(raw, text)
        self.assertIn("&lt;b>Snag&lt;/b> &amp; Co", text)
        heads = [l for l in text.splitlines() if l.startswith("#")]
        self.assertEqual(len(heads), 4, heads)  # title, M1, Open questions, Timeline: no value made a heading
        self.assertFalse(any(l.startswith("| x") for l in text.splitlines()))
        self.assertIn("\\| cell", text)

    # N7
    def test_the_timeline_says_when_there_is_no_sync(self):
        line = "Not synced yet: overdue is not shown."
        self.assertIn(line, roadmap_md.views(self.ctx(state={}))["ROADMAP.md"])
        self.assertIn(line, roadmap_md.views(self.ctx(state={"syncedAt": None}))["ROADMAP.md"])
        self.assertNotIn(line, roadmap_md.views(self.ctx())["ROADMAP.md"])
        p = v2_plan()
        p["milestones"][0].pop("due")
        self.assertNotIn(line, roadmap_md.views(self.ctx(p, state={}))["ROADMAP.md"])  # no timeline, nothing to say

    # N4: the tab reads what ROADMAP.md reads
    def test_the_payload_carries_the_numbers_roadmap_md_prints(self):
        ctx = self.ctx(features=[], unknowns=[])
        extra = roadmap.map_payload(ctx)["roadmap"]
        self.assertEqual(extra["counts"], roadmap.counts(ctx))
        self.assertEqual(sorted(f["id"] for f in extra["features"]), ["F-inbox", "F-log-snag"])
        self.assertEqual([u["id"] for u in extra["unknowns"]], ["U-offline"])
        self.assertNotIn("roadmap", roadmap.status_json(ctx))
        json.dumps(extra)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TabRoundOneTest(TabRunTest):
    """The tab and ROADMAP.md tell the same story without the features and unknowns modules, and ids are inert."""

    def test_same_numbers_without_features_and_unknowns(self):
        helper = ViewTest("test_deterministic")
        helper.setUp()
        self.addCleanup(helper.tearDown)
        ctx = helper.ctx(features=[], unknowns=[])
        d = dict(roadmap.map_payload(ctx), features=[], unknowns=[], syncedAt=None)
        d["milestones"][0]["due"] = "2027-13-45"
        (out,) = self.run_tab([d])
        text = out["text"]
        self.assertIn("0 / 2", text)  # features accepted: the plan's two, none reported accepted
        self.assertIn("U-offline", text)  # the open questions card
        self.assertNotIn("Timeline", text)  # an invalid date is not on the timeline
        d["milestones"][0]["due"] = "2027-01-15"
        (out,) = self.run_tab([d])
        self.assertIn("Timeline", out["text"])
        self.assertIn("Not synced yet: overdue is not shown.", out["text"])

    def test_ids_are_not_looked_up_through_the_prototype(self):
        d = {"milestones": [{"id": "constructor", "name": "C", "goal": "", "features": ["__proto__", "toString"], "tasks": ["hasOwnProperty"], "progress": {}},
                            {"id": "__proto__", "name": "P", "features": [], "tasks": [], "progress": {}}],
             "tasks": [{"id": "hasOwnProperty", "title": "T", "owner": "o", "milestone": "constructor", "status": "todo"},
                       {"id": "T-loose", "title": "L", "owner": "o", "milestone": "toString", "status": "todo"}],
             "features": [{"id": "F-x", "title": "X", "milestone": "__proto__", "status": "planned", "criteria": []}], "unknowns": []}
        (out,) = self.run_tab([d])
        for want in ("constructor C", "__proto__ P", "hasOwnProperty", "Not in a milestone", "T-loose", "planned"):
            self.assertIn(want, out["text"])
        self.assertNotIn("function", out["text"])


class ReviewRoundTwoTest(TempRoot):
    """Review round 2: the plan decides what is a draft, duplicates fail closed, escaping, copies, plan-fix nexts."""
    ctx = ViewTest.ctx

    def setUp(self):
        super().setUp()
        self.plan_json = v2_plan()

    def milestone(self, features, state=None):
        done = {"syncedAt": "2027-01-10T09:00:00Z", "tasks": {"T-domain": {"status": "done"}}}
        return self.ctx(state=state or done, features=features)

    # M2a: the plan, not the payload, says what a draft is
    def test_a_payload_draft_status_for_a_plan_feature_that_is_not_a_draft_is_planned(self):
        ok = stub_features(inbox="accepted")
        ctx = self.milestone([ok[0], dict(ok[1], status="draft", statusText="draft")])
        m = ctx["milestones"][0]
        self.assertFalse(m["done"])
        self.assertEqual(m["progress"], {"features": 2, "accepted": 1, "tasks": 1, "done": 1})
        self.assertIn("Features: accepted 1 of 2", roadmap_md.views(ctx)["ROADMAP.md"])
        row = next(f for f in roadmap.feature_rows(ctx["plan"], ctx) if f["id"] == "F-inbox")
        self.assertEqual((row["status"], row["draft"]), ("planned", False))
        self.assertFalse(roadmap.is_draft({"draft": False, "status": "draft"}))

    def test_a_plan_draft_stays_a_draft_whatever_the_payload_says(self):
        p = v2_plan()
        p["features"][1]["draft"] = True
        ok = stub_features(inbox="accepted")
        ctx = self.ctx(p, state={"tasks": {"T-domain": {"status": "done"}}}, features=[ok[0], dict(ok[1], draft=False, status="accepted")])
        row = next(f for f in roadmap.feature_rows(ctx["plan"], ctx) if f["id"] == "F-inbox")
        self.assertEqual((row["status"], row["draft"]), ("draft", True))
        self.assertEqual(ctx["milestones"][0]["progress"]["features"], 1)

    # M2b: entries for one id that disagree are not trusted
    def test_duplicate_payload_entries_that_disagree_are_planned(self):
        ok = stub_features(inbox="accepted")
        ctx = self.milestone([ok[0], ok[1], dict(ok[1], status="failed", statusText="failed")])
        self.assertFalse(ctx["milestones"][0]["done"])
        self.assertEqual(ctx["milestones"][0]["progress"]["accepted"], 1)
        ctx = self.milestone([ok[0], ok[1], dict(ok[1])])  # identical duplicates are fine
        self.assertTrue(ctx["milestones"][0]["done"])

    def test_a_payload_entry_without_a_milestone_key_is_used_for_the_plans_milestone(self):
        ok = stub_features(inbox="accepted")
        ctx = self.milestone([{k: v for k, v in f.items() if k != "milestone"} for f in ok])
        self.assertTrue(ctx["milestones"][0]["done"])

    # table cells: backslash first, then the pipe
    def test_backslashes_and_pipes_in_cells(self):
        self.assertEqual(roadmap.cell("a\\|b"), "a\\\\\\|b")
        self.assertEqual(roadmap.cell("a|b\\"), "a\\|b\\\\")
        p = v2_plan()
        p["tasks"][0]["title"] = "a\\|b"
        ctx = self.ctx(p)
        self.assertIn("T-domain a\\\\\\|b (you)", roadmap_md.views(ctx)["ROADMAP.md"])
        self.assertIn("T-domain a\\\\\\|b (you)", roadmap.roster_sections(ctx)[0][1])

    # no aliasing: the payload is a copy
    def test_the_roadmap_payload_shares_nothing_with_the_plan_or_the_features_payload(self):
        feats = stub_features()
        ctx = self.ctx(features=feats)
        out = roadmap.map_payload(ctx)["roadmap"]
        out["features"][0]["criteria"].append("x")
        out["features"][0]["criteria"][0]["id"] = "CHANGED"
        out["features"][0]["blockedBy"].append("U-x")
        self.assertEqual([c["id"] for c in feats[0]["criteria"]], ["C1", "C2"])
        self.assertEqual(feats[0]["blockedBy"], [])
        plan_only = self.ctx(features=[])
        out = roadmap.map_payload(plan_only)["roadmap"]
        out["features"][0]["criteria"][0]["scenario"] = "CHANGED"
        out["unknowns"][0]["blocks"].append("P9")
        self.assertNotEqual(plan_only["plan"]["features"][0]["criteria"][0]["scenario"], "CHANGED")
        self.assertEqual(plan_only["plan"]["unknowns"][0]["blocks"], ["P2"])

    # next on a plan that already fails validation: fix the plan first
    def test_next_on_a_broken_plan_is_to_fix_it(self):
        plan = v2_plan()
        plan["tasks"][0]["owner"] = "backend-lead"  # an owner action owned by an agent
        self.write_plan(plan)
        loaded = bplan.load_plan(self.root)
        for args in (("T-domain", "done", "evaluator"), ("T-domain", "doing", "owner"), ("T-domain", "doing", "nobody")):
            with self.assertRaises(BlueprintError) as cm:
                roadmap.set_task(self.root, loaded, *args)
            self.assertTrue(cm.exception.next.startswith("fix .blueprint/blueprint.json ("), cm.exception.next)
            self.assertTrue(cm.exception.next.endswith(f"then run: {PREFIX} validate"), cm.exception.next)
        plan = self.duplicate_kinds()
        loaded = bplan.load_plan(self.root)
        with self.assertRaises(BlueprintError) as cm:
            roadmap.set_task(self.root, loaded, "T-domain", "doing", "backend-lead")
        self.assertIn("different kinds", cm.exception.next)
        self.assertTrue(cm.exception.next.startswith("fix .blueprint/blueprint.json ("))

    def duplicate_kinds(self):
        plan = v2_plan()
        human = next(r for r in plan["roster"] if r["id"] == "owner")
        plan["roster"].append(dict(human, kind="agent"))
        self.write_plan(plan)
        return plan

    # the other surviving mutations
    def test_status_milestone_prints_one_line_per_task_and_feature(self):
        p = v2_plan()
        p["tasks"][0]["title"] = "Register\n## the domain"
        p["tasks"][0]["owner"] = "owner"
        p["features"][0]["title"] = "Log\na snag"
        ctx = self.ctx(p, features=[dict(stub_features()[0], statusText="accepted\nnow"), stub_features()[1]])
        lines = roadmap.status_lines(ctx, type("A", (), {"milestone": "M1", "tasks": False})())
        self.assertFalse(any("\n" in l for l in lines))
        self.assertIn("    task T-domain  todo  owner (you)  Register ## the domain", lines)
        self.assertIn("    feature F-log-snag  accepted now", lines)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TabRoundTwoTest(TabRunTest):
    def test_the_tab_date_rule_rejects_trailing_characters(self):
        base = {"milestones": [{"id": "M1", "name": "n", "due": "x", "features": [], "tasks": [], "progress": {}}], "tasks": [],
                "features": [], "unknowns": [], "syncedAt": "2027-01-01T00:00:00Z"}
        bad = ["2027-01-15\n", "2027-01-15\r", " 2027-01-15", "2027-01-15 ", "2027-02-29", "2027-13-01", "0000-01-01"]
        good = ["2027-01-15", "2028-02-29"]
        outs = self.run_tab([dict(base, milestones=[dict(base["milestones"][0], due=d)]) for d in bad + good])
        for d, o in zip(bad, outs):
            self.assertNotIn("Timeline", o["text"], repr(d))
        for d, o in zip(good, outs[len(bad):]):
            self.assertIn("Timeline", o["text"], repr(d))

    def test_the_tiles_read_the_roadmap_counts(self):
        d = {"milestones": [{"id": "M1", "name": "n", "features": [], "tasks": [], "progress": {}}], "tasks": [],
             "features": [], "unknowns": [],
             "roadmap": {"counts": {"milestones": 1, "milestonesDone": 0, "featuresAccepted": 7, "features": 9, "tasksDone": 0,
                                    "tasks": 0, "blocking": 0}, "features": [], "unknowns": []}}
        (out,) = self.run_tab([d])
        self.assertIn("7 / 9", out["text"])


if __name__ == "__main__":
    unittest.main()
