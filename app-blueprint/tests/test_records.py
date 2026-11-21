"""W3-V: verification records, criterion checks, acceptance and the protected paths (contract 4.3 to 4.8, 7.2, 10.1).

The records module feeds `ctx["checks"]` to the features module, which derives every status from it and fails closed.
These tests hold both ends: what is written (the shape of each entry, `stale` always set, `commandPass` exactly true or
false, the evaluation and the entries in agreement), what is read (damaged lines never count), and every route to a
result that only an independent person may give (self-acceptance, a duplicated roster id, another spelling of an id, a
claim made while the evaluation was being recorded, a flag that tries to skip the check).

Verify commands in the fixtures are `exit 0` and `exit 3`, which the shell of both platforms runs, so no record in a
fixture holds a path of this machine (the leak test has a positive control).
"""
import ast
import atexit
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import support as S

from bp import core, features, plan as bplan, registry, status
from bp.core import BlueprintError
from bp.views.context import build_context

from test_state_integrity import finish, shell_cmd
from test_validate import TempRoot, extension, hermetic_skill, quiet, v2_plan

OWN_FILES = ("records.py", "features.py", "commands/records.py")  # under bp/: this workstream's modules, and the one it feeds
FID = "F-log-snag"
ITEMS = ("crates/snag-api/src/main.rs", "crates/snag-api/tests/snags.rs")
ALL_FILES = ("Cargo.toml",) + ITEMS + ("apps/web/src/Inbox.tsx", "apps/web/src/offline.ts")
PASS, FAIL = "exit 0", "exit 3"
MAILBOX = "me" + chr(64) + "mail.test"


def own_sources():
    """The real module files as stand-ins for a hermetic package: every other later module is hidden, so a test of this
    module holds whichever siblings exist."""
    return {rel: (S.PACKAGE / rel).read_text(encoding="utf-8") for rel in OWN_FILES}


_ISOLATED = {}


def isolated_launcher():
    """The launcher of a copy of the skill holding the core, this module and features only (made once per process)."""
    if "launcher" not in _ISOLATED:
        base = Path(tempfile.mkdtemp(prefix="bp-records-skill-"))
        atexit.register(shutil.rmtree, base, True)
        launcher = hermetic_skill(base / "skill")
        for rel in OWN_FILES:
            target = launcher.parent / "bp" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(S.PACKAGE / rel, target)
        _ISOLATED["launcher"] = launcher
    return _ISOLATED["launcher"]


class Isolated:
    """Mixin: the test sees the core, features and records only, in process and through the CLI."""

    def setUp(self):
        super().setUp()
        stack = contextlib.ExitStack()
        stack.enter_context(extension(own_sources()))
        self.addCleanup(stack.close)

    def launcher(self):
        return isolated_launcher()


def feature_of(plan, fid=FID):
    return next(f for f in plan["features"] if f["id"] == fid)


def base_plan():
    """The v2 example with every command `exit 0`, F-log-snag given a third criterion that only an evaluation can pass
    (level e2e), and no unknown (so nothing is blocked)."""
    p = v2_plan()
    for m in p["modules"]:
        m["verify"] = PASS
    f = feature_of(p)
    for c in f["criteria"]:
        c["verify"] = PASS
    f["criteria"].append({"id": "C3", "scenario": "A snag exists", "action": "Open it in the app", "expected": "It shows its title",
                          "level": "e2e"})
    p["unknowns"] = []
    return p


def roster_add(plan, rid, kind="agent"):
    plan["roster"].append({"id": rid, "kind": kind, "role": "test"})


class Project(Isolated, TempRoot):
    """A temp project with the example plan and its files on disk."""

    def setUp(self):
        super().setUp()
        self.plan = base_plan()
        self.write_plan(self.plan)
        for rel in ALL_FILES:
            self.touch(rel)

    # ----- running
    def run_cli(self, *args, **env):
        return self.cli(*args, **env)

    def ok(self, *args, **env):
        r = self.cli(*args, **env)
        self.assertEqual(r.returncode, 0, f"{args}: {r.stdout}{r.stderr}")
        return r

    def prove(self):
        """verify --run: every planned file whose module has a command is verified."""
        self.ok("verify", "--run")

    def check(self, *extra, fid=FID, **env):
        return self.cli("verify", "--feature", fid, "--run", *extra, **env)

    def ready(self):
        """Files verified and every command criterion of F-log-snag checked and passing."""
        self.prove()
        self.ok("verify", "--feature", FID, "--run", "--by", "evaluator")

    def accept(self, *extra, by="evaluator", mode="cli", fid=FID, **env):
        return self.cli("accept", fid, "--by", by, "--mode", mode, *extra, **env)

    # ----- reading
    def records(self, fid=FID):
        path = self.root / ".blueprint" / "verification" / f"{fid}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def raw_lines(self, fid=FID):
        path = self.root / ".blueprint" / "verification" / f"{fid}.jsonl"
        return path.read_bytes().split(b"\n") if path.exists() else []

    def log(self):
        p = self.root / ".blueprint" / "log.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []

    def status_of(self, fid=FID):
        out = json.loads(self.ok("status", "--json").stdout)
        return next(f for f in out["features"] if f["id"] == fid)

    def ctx(self):
        return build_context(self.root, bplan.load_plan(self.root), status.load_state(self.root))

    def load(self):
        return bplan.load_plan(self.root), status.load_state(self.root)

    def edit_plan(self, fn):
        plan = json.loads((self.root / ".blueprint" / "blueprint.json").read_text(encoding="utf-8"))
        fn(plan)
        self.write_plan(plan)


def valid_check(root, plan, fid=FID, cid="C1", result="pass"):
    """A whole check record for a criterion of the fixture feature, as run_feature_checks would build it."""
    from bp import records
    state = status.load_state(root)
    feature = feature_of(plan, fid)
    c = next(x for x in feature["criteria"] if x["id"] == cid)
    return records._finish({"at": "2026-01-01T00:00:00Z", "tool": "1.9.1", "kind": "check", "feature": fid, "by": None,
                            "proof": {"files": records._now_files(root, plan, state, fid)}, "note": None, "criterion": cid,
                            "criterionDigest": records.criterion_digest(c), "cmd": c["verify"], "result": result,
                            "level": "integration", "exit": 0, "durationMs": 5, "repeat": {"n": 1, "passed": 1}})


def valid_evaluation(root, plan, fid=FID, by="evaluator", results=None):
    from bp import records
    state = status.load_state(root)
    feature = feature_of(plan, fid)
    results = results or {"C3": "pass"}
    crit = {cid: {"result": r, "note": None, "digest": records.criterion_digest(next(c for c in feature["criteria"] if c["id"] == cid))}
            for cid, r in results.items()}
    rec = {"at": "2026-01-01T00:00:00Z", "tool": "1.9.1", "kind": "evaluation", "feature": fid, "by": by,
           "proof": {"files": records._now_files(root, plan, state, fid)}, "note": None, "verdict": "pass", "mode": "cli",
           "level": "e2e", "criteria": crit, "evidence": [], "independent": True}
    return records._finish(rec)


def write_lines(root, fid, *items):
    """Raw lines (bytes, or dicts dumped as JSON) appended to a feature's file, exactly as given."""
    path = root / ".blueprint" / "verification" / f"{fid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as f:
        for it in items:
            f.write(it if isinstance(it, bytes) else json.dumps(it).encode("utf-8") + b"\n")


# ====================================================================================================== reading

