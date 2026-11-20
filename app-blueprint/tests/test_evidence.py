"""W2-E: the evidence store (contract 5): records and content-derived ids, digests, freshness in all four states,
storage under the state lock (parallel writers lose nothing), damaged input, the export bundle, import, the hooks
that give validate the evidence ids and the map its Studies tab, and the `evidence` command.

CLI tests run the launcher of a copy of the skill holding the core and this workstream's files (and the stand-ins a
test names), so they hold whatever sibling modules exist; in-process tests call this workstream's functions directly.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support as S

import fixtures_archives as FA
import fixtures_text as FT
from bp import core, evidence
from bp.core import BlueprintError
from bp.study import engine, safety
from bp.version import TOOL_VERSION

from test_study_core import (LANES, MOBILE, SOURCE, STUB_SOURCE, TempProject, isolated_launcher, leaks,
                             machine_strings, project_text)

DIG = "sha256:" + "ab" * 32


def rec(**kw):
    args = dict(study="S-a", kind="note.fact", key="k", claim="A fact.", basis="observed",
                source={"artifact": ".", "path": "a.txt", "line": 1, "offset": None, "digest": DIG, "excerpt": None},
                by="agent:lead")
    args.update(kw)
    by = args.pop("by")
    return evidence.make_record(args.pop("study"), args.pop("kind"), args.pop("key"), args.pop("claim"),
                                args.pop("basis"), args.pop("source"), by=by, **args)


class RecordTest(unittest.TestCase):
    def test_the_id_is_the_fact_not_the_wording(self):
        a = rec()
        self.assertRegex(a["id"], r"^ev-[0-9a-f]{16}$")
        for other in (rec(claim="Said another way."), rec(study="S-b"), rec(by="tool:web@9.9.9"),
                      rec(data={"x": 1}), rec(limitations=["one"]), rec(links=["ev-" + "0" * 16]),
                      rec(coverage="partial"), rec(at="2027-01-01T00:00:00Z")):
            self.assertEqual(other["id"], a["id"])
        src = dict(a["source"])
        for change in ({"line": 2}, {"path": "b.txt"}, {"digest": "sha256:" + "cd" * 32}, {"offset": 3},
                       {"artifact": "x.apk"}):
            self.assertNotEqual(rec(source=dict(src, **change))["id"], a["id"], change)
        for change in ({"kind": "note.other"}, {"key": "k2"}, {"basis": "inferred"}):
            self.assertNotEqual(rec(**change)["id"], a["id"], change)
        expect = core.content_id("ev-", {"kind": "note.fact", "key": "k", "basis": "observed", "artifact": ".",
                                         "path": "a.txt", "line": 1, "offset": None, "digest": DIG}, 16)
        self.assertEqual(a["id"], expect)

    def test_unsourced_notes_keep_their_claim_in_the_id(self):
        src = {"artifact": ".", "path": None, "line": None, "offset": None, "digest": None, "excerpt": None}
        one, two = rec(source=src, claim="First note."), rec(source=src, claim="Second note.")
        self.assertNotEqual(one["id"], two["id"])
        self.assertEqual(evidence.record_id(one), one["id"])
        self.assertFalse(evidence.edited(one))
        self.assertTrue(evidence.edited(dict(one, claim="hand edited")))

    def test_strings_are_scrubbed_cut_and_cleaned(self):
        secret = FT.SECRETS["aws-key"]
        r = rec(key="k " + secret, claim=f"Uses {secret}\nacross lines.", data={"v": secret, secret: [secret, 1.5],
                                                                               "nan": float("nan")},
                source={"artifact": ".", "path": "a.txt", "line": 3, "offset": None, "digest": DIG,
                        "excerpt": "x" * 150 + secret + "y" * 300},
                limitations=["see " + secret, "see " + secret])
        blob = json.dumps(r)
        self.assertNotIn(secret, blob)
        self.assertEqual(r["claim"], "Uses <secret:aws-key> across lines.")
        self.assertEqual(r["data"]["nan"], None)
        self.assertLessEqual(len(r["source"]["excerpt"]), 160)
        self.assertEqual(r["limitations"], ["see <secret:aws-key>"])
        long = rec(key="k" * 250, claim="c" * 400)
        self.assertEqual((len(long["key"]), len(long["claim"])), (200, 300))
        self.assertIn("key cut to 200 characters", long["limitations"])
        self.assertIn("claim cut to 300 characters", long["limitations"])
        sur = rec(key="k\ud800", claim="c\udfff.")
        json.dumps(sur, ensure_ascii=False).encode("utf-8")
        self.assertEqual(sur["key"], "k\ufffd")

    def test_data_over_4_kb_is_trimmed_with_a_limitation(self):
        r = rec(data={"a": "x" * 3000, "b": list(range(2000)), "c": {"deep": "y" * 5000}, "d": 1})
        self.assertLessEqual(len(core.canonical_json(r["data"])), 4096)
        self.assertIn("data trimmed", r["limitations"])
        self.assertEqual(r["data"]["a"], "x" * 3000)
        self.assertTrue(r["data"]["b"])
        deep = {}
        cur = deep
        for _ in range(60):
            cur["n"] = {}
            cur = cur["n"]
        self.assertIn("data trimmed", rec(data=deep)["limitations"])
        self.assertNotIn("data trimmed", rec(data={"small": 1})["limitations"])

    def test_wrong_fields_are_refused(self):
        for kw in ({"kind": "Bad"}, {"kind": "a"}, {"kind": "a.b\n"}, {"basis": "seen"}, {"coverage": "all"},
                   {"by": "someone"}, {"by": "tool:Web@1"}, {"study": "S-Bad"}, {"key": " "}, {"claim": ""},
                   {"links": ["ev-1"]}, {"data": [1]},
                   {"source": {"artifact": "/abs/x.apk", "path": None, "line": None, "offset": None, "digest": None}},
                   {"source": {"artifact": ".", "path": "/etc/x", "line": None, "offset": None, "digest": None}},
                   {"source": {"artifact": ".", "path": "a/../../x", "line": None, "offset": None, "digest": None}},
                   {"source": {"artifact": ".", "path": "C:/x", "line": None, "offset": None, "digest": None}},
                   {"source": {"artifact": ".", "path": "a\\b", "line": None, "offset": None, "digest": None}},
                   {"source": {"artifact": ".", "path": "a", "line": 0, "offset": None, "digest": None}},
                   {"source": {"artifact": ".", "path": "a", "line": True, "offset": None, "digest": None}},
                   {"source": {"artifact": ".", "path": "a", "line": None, "offset": None, "digest": "sha256:x"}},
                   {"source": {"artifact": ".", "path": "a", "line": None, "offset": None, "digest": None, "x": 1}}):
            with self.assertRaises(ValueError, msg=kw):
                rec(**kw)

    def test_validate_record(self):
        good = rec()
        self.assertEqual(evidence.validate_record(good), [])
        self.assertEqual(evidence.validate_record(dict(good, superseded=True)), [])
        bad = dict(good)
        del bad["kind"]
        self.assertIn("kind is not of the form lane.name", evidence.validate_record(bad))
        self.assertTrue(evidence.validate_record(dict(good, data={"x": "y" * 5000})))
        self.assertTrue(evidence.validate_record(dict(good, source=dict(good["source"], path="/abs"))))
        self.assertTrue(evidence.validate_record(dict(good, superseded="yes")))
        self.assertEqual(evidence.validate_record(5), ["the record is not an object"])


class StoreTest(TempProject):
    def write_lines(self, sid, lines):
        folder = self.root / ".blueprint/studies" / sid
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "evidence.jsonl").write_bytes("".join(lines).encode("utf-8"))

    def test_damaged_lines_are_skipped_and_the_first_of_two_ids_wins(self):
        a, b = rec(), rec(key="k2")
        lines = [json.dumps(a) + "\n", "\n", json.dumps(dict(a, claim="second copy")) + "\n",
                 '{"id": "ev-0000000000000000", "x": NaN}\n', '{"id": "ev-1111111111111111", "x": 1e999}\n',
                 '{"id": "ev-2222222222222222", "s": "\\ud800"}\n', '{"id": "not-an-id"}\n', "[1, 2]\n",
                 "[" * 100_000 + "\n", '{"id": "ev-3333333333333333", "kind"\n', json.dumps(b)[:-5]]
        self.write_lines("S-a", lines)
        recs, bad = evidence.load_info(self.root, "S-a")
        self.assertEqual([r["id"] for r in recs], [a["id"]])
        self.assertEqual(recs[0]["claim"], "A fact.")
        self.assertEqual(bad, 8)
        self.assertEqual(evidence.all_ids(self.root), {a["id"]})

    def test_a_missing_kind_still_loads_and_validate_record_names_it(self):
        r = rec()
        del r["kind"]
        self.write_lines("S-a", [json.dumps(r) + "\n"])
        self.assertEqual(evidence.validate_record(evidence.load(self.root, "S-a")[0]), ["kind is not of the form lane.name"])

    def test_add_dedups_makes_s_notes_and_repairs_a_cut_line(self):
        r = rec(study="S-notes")
        stored, added = evidence.add(self.root, "S-notes", r)
        self.assertTrue(added)
        self.assertFalse(evidence.add(self.root, "S-notes", r)[1])
        study = evidence.load_study(self.root, "S-notes")
        self.assertEqual((study["kind"], study["target"]["own"], study["counts"]["evidence"]), ("notes", True, 1))
        path = self.root / ".blueprint/studies/S-notes/evidence.jsonl"
        with open(path, "ab") as f:
            f.write(b'{"id": "ev-99')  # a crash in the middle of a line
        evidence.add(self.root, "S-notes", rec(study="S-notes", key="k3"))
        lines = path.read_bytes().split(b"\n")
        self.assertEqual(lines[-1], b"")
        self.assertEqual(len(evidence.load(self.root, "S-notes")), 2)
        with self.assertRaises(BlueprintError) as cm:
            evidence.add(self.root, "S-other", rec(study="S-other"))
        self.assertEqual(cm.exception.error, "study-not-found")
        self.assertIn("event", (self.root / ".blueprint/log.jsonl").read_text(encoding="utf-8"))

    def test_writes_are_atomic_sorted_and_never_through_a_link(self):
        recs = [rec(key=f"k{i}") for i in range(5)]
        evidence.write_study_records(self.root, "S-w", recs + recs[:2])
        got = evidence.load(self.root, "S-w")
        self.assertEqual([r["id"] for r in got], sorted(r["id"] for r in recs))
        folder = self.root / ".blueprint/studies/S-w"
        self.assertEqual([p.name for p in folder.iterdir()], ["evidence.jsonl"])
        outside = self.tmp / "outside"
        outside.mkdir()
        link = self.root / ".blueprint/studies/S-link"
        if not FA._link(link, outside, True):  # a symlink, or a junction on Windows
            self.skipTest("cannot make a symlink or junction here")
        with self.assertRaises(BlueprintError) as cm:
            evidence.write_study_records(self.root, "S-link", recs)
        self.assertEqual(cm.exception.error, "write-failed")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertNotIn("S-link", evidence.study_ids(self.root))

    def test_a_held_lock_is_state_busy_and_writes_nothing(self):
        holder = subprocess.Popen([S.PY, "-B", "-c", "import sys,time; sys.path.insert(0, sys.argv[1]);"
                                   "from bp import core; from pathlib import Path\n"
                                   "with core.state_lock(Path(sys.argv[2])):\n print('held', flush=True); time.sleep(30)",
                                   str(S.SCRIPTS), str(self.root)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        with mock.patch.dict(os.environ, {"BLUEPRINT_LOCK_TIMEOUT": "0.3"}):
            with self.assertRaises(BlueprintError) as cm:
                evidence.add(self.root, "S-notes", rec(study="S-notes"))
        self.assertEqual(cm.exception.error, "state-busy")
        self.assertFalse((self.root / ".blueprint/studies").exists())

    def test_sixteen_parallel_adds_lose_nothing(self):
        launcher = isolated_launcher({})
        procs = [subprocess.Popen([S.PY, str(launcher), "evidence", "add", "--kind", "note.par", "--key", f"k{i}",
                                   "--claim", f"Parallel fact {i}.", "--basis", "inferred", "--root", str(self.root)],
                                  cwd=str(self.root), env=S.tool_env(BLUEPRINT_LOCK_TIMEOUT="120"),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(16)]
        for p in procs:
            out, err = p.communicate(timeout=300)
            self.assertEqual(p.returncode, 0, err)
        text = (self.root / ".blueprint/studies/S-notes/evidence.jsonl").read_text(encoding="utf-8")
        lines = [x for x in text.split("\n") if x]
        self.assertEqual(len(lines), 16)
        self.assertEqual({json.loads(x)["key"] for x in lines}, {f"k{i}" for i in range(16)})
        self.assertEqual(evidence.load_study(self.root, "S-notes")["counts"]["evidence"], 16)
        self.assertEqual(len((self.root / ".blueprint/log.jsonl").read_text(encoding="utf-8").splitlines()), 16)


class DamagedStudyTest(TempProject):
    def test_a_damaged_study_json_is_a_named_error_and_nothing_changes(self):
        engine.run_study(self.root, "source", self.root, study_id="S-d", lanes=[])
        path = self.root / ".blueprint/studies/S-d/study.json"
        for bad in (b"{broken", b"\xff\xfe", b"[]", b'{"id": "S-x"}', b'{"id": "S-d", "kind": "source", "target": 5}',
                    b'{"id": "S-d", "kind": "source", "target": {"name": "x", "own": "yes"}}',
                    b'{"id": "S-d", "kind": "source", "target": {"name": "x", "own": true, "path": "/abs"}}',
                    b'{"id": "S-d", "kind": "source", "x": NaN}', b"[" * 50_000):
            path.write_bytes(bad)
            with self.assertRaises(BlueprintError) as cm:
                evidence.load_study(self.root, "S-d")
            self.assertEqual(cm.exception.error, evidence.BAD_STUDY, bad[:30])
            self.assertEqual(path.read_bytes(), bad)
            r = self.cli("study", "show", "S-d", "--json", extra={})
            self.assertEqual((r.returncode, json.loads(r.stdout)["error"]["code"]), (1, "bad-study"))
            self.assertNotIn("Traceback", r.stderr)
            r = self.cli("study", "list", extra={})
            self.assertEqual(r.returncode, 0)
            self.assertIn("left out", r.stderr)
            self.assertEqual(path.read_bytes(), bad)

    def test_a_damaged_behaviours_json_stops_export(self):
        engine.run_study(self.root, "source", self.root, study_id="S-d", lanes=[])
        (self.root / ".blueprint/studies/S-d/behaviours.json").write_text("{nope", encoding="utf-8")
        with self.assertRaises(BlueprintError) as cm:
            evidence.export_bundle(self.root)
        self.assertEqual(cm.exception.error, "bad-study")


class FreshnessTest(TempProject):
    def test_a_folder_study_in_all_four_states(self):
        self.put("src/a.js", "a\r\n")
        self.put("src/b.js", "b\n")
        self.put("src/c.js", "c\n")
        engine.run_study(self.root, "source", self.root / "src", authorization="The project lead allowed this.",
                         lanes=[SOURCE])
        study = evidence.load_study(self.root, "S-src")
        recs = {r["key"]: r for r in evidence.load(self.root, "S-src")}
        self.assertEqual(study["target"]["path"], "src")
        self.put("src/a.js", "a\n")  # only the line ending changed: the proof rule still says fresh
        self.put("src/b.js", "b changed\n")
        (self.root / "src/c.js").unlink()
        states = evidence.freshness_all(self.root, study, list(recs.values()))
        self.assertEqual({k: states[r["id"]] for k, r in recs.items()},
                         {"a.js": "fresh", "b.js": "stale", "c.js": "missing"})
        note = rec(study="S-src", source={"artifact": ".", "path": None, "line": None, "offset": None, "digest": None})
        self.assertEqual(evidence.freshness(self.root, study, note), "unknown")
        self.assertEqual(evidence.freshness(self.root, study, {"id": "x"}), "unknown")
        weird = dict(recs["a.js"], source=dict(recs["a.js"]["source"], path="../outside.js"))
        self.assertEqual(evidence.freshness(self.root, study, weird), "unknown")
        shutil.rmtree(self.root / "src")
        self.assertEqual(set(evidence.freshness_all(self.root, study, list(recs.values())).values()), {"missing"})

    def test_an_archive_study_its_members_and_nested_members(self):
        inner = FA.zip_bytes([FA.ZipEntry("AndroidManifest.xml", "m1")])
        FA.make_xapk(self.root / "app.xapk", {"base.apk": inner}, files={"manifest.json": "{}"})
        engine.run_study(self.root, "mobile", self.root / "app.xapk", own=True, lanes=[MOBILE])
        study = evidence.load_study(self.root, "S-app")
        recs = {r["key"]: r for r in evidence.load(self.root, "S-app")}
        self.assertEqual(set(recs), {"base.apk!/AndroidManifest.xml", "manifest.json"})
        self.assertEqual(set(evidence.freshness_all(self.root, study, list(recs.values())).values()), {"fresh"})
        changed = FA.zip_bytes([FA.ZipEntry("AndroidManifest.xml", "m2")])
        FA.make_xapk(self.root / "app.xapk", {"base.apk": changed}, files={"manifest.json": "{}"})
        states = evidence.freshness_all(self.root, study, list(recs.values()))
        self.assertEqual(states[recs["base.apk!/AndroidManifest.xml"]["id"]], "stale")
        self.assertEqual(states[recs["manifest.json"]["id"]], "fresh")
        FA.make_xapk(self.root / "app.xapk", {"base.apk": changed})
        self.assertEqual(evidence.freshness(self.root, study, recs["manifest.json"]), "missing")
        (self.root / "app.xapk").unlink()
        self.assertEqual(evidence.freshness(self.root, study, recs["manifest.json"]), "missing")

    def test_a_target_outside_the_project_needs_target_and_unknown_suffixes_need_a_resolver(self):
        p = FA.make_apk(self.tmp / "vendor.apk", FA.SNAGDESK_MANIFEST, None, None, files={"assets/x.blob": "x"})
        engine.run_study(self.root, "mobile", p, own=True, lanes=[MOBILE])
        study = evidence.load_study(self.root, "S-vendor")
        recs = evidence.load(self.root, "S-vendor")
        self.assertEqual(set(evidence.freshness_all(self.root, study, recs).values()), {"unknown"})
        self.assertEqual(set(evidence.freshness_all(self.root, study, recs, target=p).values()), {"fresh"})
        nested = dict(recs[0], source=dict(recs[0]["source"], path="assets/x.blob!/inside.txt"))
        study_changed = dict(study, target=dict(study["target"], digest=None))  # no fast path
        self.assertEqual(evidence.freshness(self.root, study_changed, nested, target=p), "unknown")
        calls = []

        def blob(outer, inner, budget):
            calls.append(inner)
            if inner != "inside.txt":
                raise safety.Skip(safety.MISSING)
            return b"inner bytes"
        nested["source"]["digest"] = core.sha256_bytes(b"inner bytes")
        self.assertEqual(evidence.freshness(self.root, study_changed, nested, target=p, resolvers={".blob": blob}),
                         "fresh")
        nested["source"]["path"] = "assets/x.blob!/gone.txt"
        self.assertEqual(evidence.freshness(self.root, study_changed, nested, target=p, resolvers={".blob": blob}),
                         "missing")
        self.assertEqual(calls, ["inside.txt", "gone.txt"])


class BundleTest(TempProject):
    def make_store(self):
        self.put("src/a.js", "a\n" + FT.SECRETS["slack"] + "\n")
        engine.run_study(self.root, "source", self.root, study_id="S-one", lanes=[SOURCE])
        evidence.add(self.root, "S-notes", rec(study="S-notes"))
        (self.root / ".blueprint/studies/S-one/behaviours.json").write_text(json.dumps(
            {"study": "S-one", "behaviours": [{"id": "B-0123456789ab", "status": "candidate"}]}), encoding="utf-8")

    def test_export_is_byte_identical_and_sorted(self):
        self.make_store()
        out1, out2 = self.tmp / "one.json", self.tmp / "two.json"
        r1 = self.cli("evidence", "export", "--out", out1, extra={})
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(self.cli("evidence", "export", "--out", out2, extra={}).returncode, 0)
        self.assertEqual(out1.read_bytes(), out2.read_bytes())
        self.assertRegex(r1.stdout, r"^Wrote 2 studies, \d+ records to ")
        bundle = json.loads(out1.read_text(encoding="utf-8"))
        self.assertEqual(list(bundle), ["format", "tool", "studies", "records", "behaviours"])
        self.assertEqual((bundle["format"], bundle["tool"]), ("app-blueprint-evidence/1", TOOL_VERSION))
        self.assertEqual([s["id"] for s in bundle["studies"]], ["S-notes", "S-one"])
        self.assertEqual([r["id"] for r in bundle["records"]], sorted(r["id"] for r in bundle["records"]))
        self.assertEqual(bundle["behaviours"][0]["study"], "S-one")
        self.assertNotIn(FT.SECRETS["slack"], out1.read_text(encoding="utf-8"))
        only = evidence.export_bundle(self.root, ["S-notes"])
        self.assertEqual([s["id"] for s in only["studies"]], ["S-notes"])
        self.assertEqual(self.cli("evidence", "export", "--out", out1, "--study", "S-zzz", extra={}).returncode, 1)

    def test_import_merges_and_counts(self):
        self.make_store()
        bundle = evidence.export_bundle(self.root)
        other = TempProject()
        other.setUp()
        self.addCleanup(other.doCleanups)
        counts = evidence.import_bundle(other.root, bundle)
        self.assertEqual(counts["studies"], {"added": 2, "duplicate": 0, "conflict": 0, "malformed": 0})
        self.assertEqual(counts["records"]["added"], len(bundle["records"]))
        self.assertEqual(counts["behaviours"]["added"], 1)
        again = evidence.import_bundle(other.root, bundle)
        self.assertEqual((again["added"], again["conflict"], again["malformed"]), (0, 0, 0))
        self.assertEqual(again["duplicate"], 2 + len(bundle["records"]) + 1)
        changed = json.loads(json.dumps(bundle))
        changed["studies"][0]["at"] = "2000-01-01T00:00:00Z"
        changed["records"][0]["claim"] = "Another claim."
        changed["behaviours"][0]["behaviours"][0]["status"] = "reviewed"
        bad = dict(changed["records"][1])
        del bad["kind"]
        changed["records"].append(bad)
        changed["records"].append(dict(changed["records"][1], study="S-elsewhere"))
        third = evidence.import_bundle(other.root, changed)
        self.assertEqual(third["studies"]["conflict"], 1)
        self.assertEqual(third["records"]["conflict"], 1)
        self.assertEqual(third["records"]["malformed"], 2)
        self.assertEqual(third["behaviours"]["conflict"], 1)
        self.assertEqual(evidence.load(other.root, "S-one")[0]["claim"], evidence.load(self.root, "S-one")[0]["claim"])

    def test_a_file_that_is_not_a_bundle_is_refused(self):
        cases = [b"", b"not json", b'{"format": "rea"}', b'{"format": "app-blueprint-evidence/1", "records": 5}',
                 b'{"format": "app-blueprint-evidence/1", "x": NaN}', b"[" * 100_000, b"\xff\xfe",
                 b'{"format": "app-blueprint-evidence/1", "s": "\\ud800"}']
        for i, data in enumerate(cases):
            p = self.put(f"b{i}.json", data, self.tmp)
            r = self.cli("evidence", "import", p, "--json", extra={})
            self.assertEqual(r.returncode, 1, data[:20])
            self.assertEqual(json.loads(r.stdout)["error"]["code"], "bad-bundle")
            self.assertNotIn("Traceback", r.stderr)
        r = self.cli("evidence", "import", self.tmp / "none.json", "--json", extra={})
        self.assertEqual(json.loads(r.stdout)["error"]["code"], "bad-bundle")
        self.assertFalse((self.root / ".blueprint/studies").exists())

    def test_import_through_the_cli(self):
        self.make_store()
        out = self.tmp / "b.json"
        self.assertEqual(self.cli("evidence", "export", "--out", out, extra={}).returncode, 0)
        other = TempProject()
        other.setUp()
        self.addCleanup(other.doCleanups)
        r = other.cli("evidence", "import", out, "--json", extra={})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["data"]["conflict"], 0)
        r = other.cli("evidence", "import", out, extra={})
        self.assertIn("0 added", r.stdout)


class EvidenceCommandTest(TempProject):
    def test_add_list_show_stale(self):
        self.put("docs/a.md", "line one\n" + FT.SECRETS["github-token"] + " here\n")
        r = self.cli("evidence", "add", "--kind", "note.doc", "--key", "a", "--claim", "The doc says so.", "--basis",
                     "observed", "--path", "docs/a.md", "--line", "2", "--by", "lead", "--limitation", "only line 2",
                     "--json", extra={})
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["command"], "evidence add")
        got = out["data"]["record"]
        self.assertEqual((got["study"], got["by"], got["coverage"]), ("S-notes", "agent:lead", "unknown"))
        self.assertEqual(got["source"]["path"], "docs/a.md")
        self.assertEqual(got["source"]["digest"], core.proof_digest(self.root / "docs/a.md"))
        self.assertEqual(got["source"]["excerpt"], "<secret:github-token> here")
        r = self.cli("evidence", "add", "--kind", "note.idea", "--key", "x", "--claim", "An idea.", "--basis",
                     "inferred", "--link", got["id"], extra={})
        self.assertRegex(r.stdout, r"^Recorded ev-[0-9a-f]{16} in S-notes\.")
        r = self.cli("evidence", "list", extra={})
        self.assertIn(f"{got['id']}  observed  fresh  The doc says so.", r.stdout)
        self.put("docs/a.md", "changed\n")
        r = self.cli("evidence", "list", "--stale", "--json", extra={})
        rows = json.loads(r.stdout)["data"]["records"]
        self.assertEqual([(x["id"], x["freshness"]) for x in rows], [(got["id"], "stale")])
        self.assertIn("⚠ stale", self.cli("evidence", "list", extra={}).stdout)
        r = self.cli("evidence", "show", got["id"], "--json", extra={})
        self.assertEqual(json.loads(r.stdout)["data"]["freshness"], "stale")
        self.assertIn("excerpt: <secret:github-token> here", self.cli("evidence", "show", got["id"], extra={}).stdout)
        r = self.cli("evidence", "stale", "--json", extra={})
        self.assertEqual(json.loads(r.stdout)["data"]["studies"]["S-notes"], {"fresh": 0, "stale": 1, "missing": 0,
                                                                             "unknown": 1})
        self.assertEqual(self.cli("evidence", "stale", extra={}).returncode, 0)
        self.assertEqual(self.cli("evidence", "stale", "--strict", extra={}).returncode, 1)
        r = self.cli("evidence", "list", "--kind", "note.idea", "--json", extra={})
        self.assertEqual(len(json.loads(r.stdout)["data"]["records"]), 1)

    def test_refusals(self):
        def code(*args):
            r = self.cli(*args, "--json", extra={})
            return r.returncode, json.loads(r.stdout)["error"]["code"] if r.stdout.strip() else None
        base = ("evidence", "add", "--key", "k", "--claim", "c", "--basis", "observed")
        self.assertEqual(code(*base, "--kind", "Bad"), (64, "usage"))
        self.assertEqual(code(*base, "--kind", "a.b", "--study", "s-x"), (64, "usage"))
        self.assertEqual(code(*base, "--kind", "a.b", "--study", "S-none"), (1, "study-not-found"))
        self.assertEqual(code(*base, "--kind", "a.b", "--line", "3"), (64, "usage"))
        self.assertEqual(code(*base, "--kind", "a.b", "--path", "../../etc/passwd"), (64, "usage"))
        self.assertEqual(code(*base, "--kind", "a.b", "--link", "ev-1"), (64, "usage"))
        self.assertEqual(code(*base, "--kind", "a.b", "--link", "ev-" + "0" * 16), (1, "evidence-not-found"))
        self.assertEqual(code(*base, "--kind", "a.b", "--by", "two words"), (64, "usage"))
        self.assertEqual(code("evidence", "add", "--kind", "a.b", "--key", "k" * 201, "--claim", "c", "--basis",
                              "observed"), (64, "usage"))
        self.assertEqual(code("evidence", "show", "ev-zz"), (1, "evidence-not-found"))
        self.assertEqual(code("evidence", "show", "ev-" + "1" * 16), (1, "evidence-not-found"))
        self.assertEqual(code("evidence", "stale", "--target", "x"), (64, "usage"))
        self.assertEqual(code("evidence", "list", "--study", "S-none"), (1, "study-not-found"))
        r = self.cli("evidence", "add", "--kind", "a.b", "--key", "k", "--claim", "c", "--basis", "seen", extra={})
        self.assertEqual(r.returncode, 2)
        self.assertFalse((self.root / ".blueprint/studies").exists())
        r = self.cli("evidence", "add", "--kind", "a.b", "--key", "k", "--claim", "c", "--basis", "observed",
                     "--study", "S-x; rm -rf /", extra={})
        self.assertNotIn("rm -rf", r.stderr.split("next:", 1)[-1])

    def test_evidence_stale_with_a_target_outside_the_project(self):
        p = FA.make_apk(self.tmp / "vendor.apk", FA.SNAGDESK_MANIFEST, None, None, files={"a.txt": "a"})
        engine.run_study(self.root, "mobile", p, own=True, lanes=[MOBILE])
        r = self.cli("evidence", "stale", "--study", "S-vendor", "--strict", "--json", extra={})
        self.assertEqual(r.returncode, 0)  # unknown (cannot tell) is counted, never a failure
        self.assertEqual(json.loads(r.stdout)["data"]["studies"]["S-vendor"]["unknown"], 2)
        r = self.cli("evidence", "stale", "--study", "S-vendor", "--target", p, "--json", extra={})
        self.assertEqual(json.loads(r.stdout)["data"]["studies"]["S-vendor"]["fresh"], 2)
        FA.make_apk(p, FA.SNAGDESK_MANIFEST, None, None, files={"a.txt": "b"})
        r = self.cli("evidence", "stale", "--study", "S-vendor", "--target", p, "--strict", extra={})
        self.assertEqual(r.returncode, 1)
        self.assertIn("⚠ stale", r.stdout)

    def test_missing_fails_closed_like_stale(self):
        self.put("docs/a.md", "a\n")
        self.put("docs/b.md", "b\n")
        for name in ("a", "b"):
            self.assertEqual(self.cli("evidence", "add", "--kind", "note.doc", "--key", name, "--claim", "A doc.",
                                      "--basis", "observed", "--path", f"docs/{name}.md", extra={}).returncode, 0)
        self.assertEqual(self.cli("evidence", "stale", "--strict", extra={}).returncode, 0)
        (self.root / "docs/a.md").unlink()
        r = self.cli("evidence", "stale", "--strict", "--json", extra={})
        self.assertEqual(r.returncode, 1)
        data = json.loads(r.stdout)["data"]
        self.assertEqual((data["studies"]["S-notes"]["missing"], data["stale"]["S-notes"]), (1, []))
        self.assertEqual(len(data["missing"]["S-notes"]), 1)
        self.assertIn("⚠ missing", self.cli("evidence", "stale", extra={}).stdout)
        self.put("docs/b.md", "b changed\n")
        r = self.cli("evidence", "list", "--stale", "--json", extra={})
        self.assertEqual(sorted(x["freshness"] for x in json.loads(r.stdout)["data"]["records"]), ["missing", "stale"])
        text = self.cli("evidence", "list", "--stale", extra={}).stdout
        self.assertIn("⚠ missing", text)
        self.assertIn("⚠ stale", text)

    def test_the_three_answers_for_readers(self):
        self.assertEqual(evidence.CHANGED, ("stale", "missing"))
        self.assertEqual([evidence.is_changed(s) for s in ("fresh", "stale", "missing", "unknown", None)],
                         [False, True, True, False, False])
        self.assertTrue(evidence.all_changed(["stale", "missing"]))
        self.assertTrue(evidence.all_changed(["missing"]))
        for states in ([], ["stale", "unknown"], ["stale", "fresh"], ["unknown"]):
            self.assertFalse(evidence.all_changed(states), states)


class PrivateCoreHelperTest(unittest.TestCase):
    """evidence.write_text depends on core._retry_windows (private) until the core offers a public atomic text
    writer: fail loudly if it disappears or changes its call shape."""

    def test_retry_windows_still_has_the_shape_write_text_uses(self):
        import inspect
        self.assertTrue(callable(getattr(core, "_retry_windows", None)), "core._retry_windows is gone")
        params = list(inspect.signature(core._retry_windows).parameters.values())
        self.assertEqual([(p.name, p.kind) for p in params[:2]],
                         [("fn", inspect.Parameter.POSITIONAL_OR_KEYWORD), ("args", inspect.Parameter.VAR_POSITIONAL)])
        self.assertEqual(core._retry_windows(lambda a, b: a + b, 2, 3), 5)
        calls = []
        self.assertIsNone(core._retry_windows(lambda: calls.append(1)))
        self.assertEqual(calls, [1])


class HooksTest(TempProject):
    def test_validate_context_and_context(self):
        self.assertEqual(evidence.validate_context(None, {}), {})
        self.assertEqual(evidence.validate_context(self.root, {}), {"evidenceIds": set()})
        r, _ = evidence.add(self.root, "S-notes", rec(study="S-notes"))
        self.assertEqual(evidence.validate_context(self.root, {}), {"evidenceIds": {r["id"]}})
        ctx = {"studies": []}
        evidence.context(self.root, {}, {}, ctx)
        self.assertEqual([s["id"] for s in ctx["studies"]], ["S-notes"])
        s = ctx["studies"][0]
        self.assertEqual(set(s), {"id", "kind", "target", "authorization", "tool", "at", "coverage", "counts",
                                  "freshness", "behaviours"})
        self.assertEqual(s["target"], {"name": ".", "own": True})
        self.assertEqual(s["behaviours"], {"candidate": 0, "reviewed": 0, "rejected": 0})
        payload = evidence.map_payload(ctx)
        self.assertEqual(list(payload), ["studies"])
        payload["studies"][0]["id"] = "changed"
        self.assertEqual(ctx["studies"][0]["id"], "S-notes")
        self.assertEqual(evidence.map_payload({}), {"studies": []})
        self.assertEqual(evidence.map_payload({"studies": "x"}), {"studies": []})

    def test_the_map_freshness_work_is_bounded_and_fails_closed(self):
        self.put("src/a.js", "a" * 1000 + "\n")
        engine.run_study(self.root, "source", self.root, study_id="S-b", lanes=[SOURCE])
        with mock.patch.dict(evidence.CONTEXT_LIMITS, {"totalBytes": 10}):
            ctx = {}
            evidence.context(self.root, {}, {}, ctx)
        f = ctx["studies"][0]["freshness"]
        self.assertEqual((f["fresh"], f["stale"]), (0, 0))
        self.assertGreater(f["unknown"], 0)

    def test_validate_warns_only_for_evidence_that_is_not_in_the_store(self):
        unknowns = (S.PACKAGE / "unknowns.py")
        if not unknowns.exists():
            self.skipTest("the unknowns module (W1-U) is not in this tree")
        launcher = isolated_launcher({"unknowns.py": unknowns.read_text(encoding="utf-8")})
        r = self.cli("evidence", "add", "--kind", "note.a", "--key", "a", "--claim", "A.", "--basis", "inferred",
                     "--json", launcher=launcher)
        known = json.loads(r.stdout)["data"]["record"]["id"]
        plan = {"version": 2, "app": {"name": "T"}, "files": [],
                "unknowns": [{"id": "U-a", "question": "Q?", "severity": "low", "status": "open",
                              "evidenceFor": [known, "ev-" + "f" * 16]}]}
        (self.root / ".blueprint/blueprint.json").write_text(json.dumps(plan), encoding="utf-8")
        r = self.cli("validate", "--json", launcher=launcher)
        warnings = json.loads(r.stdout)["data"]["warnings"]
        flagged = [w["message"] for w in warnings if w["code"] == "unknown-evidence"]
        self.assertEqual(len(flagged), 1, warnings)
        self.assertIn("ev-" + "f" * 16, flagged[0])

    def test_the_map_carries_the_studies_and_nothing_names_this_machine(self):
        self.put("src/a.js", "a\n" + FT.SECRETS["jwt"] + "\n")
        p = FA.make_apk(self.tmp / "vendor.apk", FA.SNAGDESK_MANIFEST, None, None, files={"a.txt": "a"})
        self.assertEqual(self.cli("study", "source", self.root).returncode, 0)
        r = self.cli("study", "mobile", p, "--authorization", f"Allowed by the vendor licence at {self.tmp}")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.cli("evidence", "add", "--kind", "note.a", "--key", "a", "--claim", "A note.", "--basis",
                                  "inferred", "--path", "src/a.js", "--line", "2").returncode, 0)
        r = self.cli("render", "--force")  # the test plan is a sketch; the map is what is checked
        self.assertEqual(r.returncode, 0, r.stderr)
        html = (self.root / ".blueprint/map.html").read_text(encoding="utf-8")
        self.assertIn('"studies": [{"id": "S-notes"', html)
        self.assertIn("id: 'studies'", html)
        texts = project_text(self.root)
        self.assertTrue(any(n.endswith("evidence.jsonl") for n in texts))
        machine = machine_strings(self.tmp, self.root)
        for name, text in texts.items():
            self.assertEqual(leaks(text, machine), [], name)
            for value in (FT.SECRETS["jwt"], json.dumps(FT.SECRETS["jwt"])[1:-1]):
                self.assertNotIn(value, text, name)


TAB = S.SKILL / "assets" / "tab-studies.js"
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
if (BP_TABS.length !== 1 || BP_TABS[0].id !== 'studies') throw new Error('the tab did not register once as studies');
const H = { el: function (t, a, k) { const n = node(t); if (a) for (const x in a) { if (x === 'text') n.textContent = a[x];
  else if (x === 'cls') n.className = a[x]; else n.setAttribute(x, a[x]); }
  (k || []).forEach(function (c) { if (c != null) n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); }); return n; } };
const out = [];
for (const D of JSON.parse(fs.readFileSync(0, 'utf8'))) {
  const host = node('section');
  out.push({ label: BP_TABS[0].label(D), text: (BP_TABS[0].render(host, D, H), text(host)) });
}
console.log(JSON.stringify(out));
"""


