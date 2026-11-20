"""W2-E: the study engine core (contract 6.1 to 6.3): safety (limits, Budget, SafeZip, walk, scrub, scan windows,
excerpts, reading rules), keys, Target, Emitter, run_study with stub lanes, authorization, manifest_facts and the
`study` command as far as the engine owns it.

No lane module is used: lanes are stand-ins written by these tests, so the engine is proven with zero lanes, one lane
and lanes arriving one by one. CLI tests run the launcher of a copy of the skill that holds the core, this
workstream's files and the stand-ins they name (isolated_launcher), so they hold whatever sibling modules exist.
"""
import atexit
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest import mock

import support as S

import fixtures_archives as FA
import fixtures_text as FT
from bp import core, evidence
from bp.core import BlueprintError
from bp.study import engine, keys, safety
from bp.version import TOOL_VERSION

from test_validate import hermetic_skill

OWN_FILES = ("evidence.py", "study/__init__.py", "study/engine.py", "study/safety.py", "study/keys.py",
             "commands/evidence.py", "commands/study.py")

# ------------------------------------------------------------------------------------------------ stand-in lanes

STUB_SOURCE = r'''
"""A stand-in lane for the engine tests: one stub.file record per text file, secret.embedded as 6.2 says."""
from bp.study import safety
LANE = "source"
KINDS = {"stub.file": "a file the stand-in read", "secret.embedded": "a credential-like string"}
FORMATS = "folders"


def detect(path):
    return path.is_dir()


def run(target, em, opts):
    for rel in target.files():
        try:
            data = target.read(rel, safety.LIMITS["textBytes"])
            text, replaced = safety.decode_text(data)
        except safety.Skip as e:
            em.skip(rel, e.reason, e.note)
            continue
        digest = target.digest_of(rel, data)
        first = text.split("\n", 1)[0]
        em.emit("stub.file", rel, "The folder holds " + rel + ".", path=rel, digest=digest, line=1, excerpt=first,
                data={"bytes": len(data)}, coverage="partial" if replaced else "complete",
                limitations=[safety.UTF8_NOTE] if replaced else ())
        _, found = safety.scrub(text)
        for name in sorted(set(found)):
            em.emit("secret.embedded", name + "@" + rel, "A credential-like string (" + name + ") is embedded in "
                    + rel + ".", path=rel, digest=digest, data={"count": found.count(name)})
'''

STUB_MOBILE = r'''
"""A stand-in archive lane: one stub.member record per member, nested .apk members read through SafeZip."""
from bp.study import safety
LANE = "mobile"
KINDS = {"stub.member": "an archive member"}
FORMATS = "zip archives"


def detect(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == safety.ZIP_MAGIC
    except OSError:
        return False


def run(target, em, opts):
    z = target.zip()
    for name, reason, note in z.skipped:
        em.skip(name, reason, note)
    _walk(z, "", em, target, 0)


def _walk(z, prefix, em, target, depth):
    for name, size in z.members():
        path = prefix + name
        try:
            data = z.read(name, safety.LIMITS["memberBytes"])
        except safety.Skip as e:
            em.skip(path, e.reason, e.note)
            continue
        if name.endswith(".apk"):
            try:
                inner = safety.SafeZip(data, em.budget, depth=depth + 1)
            except safety.Skip as e:
                em.skip(path, e.reason, e.note)
                continue
            with inner:
                for n, r, no in inner.skipped:
                    em.skip(path + "!/" + n, r, no)
                _walk(inner, path + "!/", em, target, depth + 1)
            continue
        em.emit("stub.member", path, "The archive holds " + path + ".", path=path, digest=target.digest_of(path, data))
'''


def load_stub(src, name):
    mod = type(sys)(name)
    exec(compile(src, name, "exec"), mod.__dict__)  # noqa: S102 - the test's own stand-in source
    return mod


SOURCE = load_stub(STUB_SOURCE, "stub_source_lane")
MOBILE = load_stub(STUB_MOBILE, "stub_mobile_lane")

_LAUNCHERS = {}


def isolated_launcher(extra=None):
    """The launcher of a skill copy with the core, this workstream's files and `extra` ({rel under bp/: source}); made
    once per set of extras and removed when the test process ends."""
    key = tuple(sorted((extra or {}).items()))
    if key not in _LAUNCHERS:
        base = Path(tempfile.mkdtemp(prefix="bp-study-skill-"))
        atexit.register(shutil.rmtree, base, True)
        launcher = hermetic_skill(base / "skill")
        for rel in OWN_FILES:
            target = launcher.parent / "bp" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(S.PACKAGE / rel, target)
        for rel, src in (extra or {}).items():
            target = launcher.parent / "bp" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(src, encoding="utf-8")
        shutil.copyfile(S.SKILL / "assets" / "tab-studies.js", launcher.parent.parent / "assets" / "tab-studies.js")
        _LAUNCHERS[key] = launcher
    return _LAUNCHERS[key]


LANES = {"study/source.py": STUB_SOURCE, "study/mobile.py": STUB_MOBILE}

# ------------------------------------------------------------------------------------------------ leak scanner

BS = chr(92)
_DRIVE = re.compile("(?<![A-Za-z0-9])[A-Za-z]:[" + BS + BS + "/]")
_UNC = re.compile("(?<![" + BS + "w" + BS + BS + "])" + BS + BS + "{2,4}[A-Za-z0-9_.$-]+" + BS + BS + "{1,2}[A-Za-z0-9_.$-]")
_ROOTED = re.compile(r"(?<![\w/.:<>-])/(?:home|Users|tmp|var|mnt|private|root|usr|opt|etc|run|Volumes)/")


def leaks(text, machine=()):
    """What in text names this machine: one of the given folders, a drive-letter, UNC or rooted path."""
    found = [m for m in machine if m and m in text]
    for rx in (_DRIVE, _UNC, _ROOTED):
        m = rx.search(text)
        if m:
            found.append(m.group(0))
    return found


def machine_strings(*folders):
    out = {tempfile.gettempdir(), Path(tempfile.gettempdir()).as_posix(), str(S.SKILL), S.SKILL.as_posix(),
           os.path.expanduser("~"), Path(os.path.expanduser("~")).as_posix()}
    for f in folders:
        out |= {str(f), Path(f).as_posix(), str(Path(f).resolve()), Path(f).resolve().as_posix()}
    return out


def project_text(root):
    """Every file under .blueprint/ except the tool copy, as text."""
    out = {}
    for p in sorted((Path(root) / ".blueprint").rglob("*")):
        if p.is_file() and "bin" not in p.relative_to(Path(root) / ".blueprint").parts[:1]:
            out[p.relative_to(root).as_posix()] = p.read_text(encoding="utf-8", errors="replace")
    return out


