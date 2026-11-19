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
             "Next: add milestones and features (see references/schema.md), then run validate.")
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
                self.assertIn("warning: state.json", r.stderr)
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
        self.assertIn("  next: python <skill>/scripts/blueprint.py init --refresh-tool", r.stderr)
        self.assertEqual(snapshot(self.root), shot)

    def test_old_copy_json_envelope(self):
        self.fake_copy("1.3.0")
        r = self.migrate("--json")
        self.assertEqual(r.returncode, 1)
        err = self.envelope(r)
        self.assertEqual((err["ok"], err["command"], err["error"]["code"]), (False, "migrate", "old-tool-copy"))
        self.assertEqual(err["error"]["next"], "python <skill>/scripts/blueprint.py init --refresh-tool")

    def test_a_copy_of_unreadable_version_counts_as_old_and_a_current_one_does_not(self):
        self.fake_copy(None)
        self.assertEqual(self.migrate("--dry-run", "--json").returncode, 1)
        self.fake_copy(MIN_SCHEMA2_TOOL)
        self.assertEqual(self.migrate("--dry-run").returncode, 0)
        self.fake_copy("1.9.0")
        self.assertEqual(self.migrate("--dry-run").returncode, 1 if MIN_SCHEMA2_TOOL > "1.9.0" else 0)

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

    def test_a_state_that_cannot_be_replaced_leaves_a_plan_that_still_loads(self):
        real = os.replace

        def failing(src, dst, *a, **k):
            if Path(dst).name == "state.json":
                raise PermissionError(13, "Permission denied")
            return real(src, dst, *a, **k)

        with mock.patch.object(cmd.os, "replace", failing):
            code, _, _, exc = self.run_in_process()
        self.assertEqual((code, exc.error), (1, "write-failed"))
        self.assertEqual(self.plan()["version"], 2)  # written before the state, as 3.4 orders it
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bplan.load_state(self.root)["stateVersion"], 2)  # the state upgrades in memory
        self.assertEqual(self.migrate().stdout, "blueprint.json is already schema version 2. Nothing to do.\n")

    def test_a_failing_view_render_does_not_undo_the_migration(self):
        with mock.patch("bp.views.render.render_all", side_effect=RuntimeError("boom")):
            code, out, err, _ = self.run_in_process()
        self.assertEqual(code, 0)
        self.assertEqual(self.plan()["version"], 2)
        self.assertIn("warning: the views were not regenerated", err)
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


if __name__ == "__main__":
    unittest.main()