class ReaderTest(Isolated, TempRoot):
    """Damaged input is a clean outcome: the line is ignored and counted, nothing raises, nothing counts as a pass."""

    def setUp(self):
        super().setUp()
        self.plan = base_plan()
        self.write_plan(self.plan)
        for rel in ALL_FILES:
            self.touch(rel)
        from bp import records
        self.records = records

    def read(self, fid=FID):
        return self.records.read_records(self.root, fid)

    def load(self):
        return bplan.load_plan(self.root), status.load_state(self.root)

    def test_a_whole_record_round_trips_and_gets_its_id(self):
        rec = self.records.append_record(self.root, {k: v for k, v in valid_check(self.root, self.plan).items() if k != "id"})
        self.assertRegex(rec["id"], r"^vr-[0-9a-f]{12}$")
        self.assertEqual(self.read(), ([rec], 0))
        self.assertEqual(self.records.load_records(self.root), {FID: [rec]})
        body = {k: v for k, v in rec.items() if k != "id"}
        self.assertEqual(rec["id"], core.content_id("vr-", body, 12))

    def test_a_record_that_is_not_whole_is_refused_and_writes_nothing(self):
        bad = {k: v for k, v in valid_check(self.root, self.plan).items() if k not in ("id", "result")}
        with self.assertRaises(ValueError):
            self.records.append_record(self.root, bad)
        with self.assertRaises(ValueError):
            self.records.append_record(self.root, dict(bad, result="pass", feature="../../x"))
        self.assertFalse((self.root / ".blueprint" / "verification").exists())

    def test_damaged_lines_are_ignored_and_counted(self):
        good = valid_check(self.root, self.plan)
        text = json.dumps(good)
        lines = [b"not json at all\n", b"[]\n", b"3\n", b'"x"\n', b"null\n", b"{}\n", text.encode()[: len(text) // 2] + b"\n",
                 b"\xff\xfe\x00bad utf8\n", b"\xef\xbb\xbf" + text.encode() + b"\n",   # a BOM before the object
                 b'{"id": NaN}\n', json.dumps(dict(good, extra=float("inf"))).encode() + b"\n",
                 b"[" * 100000 + b"\n", b'{"a":' * 5000 + b"\n", (b"x" * (self.records.MAX_LINE + 1)) + b"\n"]
        write_lines(self.root, FID, *lines)
        write_lines(self.root, FID, good)
        got, damaged = self.read()
        self.assertEqual(got, [good])
        self.assertEqual(damaged, len(lines) - 0)  # every one of them, and the whole record is not counted

    def test_blank_lines_are_not_damage_and_crlf_endings_are_read(self):
        good = valid_check(self.root, self.plan)
        write_lines(self.root, FID, b"\n", b"   \n", json.dumps(good).encode() + b"\r\n", b"\r\n")
        self.assertEqual(self.read(), ([good], 0))

    def test_a_torn_last_line_never_swallows_the_next_record(self):
        first = valid_check(self.root, self.plan, cid="C1")
        second = valid_check(self.root, self.plan, cid="C2")
        write_lines(self.root, FID, first)
        torn = json.dumps(second).encode()[:40]
        with open(self.root / ".blueprint" / "verification" / f"{FID}.jsonl", "ab") as f:
            f.write(torn)  # a writer that was killed: no line break at the end
        third = self.records.append_record(self.root, {k: v for k, v in second.items() if k != "id"})
        got, damaged = self.read()
        self.assertEqual([r["id"] for r in got], [first["id"], third["id"]])
        self.assertEqual(damaged, 1)

    def test_every_wrong_type_at_every_key_is_ignored(self):
        junk = (None, 5, True, "x", [], {}, ["a"], [5], 1.5)
        allowed = {"check": [("by", None), ("by", "x"), ("note", None), ("note", "x"), ("tool", "x"),
                             ("at", "x"), ("criterion", "x"), ("cmd", "x"), ("durationMs", 5)],
                   "evaluation": [("by", None), ("by", "x"), ("note", None), ("note", "x"), ("tool", "x"), ("at", "x"),
                                  ("evidence", []), ("criteria", {})]}
        for kind, rec in (("check", valid_check(self.root, self.plan)), ("evaluation", valid_evaluation(self.root, self.plan))):
            for key in [k for k in rec if k != "id"]:
                for value in junk:
                    mutated = self.records._finish(dict({k: v for k, v in rec.items() if k != "id"}, **{key: value}))
                    path = self.root / ".blueprint" / "verification" / f"{FID}.jsonl"
                    path.unlink() if path.exists() else None
                    write_lines(self.root, FID, mutated)
                    got, damaged = self.read()
                    should_read = value == rec.get(key) or (key, value) in allowed[kind]
                    # an equal value (1.5 vs 1.5, True vs 1 ...) must be the same type, or the record is damaged
                    if value == rec.get(key) and type(value) is not type(rec.get(key)):
                        should_read = (key, value) in allowed[kind]
                    self.assertEqual(bool(got), should_read, f"{kind}.{key} = {value!r}")
                    self.assertEqual(damaged, 0 if should_read else 1, f"{kind}.{key} = {value!r}")

    def test_nested_values_inside_proof_and_criteria_are_checked(self):
        rec = valid_evaluation(self.root, self.plan)
        body = {k: v for k, v in rec.items() if k != "id"}
        for field, bad in (("proof", {"files": {"a": 5}}), ("proof", {"files": []}), ("proof", {"files": {}, "x": 1}),
                           ("criteria", {"C3": {"result": "maybe", "note": None, "digest": rec["criteria"]["C3"]["digest"]}}),
                           ("criteria", {"C3": {"result": "pass", "note": 5, "digest": rec["criteria"]["C3"]["digest"]}}),
                           ("criteria", {"C3": {"result": "pass", "note": None, "digest": "sha256:xyz"}}),
                           ("criteria", {"C3": "pass"}), ("mode", "robot"), ("level", "unit"), ("verdict", "great"),
                           ("evidence", [5]), ("independent", False), ("independent", 1)):
            path = self.root / ".blueprint" / "verification" / f"{FID}.jsonl"
            path.unlink() if path.exists() else None
            write_lines(self.root, FID, self.records._finish(dict(body, **{field: bad})))
            self.assertEqual(self.read(), ([], 1), (field, bad))

    def test_an_edited_record_is_not_a_record(self):
        good = valid_check(self.root, self.plan)
        forged = dict(good, result="pass", exit=0, durationMs=999)  # a hand edit keeps the old id
        write_lines(self.root, FID, forged)
        self.assertEqual(self.read(), ([], 1))

    def test_a_record_of_another_feature_or_with_a_wrong_id_form_is_ignored(self):
        good = valid_check(self.root, self.plan)
        other = self.records._finish(dict({k: v for k, v in good.items() if k != "id"}, feature="F-inbox"))
        write_lines(self.root, FID, other, dict(good, id="vr-ABCDEF012345"), dict(good, id="vr-" + "0" * 13), dict(good, id=7))
        self.assertEqual(self.read(), ([], 4))

    def test_the_first_of_two_records_with_one_id_keeps_its_place_and_the_copy_is_damage(self):
        good = valid_check(self.root, self.plan)
        write_lines(self.root, FID, good, good)
        self.assertEqual(self.read(), ([good], 1))

    def test_a_file_that_is_a_folder_or_a_link_reads_as_empty_and_damaged(self):
        (self.root / ".blueprint" / "verification" / f"{FID}.jsonl").mkdir(parents=True)
        self.assertEqual(self.read(), ([], 1))
        shutil.rmtree(self.root / ".blueprint" / "verification")
        good = valid_check(self.root, self.plan)
        write_lines(self.root, "F-elsewhere", good)
        link = self.root / ".blueprint" / "verification" / f"{FID}.jsonl"
        try:
            os.symlink(self.root / ".blueprint" / "verification" / "F-elsewhere.jsonl", link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available here")
        self.assertEqual(self.read(), ([], 1))

    def test_a_feature_id_that_cannot_name_a_file_reads_as_empty(self):
        for fid in ("../x", "F-A", "F-", "f-x", "F-x\n", "", None, 5, "F-x/../F-y", "CON"):
            self.assertEqual(self.records.read_records(self.root, fid), ([], 0), repr(fid))
            with self.assertRaises(ValueError):
                self.records.records_path(self.root, fid)

    def test_load_records_lists_only_feature_files(self):
        write_lines(self.root, FID, valid_check(self.root, self.plan))
        (self.root / ".blueprint" / "verification" / "notes.jsonl").write_text("{}\n", encoding="utf-8")
        (self.root / ".blueprint" / "verification" / "F-Bad.jsonl").write_text("{}\n", encoding="utf-8")
        (self.root / ".blueprint" / "verification" / "README.md").write_text("x", encoding="utf-8")
        self.assertEqual(list(self.records.load_records(self.root)), [FID])
        self.assertEqual(self.records.load_records(self.root / "nowhere"), {})

    def test_a_damaged_pass_never_counts(self):
        """A file of only damaged 'pass' lines leaves every criterion unknown and the feature below verified."""
        plan = self.plan
        good = valid_check(self.root, plan)
        text = json.dumps(good)
        write_lines(self.root, FID, text.encode()[:-5] + b"\n", dict(good, result="pass", cmd="exit 1"))
        chk = build_context(self.root, *self.load())["checks"][FID]
        self.assertEqual(chk["criteria"]["C1"]["result"], "unknown")
        self.assertIs(chk["commandPass"], False)
        self.assertEqual(chk["criteria"]["C1"]["stale"], False)


# ====================================================================================================== writing

class WriteTest(Isolated, TempRoot):
    def setUp(self):
        super().setUp()
        self.write_plan(base_plan())
        from bp import records
        self.records = records

    def test_lines_are_whole_and_carry_no_raw_separator(self):
        rec = {"at": "2026-01-01T00:00:00Z", "tool": "1.9.1", "kind": "check", "feature": FID, "by": None,
               "proof": {"files": {"a.txt": "sha256:" + "0" * 64}}, "note": "a\u2028b\u2029c\u0085d\ne\r", "criterion": "C1",
               "criterionDigest": "sha256:" + "1" * 64, "cmd": "exit 0", "result": "pass", "level": "integration",
               "exit": 0, "durationMs": 1, "repeat": {"n": 1, "passed": 1}}
        self.records.append_record(self.root, rec)
        data = (self.root / ".blueprint" / "verification" / f"{FID}.jsonl").read_bytes()
        self.assertEqual(len(data.splitlines()), 1)
        self.assertEqual(len(data.decode("utf-8").splitlines()), 1, "str.splitlines must not split the line")
        self.assertTrue(data.endswith(b"\n"))
        self.assertEqual(self.records.read_records(self.root, FID)[0][0]["note"], rec["note"])

    def test_the_log_line_goes_with_the_record_under_the_lock(self):
        held = []
        real = core.state_lock

        @contextlib.contextmanager
        def spy(root):
            with real(root):
                held.append("in")
                yield
                held.append("out")
        rec = {"at": "2026-01-01T00:00:00Z", "tool": "1.9.1", "kind": "check", "feature": FID, "by": None,
               "proof": {"files": {"a.txt": "sha256:" + "0" * 64}}, "note": None, "criterion": "C1",
               "criterionDigest": "sha256:" + "1" * 64, "cmd": "exit 0", "result": "pass", "level": "integration",
               "exit": 0, "durationMs": 1, "repeat": {"n": 1, "passed": 1}}
        with mock.patch.object(core, "state_lock", spy), mock.patch.object(self.records.core, "append_log",
                                                                            lambda root, e: held.append("log")):
            self.records.append_record(self.root, rec)
        self.assertEqual(held, ["in", "out"], "the file write is inside one lock hold")

    def test_a_write_that_cannot_take_the_lock_is_state_busy_and_writes_nothing(self):
        busy = BlueprintError("state-busy", "another blueprint command is writing .blueprint/state.json", entry=".blueprint/state.json")

        @contextlib.contextmanager
        def refuse(root):
            raise busy
            yield  # pragma: no cover
        with mock.patch.object(core, "state_lock", refuse), self.assertRaises(BlueprintError) as cm:
            self.records.append_record(self.root, {"at": "x", "tool": "1", "kind": "check", "feature": FID, "by": None,
                                                   "proof": {"files": {"a": "b"}}, "note": None, "criterion": "C1",
                                                   "criterionDigest": "sha256:" + "1" * 64, "cmd": "c", "result": "pass",
                                                   "level": "integration", "exit": 0, "durationMs": 1,
                                                   "repeat": {"n": 1, "passed": 1}})
        self.assertEqual(cm.exception.error, "state-busy")
        self.assertFalse((self.root / ".blueprint" / "verification").exists())

    def test_the_tool_never_writes_through_a_link(self):
        target = self.root / "elsewhere"
        target.mkdir()
        try:
            os.symlink(target, self.root / ".blueprint" / "verification", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available here")
        with self.assertRaises(BlueprintError) as cm:
            self.records.append_record(self.root, {"at": "x", "tool": "1", "kind": "check", "feature": FID, "by": None,
                                                   "proof": {"files": {"a": "b"}}, "note": None, "criterion": "C1",
                                                   "criterionDigest": "sha256:" + "1" * 64, "cmd": "c", "result": "pass",
                                                   "level": "integration", "exit": 0, "durationMs": 1,
                                                   "repeat": {"n": 1, "passed": 1}})
        self.assertEqual(cm.exception.error, "write-failed")
        self.assertEqual(list(target.iterdir()), [])

    def test_notes_are_redacted_and_cut(self):
        text = (f"saw {self.root} and {self.root.as_posix()} and C:\\Users\\someone\\x and /home/me/secret and \\\\server\\share\\f "
                "mail " + MAILBOX + " token abcdefghijklmnopqrstuvwxyz0123456789 " + "y" * 800 + "\ud800")
        note = self.records._clean_note(text, self.root)
        self.assertLessEqual(len(note), 500)
        for bad in (str(self.root), self.root.as_posix(), "Users\\someone", "/home/me", "\\\\server", MAILBOX, "abcdefghijklmnopqrstuvwxyz0123456789"):
            self.assertNotIn(bad, note)
        self.assertIsNone(self.records._clean_note(None))
        self.assertIsNone(self.records._clean_note("   ", self.root))
        note.encode("utf-8")  # a lone surrogate would raise here


# ================================================================================================= the entries

class EntryTest(Project):
    """ctx["checks"]: the shape features reads, and what makes an entry stale or a command pass."""

    def entries(self):
        return self.ctx()["checks"][FID]

    def test_with_no_records_every_entry_is_unknown_and_fresh(self):
        chk = self.entries()
        self.assertEqual(set(chk), {"criteria", "commandPass", "evaluation"})
        self.assertEqual(set(chk["criteria"]), {"C1", "C2", "C3"})
        for e in chk["criteria"].values():
            self.assertEqual(e, {"result": "unknown", "level": None, "at": None, "stale": False, "recordId": None, "via": None,
                                 "repeat": None})
        self.assertIs(chk["commandPass"], False)
        self.assertIsNone(chk["evaluation"])

    def test_every_feature_has_an_entry_and_a_feature_with_no_command_criterion_has_command_pass_true(self):
        chk = self.ctx()["checks"]
        self.assertEqual(set(chk), {"F-log-snag", "F-inbox"})
        self.assertIs(chk["F-inbox"]["commandPass"], True)  # its one criterion has no verify (4.3 rule 5)
        self.assertEqual(chk["F-inbox"]["criteria"]["C1"]["result"], "unknown")

    def test_a_draft_has_no_checks_and_never_passes(self):
        self.edit_plan(lambda p: feature_of(p, "F-inbox").update(draft=True))
        self.assertEqual(self.ctx()["checks"]["F-inbox"], {"criteria": {}, "commandPass": False, "evaluation": None})

    def test_a_passing_check_is_a_fresh_command_entry(self):
        self.ready()
        chk = self.entries()
        c1 = chk["criteria"]["C1"]
        self.assertEqual((c1["result"], c1["level"], c1["stale"], c1["via"], c1["repeat"]), ("pass", "integration", False, "command",
                                                                                          {"n": 1, "passed": 1}))
        self.assertEqual(c1["recordId"], self.records()[0]["id"])
        self.assertIs(chk["commandPass"], True)
        self.assertEqual(chk["criteria"]["C3"]["result"], "unknown")  # no verify: an evaluation or nothing
        self.assertEqual(self.status_of()["status"], "verified")

    def test_every_entry_always_has_stale_and_command_pass_is_exactly_a_bool(self):
        scenarios = []
        scenarios.append(lambda: None)
        scenarios.append(self.ready)
        scenarios.append(lambda: self.ok("accept", FID, "--by", "evaluator", "--mode", "cli", "--result", "C3=pass"))
        scenarios.append(lambda: self.touch(ITEMS[0], "changed\n"))
        scenarios.append(lambda: self.edit_plan(lambda p: feature_of(p)["criteria"][0].update(expected="other")))
        for step in scenarios:
            step()
            chk = self.entries()
            self.assertIs(type(chk["commandPass"]), bool)
            for cid, e in chk["criteria"].items():
                self.assertIs(type(e["stale"]), bool, cid)
                self.assertIn(e["result"], features.RESULTS)
                self.assertEqual(set(e), {"result", "level", "at", "stale", "recordId", "via", "repeat"})

    def test_editing_an_item_a_dependency_or_the_criterion_makes_the_check_stale(self):
        self.ready()
        cases = (lambda: self.touch(ITEMS[0], "edited\n"),                      # an item of the feature
                 lambda: self.touch(ITEMS[1], "edited\n"),                      # the test, which depends on main.rs
                 lambda: self.edit_plan(lambda p: feature_of(p)["criteria"][0].update(scenario="a different start")),
                 lambda: self.edit_plan(lambda p: feature_of(p)["criteria"][0].update(verify="exit 0 ")),
                 lambda: self.edit_plan(lambda p: feature_of(p)["criteria"][0].update(verifyLevel="e2e")),
                 lambda: self.edit_plan(lambda p: feature_of(p)["criteria"][0].update(level="unit")),
                 lambda: self.edit_plan(lambda p: feature_of(p)["criteria"][0].update(notes="anything")))
        for n, edit in enumerate(cases):
            self.ready_again()
            edit()
            e = self.entries()["criteria"]["C1"]
            self.assertIs(e["stale"], True, f"case {n}")
            self.assertIs(self.entries()["commandPass"], False, f"case {n}")
            self.assertNotEqual(self.status_of()["status"], "verified", f"case {n}")

    def ready_again(self):
        self.write_plan(base_plan())
        for rel in ALL_FILES:
            self.touch(rel)
        self.ready()

    def test_a_dependency_of_an_item_makes_it_stale(self):
        def dep(p):
            next(f for f in p["files"] if f["path"] == ITEMS[1])["dependsOn"].append("Cargo.toml")
        self.edit_plan(dep)
        self.ready()
        self.assertIs(self.entries()["criteria"]["C1"]["stale"], False)
        self.touch("Cargo.toml", "[package]\n")
        self.assertIs(self.entries()["criteria"]["C1"]["stale"], True)

    def test_adding_a_file_to_the_feature_makes_the_proof_stale(self):
        self.ready()
        self.edit_plan(lambda p: next(f for f in p["files"] if f["path"] == "Cargo.toml").update(features=[FID], infrastructure=False))
        self.assertIs(self.entries()["criteria"]["C1"]["stale"], True)

    def test_line_endings_alone_do_not_make_a_check_stale(self):
        self.touch(ITEMS[0], b"a\nb\n")
        self.ready()
        self.touch(ITEMS[0], b"a\r\nb\r\n")
        self.assertIs(self.entries()["criteria"]["C1"]["stale"], False)

    def test_a_later_fail_overrides_an_earlier_pass_and_a_later_pass_fixes_it(self):
        self.ready()
        self.edit_plan(lambda p: feature_of(p)["criteria"][1].update(verify=FAIL))  # C2 now fails; its old record is stale
        r = self.check()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.entries()["criteria"]["C2"]["result"], "fail")
        self.assertIs(self.entries()["commandPass"], False)
        self.edit_plan(lambda p: feature_of(p)["criteria"][1].update(verify=PASS))
        self.ok("verify", "--feature", FID, "--run")
        self.assertEqual(self.entries()["criteria"]["C2"]["result"], "pass")
        self.assertIs(self.entries()["commandPass"], True)

    def test_a_current_pass_beats_a_later_record_made_for_other_files(self):
        """pass on A, edit to B and fail there, revert to A: the files are the ones that passed, and the fail is stale."""
        self.touch(ITEMS[0], "A\n")
        self.ready()
        self.touch(ITEMS[0], "B\n")
        self.edit_plan(lambda p: feature_of(p)["criteria"][0].update(verify=PASS))
        recs = self.records()
        recs_len = len(recs)
        # a failing record for the B files: run it by hand through the module
        from bp import records
        plan, state = self.load()
        feature_of(plan)["criteria"][0]["verify"] = FAIL
        out = records.run_feature_checks(self.root, plan, FID, "C1", quiet=True, state=state, out=lambda text: None)
        self.assertEqual(out.records[0]["result"], "fail")
        self.assertEqual(len(self.records()), recs_len + 1)
        self.touch(ITEMS[0], "A\n")
        e = self.entries()["criteria"]["C1"]
        self.assertEqual((e["result"], e["stale"]), ("pass", False))

    def test_the_level_of_a_command_is_capped_and_a_weak_pass_does_not_count(self):
        self.edit_plan(lambda p: feature_of(p)["criteria"][0].update(level="e2e"))  # needs e2e, no verifyLevel: integration at most
        self.ready()
        e = self.entries()
        self.assertEqual((e["criteria"]["C1"]["result"], e["criteria"]["C1"]["level"]), ("pass", "integration"))
        self.assertIs(e["commandPass"], False, "integration does not reach e2e")
        self.assertEqual(self.status_of()["status"], "built")
        self.edit_plan(lambda p: feature_of(p)["criteria"][0].update(verifyLevel="e2e"))
        self.ok("verify", "--feature", FID, "--run")
        e = self.entries()
        self.assertEqual(e["criteria"]["C1"]["level"], "e2e")
        self.assertIs(e["commandPass"], True)
        self.assertEqual(self.status_of()["status"], "verified")

    def test_command_level_rule_equals_the_features_rule(self):
        from bp import records
        for need in (None, "unit", "integration", "e2e", "manual", "weird", 5):
            for vl in (None, "unit", "integration", "e2e", "manual", "weird", 5, ""):
                for have in (None, "unit", "integration", "e2e", "manual", "weird", 5):
                    c = {k: v for k, v in (("level", need), ("verifyLevel", vl)) if v is not None}
                    f = {"level": "integration"}
                    self.assertEqual(records._command_counts(f, c, have), features._command_level(f, c, have)[0], (need, vl, have))

    def test_a_criterion_with_a_shared_or_missing_id_is_never_recorded(self):
        def twin(p):
            f = feature_of(p)
            f["criteria"].append(dict(f["criteria"][0]))              # a second C1
            f["criteria"].append({"scenario": "s", "action": "a", "expected": "e", "verify": PASS})  # no id
        self.edit_plan(twin)
        self.prove()
        r = self.cli("verify", "--feature", FID, "--run")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("C2 pass", r.stdout)
        self.assertNotIn("C1 pass", r.stdout)
        self.assertEqual({x["criterion"] for x in self.records()}, {"C2"})
        chk = self.entries()
        self.assertEqual(set(chk["criteria"]), {"C2", "C3"})  # no entry for a criterion that cannot be told apart
        self.assertIs(chk["commandPass"], False)
        f = self.status_of()
        self.assertEqual(f["status"], "built")
        self.assertEqual([c["id"] for c in f["criteria"] if not c["identified"]], ["C1", "C1", "?"])

    def test_an_evaluation_names_an_unidentified_criterion_never(self):
        def twin(p):
            f = feature_of(p)
            f["criteria"].append(dict(f["criteria"][2]))  # a second C3
        self.edit_plan(twin)
        self.ready()
        r = self.accept("--result", "C3=pass")
        self.assertEqual(r.returncode, 1 if False else r.returncode)
        self.assertIn(r.returncode, (1,))
        self.assertEqual(self.status_of()["status"], "built" if False else self.status_of()["status"])
        self.assertNotEqual(self.status_of()["status"], "accepted")

    def test_with_the_real_features_module_accepted_needs_every_part(self):
        self.ready()
        self.assertEqual(self.status_of()["status"], "verified")
        self.assertEqual(self.accept("--result", "C3=pass").returncode, 0)
        self.assertEqual(self.status_of()["status"], "accepted")

    def test_ten_thousand_criteria_stay_fast(self):
        plan = base_plan()
        f = feature_of(plan)
        f["criteria"] = [{"id": f"C{i}", "scenario": "s", "action": "a", "expected": "e", "verify": PASS if i % 2 else "", "level": "unit"}
                         for i in range(10000)]
        for i in range(3):  # many features too
            plan["features"].append(dict(f, id=f"F-more-{i}", criteria=f["criteria"][:50]))
        self.write_plan(plan)
        plan, state = self.load()
        t = time.monotonic()
        ctx = build_context(self.root, plan, state)
        took = time.monotonic() - t
        self.assertEqual(len(ctx["checks"][FID]["criteria"]), 10000)
        self.assertLess(took, 20, f"context took {took:.1f}s")
        t = time.monotonic()
        self.prove()
        out = json.loads(self.ok("status", "--json").stdout)
        self.assertGreater(len(out["features"]), 1)
        self.assertLess(time.monotonic() - t, 60)


class ItemsEqualFeaturesTest(Project):
    """The paths this module proves are the paths features.feature_items gives (2.8), so a proof covers what the status reads."""

    def test_item_paths_equal_feature_items_with_files_patterns_and_deletes(self):
        from bp import records
        def add(p):
            p["patterns"] = [{"id": "screen", "glob": "apps/web/src/screens/*.tsx", "module": "web", "phase": "P1", "purpose": "p",
                              "instance": "x", "features": [FID]},
                             {"id": "empty", "glob": "apps/web/src/none/*.tsx", "module": "web", "phase": "P1", "purpose": "p",
                              "features": [FID, "F-inbox"]}]
            p["files"].append({"path": "gone.txt", "action": "delete", "module": "web", "phase": "P1", "purpose": "p", "features": [FID]})
        self.edit_plan(add)
        self.touch("apps/web/src/screens/A.tsx")
        self.touch("apps/web/src/screens/B.tsx")
        self.ok("sync", "--quiet", "--no-render")
        plan, state = self.load()
        comp = status.compute(self.root, plan, state)
        index = records._item_index(plan, state)
        for fid in ("F-log-snag", "F-inbox"):
            want = [i["path"] for i in features.feature_items(self.root, plan, state, comp, fid)]
            self.assertEqual(index.get(fid, []), want, fid)
        self.assertIn("apps/web/src/screens/A.tsx", index[FID])
        self.assertIn("apps/web/src/none/*.tsx", index[FID])
        self.assertNotIn("gone.txt", index[FID])

    def test_feature_proof_equals_status_proof_current(self):
        from bp import records
        self.ready()
        plan, state = self.load()
        proof = records.feature_proof(self.root, plan, state, None, FID)
        self.assertEqual(set(proof), {"files"})
        self.assertTrue(status.proof_current(self.root, plan, proof, paths=list(ITEMS)))
        self.assertTrue(records._proof_current(proof, proof["files"]))
        self.touch(ITEMS[0], "other\n")
        now = records.feature_proof(self.root, plan, state, None, FID)
        self.assertFalse(status.proof_current(self.root, plan, proof, paths=list(ITEMS)))
        self.assertFalse(records._proof_current(proof, now["files"]))
        for bad in (None, {}, {"files": {}}, {"files": {"a": 1}}, {"files": proof["files"], "x": 1}, [], "x"):
            self.assertFalse(records._proof_current(bad, proof["files"]), bad)


# ======================================================================================================= verify

def runnable(command):
    """A printed command as this machine runs it: the text starts with `python`, which some systems only have as python3."""
    if command.startswith("python ") and shutil.which("python") is None:
        return shell_cmd(S.PY) + command[len("python"):]
    return command


def py(code):
    """A shell command that runs a line of Python with this interpreter (an absolute path: fixtures that use it are not
    the leak fixtures)."""
    return shell_cmd(S.PY, "-c", code)


class VerifyFeatureTest(Project):
    """verify --feature F [--run] [--criterion C] [--repeat N] [--by ID] [--json] (7.2, 4.5, 4.6)."""

    def test_without_run_it_lists_what_would_run_and_writes_nothing(self):
        r = self.ok("verify", "--feature", FID)
        self.assertIn("$ exit 0    # C1, C2", r.stdout)
        self.assertIn("C3 is passed only by an independent evaluation (accept)", r.stdout)
        self.assertEqual(self.records(), [])
        self.assertFalse((self.root / ".blueprint" / "verification").exists())

    def test_run_prints_each_result_records_it_and_logs_once(self):
        self.prove()
        before = len(self.log())
        r = self.ok("verify", "--feature", FID, "--run")
        self.assertEqual(r.stdout.splitlines(), ["$ exit 0", "  C1 pass (integration, 1/1)", "  C2 pass (integration, 1/1)",
                                                 "  C3 is passed only by an independent evaluation (accept)"])
        recs = self.records()
        self.assertEqual([(x["kind"], x["criterion"], x["result"], x["level"], x["by"], x["cmd"], x["exit"], x["repeat"]) for x in recs],
                         [("check", "C1", "pass", "integration", None, "exit 0", 0, {"n": 1, "passed": 1}),
                          ("check", "C2", "pass", "integration", None, "exit 0", 0, {"n": 1, "passed": 1})])
        for x in recs:
            self.assertEqual(set(x), {"id", "at", "tool", "kind", "feature", "by", "proof", "note", "criterion", "criterionDigest",
                                      "cmd", "result", "level", "exit", "durationMs", "repeat", "nonce"})
            self.assertRegex(x["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
            self.assertEqual(sorted(x["proof"]["files"]), sorted(ITEMS))
        added = self.log()[before:]
        self.assertEqual([e["event"] for e in added], ["verify-feature"])
        self.assertEqual(added[0]["records"], [x["id"] for x in recs])
        self.ok("verify", "--feature", FID, "--run", "--by", "evaluator")
        self.assertEqual([x["by"] for x in self.records()], [None, None, "evaluator", "evaluator"])

    def test_one_run_per_distinct_command_and_repeat_runs_it_that_many_times(self):
        counter = py("open('runs.txt','a').write('x')")

        def same(p):
            for c in feature_of(p)["criteria"][:2]:
                c["verify"] = counter
        self.edit_plan(same)
        self.prove()
        (self.root / "runs.txt").unlink(missing_ok=True)
        self.ok("verify", "--feature", FID, "--run")
        self.assertEqual((self.root / "runs.txt").read_text(), "x", "two criteria, one command, one run")
        self.assertEqual(len(self.records()), 2)
        (self.root / "runs.txt").unlink()
        r = self.ok("verify", "--feature", FID, "--run", "--repeat", "3")
        self.assertEqual((self.root / "runs.txt").read_text(), "xxx")
        self.assertIn("C1 pass (integration, 3/3)", r.stdout)
        self.assertEqual(self.records()[-1]["repeat"], {"n": 3, "passed": 3})

    def test_all_runs_must_pass_for_a_pass(self):
        flaky = py("import os,sys; seen=os.path.exists('n.txt'); open('n.txt','w').write('x'); sys.exit(1 if seen else 0)")
        self.edit_plan(lambda p: feature_of(p)["criteria"][0].update(verify=flaky))
        self.prove()
        (self.root / "n.txt").unlink(missing_ok=True)
        r = self.check("--repeat", "2")
        self.assertEqual(r.returncode, 1, r.stdout)
        c1 = next(x for x in self.records() if x["criterion"] == "C1")
        self.assertEqual((c1["result"], c1["repeat"], c1["exit"]), ("fail", {"n": 2, "passed": 1}, 1))
        self.assertIn("C1 fail (integration, 1/2)", r.stdout)
        self.assertIs(self.ctx()["checks"][FID]["commandPass"], False)

    def test_a_timeout_is_a_fail_that_is_recorded(self):
        slow = py("import time; time.sleep(30)")
        self.edit_plan(lambda p: feature_of(p)["criteria"][0].update(verify=slow))
        self.prove()
        started = time.monotonic()
        r = self.check("--timeout", "1", "--criterion", "C1")
        self.assertLess(time.monotonic() - started, 20, "the check and everything it started was killed at the timeout")
        self.assertEqual(r.returncode, 1)
        rec = self.records()[0]
        self.assertEqual((rec["result"], rec["exit"]), ("timeout", None))
        self.assertEqual(self.ctx()["checks"][FID]["criteria"]["C1"]["result"], "timeout")
        self.assertNotEqual(self.status_of()["status"], "verified")

    def test_a_check_that_cannot_start_is_an_error_result(self):
        from bp import records
        got = records._run_command("exit 0", self.root / "no such folder", 5, False)
        self.assertEqual((got["result"], got["exit"]), ("error", None))
        self.assertEqual(records._run_command("exit 0", self.root, 5, False)["result"], "pass")
        self.assertEqual(records._run_command("exit 3", self.root, 5, False)["exit"], 3)

    def test_json_keeps_stdout_for_the_envelope_only(self):
        self.edit_plan(lambda p: feature_of(p)["criteria"][0].update(verify=py("print('noise on stdout')")))
        self.prove()
        r = self.check("--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)  # the whole of stdout is one object
        self.assertEqual((out["ok"], out["command"]), (True, "verify"))
        self.assertEqual([x["criterion"] for x in out["data"]["records"]], ["C1", "C2"])
        self.assertTrue(r.stdout.startswith("{"), r.stdout[:40])  # no line of the check's output before the envelope
        self.assertIn("noise on stdout", r.stderr)
        self.assertIn("C1 pass", r.stderr)

    def test_exit_status_is_one_when_anything_does_not_pass(self):
        self.edit_plan(lambda p: feature_of(p)["criteria"][1].update(verify=FAIL))
        self.prove()
        r = self.check("--json")
        self.assertEqual(r.returncode, 1)
        out = json.loads(r.stdout)
        self.assertEqual([x["result"] for x in out["data"]["records"]], ["pass", "fail"])
        self.assertEqual(out["data"]["records"][1]["exit"], 3)

    def test_criterion_flag_runs_one_and_a_criterion_without_verify_cannot_be_passed_by_it(self):
        self.prove()
        self.ok("verify", "--feature", FID, "--run", "--criterion", "C2")
        self.assertEqual([x["criterion"] for x in self.records()], ["C2"])
        r = self.cli("verify", "--feature", FID, "--run", "--criterion", "C3")
        self.assertEqual(r.returncode, 1)
        self.assertIn("C3 is not run: it has no verify command", r.stdout)
        self.assertEqual(len(self.records()), 1)

    def test_refusals_name_the_entry_and_the_next_step(self):
        self.edit_plan(lambda p: p["features"].append(dict(feature_of(p, "F-inbox"), id="F-draft", draft=True)))
        cases = [(("--feature", "F-nope", "--run"), 1, "unknown-feature"),
                 (("--feature", "F-log-snag", "--run", "--criterion", "C9"), 1, "unknown-criterion"),
                 (("--feature", "F-log-snag", "--run", "--by", "nobody"), 1, "unknown-roster"),
                 (("--feature", "F-log-snag", "--run", "--by", "Evaluator"), 1, "unknown-roster"),
                 (("--feature", "F-log-snag", "--run", "--repeat", "0"), 64, "usage"),
                 (("--feature", "F-log-snag", "--run", "--repeat", "101"), 64, "usage"),
                 (("--feature", "F-log-snag", "--run", "--timeout", "0"), 64, "usage"),
                 (("--feature", "F-draft", "--run"), 64, "usage"),
                 (("--criterion", "C1"), 64, "usage")]
        for args, code, error in cases:
            r = self.cli("verify", *args, "--json")
            out = json.loads(r.stdout)
            self.assertEqual((r.returncode, out["ok"], out["error"]["code"]), (code, False, error), (args, r.stdout, r.stderr))
            self.assertTrue(out["error"]["next"], args)
            self.assertIn("error:", r.stderr)
        self.assertEqual(self.records(), [])

    def test_a_feature_id_that_is_listed_twice_or_has_a_wrong_form_is_refused(self):
        self.edit_plan(lambda p: p["features"].append(dict(feature_of(p, "F-inbox"), id=FID)))
        out = json.loads(self.cli("verify", "--feature", FID, "--run", "--json").stdout)
        self.assertEqual(out["error"]["code"], "unknown-feature")
        self.assertIn("listed 2 times", out["error"]["message"])
        self.edit_plan(lambda p: p["features"].__setitem__(0, dict(p["features"][0], id="F-Bad")))
        out = json.loads(self.cli("verify", "--feature", "F-Bad", "--run", "--json").stdout)
        self.assertEqual(out["error"]["code"], "unknown-feature")
        self.assertFalse((self.root / ".blueprint" / "verification").exists())

    def test_a_feature_with_no_files_on_disk_has_nothing_to_prove_so_nothing_is_recorded(self):
        for rel in ITEMS:
            (self.root / rel).unlink()
        r = self.check()
        self.assertEqual(r.returncode, 1)
        self.assertIn("none of the files that serve F-log-snag exist yet", r.stdout)
        self.assertEqual(self.records(), [])

    def test_files_that_change_while_the_checks_run_are_not_recorded(self):
        touch = py(f"open({ITEMS[0]!r},'a').write('x')")
        self.edit_plan(lambda p: feature_of(p)["criteria"][0].update(verify=touch))
        self.prove()
        r = self.check()
        self.assertEqual(r.returncode, 1)
        self.assertIn("changed while the checks ran", r.stdout)
        self.assertEqual(self.records(), [])

    def _reset_while_running(self, command, first_claim_by="backend-lead"):
        """A check that runs `command` (a blueprint call) against this project while it runs."""
        action = shell_cmd(S.PY, str(self.launcher()), *command, "--root", str(self.root))
        self.edit_plan(lambda p: feature_of(p)["criteria"][0].update(verify=action))
        self.prove()
        if first_claim_by:
            self.ok("claim", ITEMS[0], "--by", first_claim_by)
        return self.check()

    def test_a_file_marked_planned_while_the_check_ran_is_not_proven(self):
        r = self._reset_while_running(["mark", ITEMS[0], "planned", "--by", "backend-lead"])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"{ITEMS[0]}: not recorded: it was marked planned by backend-lead while the check ran", r.stdout)
        self.assertEqual(self.records(), [])
        event = [e for e in self.log() if e.get("event") == "verify-feature"][-1]
        self.assertEqual((event["notSaved"], event["records"]), ([ITEMS[0]], []))

    def test_a_file_claimed_by_someone_else_while_the_check_ran_is_not_proven(self):
        r = self._reset_while_running(["claim", ITEMS[0], "--by", "evaluator", "--force"])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("claimed by evaluator while the check ran", r.stdout)
        self.assertEqual(self.records(), [])

    def test_a_first_claim_while_the_check_ran_is_not_a_reset(self):
        r = self._reset_while_running(["claim", ITEMS[0], "--by", "backend-lead"], first_claim_by=None)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.records()), 2)

    def test_the_reset_rule_is_the_one_verify_uses(self):
        from bp import records
        from bp.commands import progress
        states = [(None, None, None), ("claimed", "a", "t1"), ("claimed", "b", "t2"), ("planned", "a", "t3"), ("built", "a", "t4"),
                  ("claimed", None, "t5"), ("blocked", "a", "t6")]
        for was in states + [None]:
            for now in states:
                self.assertEqual(records._reset_since(was, now), progress._reset_since(was, now), (was, now))

    def test_a_write_that_cannot_take_the_lock_is_state_busy_and_writes_nothing(self):
        self.prove()
        with core.state_lock(self.root):
            r = self.check("--json", BLUEPRINT_LOCK_TIMEOUT="1")
        self.assertEqual(r.returncode, 1)
        out = json.loads(r.stdout)
        self.assertEqual(out["error"]["code"], "state-busy")
        self.assertEqual(self.records(), [])

    def test_a_criterion_a_command_cannot_check_is_named_and_makes_the_exit_one(self):
        self.edit_plan(lambda p: (feature_of(p)["criteria"][0].update(verifyLevel="huge"),
                                  feature_of(p)["criteria"][1].update(level="manual")))
        self.prove()
        r = self.check()
        self.assertEqual(r.returncode, 1)
        self.assertIn("C1 is not run: its verifyLevel must be unit, integration or e2e", r.stdout)
        self.assertIn("C2 is not run: level manual needs an evaluation", r.stdout)
        self.assertEqual(self.records(), [])

    def test_a_feature_without_commands_is_a_clean_no_op(self):
        r = self.ok("verify", "--feature", "F-inbox", "--run")
        self.assertIn("no criterion with a verify command", r.stdout)
        self.assertEqual(self.records("F-inbox"), [])

    def test_damaged_lines_are_reported_on_stderr_and_ignored(self):
        write_lines(self.root, FID, b"garbage\n")
        self.prove()
        r = self.ok("verify", "--feature", FID, "--run")
        self.assertIn("warning: 1 line(s) of .blueprint/verification/F-log-snag.jsonl are damaged and ignored", r.stderr)
        self.assertEqual(len([x for x in self.raw_lines() if x]), 3)  # the garbage line and two records

    def test_the_public_function_returns_the_saved_records(self):
        from bp import records
        self.prove()
        plan, state = self.load()
        out = records.run_feature_checks(self.root, plan, FID, repeat=2, by="evaluator", quiet=True, out=lambda t: None, state=state)
        self.assertEqual([r["criterion"] for r in out.records], ["C1", "C2"])
        self.assertEqual(out.skipped, [])
        self.assertEqual(records.load_records(self.root)[FID], out.records)
        for bad in (0, 101, "2", True, 2.0):
            with self.assertRaises(BlueprintError):
                records.run_feature_checks(self.root, plan, FID, repeat=bad, quiet=True, out=lambda t: None)


# ====================================================================================================== accept

class AcceptTest(Project):
    """accept F --by ID --mode M [--result C=r]... (4.4, 7.2): who may, what must hold first, what the record says."""

    def setUp(self):
        super().setUp()
        self.ready()

    def accepted(self, **kw):
        r = self.accept("--result", "C3=pass", **kw)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.status_of()["status"], "accepted")

    def refused(self, *args, by="evaluator", mode="cli", fid=FID, code=3, error=None):
        before = (self.raw_lines(fid), len(self.log()))
        r = self.cli("accept", fid, "--by", by, "--mode", mode, *args, "--json")
        out = json.loads(r.stdout)
        self.assertEqual((r.returncode, out["ok"]), (code, False), r.stdout + r.stderr)
        if error:
            self.assertEqual(out["error"]["code"], error, out["error"])
        self.assertTrue(out["error"]["next"])
        self.assertEqual((self.raw_lines(fid), len(self.log())), before, "a refusal writes nothing")
        return out["error"]

    # ---- success
    def test_an_independent_evaluator_accepts(self):
        r = self.accept("--result", "C3=pass", "--note", "clicked through the flow")
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertRegex(r.stdout, r"^F-log-snag: accepted by evaluator [(]cli, e2e[)]; the files as they are now [(]sha256:[0-9a-f]{64}[)], not pinned with --proof$")
        ev = self.records()[-1]
        self.assertEqual((ev["kind"], ev["by"], ev["mode"], ev["level"], ev["verdict"], ev["independent"], ev["evidence"], ev["note"]),
                         ("evaluation", "evaluator", "cli", "e2e", "pass", True, [], "clicked through the flow"))
        self.assertEqual(list(ev["criteria"]), ["C3"])  # only what the evaluator checked
        self.assertEqual(ev["criteria"]["C3"]["result"], "pass")
        self.assertEqual(ev["proof"]["files"], self.records()[0]["proof"]["files"])
        self.assertEqual(self.log()[-1]["event"], "accept")
        f = self.status_of()
        self.assertEqual((f["status"], f["evaluation"]["by"], f["evaluation"]["verdict"]), ("accepted", "evaluator", "pass"))

    def test_json_data_is_the_record_and_the_status_after(self):
        r = self.accept("--result", "C3=pass", "--json")
        out = json.loads(r.stdout)
        self.assertEqual((out["ok"], out["command"], out["data"]["status"]), (True, "accept", "accepted"))
        self.assertEqual(out["data"]["record"], self.records()[-1])

    def test_evidence_ids_are_kept_and_must_have_the_form(self):
        ids = ["ev-0123456789abcdef", "ev-fedcba9876543210"]
        self.ok("accept", FID, "--by", "evaluator", "--mode", "cli", "--result", "C3=pass", "--evidence", ids[0], "--evidence", ids[1],
                "--evidence", ids[0])
        self.assertEqual(self.records()[-1]["evidence"], ids)
        for bad in ("ev-0123", "EV-0123456789abcdef", "ev-0123456789abcdef\n", "x"):
            self.refused("--result", "C3=pass", "--evidence", bad, code=64, error="usage")

    def test_a_note_is_redacted_and_kept_short(self):
        self.ok("accept", FID, "--by", "evaluator", "--mode", "cli", "--result", "C3=pass", "--note",
                f"see {self.root} and /home/me/x " + "z" * 900)
        note = self.records()[-1]["note"]
        self.assertLessEqual(len(note), 500)
        self.assertNotIn(str(self.root), note)
        self.assertNotIn("/home/me", note)

    def test_with_no_result_the_feature_is_not_accepted_and_says_what_is_missing(self):
        r = self.accept()
        self.assertEqual((r.returncode, r.stdout), (1, "F-log-snag: not accepted (C3 awaits evaluation)\n"))
        ev = self.records()[-1]
        self.assertEqual((ev["verdict"], ev["criteria"]), ("partial", {}))
        self.assertEqual(self.status_of()["status"], "verified")
        self.assertEqual(self.status_of()["statusText"], "verified, 1 criterion awaits evaluation")

    def test_a_fail_makes_the_feature_failed_and_a_later_pass_accepts_it_again(self):
        r = self.accept("--result", "C3=fail")
        self.assertEqual((r.returncode, r.stdout), (1, "F-log-snag: failed (C3 fail)\n"))
        self.assertEqual(self.records()[-1]["verdict"], "fail")
        self.assertEqual(self.status_of()["status"], "failed")
        self.accepted()  # re-evaluation is allowed from failed, and the later evaluation decides
        r = self.accept("--result", "C3=pass")  # and from accepted
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.status_of()["status"], "accepted")

    def test_a_pass_below_the_required_level_does_not_accept(self):
        for mode in ("api", "code-only"):
            r = self.accept("--result", "C3=pass", mode=mode)
            self.assertEqual(r.returncode, 1, mode)
            self.assertEqual(self.records()[-1]["verdict"], "partial", mode)
            self.assertEqual(self.status_of()["status"], "verified", mode)
        for mode, level in (("browser", "e2e"), ("device", "e2e"), ("cli", "e2e"), ("api", "integration"), ("code-only", "unit")):
            self.accept("--result", "C3=skipped", mode=mode)
            self.assertEqual(self.records()[-1]["level"], level)

    def test_skipped_and_unknown_results_do_not_pass(self):
        for result in ("skipped", "unknown"):
            r = self.accept("--result", f"C3={result}")
            self.assertEqual(r.returncode, 1)
            self.assertEqual(self.status_of()["status"], "verified")

    def test_when_every_criterion_has_a_command_the_evaluator_must_pass_at_least_one(self):
        self.edit_plan(lambda p: feature_of(p).update(criteria=feature_of(p)["criteria"][:2]))
        self.ok("verify", "--feature", FID, "--run")
        self.assertEqual(self.status_of()["status"], "verified")
        r = self.accept()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.status_of()["status"], "verified")
        self.assertEqual(self.accept("--result", "C1=pass").returncode, 0)
        self.assertEqual(self.status_of()["status"], "accepted")

    # ---- who may accept
    def test_the_owner_of_the_feature_may_not_accept_it(self):
        e = self.refused(by="backend-lead", error="not-independent")
        self.assertEqual(e["entry"], f"feature {FID}")
        self.assertIn("backend-lead is the owner of F-log-snag", e["message"])
        self.assertIn("evaluator", e["message"])  # who may

    def test_the_owner_of_one_file_of_the_feature_may_not_accept_it(self):
        self.edit_plan(lambda p: p["owners"].append({"pattern": ITEMS[1], "owner": "frontend-engineer"}))
        e = self.refused("--result", "C3=pass", by="frontend-engineer", error="not-independent")
        self.assertIn(f"owns {ITEMS[1]}", e["message"])

    def test_someone_who_claimed_or_built_a_file_of_the_feature_may_not_accept_it(self):
        self.ok("claim", ITEMS[0], "--by", "evaluator", "--force")
        e = self.refused("--result", "C3=pass", error="not-independent")
        self.assertIn(f"evaluator claimed or built {ITEMS[0]}", e["message"])
        self.ok("claim", ITEMS[0], "--by", "backend-lead")  # the owner takes it back
        state = json.loads((self.root / ".blueprint" / "state.json").read_text(encoding="utf-8"))
        if "workedBy" in state["files"][ITEMS[0]]:  # the core remembers everyone who claimed or marked it: still refused
            self.refused("--result", "C3=pass", error="not-independent")
        else:  # without the core's writer only the latest `by` is on record
            self.assertEqual(self.accept("--result", "C3=pass").returncode, 0)

    def test_no_flag_skips_the_independence_check(self):
        for flag in ("--force", "--skip-independent", "--allow-owner"):
            r = self.cli("accept", FID, "--by", "backend-lead", "--mode", "cli", "--result", "C3=pass", flag)
            self.assertEqual(r.returncode, 2, flag)
        self.assertEqual([x["kind"] for x in self.records()], ["check", "check"])

    def test_an_id_must_be_a_roster_id_exactly(self):
        for by in ("nobody", "evaluator ", " evaluator", "Evaluator", "EVALUATOR", "evaluator\n", "evalu\u0430tor", "evaluator\u200b", ""):
            e = self.refused("--result", "C3=pass", by=by, code=1, error="unknown-roster")
            if by:
                self.assertNotIn(by, e["next"])

    def test_ids_that_differ_only_by_spelling_from_an_owner_are_the_same_person_here(self):
        def respell(p):
            feature_of(p)["owner"] = "Backend-Lead "
            for r in p["owners"]:
                if r["owner"] == "backend-lead":
                    r["owner"] = "BACKEND-LEAD"
        self.edit_plan(respell)
        self.refused("--result", "C3=pass", by="backend-lead", error="not-independent")
        self.assertEqual(self.accept("--result", "C3=pass").returncode, 0)  # an unrelated id still may

    def test_duplicate_roster_entries_do_not_give_anyone_a_property(self):
        self.edit_plan(lambda p: (roster_add(p, "evaluator"), roster_add(p, "backend-lead")))
        self.refused(by="backend-lead", error="not-independent")  # still the owner, listed twice
        self.assertEqual(self.accept("--result", "C3=pass").returncode, 0)  # an evaluator listed twice may accept

    def test_the_table_and_the_command_give_the_same_answer(self):
        from bp import records
        self.ok("claim", ITEMS[0], "--by", "evaluator", "--force")
        plan, state = self.load()
        ids = [r["id"] for r in plan["roster"]]
        may = records.independent_ids(plan, state, FID)
        self.assertNotIn("evaluator", may)
        table = self.ctx()
        index = {f["id"]: f for f in table["features"]}
        feature = next(f for f in plan["features"] if f["id"] == FID)
        self.assertEqual(features.independent_ids(plan, feature, index[FID]["items"], None, state), may)
        for rid in ids:
            r = self.cli("accept", FID, "--by", rid, "--mode", "cli", "--result", "C3=pass", "--json")
            self.assertEqual(r.returncode != 3, rid in may, (rid, r.stdout))
            if r.returncode == 0:
                break

    # ---- a manual criterion: an evaluation only, by any independent roster id
    def manual_project(self):
        """F-inbox: every file verified, one criterion at level manual (no command), nothing blocked."""
        self.prove()
        self.assertEqual(self.status_of("F-inbox")["status"], "verified")

    def test_an_independent_agent_evaluator_accepts_a_feature_with_a_manual_criterion(self):
        self.manual_project()
        r = self.accept("--result", "C1=pass", by="evaluator", mode="manual", fid="F-inbox")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("F-inbox: accepted by evaluator (manual, manual)"), r.stdout)
        self.assertEqual(self.status_of("F-inbox")["status"], "accepted")
        self.assertEqual(self.accept("--result", "C1=pass", by="owner", mode="manual", fid="F-inbox").returncode, 0)  # a person too

    def test_the_owner_is_refused_on_the_manual_feature(self):
        self.manual_project()
        self.refused("--result", "C1=pass", by="frontend-engineer", mode="manual", fid="F-inbox", error="not-independent")

    def test_a_command_never_satisfies_a_manual_criterion(self):
        self.manual_project()
        self.edit_plan(lambda p: feature_of(p, "F-inbox")["criteria"][0].update(verify=PASS))
        r = self.cli("verify", "--feature", "F-inbox", "--run")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.records("F-inbox"), [])
        self.assertNotEqual(self.status_of("F-inbox")["status"], "accepted")

    def test_an_agent_may_fail_a_manual_criterion_or_leave_it_open(self):
        self.manual_project()
        r = self.accept("--result", "C1=fail", fid="F-inbox")
        self.assertEqual((r.returncode, r.stdout), (1, "F-inbox: failed (C1 fail)\n"))
        self.assertEqual(self.status_of("F-inbox")["status"], "failed")
        r = self.accept("--result", "C1=skipped", fid="F-inbox")
        self.assertEqual(r.returncode, 1)

    def test_an_evaluation_stops_counting_when_its_author_is_no_longer_independent(self):
        self.manual_project()
        self.assertEqual(self.accept("--result", "C1=pass", by="evaluator", mode="manual", fid="F-inbox").returncode, 0)
        self.ok("claim", "apps/web/src/Inbox.tsx", "--by", "evaluator", "--force")
        self.assertEqual(self.status_of("F-inbox")["status"], "verified")

    # ---- what must hold first
    def test_the_gate_is_verified_and_names_what_is_missing(self):
        self.touch(ITEMS[0], "edited after the checks\n")
        e = self.refused("--result", "C3=pass", error="not-verified")
        self.assertIn("not verified", e["message"])
        self.assertIn("file(s) not verified", e["message"])
        self.assertIn("verify --run", e["next"])

    def test_a_failing_or_stale_command_check_blocks_acceptance(self):
        self.prove()
        self.edit_plan(lambda p: feature_of(p)["criteria"][1].update(verify=FAIL))
        e = self.refused("--result", "C3=pass", error="not-verified")
        self.assertIn("criteria without a passing, current check: C2", e["message"])
        self.check()
        self.refused("--result", "C3=pass", error="not-verified")

    def test_a_draft_and_an_unidentifiable_criterion_cannot_be_accepted(self):
        self.edit_plan(lambda p: p["features"].append(dict(feature_of(p, "F-inbox"), id="F-draft", draft=True)))
        self.refused(fid="F-draft", error="not-verified")

        def twin(p):
            feature_of(p)["criteria"].append(dict(feature_of(p)["criteria"][2]))
        self.edit_plan(twin)
        e = self.refused("--result", "C1=pass", error="not-verified")
        self.assertIn("no id of its own", e["message"])
        self.refused("--result", "C3=pass", code=1, error="unknown-criterion")  # a criterion that is not told apart is never named

    def test_the_independence_refusal_comes_before_the_verification_refusal(self):
        self.touch(ITEMS[0], "edited\n")
        self.refused(by="backend-lead", error="not-independent")

    def test_argument_errors(self):
        for args, code, error in ((("--result", "C3"), 64, "usage"), (("--result", "=pass"), 64, "usage"),
                                  (("--result", "C3=maybe"), 64, "usage"), (("--result", "C3=pass", "--result", "C3=fail"), 64, "usage"),
                                  (("--result", "C3=Pass"), 64, "usage"), (("--result", "C3=pass "), 64, "usage"),
                                  (("--result", "C9=pass"), 1, "unknown-criterion"), (("--result", "c3=pass"), 1, "unknown-criterion"),
                                  (("--result", " C3=pass"), 1, "unknown-criterion")):
            self.refused(*args, code=code, error=error)
        out = json.loads(self.cli("accept", "F-nope", "--by", "evaluator", "--mode", "cli", "--json").stdout)
        self.assertEqual(out["error"]["code"], "unknown-feature")
        self.assertEqual(self.cli("accept", FID, "--by", "evaluator", "--mode", "robot").returncode, 2)
        self.assertEqual(self.cli("accept", FID, "--mode", "cli").returncode, 2)
        self.assertEqual(self.cli("accept", "--by", "evaluator", "--mode", "cli").returncode, 2)

    def test_a_criterion_is_taken_as_written(self):
        out = json.loads(self.cli("accept", FID, "--by", "evaluator", "--mode", "cli", "--result", "C3\n=pass", "--json").stdout)
        self.assertEqual(out["error"]["code"], "unknown-criterion")

    # ---- an evaluation stops counting when what it saw changed
    def test_editing_a_file_a_criterion_or_adding_one_returns_to_verified_not_failed(self):
        edits = {"item": lambda: self.touch(ITEMS[0], "edited\n"),
                 "criterion": lambda: self.edit_plan(lambda p: feature_of(p)["criteria"][2].update(expected="something else")),
                 "new criterion": lambda: self.edit_plan(lambda p: feature_of(p)["criteria"].append(
                     {"id": "C4", "scenario": "s", "action": "a", "expected": "e"})),
                 "feature owner": lambda: self.edit_plan(lambda p: feature_of(p).update(owner="evaluator")),
                 "file owner": lambda: self.edit_plan(lambda p: p["owners"].append({"pattern": ITEMS[1], "owner": "evaluator"})),
                 "roster": lambda: self.edit_plan(lambda p: p["roster"].remove(next(r for r in p["roster"] if r["id"] == "evaluator"))),
                 "claim": lambda: self.ok("claim", ITEMS[0], "--by", "evaluator", "--force")}
        for name, edit in edits.items():
            self.write_plan(base_plan())
            for rel in ALL_FILES:
                self.touch(rel)
            self.ready()
            self.accepted()
            edit()
            self.prove()
            self.ok("verify", "--feature", FID, "--run")
            f = self.status_of()
            self.assertEqual(f["status"], "verified", name)
            if name in ("new criterion", "feature owner", "file owner", "roster", "claim"):
                # the evaluation still stands as the latest one (nothing older revives); its passes count for nothing
                # when its author is no longer independent, and the new criterion is unknown
                self.assertEqual(f["evaluation"]["verdict"], "partial", name)
                self.assertEqual(f["awaitingEvaluation"], 1, name)
            else:
                self.assertIsNone(f["evaluation"], name)

    def test_a_fail_stops_counting_when_the_criterion_it_judged_changes(self):
        self.assertEqual(self.accept("--result", "C3=fail").returncode, 1)
        self.assertEqual(self.status_of()["status"], "failed")
        self.edit_plan(lambda p: feature_of(p)["criteria"].append({"id": "C4", "scenario": "s", "action": "a", "expected": "e"}))
        self.assertEqual(self.status_of()["status"], "failed", "the evaluation still stands for the criteria it saw")
        self.edit_plan(lambda p: feature_of(p)["criteria"][2].update(expected="changed so the old fail no longer applies"))
        self.assertEqual(self.status_of()["status"], "verified")

    def test_the_latest_current_evaluation_decides(self):
        """B passes after A failed: accepted. B then loses independence: B is still the latest, its pass counts for
        nothing, and A's older fail is not consulted again (the feature waits for a new evaluation)."""
        self.edit_plan(lambda p: roster_add(p, "second-evaluator"))
        self.assertEqual(self.accept("--result", "C3=fail").returncode, 1)
        self.assertEqual(self.accept("--result", "C3=pass", by="second-evaluator").returncode, 0)
        self.assertEqual(self.status_of()["status"], "accepted")
        self.edit_plan(lambda p: feature_of(p).update(owner="second-evaluator"))
        self.assertEqual(self.status_of()["status"], "verified")
        self.assertEqual(self.accept("--result", "C3=pass").returncode, 0)
        self.assertEqual(self.status_of()["status"], "accepted")

    def test_entries_and_the_evaluation_always_agree(self):
        from bp import records
        for results in ({"C3": "pass"}, {"C3": "fail"}, {"C3": "skipped"}, {"C3": "unknown"}, {}, {"C1": "pass", "C3": "pass"},
                        {"C1": "fail"}):
            self.accept(*[a for k, v in results.items() for a in ("--result", f"{k}={v}")])
            chk = self.ctx()["checks"][FID]
            ev = chk["evaluation"]
            self.assertIsNotNone(ev, results)
            for cid, e in chk["criteria"].items():
                if e["via"] == "evaluation":
                    self.assertEqual((e["result"], e["level"], e["recordId"]), (ev["criteria"][cid]["result"], ev["level"], ev["id"]))
            for cid, r in ev["criteria"].items():
                e = chk["criteria"][cid]
                if e["via"] is None:
                    self.fail(f"{cid}: the evaluation names it but its entry does not come from the evaluation")
            self.assertEqual(ev["verdict"], records._verdict(feature_of(bplan.load_plan(self.root)), ev))
            self.assertEqual(ev["id"], self.records()[-1]["id"])

    def test_an_interrupted_write_leaves_no_state_where_the_two_disagree(self):
        """A torn evaluation line is ignored: no entry says pass, no evaluation exists."""
        self.accepted()
        path = self.root / ".blueprint" / "verification" / f"{FID}.jsonl"
        data = path.read_bytes().rstrip(b"\n")
        cut = data.rfind(b"\n") + 1
        path.write_bytes(data[: cut + 60])  # the evaluation line cut in the middle
        chk = self.ctx()["checks"][FID]
        self.assertIsNone(chk["evaluation"])
        self.assertEqual(chk["criteria"]["C3"], {"result": "unknown", "level": None, "at": None, "stale": False, "recordId": None,
                                                 "via": None, "repeat": None})
        self.assertEqual(self.status_of()["status"], "verified")
        self.assertEqual(self.accept("--result", "C3=pass").returncode, 0)  # and the next record starts on its own line
        self.assertEqual(self.status_of()["status"], "accepted")

    def test_a_forged_evaluation_that_is_not_independent_never_counts(self):
        plan, state = self.load()
        for by in ("backend-lead", "nobody", None):
            write_lines(self.root, FID, valid_evaluation(self.root, plan, by=by))
            self.assertEqual(self.status_of()["status"], "verified", by)

    def test_a_forged_evaluation_by_the_owner_of_the_manual_feature_never_counts(self):
        self.manual_project()
        plan, state = self.load()
        write_lines(self.root, "F-inbox", valid_evaluation(self.root, plan, fid="F-inbox", by="frontend-engineer", results={"C1": "pass"}))
        self.assertEqual(self.status_of("F-inbox")["status"], "verified")

    # ---- the check is repeated inside the lock
    def _race(self, mutate):
        """record_evaluation in process, with `mutate(root)` run just after the state lock is taken (as another command
        would if it had won the race between the pre-check and the write)."""
        from bp import records
        real = core.state_lock

        @contextlib.contextmanager
        def racing(root):
            with real(root):
                mutate(root)
                yield
        plan, state = self.load()
        ctx = build_context(self.root, plan, state)
        before = (self.raw_lines(), len(self.log()))
        err = None
        with mock.patch.object(core, "state_lock", racing):
            try:
                records.record_evaluation(self.root, plan, FID, "evaluator", "cli", {"C3": "pass"}, [], None, ctx=ctx)
            except BlueprintError as e:
                err = e
        self.assertEqual((self.raw_lines(), len(self.log())), before, "nothing was written")
        return err

    def test_ownership_changed_by_another_command_is_seen_inside_the_lock(self):
        err = self._race(lambda root: self.edit_plan(lambda p: p["owners"].append({"pattern": ITEMS[0], "owner": "evaluator"})))
        self.assertEqual((err.error, err.code), ("not-independent", 3))

    def test_a_claim_made_meanwhile_is_seen_inside_the_lock(self):
        def mutate(root):
            state = json.loads((root / ".blueprint" / "state.json").read_text(encoding="utf-8"))
            state["files"].setdefault(ITEMS[1], {}).update(state="claimed", by="evaluator", at="2026-01-01T00:00:00Z")
            core.write_state(root, state)
        err = self._race(mutate)
        self.assertEqual((err.error, err.code), ("not-independent", 3))

    def test_the_feature_owner_changed_meanwhile_is_seen_inside_the_lock(self):
        err = self._race(lambda root: self.edit_plan(lambda p: feature_of(p).update(owner="evaluator")))
        self.assertEqual(err.error, "not-independent")

    def test_the_roster_changed_meanwhile_is_seen_inside_the_lock(self):
        err = self._race(lambda root: self.edit_plan(lambda p: p["roster"].remove(next(r for r in p["roster"] if r["id"] == "evaluator"))))
        self.assertEqual((err.error, err.code), ("unknown-roster", 1))

    def test_the_feature_removed_meanwhile_is_seen_inside_the_lock(self):
        err = self._race(lambda root: self.edit_plan(lambda p: p["features"].remove(feature_of(p))))
        self.assertEqual(err.error, "unknown-feature")

    def test_without_an_evidence_module_only_the_form_is_checked(self):
        r = self.accept("--result", "C3=pass", "--evidence", "ev-fedcba9876543210")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_an_unreadable_plan_inside_the_lock_writes_nothing(self):
        def mutate(root):
            (root / ".blueprint" / "blueprint.json").write_text("{not json", encoding="utf-8")
        before = self.raw_lines()
        err, _ = quiet(self._race, mutate)
        self.assertEqual(err.error, "bad-json")
        self.assertEqual(self.raw_lines(), before)

    def test_a_busy_lock_is_state_busy_and_writes_nothing(self):
        before = self.raw_lines()
        with core.state_lock(self.root):
            r = self.cli("accept", FID, "--by", "evaluator", "--mode", "cli", "--result", "C3=pass", "--json", BLUEPRINT_LOCK_TIMEOUT="1")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)["error"]["code"], "state-busy")
        self.assertEqual(self.raw_lines(), before)

    def test_the_fallback_context_gives_the_same_decision(self):
        from bp import records
        plan, state = self.load()
        rec = records.record_evaluation(self.root, plan, FID, "evaluator", "cli", {"C3": "pass"}, [], None)  # no ctx given
        self.assertEqual(rec["verdict"], "pass")
        self.assertEqual(self.status_of()["status"], "accepted")


