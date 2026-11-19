"""W0-B: the `migrate` command (contract 3.4), the migrate hints of 1.5 row d, and the version 2 example (2.10).

Every rule of 3.4 has a test that fails when the rule is broken: the cli tests run the real launcher as a subprocess
(exit status, stdout, stderr, the --json envelope, the bytes and modification time of every file); the in-process tests
call cmd_migrate to inject failures (an interrupted replace) and to run the malformed-plan sweep quickly.
"""
import contextlib
import copy
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import support as S

from bp import core, registry
from bp import migrate as bpmigrate
from bp import plan as bplan
from bp.commands import migrate as cmd
from bp.version import MIN_SCHEMA2_TOOL, TOOL_VERSION
from test_validate import TempRoot, v1_fixtures, v1_plan, v2_plan

EXAMPLE_V2 = S.SKILL / "references" / "example-blueprint-v2.json"
DRY_LINE = "version 1 -> 2; adds milestones, features, tasks, unknowns (empty); state.json gains stateVersion 2"
DONE_LINE = ("Migrated blueprint.json to schema version 2. Backup: .blueprint/blueprint.v1.json. "
             "Next: add milestones and features (see references/schema.md in the skill), then run validate.")
NEW = ("milestones", "features", "tasks", "unknowns")
OLD = 946684800 * 10 ** 9  # 2000-01-01, so "unchanged modification time" cannot be a same-tick coincidence


def snapshot(root):
    """{relative path: (bytes, mtime_ns)} of every file under .blueprint, except the project's own tool copy."""
    out = {}
    base = root / ".blueprint"
    for p in sorted(base.rglob("*")):
        if p.is_file() and "bin" not in p.relative_to(base).parts[:1]:
            out[p.relative_to(base).as_posix()] = (p.read_bytes(), p.stat().st_mtime_ns)
    return out


class MigrateCase(TempRoot):
    """A project folder whose .blueprint/blueprint.json is a version 1 plan (state.json from a 1.x init)."""

    def setUp(self):
        super().setUp()
        self.bp = self.root / ".blueprint"
        self.plan_path = self.bp / "blueprint.json"
        self.state_path = self.bp / "state.json"
        self.set_plan(v1_plan())

    def set_plan(self, plan, state=True):
        text = json.dumps(plan, indent=2, ensure_ascii=False) + "\n"
        self.plan_path.write_bytes(text.encode("utf-8"))
        os.utime(self.plan_path, ns=(OLD, OLD))
        if not state:
            self.state_path.unlink(missing_ok=True)
        if state:
            self.set_state({"files": {"crates/snag-api/src/main.rs": {"state": "built", "by": "backend-lead",
                                                                        "at": "2026-11-01T00:00:00Z", "note": None,
                                                                        "verified": {"sha": "ab" * 20, "at": "x"}}},
                            "patterns": {}, "unplanned": ["stray.txt"], "syncedAt": None, "head": None})

    def set_state(self, state):
        self.state_path.write_bytes((json.dumps(state, indent=2) + "\n").encode("utf-8"))
        os.utime(self.state_path, ns=(OLD, OLD))

    def plan(self):
        return json.loads(self.plan_path.read_text(encoding="utf-8"))

    def state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def migrate(self, *flags):
        return self.cli("migrate", *flags)

    def envelope(self, r):
        self.assertEqual(r.stderr.count("Traceback"), 0, r.stderr)
        return json.loads(r.stdout)