class TabTest(unittest.TestCase):
    def run_tab(self, payloads):
        r = subprocess.run(["node", "-e", HARNESS, "--", str(TAB)], input=json.dumps(payloads), capture_output=True,
                           text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_source_is_plain_and_offline(self):
        src = TAB.read_text(encoding="utf-8")
        self.assertIn("id: 'studies'", src)
        for bad in ("http://", "https://", "fetch(", "XMLHttpRequest", "import ", "require(", "innerHTML",
                    "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "WebSocket", "sendBeacon", "url("):
            self.assertNotIn(bad, src, bad)
        self.assertNotIn("</script", src.lower())
        from bp.views.render import tab_scripts
        self.assertTrue(any("id: 'studies'" in t for t in tab_scripts()))

    @unittest.skipUnless(shutil.which("node"), "node is not installed (the tab is run in node)")
    def test_it_draws_studies_and_never_throws(self):
        study = {"id": "S-acme", "kind": "mobile", "target": {"name": "acme.apk", "own": False},
                 "authorization": {"statement": "The vendor's licence allows analysis."}, "tool": "1.9.1",
                 "at": "2027-01-02T03:04:05Z",
                 "coverage": {"filesSeen": 10, "filesScanned": 8, "filesSkipped": 2, "skipped": {"binary": 2},
                              "missing": ["sources/"], "truncated": True, "stoppedBy": "time-limit"},
                 "counts": {"evidence": 42, "secrets": 1}, "freshness": {"fresh": 40, "stale": 2, "missing": 0, "unknown": 0},
                 "behaviours": {"candidate": 3, "reviewed": 1, "rejected": 0}}
        outs = self.run_tab([{"studies": [study]}, {}, {"studies": []}, {"studies": "x"},
                             {"studies": [None, 5, [], {"id": 3, "target": "x", "coverage": {"skipped": {"__proto__": 1}},
                                                        "counts": {"evidence": "NaN"}, "freshness": None, "kind": "__proto__"}]}])
        text = outs[0]["text"]
        self.assertEqual(outs[0]["label"], "Studies (1)")
        for want in ("S-acme", "mobile app", "acme.apk", "not own: not committed", "42 records", "1 secrets redacted",
                     "2 stale", "40 fresh", "1 of 4 reviewed", "8 of 10 files read", "2 skipped (2 binary)",
                     "stopped by time-limit", "missing: sources/", "The vendor's licence allows analysis.", "2027-01-02"):
            self.assertIn(want, text)
        for o in outs[1:4]:
            self.assertIn("No studies yet", o["text"])
            self.assertEqual(o["label"], "Studies")
        self.assertIn("(no id)", outs[4]["text"])


if __name__ == "__main__":
    unittest.main()