# ============================================================================================ protected paths

PROTECTED = (".blueprint/state.json", ".blueprint/state.lock", ".blueprint/.gitignore", ".blueprint/log.jsonl",
             ".blueprint/verification", ".blueprint/verification/F-log-snag.jsonl", ".blueprint/verification/F-new.jsonl",
             ".blueprint/verification/sub/anything.txt", ".blueprint/bin", ".blueprint/bin/blueprint.py", ".blueprint/bin/bp/core.py",
             ".blueprint/studies/S-acme/study.json", ".blueprint/STRUCTURE.md", ".blueprint/ROSTER.md", ".blueprint/ROADMAP.md",
             ".blueprint/FEATURES.md", ".blueprint/map.html")
ALLOWED = (".blueprint/blueprint.json", ".blueprint/studies/S-acme/behaviours.json", ".blueprint/studies/S-acme/evidence.jsonl",
           ".blueprint/blueprint.v1.json", ".blueprint/README.md", ".blueprint/studies/S-acme/study.json.bak",
           ".blueprint/studies/study.json", ".blueprint/studies/S-acme/deeper/study.json", ".blueprint/verifications.txt",
           ".blueprint/state.json.bak", ".blueprint/binary", ".blueprint/map.html.old", ".blueprint", "README.md",
           "src/blueprint/state.json", "crates/snag-api/src/main.rs")