class WriteTest(MigrateCase):
    def test_migrates_and_prints_the_contract_line(self):
        original = self.plan_path.read_bytes()
        r = self.migrate()
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, DONE_LINE + "\n", ""))
        new = self.plan()
        self.assertEqual(new["version"], 2)
        self.assertEqual((self.bp / "blueprint.v1.json").read_bytes(), original)  # step 4: the original bytes

    def test_lossless_and_in_the_original_order(self):
        for name, plan in v1_fixtures().items():
            with self.subTest(plan=name):
                self.set_plan(plan, state=False)
                shutil.rmtree(self.bp / "blueprint.v1.json", ignore_errors=True)
                (self.bp / "blueprint.v1.json").unlink(missing_ok=True)
                self.assertEqual(self.migrate().returncode, 0)
                new = self.plan()
                self.assertEqual(list(new)[:len(plan)], list(plan))  # every original key first, in its order
                self.assertEqual([k for k in new if k not in plan], [k for k in NEW if k not in plan])
                back = {k: v for k, v in new.items() if not (k in NEW and k not in plan and v == [])}
                back["version"] = 1
                self.assertEqual(back, plan)  # removing what was added gives the input

    def test_state_is_upgraded_and_keeps_its_proofs(self):
        before = self.state()
        self.assertEqual(self.migrate().returncode, 0)
        after = self.state()
        self.assertEqual(list(after)[0], "stateVersion")
        self.assertEqual(after["stateVersion"], 2)
        self.assertEqual(after["tasks"], {})
        for k, v in before.items():
            self.assertEqual(after[k], v, k)  # including files[path].verified with its sha1

    def test_idempotent_and_the_second_run_writes_nothing(self):
        self.assertEqual(self.migrate().returncode, 0)
        shot = snapshot(self.root)
        r = self.migrate()
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "blueprint.json is already schema version 2. Nothing to do.\n", ""))
        self.assertEqual(snapshot(self.root), shot)

    def test_json_data(self):
        r = self.migrate("--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.envelope(r), {"ok": True, "command": "migrate", "data": {
            "from": 1, "to": 2, "dryRun": False,
            "written": [".blueprint/blueprint.json", ".blueprint/state.json", ".blueprint/blueprint.v1.json"]}})

    def test_log_event_and_views(self):
        self.assertEqual(self.migrate().returncode, 0)
        events = [json.loads(x) for x in (self.bp / "log.jsonl").read_text(encoding="utf-8").splitlines()]
        migrate_events = [e for e in events if e.get("event") == "migrate"]
        self.assertEqual(len(migrate_events), 1)
        self.assertEqual({k: migrate_events[0][k] for k in ("event", "from", "to", "tool")},
                         {"event": "migrate", "from": 1, "to": 2, "tool": TOOL_VERSION})
        self.assertTrue((self.bp / "STRUCTURE.md").is_file())  # step 6: render_all ran, validate has no errors
        self.assertTrue((self.bp / "map.html").is_file())

    def test_views_are_not_written_when_validate_has_errors(self):
        plan = v1_plan()
        plan["files"].append({"path": "x.rs", "action": "create", "module": "no-such-module", "phase": "P1", "purpose": "x"})
        self.set_plan(plan)
        r = self.migrate()
        self.assertEqual(r.returncode, 0, r.stderr)  # a plan with validation errors still migrates: only views are skipped
        self.assertEqual(self.plan()["version"], 2)
        self.assertFalse((self.bp / "STRUCTURE.md").exists())

    def test_after_migrating_the_plan_validates_and_status_has_no_schema_note(self):
        before = self.cli("validate", "--json")
        self.assertEqual(self.migrate().returncode, 0)
        after = json.loads(self.cli("validate", "--json").stdout)["data"]
        self.assertTrue(after["valid"])
        self.assertEqual([w["code"] for w in after["warnings"]],
                         [w["code"] for w in json.loads(before.stdout)["data"]["warnings"] if w["code"] != "schema-v1"])
        self.assertNotIn("Schema version 1", self.cli("status").stdout)

    def test_files_are_written_exactly_as_core_write_json_writes_them(self):
        plan = v1_plan()
        plan["app"]["summary"] = "Snags: café – 日本語"
        self.set_plan(plan)
        state = self.state()
        expected = Path(tempfile.mkdtemp(prefix="bp mig exp "))
        self.addCleanup(shutil.rmtree, expected, True)
        core.write_json(expected / "plan.json", bpmigrate.upgrade_plan(plan))
        core.write_json(expected / "state.json", bpmigrate.upgrade_state(state))
        outputs = []
        for _ in range(2):  # same bytes on the same platform, and the same on every run
            self.set_plan(plan)
            (self.bp / "blueprint.v1.json").unlink(missing_ok=True)
            self.assertEqual(self.migrate().returncode, 0)
            outputs.append((self.plan_path.read_bytes(), self.state_path.read_bytes()))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], ((expected / "plan.json").read_bytes(), (expected / "state.json").read_bytes()))
        self.assertIn("café".encode("utf-8"), outputs[0][0])  # not \u-escaped
        self.assertEqual(self.plan()["app"]["summary"], plan["app"]["summary"])

    def test_no_temporary_file_is_left(self):
        self.assertEqual(self.migrate().returncode, 0)
        self.assertEqual([p.name for p in self.bp.iterdir() if p.name.endswith(".tmp")], [])

    def test_a_plan_without_state_json_migrates_and_creates_no_state(self):
        self.set_plan(v1_plan(), state=False)
        self.assertEqual(self.migrate().returncode, 0)
        self.assertFalse(self.state_path.exists())
        self.assertEqual(self.plan()["version"], 2)

    def test_a_state_that_cannot_be_used_is_left_alone(self):
        for junk in (b"{not json", b"\xff\xfe\x00", b"[1, 2]"):
            with self.subTest(junk=junk):
                self.set_plan(v1_plan(), state=False)
                (self.bp / "blueprint.v1.json").unlink(missing_ok=True)
                self.state_path.write_bytes(junk)
                r = self.migrate()
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.state_path.read_bytes(), junk)  # never lose data
                self.assertIn("warning: .blueprint/state.json ", r.stderr)
                self.assertIn("it was left as it is. Fix or delete it, then run: python", r.stderr)
                self.assertNotIn("Error", r.stderr)  # plain words, no Python class names
                self.assertNotIn("Traceback", r.stderr)

    def test_a_version_2_state_beside_a_version_1_plan(self):
        self.set_state({"stateVersion": 2, "tasks": {"T-x": {"status": "done"}}, "files": {}})
        before = self.state_path.read_bytes()
        r = self.migrate("--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state_path.read_bytes(), before)  # nothing to upgrade, nothing rewritten
        self.assertEqual(self.envelope(r)["data"]["written"], [".blueprint/blueprint.json", ".blueprint/blueprint.v1.json"])

    def test_sections_already_present_are_kept(self):
        plan = v1_plan()
        plan["tasks"] = []
        plan["notes"] = {"keep": None}
        self.set_plan(plan, state=False)
        self.assertEqual(self.migrate("--dry-run").stdout, "version 1 -> 2; adds milestones, features, unknowns (empty)\n")
        self.assertEqual(self.migrate().returncode, 0)
        new = self.plan()
        self.assertEqual(new["notes"], {"keep": None})
        self.assertEqual([k for k in new if k in NEW], ["tasks", "milestones", "features", "unknowns"])


class BackupTest(MigrateCase):
    def test_an_existing_backup_is_never_overwritten(self):
        mine = b'{"version": 1, "mine": true}\n'
        (self.bp / "blueprint.v1.json").write_bytes(mine)
        original = self.plan_path.read_bytes()
        r = self.migrate("--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.bp / "blueprint.v1.json").read_bytes(), mine)
        # the plan being replaced is still kept: nothing is ever lost
        self.assertEqual((self.bp / "blueprint.v1.2.json").read_bytes(), original)
        self.assertEqual(self.cli("migrate").stdout, "blueprint.json is already schema version 2. Nothing to do.\n")
        self.assertEqual(self.envelope(r)["data"]["written"][-1], ".blueprint/blueprint.v1.2.json")

    def test_a_backup_of_the_same_bytes_is_reused(self):
        (self.bp / "blueprint.v1.json").write_bytes(self.plan_path.read_bytes())
        os.utime(self.bp / "blueprint.v1.json", ns=(OLD, OLD))
        r = self.migrate()
        self.assertEqual((r.returncode, r.stdout), (0, DONE_LINE + "\n"))
        self.assertEqual((self.bp / "blueprint.v1.json").stat().st_mtime_ns, OLD)
        self.set_plan(v1_plan())  # again, with the identical backup in place: it is reused and not listed as written
        self.assertEqual(self.envelope(self.migrate("--json"))["data"]["written"], [".blueprint/blueprint.json",
                                                                                  ".blueprint/state.json"])
        self.assertFalse((self.bp / "blueprint.v1.2.json").exists())


class DryRunTest(MigrateCase):
    def test_dry_run_prints_the_changes_and_writes_nothing(self):
        before = snapshot(self.root)
        r = self.migrate("--dry-run")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, DRY_LINE + "\n", ""))
        self.assertEqual(snapshot(self.root), before)  # bytes and modification times; no backup, no log, no views

    def test_without_the_write_the_version_1_file_keeps_bytes_and_mtime(self):
        shot = snapshot(self.root)
        for argv in (["migrate", "--dry-run"], ["migrate", "--dry-run", "--json"], ["migrate", "--dry-run", "--force"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv).returncode, 0)
                self.assertEqual(snapshot(self.root), shot)
        self.assertEqual(self.plan_path.stat().st_mtime_ns, OLD)

    def test_dry_run_json(self):
        r = self.migrate("--dry-run", "--json")
        self.assertEqual(self.envelope(r), {"ok": True, "command": "migrate",
                                            "data": {"from": 1, "to": 2, "written": [], "dryRun": True}})

    def test_dry_run_still_refuses_what_a_real_run_refuses(self):
        self.set_plan({"version": 3})
        self.assertEqual(self.migrate("--dry-run", "--json").returncode, 1)