class LeakScannerTest(unittest.TestCase):
    def test_the_scanner_finds_leaks_and_passes_clean_text(self):
        root = Path(tempfile.gettempdir()) / "bp leak probe"
        machine = machine_strings(root)
        for bad in ("C:" + BS + "Users" + BS + "x", "D:/work/app", BS + BS + "server" + BS + "share" + BS + "p",
                    BS * 4 + "server" + BS * 2 + "share" + BS * 2 + "x", "/home/x/p", "cwd=/tmp/bp/x", '("/Users/x/y")',
                    "see " + str(root), "see " + root.as_posix(), json.dumps(str(root))[1:-1]):
            self.assertTrue(leaks(bad, machine), bad)
        for ok in ("https://example.test/a/b", "src/a.ts", "a/b", "2027-01-15", "S-acme", "ev-0123456789abcdef",
                   "base.apk!/AndroidManifest.xml", "sha256:" + "0" * 64, "<secret:aws-key>"):
            self.assertEqual(leaks(ok, machine), [], ok)


class TempProject(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="bp study "))
        self.root = self.tmp / "proj"
        (self.root / ".blueprint").mkdir(parents=True)
        (self.root / ".blueprint" / "blueprint.json").write_text(json.dumps({"version": 2, "app": {"name": "T"},
                                                                             "files": []}), encoding="utf-8")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def put(self, rel, data, base=None):
        p = (base or self.root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return p

    def cli(self, *args, launcher=None, extra=LANES, cwd=None, **env):
        argv = [str(a) for a in args] + ["--root", str(self.root)]
        return S.run_script(launcher or isolated_launcher(extra), argv, cwd or self.root, env=S.tool_env(**env))

    def study(self, kind="source", target=None, lanes=None, **kw):
        lanes = [SOURCE] if lanes is None else lanes
        return engine.run_study(self.root, kind, target if target is not None else self.root, lanes=lanes, **kw)


# ------------------------------------------------------------------------------------------------ safety

class LimitsTest(unittest.TestCase):
    def test_the_limits_of_6_2(self):
        L = safety.LIMITS
        self.assertEqual((L["files"], L["textBytes"], L["memberBytes"], L["totalBytes"], L["archiveEntries"]),
                         (50_000, 5 * 2 ** 20, 64 * 2 ** 20, 512 * 2 ** 20, 50_000))
        self.assertEqual((L["ratioBytes"], L["ratio"], L["nestedDepth"], L["asarHeader"]), (2 ** 20, 100, 2, 32 * 2 ** 20))
        self.assertEqual((L["evidence"], L["evidencePerKind"], L["timeLimit"], L["timeLimitMax"], L["excerpt"]),
                         (20_000, 2_000, 300, 3_600, 160))
        self.assertEqual(safety.SKIP_REASONS, ("too-large", "binary", "unsupported", "unsafe-path", "encrypted",
                                               "symlink", "ratio", "depth", "malformed", "limit", "error"))

    def test_reasons_are_a_closed_list(self):
        with self.assertRaises(ValueError):
            safety.Skip("too-big")
        with self.assertRaises(ValueError):
            safety.Stop("tired")
        self.assertEqual(safety.Skip("binary").reason, "binary")
        self.assertEqual(safety.Stop("time-limit").stoppedBy, "time-limit")


class BudgetTest(unittest.TestCase):
    def test_files_bytes_and_time(self):
        now = [0.0]
        b = safety.Budget({"files": 2, "totalBytes": 10, "timeLimit": 5}, clock=lambda: now[0])
        b.file()
        b.file()
        with self.assertRaises(safety.Stop) as cm:
            b.file()
        self.assertEqual(cm.exception.stoppedBy, "file-limit")
        self.assertEqual(b.files_seen, 2)
        b.bytes(10)
        with self.assertRaises(safety.Stop) as cm:
            b.bytes(1)
        self.assertEqual(cm.exception.stoppedBy, "byte-limit")
        b.check_time()
        now[0] = 5.5
        with self.assertRaises(safety.Stop) as cm:
            b.check_time()
        self.assertEqual(cm.exception.stoppedBy, "time-limit")

    def test_evidence_counts_and_a_kind_backstop(self):
        b = safety.Budget()
        for _ in range(3):
            b.evidence("a.b")
        self.assertEqual(b.by_kind, {"a.b": 3})
        b.MAX_KINDS = 2
        b.evidence("a.c")
        with self.assertRaises(safety.Stop) as cm:
            b.evidence("a.d")
        self.assertEqual(cm.exception.stoppedBy, "evidence-limit")


class WindowsTest(unittest.TestCase):
    def test_windows_cover_the_text_with_overlap(self):
        text = "".join(chr(97 + i % 26) for i in range(200_000))
        wins = list(safety.scan_windows(text))
        self.assertEqual(wins[0][0], 0)
        for (o1, c1), (o2, _) in zip(wins, wins[1:]):
            self.assertEqual(len(c1), 64 * 1024)
            self.assertEqual(o1 + len(c1) - o2, 1024)
        self.assertEqual(wins[-1][0] + len(wins[-1][1]), len(text))
        self.assertEqual(list(safety.scan_windows("")), [(0, "")])

    def test_finditer_reports_each_match_once_like_re(self):
        rng = random.Random(7)
        parts = []
        for _ in range(4000):
            parts.append("x" * rng.randrange(0, 90))
            parts.append(rng.choice(["AKIA" + "B" * 16, "route('/a/b')", "AKIAshort", "route('')"]))
        text = "".join(parts)
        for rx in (r"AKIA[0-9A-Z]{16}", r"route\('[^'\\\n]{0,2048}'\)", r"x{5,}"):
            want = [(m.start(), m.group(0)) for m in re.finditer(rx, text)]
            got = [(g, m.group(0)) for g, m in safety.finditer(rx, text)]
            self.assertEqual(got, want, rx)
            self.assertGreater(len(text), 3 * 64 * 1024)

    def test_a_match_across_a_window_edge_is_found_whole(self):
        edge = 64 * 1024 - 1024 - 3  # starts just before the end of the first window's own part
        text = "y" * edge + "AKIA" + "C" * 16 + "y" * 70_000
        got = [(g, m.group(0)) for g, m in safety.finditer(r"AKIA[0-9A-Z]{16}", text)]
        self.assertEqual(got, [(edge, "AKIA" + "C" * 16)])


class ScrubTest(unittest.TestCase):
    def test_every_fixture_secret_is_replaced_and_named(self):
        for name, value in FT.SECRETS.items():
            out, found = safety.scrub(f"const x = '{value}';")
            self.assertEqual(found, [name], name)
            self.assertIn(f"<secret:{name}>", out)
            self.assertNotIn(value, out)
        pem_json = json.dumps({"pem": FT.SECRETS["private-key"]})
        out, found = safety.scrub(pem_json)
        self.assertEqual(found, ["private-key"])
        self.assertNotIn(FT.PEM_BODY, out)

    def test_counts_one_entry_per_match_and_long_text_is_scanned_by_window(self):
        key = FT.SECRETS["aws-key"]
        small = (key + " ") * 3
        self.assertEqual(safety.scrub(small)[1], ["aws-key"] * 3)
        big = ("filler " * 20_000 + key + " ") * 4
        out, found = safety.scrub(big)
        self.assertEqual(found, ["aws-key"] * 4)
        self.assertNotIn(key, out)
        self.assertEqual(out.count("<secret:aws-key>"), 4)

    def test_every_pattern_starts_with_a_literal(self):
        for name, rx in safety.SECRET_PATTERNS.items():
            body = rx[4:] if rx.startswith("(?i)") else rx
            self.assertTrue(re.match(r"[A-Za-z0-9-]|\[[a-z]{2}\]", body), name)
        self.assertEqual(set(safety.SECRET_PATTERNS) >= set(FT.SECRET_REGEXES), True)

    def test_the_pathological_line_stays_under_the_time_bound(self):
        tmp = Path(tempfile.mkdtemp(prefix="bp-patho-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        p, _ = FT.make_pathological_line(tmp / "bundle.js")
        text = p.read_text(encoding="ascii")
        started = time.monotonic()
        safety.scrub(text)
        for rx in list(safety.SECRET_PATTERNS.values()) + [r"""fetch\(\s{0,64}["'`]([^"'`\\\n]{0,2048})["'`]"""]:
            sum(1 for _ in safety.finditer(rx, text))
        safety.excerpt(text, len(text) // 2)
        self.assertLess(time.monotonic() - started, 10.0)


class ExcerptTest(unittest.TestCase):
    def test_one_line_at_most_160_characters_from_the_offset(self):
        line = "a" * 50 + "TARGET" + "b" * 300
        text = "first\n" + line + "\nlast"
        ex = safety.excerpt(text, text.index("TARGET"))
        self.assertTrue(ex.startswith("TARGET"))
        self.assertEqual(len(ex), 160)
        self.assertEqual(safety.excerpt("one\r\ntwo", 0), "one")
        self.assertEqual(safety.excerpt("x\ty\x00z  w\u2028next", 0), "x y z w")  # U+2028 ends the line
        self.assertEqual(safety.excerpt("", 0), "")

    def test_a_secret_across_the_cut_is_scrubbed_before_cutting(self):
        key = FT.SECRETS["github-token"]
        text = "q" * 150 + key + "tail"
        ex = safety.excerpt(text, 0)
        self.assertLessEqual(len(ex), 160)
        self.assertNotIn(key[:12], ex)
        self.assertIn("<secret", ex)

    def test_lone_surrogates_are_replaced(self):
        self.assertEqual(safety.excerpt("a\ud800b"), "a\ufffdb")
        self.assertEqual(safety.clean_text("x\udfffy"), "x\ufffdy")


class DecodeTest(unittest.TestCase):
    def test_reading_rule_1(self):
        with self.assertRaises(safety.Skip) as cm:
            safety.decode_text(b"abc\x00def")
        self.assertEqual(cm.exception.reason, "binary")
        self.assertEqual(safety.decode_text(b"x" * 9000 + b"\x00"), ("x" * 9000 + "\x00", False))  # NUL after 8 KiB
        self.assertEqual(safety.decode_text(b"\xef\xbb\xbfhi"), ("hi", False))
        text, replaced = safety.decode_text(b"ok \xff\xfe ok")
        self.assertTrue(replaced)
        self.assertIn("\ufffd", text)


class SafeZipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bp-zip-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def opened(self, builder, *a, **kw):
        p = builder(self.tmp / "t.zip", *a, **kw)
        b = safety.Budget()
        z = safety.SafeZip(p, b)
        self.addCleanup(z.close)
        return z, b

    def skipped(self, z):
        return {(n, r) for n, r, _ in z.skipped}

    def test_member_names_are_checked_on_the_full_name(self):
        z, _ = self.opened(FA.make_zip_slip)
        self.assertEqual([n for n, _ in z.members()], ["ok.txt"])
        self.assertEqual({r for _, r in self.skipped(z)}, {"unsafe-path"})
        self.assertEqual(len(z.skipped), 4)
        z, _ = self.opened(FA.make_absolute_path_zip)
        self.assertEqual([n for n, _ in z.members()], ["ok.txt"])
        self.assertEqual(len(z.skipped), 4)
        z, _ = self.opened(FA.make_nul_name_zip)
        self.assertEqual([n for n, _ in z.members()], ["ok.txt"])
        self.assertEqual([r for _, r, _ in z.skipped], ["unsafe-path"])
        self.assertNotIn("\x00", z.skipped[0][0])

    def test_bomb_encrypted_symlink_duplicate(self):
        z, b = self.opened(FA.make_zip_bomb)
        self.assertIn(("bomb.bin", "ratio"), self.skipped(z))
        with self.assertRaises(safety.Skip) as cm:
            z.read("bomb.bin", 10 ** 9)
        self.assertEqual(cm.exception.reason, "ratio")
        self.assertLess(b.bytes_read, 2 ** 20)
        z, _ = self.opened(FA.make_encrypted_member_zip)
        self.assertIn(("secret.txt", "encrypted"), self.skipped(z))
        z, _ = self.opened(FA.make_symlink_zip)
        self.assertEqual(self.skipped(z), {("link", "symlink"), ("dirlink", "symlink")})
        z, _ = self.opened(FA.make_duplicate_names_zip)
        self.assertEqual(z.read("app/config.json", 1000), b'{"which": "first"}\n')
        self.assertIn(("app/config.json", "malformed"), self.skipped(z))

    def test_damaged_members_are_malformed_and_the_rest_reads(self):
        for builder, name in ((FA.make_unsupported_method_zip, "weird.bin"), (FA.make_corrupt_deflate_zip, "bad.bin"),
                              (FA.make_truncated_member_zip, "cut.bin")):
            z, _ = self.opened(builder)
            with self.assertRaises(safety.Skip) as cm:
                z.read(name, 2 ** 20)
            self.assertEqual(cm.exception.reason, "malformed", builder.__name__)
            self.assertEqual(z.read("ok.txt", 100), b"fine\n")

    def test_a_truncated_archive_is_malformed(self):
        p = FA.make_truncated_zip(self.tmp / "t.zip")
        with self.assertRaises(safety.Skip) as cm:
            safety.SafeZip(p, safety.Budget())
        self.assertEqual(cm.exception.reason, "malformed")
        for junk in (b"", b"PK\x03\x04garbage", b"\x00" * 100):
            with self.assertRaises(safety.Skip):
                safety.SafeZip(junk, safety.Budget())

    def test_sizes_are_real_bytes_and_capped(self):
        z, b = self.opened(FA.make_oversized_member_zip)
        with self.assertRaises(safety.Skip) as cm:
            z.read("big.bin", safety.LIMITS["memberBytes"])
        self.assertEqual(cm.exception.reason, "too-large")
        self.assertLessEqual(b.bytes_read, safety.LIMITS["memberBytes"] + 65536)
        lying = FA.zip_bytes([FA.ZipEntry("x.bin", b"", method=0, compressed=b"z" * 5000, usize=5000, csize=5000)])
        z2 = safety.SafeZip(lying, safety.Budget())
        with self.assertRaises(safety.Skip) as cm:
            z2.read("x.bin", 1000)
        self.assertEqual(cm.exception.reason, "too-large")
        with self.assertRaises(safety.Skip) as cm:
            z2.read("not-there", 10)
        self.assertEqual(cm.exception.reason, safety.MISSING)

    def test_entry_limit_and_nested_depth(self):
        z, _ = self.opened(FA.make_many_directories_zip)
        self.assertEqual(z.members(), [])
        self.assertEqual([r for _, r, _ in z.skipped], ["limit"])
        outer = FA.make_nested_zip(self.tmp / "n.xapk", depth=3)
        b = safety.Budget()
        z1 = safety.SafeZip(outer, b)
        self.addCleanup(z1.close)
        data1 = z1.read("inner1.apk", 2 ** 20)
        z2 = safety.SafeZip(data1, b, depth=1)
        data2 = z2.read("inner2.apk", 2 ** 20)
        z3 = safety.SafeZip(data2, b, depth=2)
        data3 = z3.read("inner3.apk", 2 ** 20)
        with self.assertRaises(safety.Skip) as cm:
            safety.SafeZip(data3, b, depth=3)
        self.assertEqual(cm.exception.reason, "depth")

    def test_many_file_entries_stop_the_study_at_the_file_limit(self):
        p = FA.make_many_entries_zip(self.tmp / "m.zip")
        with self.assertRaises(safety.Stop) as cm:
            safety.SafeZip(p, safety.Budget({"files": 100}))
        self.assertEqual(cm.exception.stoppedBy, "file-limit")

    def test_nothing_is_extracted(self):
        p = FA.make_zip_slip(self.tmp / "t.zip")
        before = sorted(x.name for x in self.tmp.iterdir())
        with safety.SafeZip(p, safety.Budget()) as z:
            for name, _ in z.members():
                z.read(name, 1000)
        self.assertEqual(sorted(x.name for x in self.tmp.iterdir()), before)
        self.assertFalse((self.tmp.parent / "evil.txt").exists())
        src = (S.PACKAGE / "study" / "safety.py").read_text(encoding="utf-8")
        self.assertNotIn(".extract", src)


class WalkTest(TempProject):
    def test_sorted_and_counted(self):
        for rel in ("b/c.txt", "a.txt", "c.txt", "b/a.txt"):
            self.put(rel, "x", self.tmp)
        b = safety.Budget()
        got = list(safety.walk(self.tmp, b))
        self.assertEqual([g for g in got if not g.startswith("proj/")], ["a.txt", "b/a.txt", "b/c.txt", "c.txt"])
        self.assertEqual(b.files_seen, len(got))

    def test_file_limit_yields_what_was_found_then_stops(self):
        for i in range(5):
            self.put(f"d/{i}.txt", "x", self.tmp)
        out = []
        with self.assertRaises(safety.Stop):
            for rel in safety.walk(self.tmp / "d", safety.Budget({"files": 3})):
                out.append(rel)
        self.assertEqual(out, ["0.txt", "1.txt", "2.txt"])

    def test_links_and_junctions_are_skipped(self):
        try:
            made = FA.make_folder_with_symlink(self.tmp / "links")
        except FA.SymlinkUnavailable as e:
            self.skipTest(str(e))
        skips = []
        got = list(safety.walk(made["root"], safety.Budget(), skip=lambda r, why, n=None: skips.append((r, why))))
        self.assertEqual(got, ["ok.txt"])
        self.assertTrue(skips)
        self.assertEqual({why for _, why in skips}, {"symlink"})
        with self.assertRaises(safety.Skip) as cm:
            safety.read_file(made["links"][-1], 100, safety.Budget())
        self.assertEqual(cm.exception.reason, "symlink")

    def test_read_file_counts_real_bytes(self):
        p = self.put("big.txt", "x" * 5000)
        with self.assertRaises(safety.Skip) as cm:
            safety.read_file(p, 1000, safety.Budget())
        self.assertEqual(cm.exception.reason, "too-large")
        b = safety.Budget()
        self.assertEqual(safety.read_file(p, 5000, b), b"x" * 5000)
        self.assertEqual((b.bytes_read, b.files_scanned), (5000, 1))


# ------------------------------------------------------------------------------------------------ keys

class KeysTest(unittest.TestCase):
    def test_route_parameters_become_names(self):
        cases = {"/items/:itemId": "/items/{itemId}", "/blog/[slug]": "/blog/{slug}", "/files/<path>": "/files/{path}",
                 "/u/<int:id>": "/u/{id}", "/static/*": "/static/{rest*}", "/docs/[...slug]": "/docs/{slug*}",
                 "/docs/[[...slug]]": "/docs/{slug*}", "/a/{id}": "/a/{id}", "/a/{id:int}": "/a/{id}",
                 "/(marketing)/about": "/about", "/@modal/photo": "/photo", "items/": "/items", "/": "/", "": "/",
                 "//a//b/": "/a/b", "/x?y=1#z": "/x", "/u/:id(\\d+)": "/u/{id}", "/u/:id?": "/u/{id}",
                 "/f/:from-:to": "/f/{from}-{to}", "/2024/report": "/2024/report"}
        for raw, want in cases.items():
            self.assertEqual(keys.path_template(raw), want, raw)

    def test_observed_paths_template_ids(self):
        cases = {"/api/items/123": "/api/items/{id}", "/a/3f2b8c1e-5a4d-4e7b-9c1a-0d2e4f6a8b0c/c": "/a/{id}/c",
                 "/f/0123456789abcdef": "/f/{id}", "/b/Zm9vYmFyYmF6cXV4MTIzNA": "/b/{id}", "/v1/status": "/v1/status",
                 "/@me/x": "/@me/x", "/(x)": "/(x)", "/:id": "/:id", "/0123456789abcde": "/0123456789abcde"}
        for raw, want in cases.items():
            self.assertEqual(keys.path_template(raw, concrete=True), want, raw)

    def test_route_and_endpoint_keys(self):
        self.assertEqual(keys.route_key("get", "/items/:id"), "GET /items/{id}")
        for m in (None, "", "all", "use", "*", 5):
            self.assertEqual(keys.route_key(m, "/x"), "ANY /x")
        for url, want in (("https://svc:pw" + "@" + "API.example.test:8443/v1/status", "api.example.test:8443"),
                          ("https://app.example.test:443/", "app.example.test"),
                          ("http://app.example.test:80/x", "app.example.test"),
                          ("http://[::1]:8080/x", "[::1]:8080"), ("not a url", ""), ("http://h:99999/", "h")):
            self.assertEqual(keys.url_host(url), want, url)
        for entry in FT.default_har_entries():
            key = keys.endpoint_key(entry["request"]["method"], entry["request"]["url"])
            self.assertIn(key, FT.HAR_ENDPOINTS)
        self.assertEqual(len({keys.endpoint_key(e["request"]["method"], e["request"]["url"])
                              for e in FT.default_har_entries()}), len(FT.HAR_ENDPOINTS))

    def test_json_shape(self):
        self.assertEqual(keys.json_shape({"a": {"b": {"c": {"d": 1}}}}), {"a": {"b": {"c": {"d": "..."}}}})
        self.assertEqual(keys.json_shape([[[[1]]]]), [[[["..."]]]])
        self.assertEqual(keys.json_shape({"s": "x", "n": 1.5, "i": 2, "b": True, "z": None, "e": [], "l": [{"k": 1}, 2]}),
                         {"b": "boolean", "e": [], "i": "number", "l": [{"k": "number"}], "n": "number", "s": "string",
                          "z": "null"})
        many = keys.json_shape({f"k{i:03d}": i for i in range(80)})
        self.assertEqual(list(many), [f"k{i:03d}" for i in range(50)])
        shape, cut = keys.json_shape_info(FT._TREE)
        self.assertEqual(shape, FT.HAR_SHAPES["GET app.example.test/api/tree response"])
        self.assertTrue(cut)
        self.assertFalse(keys.json_shape_info({"a": 1})[1])

    def test_frameworks_and_languages(self):
        self.assertEqual(keys.FRAMEWORKS, ("react", "next", "vue", "nuxt", "svelte", "sveltekit", "angular", "express",
                                           "fastify", "nestjs", "electron", "react-native", "expo", "hermes", "flutter",
                                           "unity", "xamarin", "capacitor", "cordova", "django", "flask", "fastapi",
                                           "axum", "actix-web", "tauri", "gin", "echo", "chi", "rails", "android"))
        self.assertEqual(keys.language_of("src/a.tsx"), "TypeScript")
        self.assertEqual(keys.language_of("x.exs"), "Elixir")
        self.assertIsNone(keys.language_of("README"))
        self.assertIsNone(keys.language_of(".ts"))


# ------------------------------------------------------------------------------------------------ engine

class ProofDigestTest(TempProject):
    def test_the_bytes_form_equals_the_core_proof_digest(self):
        for i, data in enumerate((b"a\r\nb\rc\n", b"plain\n", b"\x00\x01binary\r\n", b"bad \xff utf8\r\n", b"",
                                  ("\u00e9t\u00e9\r\n" * 50_000).encode("utf-8"))):
            p = self.put(f"f{i}", data)
            self.assertEqual(engine.proof_digest_bytes(data), core.proof_digest(p), i)


class EngineTest(TempProject):
    def test_zero_lanes_still_write_a_study(self):
        study = self.study(lanes=[], study_id="S-empty")
        self.assertEqual(study["counts"], {"evidence": 0, "byKind": {}, "secrets": 0})
        folder = self.root / ".blueprint" / "studies" / "S-empty"
        self.assertEqual((folder / "evidence.jsonl").read_text(encoding="utf-8"), "")
        stored = json.loads((folder / "study.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["target"], {"name": "proj", "path": ".", "digest": None, "size": None, "own": True})
        self.assertEqual(stored["authorization"]["statement"], "own project")
        self.assertEqual((stored["tool"], stored["lanes"], stored["kind"]), (TOOL_VERSION, [], "source"))
        self.assertEqual(stored["limits"]["timeLimit"], 300)
        self.assertFalse((folder / ".gitignore").exists())

    def test_lanes_arrive_one_by_one(self):
        self.put("src/a.js", "const a = 1;\n")
        first = self.study(study_id="S-one")
        self.assertEqual(first["lanes"], ["source"])
        both = self.study(study_id="S-two", lanes=[SOURCE, load_stub(STUB_SOURCE.replace('LANE = "source"',
                                                                                          'LANE = "second"'), "s2")])
        self.assertEqual(both["lanes"], ["source", "second"])
        recs = evidence.load(self.root, "S-two")
        self.assertEqual({r["by"] for r in recs}, {f"tool:source@{TOOL_VERSION}"})  # the same fact, the first wins

    def test_records_are_sorted_deterministic_and_carry_the_digest_rule(self):
        self.put("src/a.js", "const a = 1;\r\n")
        self.put("src/b.js", "const b = 2;\n" + FT.SECRETS["aws-key"] + "\n")
        self.study(study_id="S-a")
        one = (self.root / ".blueprint/studies/S-a/evidence.jsonl").read_bytes()
        self.study(study_id="S-a", replace=True)
        two = (self.root / ".blueprint/studies/S-a/evidence.jsonl").read_bytes()
        self.assertEqual(one, two)
        recs = evidence.load(self.root, "S-a")
        self.assertEqual([r["id"] for r in recs], sorted(r["id"] for r in recs))
        by_key = {r["key"]: r for r in recs}
        self.assertEqual(by_key["src/a.js"]["source"]["digest"], core.proof_digest(self.root / "src/a.js"))
        self.assertEqual(by_key["src/a.js"]["source"]["artifact"], ".")
        self.assertIn("aws-key@src/b.js", by_key)
        self.assertEqual(by_key["aws-key@src/b.js"]["data"], {"count": 1})
        self.assertNotIn(FT.SECRETS["aws-key"], one.decode("utf-8"))
        study = evidence.load_study(self.root, "S-a")
        self.assertEqual(study["counts"]["secrets"], 1)

    def test_the_per_kind_and_total_caps_keep_the_lowest_ids_whatever_the_order(self):
        def lane(order):
            class L:
                LANE = "source"

                @staticmethod
                def run(target, em, opts):
                    for i in order:
                        em.emit("cap.a" if i % 3 else "cap.b", f"k{i}", f"Fact {i}.", path=None, digest=None)
            return L
        n = list(range(60))
        with mock.patch.dict(safety.LIMITS, {"evidencePerKind": 7, "evidence": 10}):
            a = self.study(study_id="S-up", lanes=[lane(n)])
            b = self.study(study_id="S-down", lanes=[lane(list(reversed(n)))])
        ra, rb = evidence.load(self.root, "S-up"), evidence.load(self.root, "S-down")
        self.assertEqual([r["id"] for r in ra], [r["id"] for r in rb])
        self.assertEqual(len(ra), 10)
        self.assertLessEqual(max(sum(1 for r in ra if r["kind"] == k) for k in ("cap.a", "cap.b")), 7)
        for s in (a, b):
            self.assertTrue(s["coverage"]["truncated"])
            self.assertEqual(s["coverage"]["stoppedBy"], "evidence-limit")
            self.assertTrue(any("lowest ids" in x for x in s["coverage"]["notes"]))

    def test_a_time_limit_stops_with_truncated_and_keeps_what_was_read(self):
        for i in range(6):
            self.put(f"src/{i}.js", f"x{i}\n")
        ticks = iter(range(0, 10_000, 1))  # one tick per look at the clock: 6 while listing, then one per read
        study = self.study(study_id="S-slow", time_limit=9, clock=lambda: next(ticks))
        self.assertTrue(study["coverage"]["truncated"])
        self.assertEqual(study["coverage"]["stoppedBy"], "time-limit")
        self.assertGreater(len(evidence.load(self.root, "S-slow")), 0)
        self.assertTrue(any("time-limit" in n for n in study["coverage"]["notes"]))

    def test_skips_are_counted_by_reason(self):
        self.put("src/ok.js", "fine\n")
        self.put("src/bin.dat", b"\x00\x01\x02")
        self.put("src/big.txt", b"a" * (safety.LIMITS["textBytes"] + 1))
        study = self.study(study_id="S-skips")
        cov = study["coverage"]
        self.assertEqual(cov["skipped"], {"binary": 1, "too-large": 1})
        self.assertEqual(cov["filesSkipped"], 2)
        self.assertGreaterEqual(cov["filesScanned"], 2)

    def test_a_lane_that_raises_skip_is_recorded_and_a_crash_propagates(self):
        class Skipper:
            LANE = "source"

            @staticmethod
            def run(t, em, o):
                raise safety.Skip("malformed", "the whole target")

        class Broken:
            LANE = "source"

            @staticmethod
            def run(t, em, o):
                raise ZeroDivisionError("a lane bug")
        s = self.study(study_id="S-skip", lanes=[Skipper])
        self.assertEqual(s["coverage"]["skipped"], {"malformed": 1})
        self.assertIn("the whole target", s["coverage"]["notes"])
        with self.assertRaises(ZeroDivisionError):
            self.study(study_id="S-crash", lanes=[Broken])
        self.assertFalse((self.root / ".blueprint/studies/S-crash/study.json").exists())

    def test_ids_and_targets(self):
        with self.assertRaises(BlueprintError) as cm:
            self.study(study_id="S-notes")
        self.assertEqual((cm.exception.error, cm.exception.code), ("usage", 64))
        for bad in ("S-Bad", "S-a\n", "x", "S-" + "a" * 49):
            with self.assertRaises(BlueprintError) as cm:
                self.study(study_id=bad)
            self.assertEqual(cm.exception.error, "usage", bad)
        self.study(study_id="S-same")
        with self.assertRaises(BlueprintError) as cm:
            self.study(study_id="S-same")
        self.assertEqual(cm.exception.error, "study-exists")
        with self.assertRaises(BlueprintError) as cm:
            self.study(target=self.root / "nope")
        self.assertEqual(cm.exception.error, "target-not-found")
        self.put("app.apk", "x")
        with self.assertRaises(BlueprintError) as cm:
            engine.run_study(self.root, "mobile", self.root / "app.apk", own=True, lanes=[MOBILE])
        self.assertEqual(cm.exception.error, "target-unsupported")
        self.assertIn("study import", cm.exception.message)
        for t in (0, -1, 3601, "x", None):
            with self.assertRaises(BlueprintError) as cm:
                self.study(study_id="S-t", time_limit=t)
            self.assertEqual(cm.exception.error, "usage")
        self.assertEqual(engine.default_id(self.put("Acme-3.2.APK", "x")), "S-acme-3-2")

    def test_a_target_that_is_a_link_is_refused(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        link = self.root / "lnk"
        if not FA._link(link, outside, True):  # a symlink, or a junction on Windows
            self.skipTest("cannot make a symlink or junction here")
        with self.assertRaises(BlueprintError) as cm:
            self.study(target=link, own=True)
        self.assertEqual(cm.exception.error, "target-unsupported")

    def test_a_file_outside_the_project_is_stored_by_name_and_digest(self):
        p = FA.make_apk(self.tmp / "acme.apk", FA.SNAGDESK_MANIFEST, None, None, files={"assets/a.txt": "hi"})
        s = engine.run_study(self.root, "mobile", p, own=True, lanes=[MOBILE])
        self.assertEqual(s["id"], "S-acme")
        self.assertEqual(s["target"]["path"], None)
        self.assertEqual(s["target"]["name"], "acme.apk")
        self.assertEqual(s["target"]["digest"], core.sha256_bytes(p.read_bytes()))
        self.assertEqual(s["target"]["size"], p.stat().st_size)
        recs = {r["key"]: r for r in evidence.load(self.root, "S-acme")}
        self.assertEqual(recs["assets/a.txt"]["source"]["artifact"], "acme.apk")
        self.assertEqual(recs["assets/a.txt"]["source"]["digest"], core.sha256_bytes(b"hi"))
        for name, text in project_text(self.root).items():
            self.assertEqual(leaks(text, machine_strings(self.tmp, self.root)), [], name)

    def test_target_read_refuses_paths_out_of_the_folder(self):
        self.put("src/a.txt", "a")
        t = engine.Target(self.root / "src", safety.Budget(), root=self.root)
        self.assertEqual(t.read("a.txt", 10), b"a")
        for bad in ("../.blueprint/blueprint.json", "/etc/passwd", "C:/x", ".", "a\x00b"):
            with self.assertRaises(safety.Skip, msg=bad) as cm:
                t.read(bad, 10)
            self.assertEqual(cm.exception.reason, "unsafe-path")
        with self.assertRaises(safety.Skip):
            t.zip()

    def test_the_study_package_never_runs_or_loads_anything(self):
        for path in sorted((S.PACKAGE / "study").glob("*.py")):
            if path.name not in ("__init__.py", "engine.py", "safety.py", "keys.py"):
                continue
            src = path.read_text(encoding="utf-8")
            for bad in ("import subprocess", "subprocess.", "os.system", "ctypes", "importlib", "git_ls_files", "_git(",
                        "extractall", ".extract("):
                self.assertNotIn(bad, src, f"{path.name}: {bad}")
            self.assertIsNone(re.search(r"(?<![\w.])(exec|eval|compile|__import__)\(", src), path.name)
        import ast
        init = ast.parse((S.PACKAGE / "study" / "__init__.py").read_text(encoding="utf-8"))
        self.assertEqual([type(n).__name__ for n in init.body], ["Expr"])  # a docstring only: it imports nothing


class AuthorizationTest(TempProject):
    def test_only_the_source_lane_on_the_project_or_a_tracked_folder_is_implicitly_own(self):
        a, own = engine.authorize("source", True, False)
        self.assertEqual((a["statement"], a["by"], own), ("own project", None, True))
        self.assertTrue(engine.authorize("source", False, True)[1])
        for kind, root, tracked in (("source", False, False), ("web", True, False), ("mobile", False, True),
                                    ("data", True, True)):
            with self.assertRaises(BlueprintError) as cm:
                engine.authorize(kind, root, tracked)
            self.assertEqual((cm.exception.error, cm.exception.code), ("not-authorized", 3))
            self.assertIn('--authorization "I wrote this app"', cm.exception.message)
        a, own = engine.authorize("mobile", False, False, own=True)
        self.assertEqual((a["statement"], own), ("the user's own software", True))
        a, own = engine.authorize("mobile", False, False, "The vendor's licence allows analysis.")
        self.assertEqual((a["statement"], own), ("The vendor's licence allows analysis.", False))
        with self.assertRaises(BlueprintError) as cm:
            engine.authorize("mobile", False, False, "  I may do it  ")
        self.assertEqual(cm.exception.error, "not-authorized")

    def test_the_statement_is_redacted(self):
        secret = FT.SECRETS["aws-key"]
        mail = "me" + "@" + "example.test"  # built from pieces: no address in this source file
        a, _ = engine.authorize("web", False, False, f"{mail} said so in {self.tmp} with {secret}")
        self.assertNotIn(mail, a["statement"])
        self.assertNotIn(secret, a["statement"])
        self.assertEqual(leaks(a["statement"], machine_strings(self.tmp)), [])

    def test_a_study_that_is_not_own_is_kept_out_of_git(self):
        self.put("sub/a.js", "a\n")
        s = engine.run_study(self.root, "source", self.root / "sub", authorization="The owner of the code agreed.",
                             lanes=[SOURCE])
        gi = self.root / ".blueprint/studies/S-sub/.gitignore"
        self.assertFalse(s["target"]["own"])
        self.assertEqual(gi.read_bytes(), b"*\n")
        engine.run_study(self.root, "source", self.root / "sub", own=True, replace=True, lanes=[SOURCE])
        self.assertFalse(gi.exists())
        with self.assertRaises(BlueprintError) as cm:
            engine.run_study(self.root, "source", self.root / "sub", study_id="S-sub2", lanes=[SOURCE])
        self.assertEqual(cm.exception.code, 3)
        engine.run_study(self.root, "source", self.root / "sub", study_id="S-sub2", lanes=[SOURCE], tracked=True)


class ReplaceTest(TempProject):
    def test_cited_evidence_is_kept_superseded_and_uncited_is_dropped(self):
        self.put("src/a.js", "one\n")
        self.put("src/b.js", "two\n")
        self.study(study_id="S-r")
        recs = {r["key"]: r for r in evidence.load(self.root, "S-r")}
        cited, uncited = recs["src/a.js"]["id"], recs["src/b.js"]["id"]
        plan = json.loads((self.root / ".blueprint/blueprint.json").read_text(encoding="utf-8"))
        plan["unknowns"] = [{"id": "U-a", "evidenceFor": [cited]}]
        (self.root / ".blueprint/blueprint.json").write_text(json.dumps(plan), encoding="utf-8")
        self.put("src/a.js", "one changed\n")
        self.put("src/b.js", "two changed\n")
        self.study(study_id="S-r", replace=True)
        now = {r["id"]: r for r in evidence.load(self.root, "S-r")}
        self.assertIs(now[cited].get("superseded"), True)
        self.assertNotIn(uncited, now)
        self.assertEqual(evidence.load_study(self.root, "S-r")["counts"]["evidence"], 2)
        self.put("src/a.js", "one\n")  # the old bytes again: the same id is emitted, the flag goes
        self.study(study_id="S-r", replace=True)
        self.assertNotIn("superseded", {r["id"]: r for r in evidence.load(self.root, "S-r")}[cited])

    def test_an_unreadable_citation_source_keeps_every_old_record(self):
        self.put("src/a.js", "one\n")
        self.study(study_id="S-k")
        old = {r["id"] for r in evidence.load(self.root, "S-k")}
        (self.root / ".blueprint/blueprint.json").write_text("{broken", encoding="utf-8")
        self.put("src/a.js", "other\n")
        self.study(study_id="S-k", replace=True)
        now = {r["id"]: r for r in evidence.load(self.root, "S-k")}
        self.assertTrue(old <= set(now))
        self.assertTrue(all(now[i].get("superseded") is True for i in old))


class ManifestFactsTest(TempProject):
    def em(self):
        return engine.Emitter("S-m", "app.apk", safety.Budget(), f"tool:mobile@{TOOL_VERSION}")

    def facts(self, em):
        out = {}
        for r in em.records.values():
            out.setdefault(r["kind"], {})[r["key"]] = r["data"]
        return out

    def test_a_text_manifest_gives_the_rows_of_6_7(self):
        root = ET.fromstring(FT._manifest_xml().encode("utf-8"))
        elements = []

        def walk(node, depth):
            elements.append((depth, node.tag, dict(node.attrib)))
            for child in node:
                walk(child, depth + 1)
        walk(root, 0)
        em = self.em()
        engine.manifest_facts(elements, em, path="AndroidManifest.xml", digest=None, by="import:jadx",
                              limitations=["decompiled by JADX"])
        self.assertEqual(self.facts(em), FT.MANIFEST_KINDS)
        self.assertEqual({r["by"] for r in em.records.values()}, {"import:jadx"})
        self.assertTrue(all(r["limitations"] == ["decompiled by JADX"] for r in em.records.values()))

    def test_typed_values_from_a_binary_manifest(self):
        em = self.em()
        engine.manifest_facts(FA.SNAGDESK_MANIFEST, em, path="AndroidManifest.xml", digest="sha256:" + "1" * 64,
                              by=f"tool:mobile@{TOOL_VERSION}")
        f = self.facts(em)
        self.assertEqual(f["mobile.package"], {"com.example.snagdesk": {"versionName": "1.2.3", "versionCode": 42,
                                                                         "minSdk": 24, "targetSdk": 34}})
        self.assertEqual(f["mobile.setting"], {"allowBackup": {"value": "false"}, "usesCleartextTraffic": {"value": "true"}})
        self.assertEqual(f["mobile.screen"]["com.example.snagdesk.MainActivity"], {"exported": True, "launcher": True})
        self.assertEqual(f["mobile.deeplink"], {"https://snagdesk.example.test/t": {
            "activity": "com.example.snagdesk.MainActivity", "autoVerify": True}})
        self.assertEqual(f["mobile.service"]["com.example.snagdesk.FilesProvider"], {"type": "provider", "exported": False})

    def test_value_types_and_odd_input(self):
        self.assertEqual([engine._as_int(v) for v in ("12", 12, "1.2", None, "\u0663")], [12, 12, "1.2", None, "\u0663"])
        self.assertEqual([engine._as_bool(v) for v in ("TRUE", "false", True, "yes", 1, None)],
                         [True, False, True, None, None, None])
        em = self.em()
        self.assertEqual(engine.manifest_facts([None, 5, (0, 1, {}), ("x",), (0, "application", {})], em, path=".",
                                               digest=None, by="import:apktool"), [])
        engine.manifest_facts([(0, "manifest", {}), (1, "uses-permission", {"android:name": 5})], em, path=".",
                              digest=None, by="import:apktool")
        self.assertEqual(em.records, {})
        engine.manifest_facts([(0, "manifest", {"package": "p"}), (1, "application", {}),
                               (2, "activity", {"{http://schemas.android.com/apk/res/android}name": "A"}),
                               (3, "intent-filter", {}), (4, "action", {"android:name": _VIEW}),
                               (4, "category", {"android:name": _BROWSABLE}), (4, "data", {"android:scheme": "app"})],
                              em, path=".", digest=None, by="import:apktool")
        f = self.facts(em)
        self.assertEqual(f["mobile.screen"], {"p.A": {"exported": None, "launcher": False}})
        self.assertEqual(f["mobile.deeplink"], {"app://": {"activity": "p.A", "autoVerify": None}})


_VIEW, _BROWSABLE = "android.intent.action.VIEW", "android.intent.category.BROWSABLE"


class EmitterTest(unittest.TestCase):
    def test_the_first_of_two_records_with_one_id_wins_and_notes_are_clean(self):
        em = engine.Emitter("S-e", ".", safety.Budget(), f"tool:source@{TOOL_VERSION}")
        a = em.emit("x.y", "k", "First claim.", path="a.txt", digest="sha256:" + "a" * 64, data={"n": 1})
        b = em.emit("x.y", "k", "Second claim.", path="a.txt", digest="sha256:" + "a" * 64, data={"n": 2})
        self.assertEqual(a, b)
        self.assertEqual(em.records[a]["claim"], "First claim.")
        em.note("see /home/someone/x and " + FT.SECRETS["aws-key"] + "\nnext line")
        em.note("see /home/someone/x and " + FT.SECRETS["aws-key"] + "\nnext line")
        self.assertEqual(len(em.coverage["notes"]), 1)
        self.assertEqual(leaks(em.coverage["notes"][0]), [])
        self.assertNotIn(FT.SECRETS["aws-key"], em.coverage["notes"][0])
        with self.assertRaises(ValueError):
            em.skip("x", "too-big")
        em.missing("sources/")
        em.missing("sources/")
        records, cov = em.finish()
        self.assertEqual(cov["missing"], ["sources/"])
        self.assertEqual(len(records), 1)


# ------------------------------------------------------------------------------------------------ the CLI

class StudyCommandTest(TempProject):
    def test_a_lane_study_through_the_cli(self):
        self.put("src/app.js", "app.get('/x')\n" + FT.SECRETS["stripe-live"] + "\n")
        r = self.cli("study", "source", self.root, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out["ok"], out["command"]), (True, "study source"))
        study = out["data"]["study"]
        self.assertEqual(study["target"]["own"], True)
        r = self.cli("study", "source", self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("study-exists", self.cli("study", "source", self.root, "--json").stdout)
        r = self.cli("study", "source", self.root, "--id", "S-again")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"^Studied proj \(source\): \d+ evidence records, \d+ files scanned, \d+ skipped"
                                   r".*, 1 secrets redacted\. Next: python ")
        self.assertIn("study synth S-again", r.stdout)
        r = self.cli("study", "list")
        self.assertIn("S-again  source  proj", r.stdout)
        r = self.cli("study", "show", "S-again", "--json")
        self.assertEqual(json.loads(r.stdout)["data"]["study"]["id"], "S-again")
        for name, text in project_text(self.root).items():
            self.assertNotIn(FT.SECRETS["stripe-live"], text, name)
            self.assertNotIn(json.dumps(FT.SECRETS["stripe-live"])[1:-1], text, name)

    def test_authorization_through_the_cli(self):
        apk = FA.make_apk(self.tmp / "vendor.apk", FA.SNAGDESK_MANIFEST, None, None)
        inside = FA.make_apk(self.root / "mine.apk", FA.SNAGDESK_MANIFEST, None, None)
        for target in (apk, inside):
            r = self.cli("study", "mobile", target, "--json")
            self.assertEqual(r.returncode, 3, r.stderr)
            self.assertEqual(json.loads(r.stdout)["error"]["code"], "not-authorized")
        self.assertFalse((self.root / ".blueprint" / "studies").exists())
        r = self.cli("study", "mobile", apk, "--authorization", "The vendor's licence allows analysis.")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Not committed", r.stdout)
        r = self.cli("study", "show", "S-vendor")
        self.assertIn("Not committed: remove .gitignore in the study folder to share it.", r.stdout)
        self.assertEqual(self.cli("study", "mobile", inside, "--own").returncode, 0)
        r = self.cli("study", "mobile", apk, "--own", "--authorization", "both at once is wrong")
        self.assertEqual(r.returncode, 2)
        self.put("sub/a.js", "a\n")
        r = self.cli("study", "source", self.root / "sub", "--json")  # not tracked by git: needs a statement
        self.assertEqual(r.returncode, 3, r.stdout)
        for name, text in project_text(self.root).items():
            self.assertEqual(leaks(text, machine_strings(self.tmp, self.root)), [], name)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_a_folder_git_tracks_is_implicitly_own(self):
        self.put("sub/a.js", "a\n")
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", HOME=str(self.tmp))
        for cmd in (["git", "init", "-q"], ["git", "add", "sub/a.js"]):
            subprocess.run(cmd, cwd=self.root, check=True, capture_output=True, env=env)
        r = self.cli("study", "source", self.root / "sub", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout)["data"]["study"]["authorization"]["statement"], "own project")

    def test_lanes_and_delegated_subcommands_that_are_not_built(self):
        bare = isolated_launcher({})
        for args, ws in ((("study", "web", "x"), "W2-W"), (("study", "source", "."), "W2-S"),
                         (("study", "synth", "S-x"), "W2-SY"), (("study", "import", "x", "--from", "rea"), "W2-T")):
            r = self.cli(*args, "--json", launcher=bare)
            self.assertEqual(r.returncode, 1, args)
            err = json.loads(r.stdout)["error"]
            self.assertEqual(err["code"], "not-built")
            self.assertIn(ws, err["message"])

    def test_a_delegated_subcommand_runs_its_module(self):
        stub = ('from bp.core import emit\n'
                'def command(sub, args, root):\n'
                '    emit(args, args.cmd, {"sub": sub, "study": args.study}, ["ran " + sub])\n'
                '    return 0 if sub == "synth" else 1\n')
        launcher = isolated_launcher({"study/synth.py": stub})
        r = self.cli("study", "synth", "S-x", "--json", launcher=launcher)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["data"], {"sub": "synth", "study": "S-x"})
        self.assertEqual(self.cli("study", "check", "S-x", launcher=launcher).returncode, 1)

    def test_usage_errors_and_help(self):
        r = self.cli("study", "source", self.root, "--time-limit", "0", "--json")
        self.assertEqual(r.returncode, 64)
        self.assertEqual(json.loads(r.stdout)["error"]["code"], "usage")
        r = self.cli("study", "source", self.root / "missing", "--own", "--json")
        self.assertEqual((r.returncode, json.loads(r.stdout)["error"]["code"]), (1, "target-not-found"))
        r = S.run_script(isolated_launcher(LANES), ["study", "--help"], self.root, env=S.tool_env())
        self.assertEqual(r.returncode, 0)
        for sub in ("source", "web", "desktop", "mobile", "data", "trace", "compare", "capture", "import", "synth",
                    "check", "adopt", "list", "show"):
            self.assertIn(sub, r.stdout)
            h = S.run_script(isolated_launcher(LANES), ["study", sub, "--help"], self.root, env=S.tool_env())
            self.assertEqual(h.returncode, 0, sub)
        r = self.cli("study", "show", "S-none", "--json")
        self.assertEqual(json.loads(r.stdout)["error"]["code"], "study-not-found")
        empty = Path(tempfile.mkdtemp(prefix="bp-noproj-"))
        self.addCleanup(shutil.rmtree, empty, True)
        r = S.run_script(isolated_launcher(LANES), ["study", "list", "--json", "--root", str(empty)], empty,
                         env=S.tool_env())
        self.assertEqual(json.loads(r.stdout)["error"]["code"], "no-blueprint")

    def test_files_parse_with_the_python_3_10_grammar(self):
        import ast
        for rel in OWN_FILES:
            ast.parse((S.PACKAGE / rel).read_text(encoding="utf-8"), feature_version=(3, 10))


if __name__ == "__main__":
    unittest.main()