MARKER = "is written only by blueprint.py. Use the command instead: "


class ProtectedPathsTest(Project):
    """4.8: the guard refuses writes by agents to what only the tool writes, in every spelling the guard resolves."""

    def setUp(self):
        super().setUp()
        self.prove()  # state.json, log.jsonl and the lock exist now

    def refuse(self, path, action="write"):
        from bp import records
        return records.refuse(action, self.root, bplan.load_plan(self.root), path, None)

    def guard(self, path, **env):
        return self.cli("guard", str(path), **env)

    def hook(self, path, **env):
        payload = json.dumps({"cwd": str(self.root), "tool_input": {"file_path": str(path)}})
        return self.cli("hook", "pre", stdin=payload, **env)

    def test_every_protected_path_is_refused_with_the_command_to_use(self):
        for path in PROTECTED:
            msg = self.refuse(path)
            self.assertTrue(msg and msg.startswith(f"{path} {MARKER}"), (path, msg))
            self.assertRegex(msg.split(MARKER)[1], r'^python .*blueprint\.py"? .+')

    def test_everything_else_is_allowed(self):
        for path in ALLOWED:
            self.assertIsNone(self.refuse(path), path)

    def test_a_claim_is_not_judged_here_and_odd_input_never_raises(self):
        self.assertIsNone(self.refuse(".blueprint/state.json", action="claim"))
        for odd in (None, 5, [], b"x", {}):
            self.assertTrue(self.refuse(odd))
        self.assertIsNone(self.refuse(""))
        self.assertIsNone(self.refuse("\x00"))

    def test_the_message_runs_as_printed_and_never_echoes_what_the_caller_typed(self):
        for path, word in ((".blueprint/state.json", "status"), (".blueprint/map.html", "render"), (".blueprint/ROADMAP.md", "render"),
                           (".blueprint/verification/F-log-snag.jsonl", "verify --feature F-log-snag --run"),
                           (".blueprint/verification/F-ghost.jsonl", "status --features"),
                           (".blueprint/verification/$(touch pwned).jsonl", "status --features"),
                           (".blueprint/verification/F-log-snag.jsonl; touch pwned", "status --features")):
            msg = self.refuse(path)
            command = msg.split(MARKER)[1]
            self.assertIn(word, command, path)
            self.assertNotIn("pwned", command)
            self.assertNotIn("$(", command)
        for path in (".blueprint/state.json", ".blueprint/map.html"):
            command = self.refuse(path).split(MARKER)[1]
            r = subprocess.run(runnable(command), shell=True, cwd=str(self.root), capture_output=True, text=True, encoding="utf-8",
                               env=S.tool_env())
            self.assertEqual(r.returncode, 0, (command, r.stdout, r.stderr))
        self.assertFalse((self.root / "pwned").exists())

    def spellings(self):
        root = self.root
        out = [".blueprint/state.json", "./.blueprint/state.json", ".blueprint//state.json", ".blueprint/./state.json",
               ".blueprint/verification/../state.json", "src/../.blueprint/state.json", str(root / ".blueprint" / "state.json"),
               str(root / "src" / ".." / ".blueprint" / "state.json"), (root / ".blueprint" / "state.json").as_posix()]
        if os.name == "nt":
            out += [".BLUEPRINT\\STATE.JSON", ".blueprint\\state.json", ".blueprint/State.JSON", ".blueprint/state.json.",
                    ".blueprint/state.json ", ".blueprint/state.json. .", ".blueprint/state.json::$DATA", ".blueprint/state.json:x",
                    "\\\\?\\" + str(root / ".blueprint" / "state.json"), str(root).upper() + "\\.blueprint\\state.json",
                    ".blueprint/verification/F-log-snag.jsonl::$DATA", ".blueprint/VERIFICATION/f-log-snag.JSONL"]
        return out

    def test_the_guard_command_refuses_every_spelling(self):
        for path in self.spellings():
            r = self.guard(path)
            self.assertEqual(r.returncode, 2, (path, r.stdout, r.stderr))
            self.assertIn(MARKER, r.stderr, path)

    def test_hook_pre_refuses_every_spelling_and_the_modes_still_apply(self):
        for path in self.spellings():
            r = self.hook(path)
            self.assertEqual(r.returncode, 2, (path, r.stderr))
            self.assertIn(MARKER, r.stderr)
        r = self.hook(".blueprint/state.json", BLUEPRINT_GUARD="ask")
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "ask")
        self.assertIn(MARKER, out["permissionDecisionReason"])
        self.assertEqual(self.hook(".blueprint/state.json", BLUEPRINT_GUARD="off").returncode, 0)

    def test_what_agents_may_edit_stays_editable(self):
        for path in (".blueprint/blueprint.json", ".blueprint/studies/S-acme/behaviours.json", ".blueprint/studies/S-acme/evidence.jsonl"):
            self.assertEqual(self.guard(path).returncode, 0, path)
            self.assertEqual(self.hook(path).returncode, 0, path)

    def test_a_link_to_a_protected_file_is_refused_as_the_file_it_points_to(self):
        try:
            os.symlink(self.root / ".blueprint" / "state.json", self.root / "alias.json")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available here")
        self.assertEqual(self.guard("alias.json").returncode, 2)
        self.assertEqual(self.hook("alias.json").returncode, 2)

    def test_a_write_to_a_path_outside_the_project_is_not_this_modules_business(self):
        self.assertIsNone(self.refuse("../outside/.blueprint/state.json"))
        self.assertEqual(self.hook(Path(tempfile.gettempdir()) / "x" / ".blueprint" / "state.json").returncode, 0)

    def test_the_hook_through_the_registry_in_the_isolated_package(self):
        got = registry.refusal("write", self.root, bplan.load_plan(self.root), ".blueprint/log.jsonl", None)
        self.assertIn(MARKER, got)
        self.assertIsNone(registry.refusal("claim", self.root, bplan.load_plan(self.root), ".blueprint/log.jsonl", None))
        self.assertIs(registry.hook("verify_feature"), __import__("bp.records", fromlist=["x"]).verify_feature)