class AlreadyVersion2Test(MigrateCase):
    def test_version_2_is_untouched(self):
        self.set_plan(v2_plan(), state=True)
        shot = snapshot(self.root)
        r = self.migrate()
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "blueprint.json is already schema version 2. Nothing to do.\n", ""))
        self.assertEqual(snapshot(self.root), shot)
        for flags in (["--dry-run"], ["--force"], ["--json"]):
            self.assertEqual(self.migrate(*flags).returncode, 0)
            self.assertEqual(snapshot(self.root), shot)

    def test_version_2_json(self):
        self.set_plan(v2_plan())
        self.assertEqual(self.envelope(self.migrate("--json")), {"ok": True, "command": "migrate", "data": {
            "from": 2, "to": 2, "written": [], "dryRun": False}})

    def test_version_2_is_decided_before_the_tool_copy_is_looked_at(self):
        self.set_plan(v2_plan())
        self.fake_copy("1.0.0")
        self.assertEqual(self.migrate().returncode, 0)  # step 1 precedes step 2

    def fake_copy(self, version):
        (self.bp / "bin").mkdir(exist_ok=True)
        (self.bp / "bin" / "blueprint.py").write_text("# old copy\n", encoding="utf-8")
        (self.bp / "bin" / "VERSION").write_text(version + "\n", encoding="utf-8")


class OldToolCopyTest(MigrateCase):
    def fake_copy(self, version=None):
        (self.bp / "bin").mkdir(exist_ok=True)
        (self.bp / "bin" / "blueprint.py").write_text("# old copy\n", encoding="utf-8")
        if version is not None:
            (self.bp / "bin" / "VERSION").write_text(version + "\n", encoding="utf-8")

    def test_an_old_copy_is_refused_and_nothing_is_written(self):
        self.fake_copy("1.8.0")
        shot = snapshot(self.root)
        r = self.migrate()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")
        self.assertIn("error: this project's tool copy (1.8.0) is older than " + MIN_SCHEMA2_TOOL, r.stderr)
        self.assertRegex(r.stderr, r'  next: python ".*blueprint\.py" init --refresh-tool --root ".*"')
        self.assertNotIn("<", r.stderr)
        self.assertEqual(snapshot(self.root), shot)

    def test_old_copy_json_envelope(self):
        self.fake_copy("1.3.0")
        r = self.migrate("--json")
        self.assertEqual(r.returncode, 1)
        err = self.envelope(r)
        self.assertEqual((err["ok"], err["command"], err["error"]["code"]), (False, "migrate", "old-tool-copy"))
        self.assertEqual(err["error"]["entry"], ".blueprint/bin")
        self.assertIn(" init --refresh-tool --root ", err["error"]["next"])
        self.assertTrue(err["error"]["next"].startswith('python "'))
        self.assertNotIn("<", err["error"]["next"])

    def test_a_copy_of_unreadable_version_counts_as_old_and_a_current_one_does_not(self):
        self.fake_copy(None)
        self.assertEqual(self.migrate("--dry-run", "--json").returncode, 1)
        self.fake_copy(MIN_SCHEMA2_TOOL)
        self.assertEqual(self.migrate("--dry-run").returncode, 0)
        self.fake_copy("1.9.0")
        self.assertEqual(self.migrate("--dry-run").returncode, 1 if (1, 9, 0) < tuple(int(x) for x in MIN_SCHEMA2_TOOL.split(".")) else 0)

    def test_only_the_leading_numbers_of_the_copy_version_count(self):
        self.fake_copy("2.0.0-rc1")
        self.assertEqual(self.migrate("--dry-run").returncode, 0)  # newer than 1.9.1, whatever follows the numbers
        self.fake_copy("1.10.0+local")
        self.assertEqual(self.migrate("--dry-run").returncode, 0)
        self.fake_copy("1.8.9-rc1")
        self.assertEqual(self.migrate("--dry-run").returncode, 1)

    def test_a_copy_with_no_readable_version_says_so(self):
        for text in (None, "garbage", ""):
            with self.subTest(version=text):
                self.fake_copy(text)
                r = self.migrate()
                self.assertEqual(r.returncode, 1)
                self.assertIn("error: this project's tool copy has no readable version", r.stderr)
                self.assertNotIn("unknown", r.stderr)
                self.assertIn("init --refresh-tool", r.stderr)

    def test_force_skips_the_check(self):
        self.fake_copy("1.8.0")
        r = self.migrate("--force")
        self.assertEqual((r.returncode, r.stdout), (0, DONE_LINE + "\n"), r.stderr)
        self.assertEqual(self.plan()["version"], 2)

    def test_no_copy_at_all_is_not_refused(self):
        self.assertFalse((self.bp / "bin").exists())
        self.assertEqual(self.migrate("--dry-run").returncode, 0)

    def test_the_old_check_does_not_apply_when_the_running_tool_is_that_copy(self):
        base = Path(tempfile.mkdtemp(prefix="bp mig copy "))
        self.addCleanup(shutil.rmtree, base, True)
        r = S.run_script(S.LAUNCHER, ["init", "--name", "Copy", "--root", base], base)
        self.assertEqual(r.returncode, 0, r.stderr)
        plan = {k: v for k, v in json.loads((base / ".blueprint" / "blueprint.json").read_text(encoding="utf-8")).items()
                if k not in NEW}
        plan["version"] = 1
        (base / ".blueprint" / "blueprint.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
        (base / ".blueprint" / "bin" / "VERSION").write_text("1.0.0\n", encoding="utf-8")  # what a stale VERSION says
        copy_ = base / ".blueprint" / "bin" / "blueprint.py"
        r = S.run_script(copy_, ["migrate", "--root", base], base)
        self.assertEqual((r.returncode, r.stdout), (0, DONE_LINE + "\n"), r.stderr)  # the copy itself runs it
        self.assertEqual(json.loads((base / ".blueprint" / "blueprint.json").read_text(encoding="utf-8"))["version"], 2)


class MalformedTest(MigrateCase):
    """A file that is not a loadable version 1 plan is never changed and never crashes the command."""

    def refused(self, content, code, flags=()):
        if isinstance(content, (dict, list)):
            content = json.dumps(content)
        self.plan_path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        os.utime(self.plan_path, ns=(OLD, OLD))
        shot = snapshot(self.root)
        r = self.migrate("--json", *flags)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        err = self.envelope(r)
        self.assertEqual((err["ok"], err["command"], err["error"]["code"]), (False, "migrate", code), err)
        self.assertEqual(snapshot(self.root), shot)
        human = self.migrate(*flags)
        self.assertEqual((human.returncode, human.stdout), (1, ""))
        self.assertTrue(human.stderr.strip())  # load errors keep the 1.x text; the others are `error:` lines
        self.assertNotIn("Traceback", human.stderr)
        return err

    def test_version_3_is_schema_too_new(self):
        err = self.refused({"version": 3, "files": []}, "schema-too-new")
        self.assertIn("schema version 3", err["error"]["message"])
        self.assertEqual(err["error"]["next"], "update the skill, then run init --refresh-tool")

    def test_version_that_is_not_a_whole_number(self):
        for bad in ('"1"', "true", "1.0", "null", "[1]", '"2"', "2.0"):
            with self.subTest(version=bad):
                self.refused(f'{{"version": {bad}, "files": []}}', "bad-shape")

    def test_missing_version_with_other_keys(self):
        err = self.refused({"app": {"name": "x"}, "files": [], "extra": 1}, "bad-shape")
        self.assertIn("version", err["error"]["message"])

    def test_version_below_1(self):
        for v in (0, -1):
            self.refused({"version": v}, "bad-shape")

    def test_not_an_object(self):
        for text in ("[]", '"x"', "5", "null", "true"):
            self.refused(text, "bad-shape")

    def test_unreadable_files(self):
        for content in (b"", b"{not json", b'{"version": 1,', b"\xff\xfe\x00bad", b"\xef\xbb\xbf{\"version\": 1}",
                        b"[" * 100000, b'{"a": ' + b"9" * 6000 + b', "version": 1}'):
            with self.subTest(content=content[:20]):
                self.refused(content, "bad-json")

    def test_a_huge_integer_is_refused_as_bad_json_with_the_file_unchanged(self):
        text = json.dumps(v1_plan())
        self.plan_path.write_bytes((text[:-1] + ', "big": ' + "9" * 5000 + "}").encode("utf-8"))
        os.utime(self.plan_path, ns=(OLD, OLD))
        shot = snapshot(self.root)
        for flags in (["--json"], ["--dry-run", "--json"]):
            with self.subTest(flags=flags):
                r = self.migrate(*flags)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertNotIn("Traceback", r.stderr)
                self.assertEqual(self.envelope(r)["error"]["code"], "bad-json")
                self.assertTrue(self.envelope(r)["error"]["next"])
                self.assertEqual(snapshot(self.root), shot)

    def test_no_blueprint_and_a_folder_in_its_place(self):
        self.plan_path.unlink()
        r = self.migrate("--json")
        self.assertEqual((r.returncode, self.envelope(r)["error"]["code"]), (1, "no-blueprint"))
        self.plan_path.mkdir()
        self.assertEqual(self.migrate("--json").returncode, 1)
        self.assertNotIn("Traceback", self.migrate().stderr)

    def test_version_1_with_wrongly_typed_sections_is_refused_unchanged(self):
        for key, value in (("files", "x"), ("features", "F-x"), ("tasks", {}), ("milestones", [1]), ("unknowns", "q"),
                           ("modules", 5), ("roster", {"id": "a"})):
            with self.subTest(key=key):
                plan = v1_plan()
                plan[key] = value
                self.set_plan(plan)
                shot = snapshot(self.root)
                r = self.migrate("--json")
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertEqual(self.envelope(r)["error"]["code"], "bad-shape")
                self.assertEqual(snapshot(self.root), shot)  # no backup either: nothing was started

    def test_sweep_of_wrong_types_never_crashes_and_always_writes_a_loadable_plan(self):
        values = [None, 0, 1, 1.5, True, "x", "", [], [1], [None], {}, {"a": 1}, [[]], [{}]]
        keys = ["app", "stack", "approach", "modules", "phases", "files", "patterns", "owners", "roster", "decisions",
                "ignore", "stores", "resources", "milestones", "features", "tasks", "unknowns", "extra"]
        self.set_plan(v1_plan(), state=False)
        migrated = refused = 0
        for key in keys:
            for value in values:
                plan = v1_plan()
                plan[key] = value
                self.plan_path.write_text(json.dumps(plan), encoding="utf-8")
                for stale in self.bp.glob("blueprint.v1*.json"):
                    stale.unlink()
                before = self.plan_path.read_bytes()
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    try:
                        cmd.cmd_migrate(Namespace(root=str(self.root), dry_run=False, force=True, json=False))
                        code = 0
                    except SystemExit as e:
                        code = e.code
                self.assertIn(code, (0, 1), (key, value, err.getvalue()))
                if code == 1:
                    refused += 1
                    self.assertEqual(self.plan_path.read_bytes(), before, (key, value))
                else:
                    migrated += 1
                    self.assertEqual((self.bp / "blueprint.v1.json").read_bytes(), before)
                    with contextlib.redirect_stderr(io.StringIO()):
                        loaded = bplan.load_plan(self.root)  # what migrate wrote is a plan every command can load
                    self.assertNotIn("_from", loaded)
                    self.assertEqual(loaded["version"], 2)
        self.assertGreater(migrated, 20)
        self.assertGreater(refused, 20)


class FailureTest(MigrateCase):
    def run_in_process(self, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cmd.cmd_migrate(Namespace(root=str(self.root), dry_run=False, force=False, json=kw.get("json", False)))
                return 0, out.getvalue(), err.getvalue(), None
            except SystemExit as e:
                return e.code, out.getvalue(), err.getvalue(), e

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can write a read-only file")
    def test_a_read_only_plan_is_refused_before_anything_is_written(self):
        mode = self.plan_path.stat().st_mode
        os.chmod(self.plan_path, stat.S_IREAD)
        self.addCleanup(os.chmod, self.plan_path, mode)
        shot = snapshot(self.root)
        r = self.migrate("--json")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        err = self.envelope(r)
        self.assertEqual(err["error"]["code"], "write-failed")
        self.assertIn("blueprint.json", err["error"]["entry"])
        self.assertEqual(snapshot(self.root), shot)  # no backup, no half-written state
        self.assertFalse((self.bp / "blueprint.v1.json").exists())

    def test_an_interrupted_replace_leaves_the_plan_whole_and_the_backup_kept(self):
        original = self.plan_path.read_bytes()
        real = os.replace

        def failing(src, dst, *a, **k):
            if Path(dst).name == "blueprint.json":
                raise PermissionError(13, "Permission denied")
            return real(src, dst, *a, **k)

        with mock.patch.object(cmd.os, "replace", failing):
            code, out, err, exc = self.run_in_process(json=True)
        self.assertEqual((code, exc.error), (1, "write-failed"))
        self.assertEqual(self.plan_path.read_bytes(), original)
        self.assertEqual((self.bp / "blueprint.v1.json").read_bytes(), original)
        self.assertEqual([p.name for p in self.bp.iterdir() if p.name.endswith(".tmp")], [])
        self.assertNotIn("stateVersion", self.state())  # state.json is written after the plan, so it was not touched
        # the retry finishes the job
        self.assertEqual(self.migrate().returncode, 0)
        self.assertEqual(self.plan()["version"], 2)
        self.assertEqual((self.bp / "blueprint.v1.json").read_bytes(), original)

    def test_a_state_that_cannot_be_replaced_is_a_warning_once_the_plan_is_migrated(self):
        real = os.replace

        def failing(src, dst, *a, **k):
            if Path(dst).name == "state.json":
                raise PermissionError(13, "Permission denied")
            return real(src, dst, *a, **k)

        before = self.state_path.read_bytes()
        with mock.patch.object(cmd.os, "replace", failing):
            code, out, err, exc = self.run_in_process(json=True)
        self.assertEqual((code, exc), (0, None), err)  # not a failure: the plan is migrated
        data = json.loads(out)["data"]
        self.assertEqual(data["written"], [".blueprint/blueprint.json", ".blueprint/blueprint.v1.json"])
        self.assertIn("warning: .blueprint/state.json could not be written (Permission denied). blueprint.json is "
                      "migrated; state.json was left as it is and is upgraded in memory whenever it is read", err)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual([p.name for p in self.bp.iterdir() if p.name.endswith(".tmp")], [])
        self.assertEqual(self.plan()["version"], 2)
        events = [json.loads(x)["event"] for x in (self.bp / "log.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertIn("migrate", events)  # the log and the views still happen
        self.assertTrue((self.bp / "STRUCTURE.md").is_file())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bplan.load_state(self.root)["stateVersion"], 2)  # the state upgrades in memory

    def test_a_failing_view_render_does_not_undo_the_migration(self):
        with mock.patch("bp.views.render.render_all", side_effect=RuntimeError("boom")):
            code, out, err, _ = self.run_in_process()
        self.assertEqual(code, 0)
        self.assertEqual(self.plan()["version"], 2)
        self.assertIn("warning: the views were not regenerated; run: python", err)
        self.assertNotIn("RuntimeError", err)
        self.assertEqual(out, DONE_LINE + "\n")

    def test_render_noise_never_reaches_json_stdout(self):
        def noisy(*a, **k):
            print("wrote .blueprint/X.md")
        with mock.patch("bp.views.render.render_all", noisy):
            code, out, err, _ = self.run_in_process(json=True)
        self.assertEqual(code, 0)
        json.loads(out)  # one JSON object, nothing else

    def test_the_log_is_written_after_the_plan(self):
        with mock.patch.object(cmd, "append_log", side_effect=OSError(13, "Permission denied")):
            code, out, err, _ = self.run_in_process()
        self.assertEqual(code, 0)
        self.assertIn("warning: the migration was not logged", err)


class HintsTest(MigrateCase):
    """Contract 1.5 row d: the migrate hints appear because bp/commands/migrate.py now exists."""

    def test_the_command_is_built(self):
        self.assertTrue(registry.command_built("migrate"))
        r = self.cli("migrate", "--help")
        self.assertEqual(r.returncode, 0)
        for flag in ("--dry-run", "--force", "--json", "--root"):
            self.assertIn(flag, r.stdout)
        self.assertNotIn("not built", self.migrate("--dry-run").stderr)
        self.assertEqual(self.migrate("--bogus").returncode, 2)  # argparse usage errors keep exit 2

    def test_validate(self):
        r = self.cli("validate")
        self.assertIn("warning: blueprint.json is schema version 1; it still works. "
                      "To use milestones, features, tasks and unknowns, run: blueprint.py migrate", r.stdout)
        data = json.loads(self.cli("validate", "--json").stdout)["data"]
        w = [x for x in data["warnings"] if x["code"] == "schema-v1"][0]
        self.assertEqual((w["entry"], w["next"]), ("blueprint.json", "blueprint.py migrate"))

    def test_status(self):
        r = self.cli("status")
        self.assertEqual(r.stdout.rstrip().splitlines()[-1],
                         "Schema version 1: run migrate to use milestones, features, tasks and unknowns.")

    def test_refresh_and_doctor(self):
        base = Path(tempfile.mkdtemp(prefix="bp mig hint "))
        self.addCleanup(shutil.rmtree, base, True)
        self.assertEqual(S.run_script(S.LAUNCHER, ["init", "--name", "H", "--root", base], base).returncode, 0)
        (base / ".blueprint" / "blueprint.json").write_text(json.dumps(v1_plan()), encoding="utf-8")
        r = S.run_script(S.LAUNCHER, ["init", "--refresh-tool", "--root", base], base)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Your blueprint is schema version 1. It keeps working. To use milestones, features, tasks and "
                      "unknowns, run: python .blueprint/bin/blueprint.py migrate", r.stdout)
        r = S.run_script(S.LAUNCHER, ["doctor", "--root", base], base)
        self.assertIn("blueprint.json schema: 1 (run migrate to use 2.0 planning)", r.stdout)
        # and the hint goes away once it was followed
        self.assertEqual(S.run_script(S.LAUNCHER, ["migrate", "--root", base], base).returncode, 0)
        r = S.run_script(S.LAUNCHER, ["doctor", "--root", base], base)
        self.assertIn("blueprint.json schema: 2", r.stdout)
        self.assertNotIn("run migrate", r.stdout)


class ExampleV2Test(MigrateCase):
    def load_example(self):
        return json.loads(EXAMPLE_V2.read_text(encoding="utf-8"))

    def test_the_file_is_the_contract_example(self):
        data = self.load_example()
        self.assertEqual(data["version"], 2)
        self.assertEqual(data, v2_plan())  # the copy the validate tests carry is the same document
        raw = EXAMPLE_V2.read_bytes()
        self.assertNotIn(b"\r", raw)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual([len(data[k]) for k in NEW], [1, 2, 1, 1])

    def test_it_validates_and_loads(self):
        self.set_plan(self.load_example(), state=False)
        r = self.cli("validate", "--json")
        data = self.envelope(r)["data"]
        self.assertEqual((r.returncode, data["valid"], data["errors"]), (0, True, []), data)
        self.assertEqual({k: data["counts"][k] for k in NEW}, {"milestones": 1, "features": 2, "tasks": 1, "unknowns": 1})
        with contextlib.redirect_stderr(io.StringIO()):
            plan = bplan.load_plan(self.root)
        self.assertNotIn("_from", plan)
        self.assertNotIn("schema-v1", [w["code"] for w in data["warnings"]])

    def test_migrate_leaves_it_alone(self):
        self.set_plan(self.load_example(), state=False)
        shot = snapshot(self.root)
        self.assertEqual(self.migrate().returncode, 0)
        self.assertEqual(snapshot(self.root), shot)

    def test_upgrading_in_memory_changes_nothing(self):
        data = self.load_example()
        self.assertEqual(bpmigrate.upgrade_plan(data), data)

    @unittest.skipUnless(all(registry.exists(m) for m in ("features", "roadmap", "unknowns")),
                         "needs the Wave 1 modules (features, roadmap, unknowns)")
    def test_wave_1_modules_accept_it_and_report_its_sections(self):
        self.set_plan(self.load_example(), state=False)
        for rel in ("Cargo.toml", "crates/snag-api/src/main.rs", "crates/snag-api/tests/snags.rs",
                    "apps/web/src/Inbox.tsx", "apps/web/src/offline.ts"):
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x\n", encoding="utf-8")
        data = self.envelope(self.cli("validate", "--json"))["data"]
        self.assertEqual(data["errors"], [], data)
        status = json.loads(self.cli("status", "--json").stdout)
        self.assertEqual([f["id"] for f in status["features"]], ["F-log-snag", "F-inbox"])
        self.assertEqual([m["id"] for m in status["milestones"]], ["M1"])
        self.assertEqual([t["id"] for t in status["tasks"]], ["T-domain"])
        self.assertEqual([u["id"] for u in status["unknowns"]], ["U-offline"])
        self.assertEqual(self.cli("render").returncode, 0)
        self.assertTrue((self.bp / "ROADMAP.md").is_file())
        self.assertTrue((self.bp / "FEATURES.md").is_file())


class PackagingTest(unittest.TestCase):
    def test_the_command_is_copied_by_init_refresh_tool(self):
        """install derives the copied files from disk, so the new module needs no list entry (contract 1.7)."""
        from bp import install
        self.assertIn(S.PACKAGE / "commands" / "migrate.py", install.TOOL_FILES)
        base = Path(tempfile.mkdtemp(prefix="bp mig pkg "))
        self.addCleanup(shutil.rmtree, base, True)
        self.assertEqual(S.run_script(S.LAUNCHER, ["init", "--name", "P", "--root", base], base).returncode, 0)
        copy_ = base / ".blueprint" / "bin"
        self.assertTrue((copy_ / "bp" / "commands" / "migrate.py").is_file())
        self.assertIn("bp/commands/migrate.py", (copy_ / "MANIFEST").read_text(encoding="utf-8").replace("\\", "/"))

    def test_leaves_no_bytecode(self):
        text = (S.PACKAGE / "commands" / "migrate.py").read_text(encoding="utf-8")
        self.assertNotIn("\r", text)
        self.assertFalse(list(S.PACKAGE.rglob("__pycache__")))


class UnwritableContentTest(MigrateCase):
    """Review round 1, B1 and the non-finite, duplicate-key and read-once nits: what cannot be written back is refused
    (plan) or left as it is (state) before anything is written, in a real run and in a dry run."""

    FLAGS = ([], ["--dry-run"], ["--json"], ["--dry-run", "--json"])

    def set_text(self, text):
        self.plan_path.write_bytes(text.encode("utf-8"))
        os.utime(self.plan_path, ns=(OLD, OLD))

    def plan_text(self, extra=None, **top):
        plan = v1_plan()
        plan.update(top)
        text = json.dumps(plan, indent=2)  # ensure_ascii: a lone surrogate becomes the escape \ud800
        return text if extra is None else text[:text.rindex("}")].rstrip() + ", " + extra + "\n}\n"

    def tmp_files(self):
        return [p.name for p in self.bp.iterdir() if p.name.endswith(".tmp")]

    def test_a_lone_surrogate_in_the_plan_is_refused_before_anything_is_written(self):
        plan = v1_plan()
        plan["app"]["name"] = "Snag \ud800 Desk"
        self.set_text(json.dumps(plan, indent=2))
        shot = snapshot(self.root)
        for flags in self.FLAGS:
            with self.subTest(flags=flags):
                r = self.migrate(*flags, "--json") if "--json" not in flags else self.migrate(*flags)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertNotIn("Traceback", r.stderr)
                err = self.envelope(r)["error"]
                self.assertEqual((err["code"], err["entry"]), ("bad-json", "blueprint.json"))
                self.assertIn("not valid Unicode", err["message"])
                self.assertRegex(err["next"], r"^fix \.blueprint/blueprint\.json, then run: python .*validate")
                self.assertEqual(snapshot(self.root), shot)
                self.assertEqual(self.tmp_files(), [])
        human = self.migrate()
        self.assertEqual((human.returncode, human.stdout), (1, ""))
        self.assertIn("error: blueprint.json contains text that is not valid Unicode", human.stderr)

    def test_a_lone_surrogate_in_the_state_leaves_the_state_as_it_is(self):
        state = self.state()
        state["files"]["crates/snag-api/src/main.rs"]["note"] = "\ud800"
        self.state_path.write_bytes(json.dumps(state, indent=2).encode("utf-8"))
        before = self.state_path.read_bytes()
        dry = self.migrate("--dry-run")
        self.assertEqual((dry.returncode, dry.stdout), (0, "version 1 -> 2; adds milestones, features, tasks, unknowns "
                         "(empty); state.json contains text or numbers that cannot be written back and is left as it is\n"))
        for flags in ([], ["--json"]):
            with self.subTest(flags=flags):
                (self.bp / "blueprint.v1.json").unlink(missing_ok=True)
                self.set_plan(v1_plan(), state=False)
                self.state_path.write_bytes(before)
                r = self.migrate(*flags)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertNotIn("Traceback", r.stderr)
                self.assertEqual(self.state_path.read_bytes(), before)
                self.assertIn("warning: .blueprint/state.json contains text or numbers that cannot be written back; it "
                              "was left as it is. Fix or delete it, then run: python", r.stderr)
                self.assertEqual(self.plan()["version"], 2)
                self.assertEqual(self.tmp_files(), [])
                if flags:
                    self.assertNotIn(".blueprint/state.json", self.envelope(r)["data"]["written"])

    def test_a_number_that_is_not_finite_in_the_state_is_left_alone_too(self):
        self.state_path.write_bytes(b'{"files": {}, "patterns": {}, "unplanned": [], "weight": 1E400}')
        before = self.state_path.read_bytes()
        r = self.migrate()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertNotIn(b"Infinity", self.plan_path.read_bytes())

    def test_numbers_that_are_not_finite_are_refused_naming_the_key(self):
        for literal in ("1E400", "-1E400", "NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal):
                self.set_text(self.plan_text(f'"extra": {{"deep": [0, {literal}]}}'))
                shot = snapshot(self.root)
                for flags in (["--json"], ["--dry-run", "--json"]):
                    r = self.migrate(*flags)
                    self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                    err = self.envelope(r)["error"]
                    self.assertEqual((err["code"], err["entry"]), ("bad-json", "blueprint.json"))
                    self.assertIn("extra.deep[1]", err["message"])
                    self.assertIn("run: python", err["next"])
                    self.assertEqual(snapshot(self.root), shot)  # and never an Infinity or NaN in a written file

    def test_a_repeated_key_is_named_and_the_first_value_stays_in_the_backup(self):
        self.set_text(self.plan_text('"notes": "first", "notes": "second"'))
        original = self.plan_path.read_bytes()
        dry = self.migrate("--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn('warning: blueprint.json repeats the key "notes"; the last value is kept and the original goes '
                      "to the backup", dry.stderr)
        self.assertEqual(self.plan_path.read_bytes(), original)
        r = self.migrate("--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('warning: blueprint.json repeats the key "notes"; the last value was kept and the original is in '
                      ".blueprint/blueprint.v1.json", r.stderr)
        self.envelope(r)  # stdout is still one JSON object
        self.assertEqual(self.plan()["notes"], "second")
        self.assertIn(b'"first"', (self.bp / "blueprint.v1.json").read_bytes())

    def test_the_plan_is_read_once_so_the_backup_and_the_upgrade_agree(self):
        original = self.plan_path.read_bytes()
        real = cmd.load_plan

        calls = []

        def edit_then_load(root):  # someone edits blueprint.json after migrate read it
            if not calls:
                calls.append(1)
                self.plan_path.write_bytes(original.replace(b"Snag Desk", b"Edited"))
            return real(root)

        with mock.patch.object(cmd, "load_plan", edit_then_load):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                cmd.cmd_migrate(Namespace(root=str(self.root), dry_run=False, force=True, json=False))
        self.assertEqual((self.bp / "blueprint.v1.json").read_bytes(), original)  # what was read is what is kept
        self.assertEqual(self.plan()["app"]["name"], "Snag Desk")  # and what was upgraded

    def test_a_failure_on_the_temporary_file_names_that_file(self):
        original = self.plan_path.read_bytes()
        (self.bp / "blueprint.json.tmp").mkdir()
        r = self.migrate("--json")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        err = self.envelope(r)["error"]
        self.assertEqual((err["code"], err["entry"]), ("write-failed", ".blueprint/blueprint.json.tmp"))
        self.assertIn("cannot write .blueprint/blueprint.json.tmp", err["message"])
        self.assertTrue(err["next"].startswith("make .blueprint/blueprint.json.tmp writable, then run: python"), err["next"])
        self.assertEqual(self.plan_path.read_bytes(), original)
        self.assertEqual((self.bp / "blueprint.v1.json").read_bytes(), original)

    def test_a_folder_that_refuses_new_files_is_named_as_the_folder(self):
        def deny(path, mode, *a, **k):
            return False if Path(path).resolve() == self.bp.resolve() else real_access(path, mode, *a, **k)

        real_access = os.access
        with mock.patch.object(cmd.os, "access", deny):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as cm:
                    cmd.cmd_migrate(Namespace(root=str(self.root), dry_run=False, force=True, json=False))
        e = cm.exception
        self.assertEqual((e.error, e.entry), ("write-failed", ".blueprint"))
        self.assertTrue(e.next.startswith("make the folder .blueprint writable, then run: python"), e.next)
        self.assertFalse((self.bp / "blueprint.v1.json").exists())

    def test_every_next_is_a_command_that_can_be_run_as_printed(self):
        self.set_text(self.plan_text('"extra": NaN'))
        nexts = [json.loads(self.migrate("--json").stdout)["error"]["next"]]
        (self.bp / "blueprint.json.tmp").mkdir()
        self.set_plan(v1_plan())
        nexts.append(json.loads(self.migrate("--json").stdout)["error"]["next"])
        for n in nexts:
            self.assertNotIn("<", n)
            self.assertRegex(n, r"python (\.blueprint/bin/blueprint\.py|\".*blueprint\.py\") ")

    def test_a_linked_plan_and_state_are_written_through_and_the_links_survive(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        for name in ("blueprint.json", "state.json"):
            os.replace(self.bp / name, elsewhere / name)
            try:
                os.symlink(elsewhere / name, self.bp / name)
            except (OSError, NotImplementedError, AttributeError):
                os.replace(elsewhere / name, self.bp / name)
                self.skipTest("this platform or account cannot create symbolic links")
        r = self.migrate()
        self.assertEqual((r.returncode, r.stdout), (0, DONE_LINE + "\n"), r.stderr)
        for name in ("blueprint.json", "state.json"):
            self.assertTrue((self.bp / name).is_symlink(), name)
        self.assertEqual(json.loads((elsewhere / "blueprint.json").read_text(encoding="utf-8"))["version"], 2)
        self.assertEqual(json.loads((elsewhere / "state.json").read_text(encoding="utf-8"))["stateVersion"], 2)
        self.assertTrue((self.bp / "blueprint.v1.json").is_file() and not (self.bp / "blueprint.v1.json").is_symlink())
        self.assertEqual([p.name for p in elsewhere.iterdir() if p.name.endswith(".tmp")], [])


class DryRunAgreesTest(MigrateCase):
    def test_a_dry_run_refuses_what_a_real_run_refuses(self):
        for key, value in (("files", "x"), ("tasks", {}), ("roster", [1])):
            plan = v1_plan()
            plan[key] = value
            self.set_plan(plan)
            shot = snapshot(self.root)
            for flags in (["--dry-run", "--json"], ["--json"]):
                r = self.migrate(*flags)
                self.assertEqual((r.returncode, self.envelope(r)["error"]["code"]), (1, "bad-shape"), (key, flags))
            self.assertEqual(snapshot(self.root), shot)


class WithoutTheCommandTest(unittest.TestCase):
    """Row d, the other half: a skill without bp/commands/migrate.py prints no migrate hint anywhere, and `migrate` says
    it is not built. Forcing any hint gate (validate, status, init --refresh-tool, doctor) to True turns this red."""

    def test_no_hint_and_not_built(self):
        base = Path(tempfile.mkdtemp(prefix="bp nohint "))
        self.addCleanup(shutil.rmtree, base, True)
        skill = base / "skill"
        shutil.copytree(S.SCRIPTS, skill / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(S.SKILL / "assets", skill / "assets")
        (skill / "scripts" / "bp" / "commands" / ("mig" + "rate.py")).unlink()
        launcher = skill / "scripts" / "blueprint.py"
        proj = base / "proj"
        proj.mkdir()
        self.assertEqual(S.run_script(launcher, ["init", "--name", "N", "--root", proj], base).returncode, 0)
        (proj / ".blueprint" / "blueprint.json").write_text(json.dumps(v1_plan(), indent=2), encoding="utf-8")
        for argv in (["validate"], ["status"], ["init", "--refresh-tool"], ["doctor"]):
            with self.subTest(argv=argv):
                r = S.run_script(launcher, argv + ["--root", proj], base)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertNotIn("migrate", r.stdout + r.stderr)
        self.assertIn("schema version 1", S.run_script(launcher, ["validate", "--root", proj], base).stdout)
        self.assertIn("Schema version 1: it keeps working.", S.run_script(launcher, ["status", "--root", proj], base).stdout)
        r = S.run_script(launcher, ["migrate", "--root", proj], base)
        self.assertEqual(r.returncode, 1)
        self.assertIn("error: migrate is not built", r.stderr)
        self.assertIn("W0-B", r.stderr)
        # the project's own copy was made by that skill, so it has no command either
        r = S.run_script(proj / ".blueprint" / "bin" / "blueprint.py", ["status"], proj)
        self.assertNotIn("migrate", r.stdout)


if __name__ == "__main__":
    unittest.main()