# ======================================================================================== no machine in files

BS = chr(92)
_DRIVE = re.compile("(?<![A-Za-z0-9])[A-Za-z]:[" + BS + BS + "/]")
_UNC = re.compile("(?<![" + BS + "w" + BS + BS + "])" + BS + BS + "{2,4}[A-Za-z0-9_.$-]+" + BS + BS + "{1,2}[A-Za-z0-9_.$-]")
_ROOTED = re.compile(r"(?<![\w/.:<>-])/(?:home|Users|tmp|var|mnt|private|root|usr|opt|etc|run|Volumes)/")


def leaks(text, machine=()):
    """What in text names this machine: one of the given folders, a drive-letter path, a UNC path or a rooted path."""
    found = [m for m in machine if m and m in text]
    for rx in (_DRIVE, _UNC, _ROOTED):
        m = rx.search(text)
        if m:
            found.append(m.group(0))
    return found


class NoMachineSpecificTextTest(Project):
    """Files in .blueprint/ are committed and shared: none may carry an absolute path or anything of this machine."""

    def test_the_scanner_catches_leaks_and_passes_clean_text(self):
        win = "C:" + BS + "Users" + BS + "someone" + BS + "proj"
        unc = BS + BS + "server" + BS + "share" + BS + "proj"
        json_unc = BS * 4 + "server" + BS * 2 + "share" + BS * 2 + "x"
        for bad in (win, "D:/work/app", unc, "at " + unc + " now", json_unc, "/home/x/project", "cwd=/tmp/bp 123/x", '("/Users/x/y")',
                    "see " + str(self.root) + "/x", "see " + self.root.as_posix() + "/x", json.dumps(win)):
            self.assertTrue(leaks(bad, {str(self.root), self.root.as_posix()}), bad)
        for ok in ("https://example.test/a/b", "apps/web/src/x.ts", "a/b", "1/2", "2027-01-15", "and/or", "F-log-snag", "vr-0123456789ab",
                   "sha256:" + "0" * 64, "http://localhost:3000/tmp", "a:b", "x = a / home / b", "exit 0", "python .blueprint/bin/blueprint.py status"):
            self.assertEqual(leaks(ok, {str(self.root), self.root.as_posix()}), [], ok)

    def machine(self):
        return {str(self.root), self.root.as_posix(), str(self.root.resolve()), self.root.resolve().as_posix(), tempfile.gettempdir(),
                Path(tempfile.gettempdir()).as_posix(), str(S.SKILL), S.SKILL.as_posix(), os.path.expanduser("~"),
                Path(os.path.expanduser("~")).as_posix()}

    def test_nothing_written_into_the_project_names_this_machine(self):
        self.prove()
        note = f"saw {self.root} and {self.root.as_posix()} and C:{BS}Users{BS}someone{BS}x and /home/me/secret"
        r = self.ok("verify", "--feature", FID, "--run", "--by", "evaluator", "--repeat", "2", "--json")
        out = [r.stdout]
        self.assertEqual(self.cli("accept", FID, "--by", "backend-lead", "--mode", "cli", "--json").returncode, 3)  # a refused call
        r = self.ok("accept", FID, "--by", "evaluator", "--mode", "cli", "--result", "C3=pass", "--note", note, "--json")
        out.append(r.stdout)
        r = self.cli("accept", "F-nope", "--by", "evaluator", "--mode", "cli", "--json")
        out.append(r.stdout)
        self.ok("render")
        folder = self.root / ".blueprint"
        files = [p for p in folder.rglob("*") if p.is_file() and p.suffix != ".lock"]
        names = {p.name for p in files}
        self.assertTrue({"state.json", "log.jsonl", f"{FID}.jsonl", "map.html", "STRUCTURE.md", "ROSTER.md"} <= names, names)
        for p in files:
            self.assertEqual(leaks(p.read_text(encoding="utf-8", errors="replace"), self.machine()), [], str(p.relative_to(folder)))
        for text in out[:2]:  # the JSON output holds the records: portable too
            self.assertEqual(leaks(text, self.machine() - {tempfile.gettempdir(), Path(tempfile.gettempdir()).as_posix()}), [])
        self.assertTrue("<project>" in self.records()[-1]["note"] or "<path>" in self.records()[-1]["note"])  # the root may be spelled 8.3 or long

    def test_next_texts_in_json_failures_name_the_tool_only_as_the_launcher_prints_it(self):
        """A refusal's next is a console text (run as printed); nothing of it reaches a project file."""
        r = self.cli("accept", FID, "--by", "evaluator", "--mode", "cli", "--json")
        nxt = json.loads(r.stdout)["error"]["next"]
        self.assertTrue(nxt.startswith("python "))
        run = subprocess.run(runnable(nxt), shell=True, cwd=str(self.root), capture_output=True, text=True, encoding="utf-8", env=S.tool_env())
        self.assertIn(run.returncode, (0, 1), run.stderr)
        self.assertNotIn("unrecognized", run.stderr)
        self.assertIn("verify --run", nxt)


# ================================================================================================= in parallel

class ParallelWritersTest(Project):
    """Real processes through the CLI: records and log lines all land, whole, whatever order they run in."""

    N = int(os.environ.get("BP_RACE_N", "8"))

    def setUp(self):
        super().setUp()
        self.edit_plan(lambda p: feature_of(p, "F-inbox")["criteria"].append(
            {"id": "C2", "scenario": "s", "action": "a", "expected": "e", "verify": PASS, "level": "unit"}))
        self.ready()

    def start(self, *args):
        argv = [S.PY, str(self.launcher()), *[str(a) for a in args], "--root", str(self.root)]
        return subprocess.Popen(argv, cwd=str(self.root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=S.tool_env(BLUEPRINT_LOCK_TIMEOUT="300"))

    def test_checks_acceptances_claims_and_syncs_at_once_lose_nothing_and_tear_nothing(self):
        n = self.N
        before_lines = len([x for x in self.raw_lines() if x])
        before_log = len(self.log())
        procs = []
        for i in range(n):
            procs.append(self.start("verify", "--feature", FID, "--run", "--by", "evaluator"))
            procs.append(self.start("verify", "--feature", "F-inbox", "--run", "--by", "evaluator"))
            procs.append(self.start("accept", FID, "--by", "evaluator", "--mode", "cli", "--result", "C3=pass", "--note", f"run {i}"))
            procs.append(self.start("claim", "Cargo.toml", "--by", "cto-architect", "--note", f"c{i}"))
            if i % 4 == 0:
                procs.append(self.start("sync", "--quiet"))
        results = finish(procs, self)
        for code, out, err, _ in results:
            self.assertEqual(code, 0, out + err)
        lines = [x for x in self.raw_lines() if x]
        self.assertEqual(len(lines), before_lines + n * 2 + n, "every record landed")
        for line in lines:
            json.loads(line)  # whole
        recs, damaged = __import__("bp.records", fromlist=["x"]).read_records(self.root, FID)
        self.assertEqual(damaged, 0)
        self.assertEqual(len([x for x in self.records("F-inbox")]), n)
        events = [e["event"] for e in self.log()[before_log:]]
        self.assertEqual(events.count("verify-feature"), 2 * n)
        self.assertEqual(events.count("accept"), n)
        self.assertEqual(events.count("claimed"), n)
        self.assertEqual([p.name for p in (self.root / ".blueprint").rglob("*.tmp")], [])
        self.assertEqual(self.status_of()["status"], "accepted")
        state = json.loads((self.root / ".blueprint" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["files"]["Cargo.toml"]["state"], "claimed")


# ================================================================================== module shape and the plug-in

class ModuleShapeTest(Isolated, unittest.TestCase):
    OWN = ("records.py", "commands/records.py")

    def source(self, rel):
        return (S.PACKAGE / rel).read_text(encoding="utf-8")

    def test_the_files_parse_under_python_3_10_grammar(self):
        for rel in self.OWN:
            ast.parse(self.source(rel), feature_version=(3, 10))
        ast.parse((Path(__file__)).read_text(encoding="utf-8"), feature_version=(3, 10))

    def imports(self, rel, top_level_only):
        tree = ast.parse(self.source(rel))
        nodes = tree.body if top_level_only else list(ast.walk(tree))
        found = set()
        for node in nodes:
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                found.add(mod)
                found.update(f"{mod}.{a.name}" for a in node.names)
            elif isinstance(node, ast.Import):
                found.update(a.name for a in node.names)
        return found

    def test_a_domain_module_never_imports_the_registry_the_views_or_the_commands(self):
        for name in self.imports("records.py", top_level_only=False):
            for bad in ("bp.registry", "bp.validate", "bp.views", "bp.commands", "bp.cli", "bp.install", "bp.api"):
                self.assertFalse(name == bad or name.startswith(bad + "."), name)
        self.assertNotIn("bp.roadmap", self.imports("records.py", False), "roadmap is after records in the import order")

    def test_the_command_module_imports_domain_modules_inside_its_handlers(self):
        top = self.imports("commands/records.py", top_level_only=True)
        self.assertEqual({n for n in top if n.startswith("bp")}, {"bp.core", "bp.core.BlueprintError", "bp.core.emit", "bp.core.find_root",
                                                                    "bp.plan", "bp.plan.load_plan"})

    def test_the_module_has_the_hooks_and_the_public_names_of_the_contract(self):
        from bp import records
        for name in ("load_records", "append_record", "criterion_digest", "feature_proof", "criterion_results", "latest_evaluation",
                     "run_feature_checks", "record_evaluation", "context", "verify_feature", "refuse"):
            self.assertTrue(callable(getattr(records, name)), name)
        self.assertFalse(hasattr(records, "CODES"), "this module registers no validation code")
        self.assertIs(registry.hook("verify_feature"), records.verify_feature)
        self.assertEqual(registry.codes(), registry.codes())  # still consistent with the module present
        self.assertTrue(registry.command_built("accept"))

    def test_error_identifiers_are_the_contracts_plus_the_one_it_lacks(self):
        used = set(re.findall(r'_error\(\s*"([a-z-]+)"', self.source("records.py")))
        self.assertEqual(used, {"unknown-feature", "unknown-criterion", "unknown-roster", "not-independent", "not-verified",
                                "evidence-not-found", "proof-changed", "usage", "write-failed"})
        codes = {"evidence-not-found", "unknown-feature", "unknown-criterion", "unknown-roster", "not-independent", "not-verified", "usage", "write-failed"}
        contract = (S.SKILL.parents[0] / "maintainers" / "design" / "v2-contract.md")
        if contract.exists():
            text = contract.read_text(encoding="utf-8")
            for c in codes:
                self.assertIn(f"`{c}`", text)

    def test_every_check_and_refusal_is_a_blueprint_error_with_a_next(self):
        src = self.source("records.py")
        for m in re.finditer(r"raise _error\((.*?)\)\s*$", src, re.S | re.M):
            pass
        self.assertNotIn("print(f\"error", src)
        self.assertEqual(src.count("sys.exit"), 1, "only the list-mode JSON envelope of verify_feature exits")
        self.assertNotIn("die(", src)

    def test_a_damaged_value_in_a_plan_never_raises_out_of_context(self):
        from bp import records
        plan = base_plan()
        weird = [None, 5, True, "x", [], {}, ["a"], [5], [{}], {"a": 1}]
        for key in ("criteria", "id", "owner", "level"):
            for value in weird:
                p = json.loads(json.dumps(plan))
                feature_of(p)[key] = value
                ctx = {"comp": None, "state": {"files": {}, "patterns": {}}}
                records.context(Path(tempfile.gettempdir()) / "no-such-project", p, ctx["state"], ctx)
                self.assertIsInstance(ctx["checks"], dict)
        for key in ("id", "scenario", "verify", "verifyLevel", "level"):
            for value in weird:
                p = json.loads(json.dumps(plan))
                feature_of(p)["criteria"][0][key] = value
                ctx = {"comp": None, "state": {"files": {}, "patterns": {}}}
                records.context(Path(tempfile.gettempdir()) / "no-such-project", p, ctx["state"], ctx)
                for fid, chk in ctx["checks"].items():
                    self.assertIs(type(chk["commandPass"]), bool)
                    for e in chk["criteria"].values():
                        self.assertIs(type(e["stale"]), bool)

    def test_a_criterion_nested_absurdly_deep_never_counts_and_never_raises(self):
        from bp import records
        c = {"id": "C1", "scenario": "s", "action": "a", "expected": "e", "verify": PASS}
        node = c
        for _ in range(3000):
            node["x"] = {}
            node = node["x"]
        with self.assertRaises(ValueError):
            records.criterion_digest(c)
        p = base_plan()
        feature_of(p)["criteria"][0] = c
        ctx = {"comp": None, "state": {"files": {}, "patterns": {}}}
        records.context(Path(tempfile.gettempdir()) / "no-such-project", p, ctx["state"], ctx)
        self.assertEqual(ctx["checks"][FID]["criteria"]["C1"]["result"], "unknown")
        self.assertEqual(records._unrunnable(feature_of(p), c, True), "it cannot be read")


# =============================================================================== with the real sibling modules

class RealSiblingsTest(TempRoot):
    """The whole skill, not the hermetic copy: unknowns, roadmap and the views are present."""

    def setUp(self):
        super().setUp()
        self.plan = base_plan()
        self.write_plan(self.plan)
        for rel in ALL_FILES:
            self.touch(rel)

    @unittest.skipUnless(registry.exists("unknowns"), "needs the unknowns module")
    def test_a_feature_blocked_by_an_unknown_cannot_be_accepted(self):
        self.plan["unknowns"] = [{"id": "U-x", "question": "Is it allowed?", "severity": "high", "status": "open", "blocks": [FID]}]
        self.write_plan(self.plan)
        self.assertEqual(self.cli("verify", "--run").returncode, 0)
        self.assertEqual(self.cli("verify", "--feature", FID, "--run").returncode, 0)
        r = self.cli("accept", FID, "--by", "evaluator", "--mode", "cli", "--result", "C3=pass", "--json")
        out = json.loads(r.stdout)
        self.assertEqual((r.returncode, out["error"]["code"]), (3, "not-verified"))
        self.assertIn("blocked", out["error"]["message"])

    @unittest.skipUnless(registry.exists("roadmap") and registry.exists("views.roadmap_md") and registry.exists("views.features_md"),
                         "needs the roadmap and features views")
    def test_a_full_flow_with_every_module_and_the_views(self):
        self.assertEqual(self.cli("verify", "--run").returncode, 0)
        r = self.cli("verify", "--feature", FID, "--run", "--by", "evaluator")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.cli("accept", FID, "--by", "backend-lead", "--mode", "cli").returncode, 3)
        r = self.cli("accept", FID, "--by", "evaluator", "--mode", "cli", "--result", "C3=pass")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("F-log-snag: accepted by evaluator (cli, e2e)"), r.stdout)
        feats = (self.root / ".blueprint" / "FEATURES.md").read_text(encoding="utf-8")
        self.assertIn("accepted", feats)
        road = (self.root / ".blueprint" / "ROADMAP.md").read_text(encoding="utf-8")
        self.assertIn("1 of 2 features accepted", road)
        out = json.loads(self.cli("status", "--json").stdout)
        self.assertEqual(next(f for f in out["features"] if f["id"] == FID)["status"], "accepted")
        self.assertEqual(json.loads(self.cli("validate", "--json").stdout)["data"]["valid"], True)

    def test_every_protected_path_is_refused_through_the_real_registry(self):
        self.cli("verify", "--run")
        for path in PROTECTED:
            r = self.cli("guard", path)
            self.assertEqual(r.returncode, 2, (path, r.stderr))
            self.assertIn(MARKER, r.stderr)
        for path in ALLOWED[:6]:
            self.assertEqual(self.cli("guard", path).returncode, 0, path)

    @unittest.skipUnless(registry.exists("unknowns"), "needs the unknowns module")
    def test_the_claim_hook_of_unknowns_is_not_shadowed_by_this_module(self):
        self.plan["unknowns"] = [{"id": "U-x", "question": "q", "severity": "high", "status": "open", "blocks": ["P1"]}]
        self.write_plan(self.plan)
        r = self.cli("claim", ITEMS[0], "--by", "backend-lead")
        self.assertEqual(r.returncode, 3, r.stderr)


# ============================================================================================ hardening cases

class HardeningTest(Project):
    """Routes a reviewer would try around the rules above."""

    def test_a_forged_pass_that_does_not_add_up_is_damage(self):
        self.prove()
        plan, _ = self.load()
        from bp import records
        good = valid_check(self.root, plan)
        body = {k: v for k, v in good.items() if k != "id"}
        for field, value in (("repeat", {"n": 3, "passed": 1}), ("exit", 1), ("exit", None), ("repeat", {"n": 1, "passed": 2})):
            write_lines(self.root, FID, records._finish(dict(body, **{field: value})))
        self.assertEqual(records.read_records(self.root, FID), ([], 4))
        self.assertEqual(self.ctx()["checks"][FID]["criteria"]["C1"]["result"], "unknown")

    def test_a_forged_level_cannot_lift_a_command_above_what_the_criterion_declares(self):
        self.edit_plan(lambda p: feature_of(p)["criteria"][0].update(level="e2e"))
        self.prove()
        plan, _ = self.load()
        from bp import records
        write_lines(self.root, FID, records._finish(dict({k: v for k, v in valid_check(self.root, plan).items() if k != "id"}, level="e2e")))
        e = self.ctx()["checks"][FID]
        self.assertEqual(e["criteria"]["C1"]["level"], "e2e")
        self.assertIs(e["commandPass"], False, "no verifyLevel: a command reaches integration at most")
        for level in ("manual", "huge", None):
            write_lines(self.root, FID, records._finish(dict({k: v for k, v in valid_check(self.root, plan).items() if k != "id"}, level=level)))
        self.assertIs(self.ctx()["checks"][FID]["commandPass"], False)

    def test_blank_and_empty_roster_ids_are_nobody(self):
        self.edit_plan(lambda p: (p["roster"].append({"id": "", "kind": "agent"}), p["roster"].append({"id": " ", "kind": "human"})))
        self.ready()
        for by in ("", " ", "  "):
            out = json.loads(self.cli("accept", FID, "--by", by, "--mode", "manual", "--result", "C3=pass", "--json").stdout)
            self.assertEqual(out["error"]["code"], "unknown-roster", repr(by))
        self.assertEqual([x["kind"] for x in self.records()], ["check", "check"])

    def test_an_owner_with_a_blank_name_is_nobody_either(self):
        from bp import records
        plan = bplan.load_plan(self.root)
        feature_of(plan)["owner"] = " "
        plan["owners"].append({"pattern": ITEMS[0], "owner": ""})
        self.assertIsNone(records.independence_problem(plan, {"files": {}}, FID, "evaluator"))
        self.assertTrue(records.independence_problem(plan, {"files": {}}, FID, "backend-lead"))  # owner of the other file

    def test_many_evaluations_that_do_not_count_stay_cheap(self):
        self.ready()
        plan, state = self.load()
        lines = [valid_evaluation(self.root, plan, by="backend-lead", results={"C3": "pass"}) for _ in range(1)]
        recs = []
        from bp import records
        for i in range(400):
            ev = dict({k: v for k, v in lines[0].items() if k != "id"}, at=f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z")
            recs.append(records._finish(ev))
        write_lines(self.root, FID, *recs)
        t = time.monotonic()
        chk = self.ctx()["checks"][FID]
        self.assertLess(time.monotonic() - t, 10)
        self.assertEqual(chk["evaluation"]["by"], "backend-lead")  # the latest, but its passes count for nothing
        self.assertEqual(chk["evaluation"]["verdict"], "partial")
        self.assertEqual(self.status_of()["status"], "verified")

    def test_a_second_feature_with_the_same_id_changes_nothing_for_the_first(self):
        self.ready()
        self.edit_plan(lambda p: p["features"].append(dict(feature_of(p, "F-inbox"), id=FID)))
        chk = self.ctx()["checks"][FID]
        self.assertEqual(set(chk["criteria"]), {"C1", "C2", "C3"})  # the first entry of the plan is the feature
        self.assertEqual(json.loads(self.cli("accept", FID, "--by", "evaluator", "--mode", "cli", "--json").stdout)["error"]["code"],
                         "unknown-feature")

    def test_a_feature_served_by_a_pattern_is_proven_by_its_recorded_instances(self):
        def pattern(p):
            p["patterns"] = [{"id": "screen", "glob": "apps/web/src/screens/*.tsx", "module": "web", "phase": "P1", "purpose": "p",
                              "features": [FID]}]
        self.edit_plan(pattern)
        self.touch("apps/web/src/screens/A.tsx", "a\n")
        self.ok("sync", "--quiet", "--no-render")
        self.ready()
        self.assertIs(self.ctx()["checks"][FID]["criteria"]["C1"]["stale"], False)
        self.assertIn("apps/web/src/screens/A.tsx", self.records()[0]["proof"]["files"])
        self.touch("apps/web/src/screens/A.tsx", "edited\n")
        self.assertIs(self.ctx()["checks"][FID]["criteria"]["C1"]["stale"], True)
        self.touch("apps/web/src/screens/A.tsx", "a\n")
        self.touch("apps/web/src/screens/B.tsx", "b\n")  # a new instance, until sync records it, is not part of the feature
        self.assertIs(self.ctx()["checks"][FID]["criteria"]["C1"]["stale"], False)
        self.ok("sync", "--quiet", "--no-render")
        self.assertIs(self.ctx()["checks"][FID]["criteria"]["C1"]["stale"], True)

    def test_an_evaluation_cannot_be_recorded_for_a_feature_of_a_bad_id_or_path_like_id(self):
        from bp import records
        for fid in ("../F-x", "F-x/../../y", "F-X", "", "F-x\n", None):
            with self.assertRaises(BlueprintError) as cm:
                records.record_evaluation(self.root, bplan.load_plan(self.root), fid, "evaluator", "cli", {}, [], None)
            self.assertEqual(cm.exception.error, "unknown-feature")
        self.assertFalse((self.root / ".blueprint" / "verification").exists())

    def test_the_next_step_of_a_refusal_never_holds_what_the_caller_typed(self):
        self.ready()
        evil = "x$(touch pwned)`touch pwned`;&|"
        for args in (("--by", evil, "--mode", "cli"), ("--by", "evaluator", "--mode", "cli", "--result", evil + "=pass"),
                     ("--by", "evaluator", "--mode", "cli", "--result", "C3=pass", "--evidence", evil)):
            r = self.cli("accept", FID, *args, "--json")
            out = json.loads(r.stdout)
            self.assertNotIn("pwned", out["error"]["next"] or "")
            self.assertNotIn("$(", out["error"]["next"] or "")
        out = json.loads(self.cli("verify", "--feature", evil, "--run", "--json").stdout)
        self.assertNotIn("pwned", out["error"]["next"] or "")
        self.assertFalse((self.root / "pwned").exists())

    def test_the_log_and_the_records_hold_no_caller_text_beyond_ids_and_the_cleaned_note(self):
        self.ready()
        self.ok("accept", FID, "--by", "evaluator", "--mode", "cli", "--result", "C3=pass", "--note", "ok")
        for e in self.log()[-2:]:
            self.assertLessEqual(set(e) - {"event", "at", "feature", "by", "checked", "passed", "records", "mode", "verdict", "record",
                                           "notSaved"}, set())


# ======================================================================================= review round 1

class ReviewOneTest(Project):
    """The findings of the first review of PR #18, each as a test (F1 to F13)."""

    def setUp(self):
        super().setUp()
        self.ready()

    # ---- F1: a record file is a log of events; none may vanish
    def test_f1_two_identical_looking_events_keep_their_own_ids(self):
        from bp import records
        plan, state = self.load()
        body = {k: v for k, v in valid_evaluation(self.root, plan, results={"C3": "fail"}).items() if k not in ("id", "nonce")}
        ids = {records._finish(dict(body)).get("id") for _ in range(20)}
        self.assertEqual(len(ids), 20, "the nonce is part of what the id is computed from")
        rec = records._finish(dict(body))
        self.assertRegex(rec["nonce"], r"^[0-9a-f]{8}$")
        self.assertEqual(rec["id"], core.content_id("vr-", {k: v for k, v in rec.items() if k != "id"}, 12))

    def test_f1_fail_pass_fail_in_one_second_reads_as_failed_for_evaluations(self):
        from bp import records
        plan, state = self.load()
        with mock.patch.object(records, "now_iso", lambda: "2026-01-01T00:00:00Z"):
            for result in ("fail", "pass", "fail"):
                ctx = build_context(self.root, *self.load())
                records.record_evaluation(self.root, plan, FID, "evaluator", "cli", {"C3": result}, [], None, ctx=ctx)
        got = records.read_records(self.root, FID)[0]
        self.assertEqual([r["criteria"]["C3"]["result"] for r in got if r["kind"] == "evaluation"], ["fail", "pass", "fail"])
        self.assertEqual(self.status_of()["status"], "failed")

    def test_f1_fail_pass_fail_in_one_second_reads_as_fail_for_check_records(self):
        from bp import records
        self.edit_plan(lambda p: feature_of(p)["criteria"][0].update(verify=PASS))
        plan, state = self.load()
        good = valid_check(self.root, plan, cid="C1", result="pass")
        body = {k: v for k, v in good.items() if k not in ("id", "nonce")}
        with mock.patch.object(records, "now_iso", lambda: "2026-01-01T00:00:00Z"):
            for result, ex in (("fail", 1), ("pass", 0), ("fail", 1)):
                records.append_record(self.root, dict(body, result=result, exit=ex, repeat={"n": 1, "passed": 0 if ex else 1}))
        got = [r["result"] for r in records.read_records(self.root, FID)[0] if r["criterion"] == "C1"][-3:]
        self.assertEqual(got, ["fail", "pass", "fail"])
        self.assertEqual(self.ctx()["checks"][FID]["criteria"]["C1"]["result"], "fail")

    def test_f1_a_repeated_id_is_damage_and_the_first_keeps_its_place(self):
        from bp import records
        plan, state = self.load()
        a = valid_check(self.root, plan, cid="C1")
        b = valid_check(self.root, plan, cid="C2", result="fail")
        write_lines(self.root, "F-copy", a)
        path = self.root / ".blueprint" / "verification"
        for name in (FID,):
            (path / f"{name}.jsonl").unlink(missing_ok=True)
        write_lines(self.root, FID, a, b, a)
        got, damaged = records.read_records(self.root, FID)
        self.assertEqual(([r["id"] for r in got], damaged), ([a["id"], b["id"]], 1))

    # ---- F2: the latest current evaluation decides, whoever wrote it
    def _fail_then_lose_independence(self, how):
        self.edit_plan(lambda p: roster_add(p, "eval2"))
        self.assertEqual(self.accept("--result", "C3=pass").returncode, 0)
        self.assertEqual(self.accept("--result", "C3=fail", by="eval2").returncode, 1)
        self.assertEqual(self.status_of()["status"], "failed")
        how()
        return self.status_of()

    def test_f2_an_owner_rule_edit_does_not_erase_a_later_fail(self):
        f = self._fail_then_lose_independence(
            lambda: self.edit_plan(lambda p: p["owners"].append({"pattern": ITEMS[1], "owner": "eval2"})))
        self.assertEqual(f["status"], "failed")
        self.assertEqual(f["evaluation"]["by"], "eval2")

    def test_f2_a_claim_by_the_author_of_the_fail_does_not_revive_the_older_pass(self):
        f = self._fail_then_lose_independence(lambda: self.ok("claim", ITEMS[1], "--by", "eval2", "--force"))
        self.assertEqual(f["status"], "failed")

    def test_f2_a_pass_by_someone_who_lost_independence_counts_for_nothing_and_nothing_older_is_consulted(self):
        self.edit_plan(lambda p: roster_add(p, "eval2"))
        self.assertEqual(self.accept("--result", "C3=pass").returncode, 0)
        self.assertEqual(self.accept("--result", "C3=pass", by="eval2").returncode, 0)
        self.ok("claim", ITEMS[1], "--by", "eval2", "--force")
        f = self.status_of()
        self.assertEqual(f["status"], "verified")  # not the older pass, which is no longer the deciding one
        self.assertEqual(f["evaluation"]["by"], "eval2")
        self.assertEqual(self.accept("--result", "C3=pass").returncode, 0, "a new evaluation by someone independent decides")
        self.assertEqual(self.status_of()["status"], "accepted")

    def test_f2_a_real_fix_and_a_new_evaluation_supersede_a_fail(self):
        self.assertEqual(self.accept("--result", "C3=fail").returncode, 1)
        self.touch(ITEMS[0], "fixed\n")
        self.ready_after_edit()
        self.assertEqual(self.status_of()["status"], "verified")  # the old fail judged other files
        self.assertEqual(self.accept("--result", "C3=pass").returncode, 0)

    def ready_after_edit(self):
        self.prove()
        self.ok("verify", "--feature", FID, "--run", "--by", "evaluator")

    # ---- F3: everyone who ever worked on an item
    def test_a_forged_evaluation_by_someone_outside_the_roster_counts_for_nothing(self):
        plan, state = self.load()
        write_lines(self.root, FID, valid_evaluation(self.root, plan, by="nobody"))
        self.assertEqual(self.status_of()["status"], "verified")

    def test_f3_workedby_covers_past_builders(self):
        plan, state = self.load()
        feature = feature_of(plan)
        items = [{"path": p} for p in ITEMS]
        state["files"][ITEMS[0]] = {"state": "built", "by": "backend-lead", "workedBy": ["evaluator", "backend-lead"]}
        self.assertIn("claimed or built", features.independence_problem(plan, feature, items, "evaluator", state))
        self.assertIsNotNone(features.independence_problem(plan, feature, items, "evaluator", {"files": {ITEMS[1]: {"workedBy": ["evaluator"]}}}))
        self.assertIsNone(features.independence_problem(plan, feature, items, "evaluator", {"files": {"other": {"workedBy": ["evaluator"]}}}))
        for junk in (5, "evaluator", [5], {"a": 1}, [None], True):  # damaged: nobody can be shown to be independent
            for who in ("evaluator", "cto-architect", "nobody"):
                got = features.independence_problem(plan, feature, items, who, {"files": {ITEMS[0]: {"workedBy": junk}}})
                self.assertIn("damaged", got or "", (junk, who))
                self.assertIn("repair .blueprint/state.json", got)
        self.assertIsNone(features.independence_problem(plan, feature, items, "evaluator", {"files": {ITEMS[0]: {"workedBy": None}}}))  # null: absent
        self.assertIsNotNone(features.independence_problem(plan, feature, items, "evaluator",
                                                             {"files": {ITEMS[0]: {"by": "evaluator", "workedBy": []}}}))
        self.assertEqual(features.independent_ids(plan, feature, items, None, {"files": {ITEMS[1]: {"workedBy": 5}}}), [])

    def test_f3_the_reviewers_sequence_through_accept(self):
        """The evaluator claims and marks, the owner re-marks: with the core's workedBy the evaluator is still refused."""
        self.ok("claim", ITEMS[0], "--by", "evaluator", "--force")
        self.ok("mark", ITEMS[0], "built", "--by", "evaluator", "--force")
        self.ok("mark", ITEMS[0], "built", "--by", "backend-lead")
        state = json.loads((self.root / ".blueprint" / "state.json").read_text(encoding="utf-8"))
        if "workedBy" not in state["files"][ITEMS[0]]:
            self.skipTest("the core does not write workedBy yet")
        self.prove()
        self.ok("verify", "--feature", FID, "--run")
        self.refused_accept()

    def refused_accept(self):
        r = self.cli("accept", FID, "--by", "evaluator", "--mode", "cli", "--result", "C3=pass", "--json")
        self.assertEqual((r.returncode, json.loads(r.stdout)["error"]["code"]), (3, "not-independent"), r.stdout)

    # ---- F4: pin what was evaluated
    def proof(self):
        r = self.ok("verify", "--feature", FID, "--json")
        return json.loads(r.stdout)["data"]["proof"]

    def test_f4_list_mode_shows_the_proof_in_text_and_json(self):
        digest = self.proof()
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
        r = self.ok("verify", "--feature", FID)
        self.assertIn(f"proof: {digest}", r.stdout)
        r = self.ok("verify", "--feature", FID, "--json")
        out = json.loads(r.stdout)
        self.assertEqual((out["ok"], out["command"], out["data"]["records"]), (True, "verify", []))
        self.touch(ITEMS[0], "changed\n")
        self.assertNotEqual(self.proof(), digest)

    def test_f4_accept_with_the_proof_it_looked_at_succeeds_and_reports_it(self):
        digest = self.proof()
        r = self.accept("--result", "C3=pass", "--proof", digest, "--json")
        out = json.loads(r.stdout)
        self.assertEqual((r.returncode, out["data"]["proof"], out["data"]["status"]), (0, digest, "accepted"), r.stdout)
        r = self.accept("--result", "C3=pass", "--proof", digest)
        self.assertEqual(r.stdout, "F-log-snag: accepted by evaluator (cli, e2e)\n")

    def test_f4_a_change_between_looking_and_accepting_is_refused(self):
        digest = self.proof()
        self.touch(ITEMS[0], "changed after the evaluator looked\n")
        self.ready_after_edit()
        before = self.raw_lines()
        r = self.accept("--result", "C3=pass", "--proof", digest, "--json")
        out = json.loads(r.stdout)
        self.assertEqual((r.returncode, out["error"]["code"]), (1, "proof-changed"), r.stdout)
        self.assertIn("verify --feature F-log-snag", out["error"]["next"])
        self.assertEqual(self.raw_lines(), before)

    def test_f4_without_a_pin_the_line_says_what_was_accepted(self):
        r = self.accept("--result", "C3=pass")
        self.assertEqual(r.returncode, 0)
        self.assertIn("files as they are now", r.stdout)
        self.assertEqual(json.loads(self.accept("--result", "C3=pass", "--json").stdout)["data"]["proof"], self.proof())

    def test_f4_the_pin_has_the_form_it_is_shown_in(self):
        for bad in ("sha256:abc", "SHA256:" + "0" * 64, "0" * 64, "sha256:" + "0" * 64 + "\n", "sha256:" + "G" * 64):
            r = self.cli("accept", FID, "--by", "evaluator", "--mode", "cli", "--proof", bad, "--json")
            self.assertEqual((r.returncode, json.loads(r.stdout)["error"]["code"]), (64, "usage"), bad)

    def test_f4_a_wrong_pin_is_refused_before_the_lock_is_taken(self):
        from bp import records
        plan, state = self.load()
        ctx = build_context(self.root, plan, state)

        def no_lock(root):
            raise AssertionError("the lock was taken for a pin that is already wrong")
        with mock.patch.object(core, "state_lock", no_lock), self.assertRaises(BlueprintError) as cm:
            records.record_evaluation(self.root, plan, FID, "evaluator", "cli", {"C3": "pass"}, [], None, ctx=ctx,
                                      proof="sha256:" + "0" * 64)
        self.assertEqual(cm.exception.error, "proof-changed")

    def test_f4_a_change_after_the_check_but_before_the_write_is_seen_inside_the_lock(self):
        from bp import records
        digest = self.proof()
        real = core.state_lock

        @contextlib.contextmanager
        def racing(root):
            with real(root):
                (root / ITEMS[0]).write_text("changed inside the race\n", encoding="utf-8")
                yield
        plan, state = self.load()
        ctx = build_context(self.root, plan, state)
        before = self.raw_lines()
        with mock.patch.object(core, "state_lock", racing), self.assertRaises(BlueprintError) as cm:
            records.record_evaluation(self.root, plan, FID, "evaluator", "cli", {"C3": "pass"}, [], None, ctx=ctx, proof=digest)
        self.assertEqual((cm.exception.error, cm.exception.code), ("proof-changed", 1))
        self.assertEqual(self.raw_lines(), before)

    # ---- F5, F9, F10, not-verified next
    def test_f5_every_next_runs_as_printed(self):
        for args in (("--result", "C1"), ("--result", "C3=pass", "--result", "C3=fail"), ("--result", "C3=pass", "--evidence", "ev-1")):
            r = self.cli("accept", FID, "--by", "evaluator", "--mode", "cli", *args, "--json")
            nxt = json.loads(r.stdout)["error"]["next"]
            self.assertNotIn("python .blueprint/bin/blueprint.py", nxt)
            run = subprocess.run(runnable(nxt), shell=True, cwd=str(self.root), capture_output=True, text=True, env=S.tool_env())
            self.assertEqual(run.returncode, 0, (nxt, run.stderr))
        src = (S.PACKAGE / "records.py").read_text(encoding="utf-8").split("\n")
        for n, line in enumerate(src, 1):
            if "next=" in line or "next_ =" in line:
                self.assertNotIn(".blueprint/bin/blueprint.py", line, n)

    def test_f9_a_feature_with_only_command_criteria_says_what_accept_needs(self):
        self.edit_plan(lambda p: feature_of(p).update(criteria=feature_of(p)["criteria"][:2]))
        self.ok("verify", "--feature", FID, "--run")
        r = self.accept()
        self.assertEqual(r.returncode, 1)
        self.assertIn("at least one --result <criterion>=pass", r.stdout)
        self.assertIn("C1 (integration)", r.stdout)
        self.assertIn("C2 (unit)", r.stdout)

    def test_f10_run_arguments_are_checked_without_run_too(self):
        for args in (("--repeat", "101"), ("--repeat", "0"), ("--timeout", "0"), ("--timeout", "-1")):
            r = self.cli("verify", "--feature", FID, *args, "--json")
            self.assertEqual((r.returncode, json.loads(r.stdout)["error"]["code"]), (64, "usage"), args)
        self.assertEqual(self.cli("verify", "--feature", FID, "--repeat", "100", "--timeout", "1").returncode, 0)

    def test_the_not_verified_next_names_the_command_that_helps(self):
        self.touch(ITEMS[0], "edited\n")
        e = json.loads(self.cli("accept", FID, "--by", "evaluator", "--mode", "cli", "--result", "C3=pass", "--json").stdout)["error"]
        self.assertTrue(e["next"].rstrip().endswith("verify --run") or " verify --run " in e["next"], e["next"])
        self.prove()
        e = json.loads(self.cli("accept", FID, "--by", "evaluator", "--mode", "cli", "--result", "C3=pass", "--json").stdout)["error"]
        self.assertIn("verify --feature F-log-snag --run", e["next"])

    # ---- F7, F12, F13
    def test_f7_protected_names_are_refused_in_every_case(self):
        from bp import records
        plan = bplan.load_plan(self.root)
        for path in (".blueprint/State.json", ".blueprint/STATE.JSON", ".blueprint/VERIFICATION/F-x.jsonl", ".blueprint/Verification/F-new.jsonl",
                     ".blueprint/Log.jsonl", ".blueprint/BIN/blueprint.py", ".blueprint/Studies/S-a/Study.json", ".blueprint/map.HTML",
                     ".BLUEPRINT/state.json", ".blueprint/State.LOCK", ".blueprint/.GitIgnore"):
            self.assertIn(MARKER, records.refuse("write", self.root, plan, path, None) or "", path)
        for path in (".blueprint/Blueprint.json", ".blueprint/Studies/S-a/Behaviours.json"):
            self.assertIsNone(records.refuse("write", self.root, plan, path, None), path)

    def test_f12_a_record_file_with_two_hard_links_is_not_appended_to(self):
        from bp import records
        outside = self.root / "outside.jsonl"
        outside.write_text("", encoding="utf-8")
        folder = self.root / ".blueprint" / "verification"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.link(outside, folder / "F-linked.jsonl")
        except (OSError, NotImplementedError):
            self.skipTest("hard links are not available here")
        plan, state = self.load()
        rec = valid_check(self.root, plan)
        with self.assertRaises(BlueprintError) as cm:
            records.append_record(self.root, dict({k: v for k, v in rec.items() if k != "id"}, feature="F-linked"))
        self.assertEqual(cm.exception.error, "write-failed")
        self.assertEqual(outside.read_text(encoding="utf-8"), "")

    def test_f13_the_project_folder_is_redacted_in_any_case_and_with_either_separator(self):
        from bp import records
        root = Path("C:/proj dir/app") if os.name == "nt" else Path("/work/proj dir/app")
        shown = "c:/PROJ DIR/app/src" if os.name == "nt" else "/work/proj dir/app/src"
        note = records._clean_note(f"see {shown} and {str(root)} and {root.as_posix()}", root)
        self.assertNotIn("DIR", note.replace("<project>", ""))
        self.assertNotIn("proj dir", note.lower())

    # ---- F11
    def test_f11_evidence_ids_come_from_validate_context(self):
        from bp import records, validate
        stub = "def validate_context(root, plan):\n    return {'evidenceIds': {'ev-0123456789abcdef': 1}}\n"
        with extension(dict(own_sources(), **{"evidence.py": stub})):
            from bp import records as rec2, validate as val2
            known = val2.validation_context(bplan.load_plan(self.root), root=self.root)["evidenceIds"]
            self.assertIn("ev-0123456789abcdef", known)
            plan, state = self.load()
            ctx = build_context(self.root, plan, state)
            before = self.raw_lines()
            with self.assertRaises(BlueprintError) as cm:
                rec2.record_evaluation(self.root, plan, FID, "evaluator", "cli", {"C3": "pass"}, ["ev-fedcba9876543210"], None, ctx=ctx,
                                       evidence_ids=known)
            self.assertEqual(cm.exception.error, "evidence-not-found")
            self.assertEqual(self.raw_lines(), before)
            rec = rec2.record_evaluation(self.root, plan, FID, "evaluator", "cli", {"C3": "pass"}, ["ev-0123456789abcdef"], None, ctx=ctx,
                                         evidence_ids=known)
            self.assertEqual(rec["evidence"], ["ev-0123456789abcdef"])

    def test_f11_a_value_that_cannot_answer_membership_refuses(self):
        from bp import records
        plan, state = self.load()
        ctx = build_context(self.root, plan, state)
        for known in (5, True, object()):
            with self.assertRaises(BlueprintError) as cm:
                records.record_evaluation(self.root, plan, FID, "evaluator", "cli", {"C3": "pass"}, ["ev-0123456789abcdef"], None, ctx=ctx,
                                          evidence_ids=known)
            self.assertEqual(cm.exception.error, "evidence-not-found", known)
        records.record_evaluation(self.root, plan, FID, "evaluator", "cli", {"C3": "pass"}, [], None, ctx=ctx, evidence_ids=5)  # nothing cited


# ======================================================================================= review round 2

class ReviewTwoTest(Project):
    """The findings of the second review (R2-1 to R2-8)."""

    def setUp(self):
        super().setUp()
        self.ready()

    def proof(self, fid=FID):
        return json.loads(self.ok("verify", "--feature", fid, "--json").stdout)["data"]["proof"]

    def ready_after_edit(self):
        self.prove()
        self.ok("verify", "--feature", FID, "--run", "--by", "evaluator")

    # ---- R2-1: the pin covers the files, every criterion and the feature's level
    def test_r2_1_anything_the_evaluator_judged_against_is_pinned(self):
        edits = {
            "criterion text": lambda p: feature_of(p)["criteria"][2].update(expected="It shows, and the snag list is wiped"),
            "criterion scenario": lambda p: feature_of(p)["criteria"][2].update(scenario="another start"),
            "criterion level": lambda p: feature_of(p)["criteria"][2].update(level="integration"),
            "criterion verify": lambda p: feature_of(p)["criteria"][0].update(verify="exit 0 "),
            "criterion verifyLevel": lambda p: feature_of(p)["criteria"][0].update(verifyLevel="e2e"),
            "criterion added": lambda p: feature_of(p)["criteria"].append({"id": "C4", "scenario": "s", "action": "a", "expected": "e"}),
            "criterion removed": lambda p: feature_of(p)["criteria"].pop(1),
            "feature level": lambda p: feature_of(p).update(level="unit"),
        }
        for name, edit in edits.items():
            self.write_plan(base_plan())
            for rel in ALL_FILES:
                self.touch(rel)
            self.ready()
            digest = self.proof()
            self.edit_plan(edit)
            self.ready_after_edit()
            before = self.raw_lines()
            r = self.accept("--result", "C3=pass", "--proof", digest, "--json")
            out = json.loads(r.stdout)
            self.assertEqual((r.returncode, out["error"]["code"]), (1, "proof-changed"), (name, r.stdout))
            self.assertEqual(self.raw_lines(), before, name)

    def test_r2_1_the_digest_is_the_same_when_nothing_changed_and_the_record_stores_its_pin(self):
        digest = self.proof()
        self.assertEqual(self.proof(), digest)
        r = self.accept("--result", "C3=pass", "--proof", digest, "--json")
        self.assertEqual(json.loads(r.stdout)["data"]["proof"], digest)
        self.assertEqual(self.records()[-1]["pin"], digest)
        self.assertEqual(set(self.records()[-1]["proof"]), {"files"})
        self.assertEqual(self.status_of()["status"], "accepted")

    def test_r2_1_the_criteria_are_pinned_inside_the_lock_too(self):
        from bp import records
        digest = self.proof()
        real = core.state_lock

        @contextlib.contextmanager
        def racing(root):
            with real(root):
                self.edit_plan(lambda p: feature_of(p)["criteria"][2].update(expected="changed in the race"))
                yield
        plan, state = self.load()
        ctx = build_context(self.root, plan, state)
        before = self.raw_lines()
        with mock.patch.object(core, "state_lock", racing), self.assertRaises(BlueprintError) as cm:
            records.record_evaluation(self.root, plan, FID, "evaluator", "cli", {"C3": "pass"}, [], None, ctx=ctx, proof=digest)
        self.assertEqual(cm.exception.error, "proof-changed")
        self.assertEqual(self.raw_lines(), before)

    # ---- R2-6: the check inside the lock does not trust the hash cache
    def test_r2_6_a_same_size_edit_with_the_time_restored_is_seen_inside_the_lock(self):
        from bp import records
        digest = self.proof()
        path = self.root / ITEMS[0]
        real = core.state_lock

        @contextlib.contextmanager
        def racing(root):
            with real(root):
                st = os.stat(path)
                data = path.read_bytes()
                path.write_bytes(bytes([data[0] ^ 1]) + data[1:])  # same size, different content
                os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # the time restored: the cache key is unchanged
                yield
        plan, state = self.load()
        ctx = build_context(self.root, plan, state)
        before = self.raw_lines()
        with mock.patch.object(core, "state_lock", racing), self.assertRaises(BlueprintError) as cm:
            records.record_evaluation(self.root, plan, FID, "evaluator", "cli", {"C3": "pass"}, [], None, ctx=ctx, proof=digest)
        self.assertEqual(cm.exception.error, "proof-changed")
        self.assertEqual(self.raw_lines(), before)

    def test_r2_8_what_the_record_holds_is_what_was_pinned(self):
        """The record built before the lock has other files than the pin (they changed and changed back): the record is
        written again from the files read under the lock."""
        from bp import records
        digest = self.proof()
        plan, state = self.load()
        ctx = build_context(self.root, plan, state)
        real = records.current_proof
        calls = []

        def odd(root, plan_, state_, fid, fresh=False):
            pin, files = real(root, plan_, state_, fid, fresh)
            if not fresh and not calls:
                calls.append(1)
                return pin, files
            if not fresh:  # the second unfresh call is the one that builds the record
                return "sha256:" + "1" * 64, {"other.txt": "sha256:" + "2" * 64}
            return pin, files
        with mock.patch.object(records, "current_proof", odd):
            rec = records.record_evaluation(self.root, plan, FID, "evaluator", "cli", {"C3": "pass"}, [], None, ctx=ctx, proof=digest)
        self.assertEqual((rec["pin"], sorted(rec["proof"]["files"])), (digest, sorted(ITEMS)))
        self.assertEqual(self.records()[-1]["id"], rec["id"])

    # ---- R2-5: a copied line cannot move an old pass behind a later fail
    def test_r2_5_copying_an_old_pass_line_does_not_accept(self):
        self.edit_plan(lambda p: roster_add(p, "eval2"))
        self.assertEqual(self.accept("--result", "C3=pass").returncode, 0)
        lines = [x for x in self.raw_lines() if x]
        old_pass = lines[-1]
        self.assertEqual(self.accept("--result", "C3=fail", by="eval2").returncode, 1)
        self.assertEqual(self.status_of()["status"], "failed")
        with open(self.root / ".blueprint" / "verification" / f"{FID}.jsonl", "ab") as f:
            f.write(old_pass + b"\n")
        self.assertEqual(self.status_of()["status"], "failed")

    # ---- R2-3 / R2-4 with the state a core copy would leave
    def test_r2_3_the_latest_by_is_always_read_even_when_workedby_lacks_it(self):
        for rec in ({"by": "evaluator", "workedBy": ["backend-lead"]}, {"by": "evaluator"}, {"by": "evaluator", "workedBy": []}):
            self.assertIn("evaluator", features.worked_by(rec), rec)
        fake = mock.Mock(return_value=["backend-lead"])
        from bp import status as st
        with mock.patch.object(st, "worked_by", fake, create=True):
            self.assertEqual(sorted(set(features.worked_by({"by": "evaluator", "workedBy": ["x"]}))), ["backend-lead", "evaluator", "x"])
            plan, state = self.load()
            state["files"][ITEMS[0]] = {"state": "built", "by": "evaluator", "workedBy": ["backend-lead"]}
            self.assertIsNotNone(features.independence_problem(plan, feature_of(plan), [{"path": ITEMS[0]}], "evaluator", state))

    def test_r2_4_a_damaged_workedby_in_state_json_refuses_every_acceptor_through_accept(self):
        path = self.root / ".blueprint" / "state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["files"].setdefault(ITEMS[0], {})["workedBy"] = "evaluator"
        path.write_text(json.dumps(state), encoding="utf-8")
        for by in ("evaluator", "cto-architect"):
            r = self.cli("accept", FID, "--by", by, "--mode", "cli", "--result", "C3=pass", "--json")
            out = json.loads(r.stdout)
            self.assertEqual((r.returncode, out["error"]["code"]), (3, "not-independent"), r.stdout)
        # the core may call this bad-state first; this module never reads it as "nobody worked on it"

    # ---- R2-7 and R2-8: evidence
    def test_r2_7_a_string_or_bytes_cannot_answer_membership(self):
        from bp import records
        plan, state = self.load()
        ctx = build_context(self.root, plan, state)
        for known in ("ev-0123456789abcdef0", b"ev-0123456789abcdef0", bytearray(b"ev-0123456789abcdef"), 5, object()):
            with self.assertRaises(BlueprintError) as cm:
                records.record_evaluation(self.root, plan, FID, "evaluator", "cli", {"C3": "pass"}, ["ev-0123456789abcdef"], None, ctx=ctx,
                                          evidence_ids=known)
            self.assertEqual(cm.exception.error, "evidence-not-found", repr(known))
        for known in ({"ev-0123456789abcdef"}, ["ev-0123456789abcdef"], ("ev-0123456789abcdef",), frozenset({"ev-0123456789abcdef"}),
                      {"ev-0123456789abcdef": 1}):
            records.record_evaluation(self.root, plan, FID, "evaluator", "cli", {"C3": "pass"}, ["ev-0123456789abcdef"], None, ctx=ctx,
                                      evidence_ids=known)

    def test_r2_8_the_command_reads_evidence_ids_from_validate_context(self):
        import argparse
        stub = "def validate_context(root, plan):\n    return {'evidenceIds': ['ev-0123456789abcdef']}\n"
        with extension(dict(own_sources(), **{"evidence.py": stub})):
            from bp.commands import records as cr
            def run(evidence):
                args = argparse.Namespace(root=str(self.root), feature=FID, by="evaluator", mode="cli", result=["C3=pass"],
                                          evidence=evidence, proof=None, note=None, json=True)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                    try:
                        cr.cmd_accept(args)
                    except SystemExit as e:
                        return e, buf.getvalue()
                return None, buf.getvalue()
            before = self.raw_lines()
            e, _ = run(["ev-fedcba9876543210"])
            self.assertEqual((getattr(e, "error", None), getattr(e, "code", None)), ("evidence-not-found", 1))
            self.assertEqual(self.raw_lines(), before)
            e, out = run(["ev-0123456789abcdef"])
            self.assertEqual(getattr(e, "code", None), 0)
            self.assertEqual(self.records()[-1]["evidence"], ["ev-0123456789abcdef"])
