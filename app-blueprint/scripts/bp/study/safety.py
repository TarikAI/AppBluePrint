"""study.safety: limits, the study budget, safe zip reading, safe folder walking, secret scrubbing and excerpts
(contract 6.2). Every lane and importer reads target bytes through these, so the rules of 6.1 hold in one place:
never follow a link, never extract an archive to disk, count real bytes (never trust a header), store at most 160
characters of any target text, and never store a credential-like value (not even a hash of it).

Imports the standard library and nothing of bp (contract 1.2: core only; it needs none of it). Public names:
LIMITS, SKIP_REASONS, STOP_REASONS, Skip, Stop, Budget, scan_windows, finditer, SafeZip, walk, scrub,
SECRET_PATTERNS, excerpt, one_line, clean_text, decode_text, read_file, is_link, PARSE_ERRORS, UTF8_NOTE, deeper_note,
MISSING, ZIP_MAGIC, ZIP_SUFFIXES.
"""
import io
import json
import os
import plistlib
import re
import sqlite3
import stat
import struct
import time
import xml.etree.ElementTree
import xml.parsers.expat
import zipfile
import zlib

MIB = 1024 * 1024

# Contract 6.2. The keys are camelCase because the effective limits are stored in study.json (6.1, `limits`).
LIMITS = {
    "files": 50_000,                 # files seen per study (Stop "file-limit")
    "textBytes": 5 * MIB,            # a text file a lane reads; larger: skip `too-large`
    "memberBytes": 64 * MIB,         # one archive member (dex files need it)
    "totalBytes": 512 * MIB,         # everything read in one study (Stop "byte-limit")
    "archiveEntries": 50_000,        # entries of one archive; the rest is a `limit` skip
    "ratioBytes": 1 * MIB,           # a member over this size ...
    "ratio": 100,                    # ... and over 100 times its compressed size is skipped (`ratio`)
    "nestedDepth": 2,                # XAPK/APKS -> APK; deeper: `depth`
    "asarHeader": 32 * MIB,          # asar header JSON
    "captureBytes": 5 * MIB,         # a capture file (bad-capture above it)
    "reaBundleBytes": 64 * MIB,      # a rea bundle (target-unsupported above it)
    "evidence": 20_000,              # records per study (the lowest ids are kept, 6.3)
    "evidencePerKind": 2_000,        # records per kind (the lowest ids are kept, 6.3)
    "timeLimit": 300,                # seconds per study (--time-limit, at most timeLimitMax)
    "timeLimitMax": 3_600,
    "excerpt": 160,                  # characters of target text in one excerpt
}

SKIP_REASONS = ("too-large", "binary", "unsupported", "unsafe-path", "encrypted", "symlink", "ratio", "depth",
                "malformed", "limit", "error")  # the closed list of 6.1
STOP_REASONS = ("time-limit", "file-limit", "byte-limit", "evidence-limit")
MISSING = "missing"  # Skip reason of a resolver or SafeZip.read for a member that is not there (freshness: `missing`)

# What a parser may raise on malformed input (6.2): every lane catches these around its parsing.
PARSE_ERRORS = (struct.error, IndexError, ValueError, KeyError, UnicodeError, json.JSONDecodeError,
                plistlib.InvalidFileException, xml.parsers.expat.ExpatError, xml.etree.ElementTree.ParseError,
                sqlite3.DatabaseError, RecursionError)

UTF8_NOTE = "not valid UTF-8; undecodable bytes replaced"  # reading rule 1
ZIP_MAGIC = b"PK\x03\x04"
ZIP_SUFFIXES = (".apk", ".aab", ".apks", ".xapk", ".ipa", ".zip", ".jar")  # the zip family of 5.6


def deeper_note(n):
    """The limitation of reading rule 3 for a walk cut at depth n."""
    return f"nested deeper than {n} levels; deeper levels not read"


class Skip(Exception):
    """A unit (file, member, embedded document) is not read. reason is one of SKIP_REASONS (or MISSING, which only a
    member lookup raises); note is optional text for the study notes."""

    def __init__(self, reason, note=None):
        if reason not in SKIP_REASONS and reason != MISSING:
            raise ValueError(f"unknown skip reason {reason!r}")
        super().__init__(reason if note is None else f"{reason}: {note}")
        self.reason = reason
        self.note = note


class Stop(Exception):
    """The study stops: a study-wide limit was reached. stoppedBy is one of STOP_REASONS."""

    def __init__(self, stoppedBy):  # noqa: N803 (the contract's name)
        if stoppedBy not in STOP_REASONS:
            raise ValueError(f"unknown stop reason {stoppedBy!r}")
        super().__init__(stoppedBy)
        self.stoppedBy = stoppedBy


class Budget:
    """The limits of one study and what has been used. file() counts a file seen, bytes(n) counts bytes read,
    check_time() checks the wall time, evidence(kind) counts a new record; each raises Stop when its limit is passed.
    The clock is injectable for tests. Counters: files_seen, files_scanned, bytes_read, by_kind."""

    MAX_KINDS = 500  # a memory backstop: the lane tables name about 70 kinds

    def __init__(self, limits=None, clock=time.monotonic):
        self.limits = dict(LIMITS)
        self.limits.update(limits or {})
        self.clock = clock
        self.started = clock()
        self.files_seen = 0
        self.files_scanned = 0
        self.bytes_read = 0
        self.by_kind = {}

    def check_time(self):
        if self.clock() - self.started > self.limits["timeLimit"]:
            raise Stop("time-limit")

    def file(self):
        self.check_time()
        if self.files_seen >= self.limits["files"]:
            raise Stop("file-limit")
        self.files_seen += 1

    def bytes(self, n):
        self.bytes_read += max(0, int(n))
        if self.bytes_read > self.limits["totalBytes"]:
            raise Stop("byte-limit")

    def scanned(self):
        self.files_scanned += 1

    def evidence(self, kind):
        """Count one new record of kind. The per-kind and per-study caps keep the records with the lowest ids (6.3),
        which needs every candidate, so passing them does not stop the scan; only an absurd number of kinds does."""
        if kind not in self.by_kind and len(self.by_kind) >= self.MAX_KINDS:
            raise Stop("evidence-limit")
        self.by_kind[kind] = self.by_kind.get(kind, 0) + 1


# ------------------------------------------------------------------------------------------------ text and windows

WINDOW = 64 * 1024
OVERLAP = 1024


def scan_windows(text):
    """(offset, chunk) windows of 64 KiB with 1 KiB of overlap over text. Every regex scan of target text runs per
    window, so no scan is quadratic; use finditer() to get each match once."""
    n = len(text)
    step = WINDOW - OVERLAP
    start = 0
    while True:
        yield start, text[start:start + WINDOW]
        if start + WINDOW >= n:
            return
        start += step


def finditer(rx, text):
    """Every match of rx in text, found window by window, as (global offset, match): each window owns the matches
    that start in its first 63 KiB (the last window owns the rest), so a match in an overlap is reported once, by its
    global offset, and matches never overlap (like re.finditer). A match is at most 1 KiB longer than the text its
    window owns; patterns are bounded (6.2), so this is no limit in practice. m.start() is relative to the window."""
    if isinstance(rx, str):
        rx = re.compile(rx)
    n = len(text)
    last_end = 0
    for off, chunk in scan_windows(text):
        own_end = n if off + len(chunk) >= n else off + len(chunk) - OVERLAP
        for m in rx.finditer(chunk):
            g = off + m.start()
            if g < off or g < last_end:
                continue
            if g >= own_end:
                break
            yield g, m
            last_end = max(off + m.end(), g + 1)


_SURROGATE = re.compile("[\ud800-\udfff]")


def clean_text(text):
    """text with every lone surrogate replaced by U+FFFD, so it can be written as UTF-8."""
    return _SURROGATE.sub("�", text) if _SURROGATE.search(text) else text


def decode_text(data):
    """Reading rule 1: (text, replaced). Skip('binary') when the first 8,192 bytes hold a NUL; a UTF-8 BOM is dropped;
    the rest is decoded as UTF-8 with errors='replace'. replaced is True when anything was replaced: every record from
    that text then gets coverage partial and the limitation UTF8_NOTE."""
    if b"\x00" in data[:8192]:
        raise Skip("binary")
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8"), False
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), True


# ------------------------------------------------------------------------------------------------ secrets

# Contract 6.2 (minimum, extended): every pattern starts with a literal or an alternation of literals. private-key
# also takes the key body that follows the header (a bounded, single character class: linear time), so no part of a
# PEM key is ever stored.
SECRET_PATTERNS = {
    "aws-key": r"AKIA[0-9A-Z]{16}",
    "google-api-key": r"AIza[0-9A-Za-z_\-]{35}",
    "private-key": r"-----BEGIN [A-Z ]{0,64}PRIVATE KEY-----[A-Za-z0-9+/=\s\\-]{0,8192}",
    "jwt": r"eyJ[\w-]{10,}\.eyJ[\w-]{10,}\.[\w-]{10,}",
    "stripe-live": r"[sr]k_live_[0-9A-Za-z]{16,}",
    "slack": r"xox[abprs]-[0-9A-Za-z-]{10,}",
    "github-token": r"gh[pousr]_[0-9A-Za-z]{30,}",
    "bearer": r"(?i)bearer\s+[\w.\-]{16,}",
}
_SECRET_RX = [(name, re.compile(rx)) for name, rx in SECRET_PATTERNS.items()]
# a cheap test before a pattern runs: the literal it starts with (bearer: lower-cased)
_SECRET_HINT = {"aws-key": "AKIA", "google-api-key": "AIza", "private-key": "-----BEGIN", "jwt": "eyJ",
                "stripe-live": "k_live_", "slack": "xox", "github-token": "gh", "bearer": "bearer"}


def scrub(text):
    """(text with every SECRET_PATTERNS match replaced by <secret:NAME>, the names found, one entry per match, in
    pattern order). Patterns run one after another; a long text is scanned window by window."""
    if not isinstance(text, str):
        raise TypeError("scrub() takes text")
    found = []
    lower = None
    for name, rx in _SECRET_RX:
        hint = _SECRET_HINT[name]
        if name == "bearer":
            lower = text.lower() if lower is None else lower
            if hint not in lower:
                continue
        elif hint not in text:
            continue
        if len(text) <= WINDOW:
            n = [0]

            def sub(_m, n=n, name=name):
                n[0] += 1
                return f"<secret:{name}>"
            text = rx.sub(sub, text)
            found += [name] * n[0]
        else:
            spans = [(g, g + len(m.group(0))) for g, m in finditer(rx, text)]
            if spans:
                parts, pos = [], 0
                for s, e in spans:
                    parts.append(text[pos:s])
                    parts.append(f"<secret:{name}>")
                    pos = e
                parts.append(text[pos:])
                text = "".join(parts)
                found += [name] * len(spans)
        lower = None
    return text, found


_BREAKS = "\r\n\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
_CONTROL = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")
_SPACES = re.compile(r"\s+")


def one_line(text):
    """text on one line: control characters and runs of whitespace become one space; trimmed."""
    return _SPACES.sub(" ", _CONTROL.sub(" ", text)).strip()


def excerpt(text, col=0):
    """At most 160 characters of the line of text that holds offset col, starting there, on one line, scrubbed of
    secrets (scrubbed before it is cut, with 4 KiB of the line around it, so a cut never leaves part of a secret).
    "" for no text."""
    if not isinstance(text, str) or not text:
        return ""
    text = clean_text(text)
    col = min(max(0, int(col)), len(text))
    lo, hi = max(0, col - 4096), min(len(text), col + 4096)
    region = text[lo:hi]
    rel = col - lo
    start = max(region.rfind(c, 0, rel) for c in _BREAKS) + 1
    ends = [i for i in (region.find(c, rel) for c in _BREAKS) if i >= 0]
    line = region[start:min(ends) if ends else len(region)]
    rel -= start
    clean, _ = scrub(line)
    at = len(scrub(line[:rel])[0]) if rel else 0
    return one_line(clean[at:])[:LIMITS["excerpt"]].rstrip()


# ------------------------------------------------------------------------------------------------ zip archives

_ZIP_ERRORS = (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, zlib.error, EOFError, ValueError,
               struct.error, OverflowError, IndexError, KeyError, UnicodeError, RecursionError)
_DRIVE = re.compile(r"[A-Za-z]:")


def _unsafe_name(raw):
    """Why a member name is unsafe (6.2): absolute, a drive letter, a '..' part or a NUL; None when it is safe."""
    name = raw.replace("\\", "/")
    if "\x00" in raw:
        return "the member name holds a NUL"
    if name.startswith("/") or _DRIVE.match(name):
        return "the member name is absolute"
    if ".." in name.split("/"):
        return "the member name climbs out with '..'"
    if not name.strip("/"):
        return "the member name is empty"
    return None


class SafeZip:
    """A zip archive read in memory, never extracted (6.2). zipfile is used for the central directory only; each
    member is streamed with ZipFile.open and its real bytes counted. members() lists the readable file members as
    (name, size); skipped holds (name, reason, note) for the others (unsafe-path, symlink, encrypted, ratio,
    malformed for a second member of the same name; limit for entries past 50,000). A broken archive raises
    Skip('malformed'); one nested deeper than the limit Skip('depth'). Every member listed counts as a file seen."""

    def __init__(self, path_or_bytes, budget, depth=0):
        if depth > budget.limits["nestedDepth"]:
            raise Skip("depth", f"an archive nested {depth} levels deep is not read (the limit is "
                                f"{budget.limits['nestedDepth']})")
        self.budget = budget
        self.depth = depth
        self.skipped = []
        self._fh = None
        try:
            if isinstance(path_or_bytes, (bytes, bytearray, memoryview)):
                self._fh = io.BytesIO(bytes(path_or_bytes))
            else:
                self._fh = _open_no_follow(path_or_bytes)
            self._zf = zipfile.ZipFile(self._fh)
            infos = self._zf.infolist()
        except Skip:
            self.close()
            raise
        except _ZIP_ERRORS as e:
            self.close()
            raise Skip("malformed", f"not a readable zip archive ({type(e).__name__})") from None
        except OSError as e:
            self.close()
            raise Skip("error", f"cannot be read ({e.strerror or type(e).__name__})") from None
        try:
            self._list(infos)
        except BaseException:
            self.close()
            raise

    def _list(self, infos):
        budget = self.budget
        limit = budget.limits["archiveEntries"]
        if len(infos) > limit:
            self.skipped.append(("", "limit", f"{len(infos) - limit} archive entries past the {limit}-entry limit "
                                              "were not read"))
            infos = infos[:limit]
        self._members = {}
        order = []
        for info in infos:
            raw = info.orig_filename  # zipfile cuts .filename at a NUL; the full name is only here
            name = raw.replace("\\", "/")
            if name.endswith("/") or info.is_dir():
                continue
            why = _unsafe_name(raw)
            if why:
                self.skipped.append((_shown(name), "unsafe-path", why))
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                self.skipped.append((name, "symlink", None))
                continue
            if info.flag_bits & 0x1:
                self.skipped.append((name, "encrypted", None))
                continue
            if info.file_size > budget.limits["ratioBytes"] and \
                    info.file_size > budget.limits["ratio"] * max(info.compress_size, 1):
                self.skipped.append((name, "ratio", None))
                continue
            if name in self._members:
                self.skipped.append((name, "malformed", "a second member with the same name; the first is read"))
                continue
            budget.file()
            self._members[name] = info
            order.append((name, info.file_size))
        self._order = order

    def members(self):
        return list(self._order)

    def __contains__(self, name):
        return name in self._members

    def read(self, name, cap):
        """The bytes of a member, at most cap: real bytes are counted while streaming (a header that lies does not
        matter); more than cap is Skip('too-large'), a real size past the ratio rule Skip('ratio'), damage
        Skip('malformed'), a name that is not a readable member Skip(MISSING) or the reason it was skipped."""
        info = self._members.get(name)
        if info is None:
            for n, reason, note in self.skipped:
                if n == name:
                    raise Skip(reason, note)
            raise Skip(MISSING)
        self.budget.check_time()
        limits = self.budget.limits
        ratio_cap = max(limits["ratioBytes"], limits["ratio"] * max(info.compress_size, 1))
        out, total = [], 0
        try:
            with self._zf.open(info) as f:
                while True:
                    chunk = f.read(min(65536, cap + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    self.budget.bytes(len(chunk))
                    out.append(chunk)
                    if total > cap:
                        raise Skip("too-large")
                    if total > ratio_cap:
                        raise Skip("ratio")
        except (Skip, Stop):
            raise
        except RuntimeError as e:  # zipfile: "password required" for an encrypted member
            raise Skip("encrypted" if "encrypt" in str(e).lower() or "password" in str(e).lower() else "malformed") \
                from None
        except _ZIP_ERRORS as e:
            raise Skip("malformed", f"member cannot be read ({type(e).__name__})") from None
        except OSError as e:
            raise Skip("error", f"member cannot be read ({e.strerror or type(e).__name__})") from None
        self.budget.scanned()
        return b"".join(out)

    def close(self):
        zf = getattr(self, "_zf", None)
        try:
            if zf is not None:
                zf.close()
        finally:
            if self._fh is not None:
                self._fh.close()
                self._fh = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _shown(name):
    """A member name as it may be shown in a note: control characters as '?'."""
    return re.sub(r"[\x00-\x1f\x7f]", "?", name)


def is_link(path):
    """A symlink, or on Windows any reparse point (a junction)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _open_no_follow(path):
    """Open a file for reading without following a link at its last part (O_NOFOLLOW where it exists; an lstat check
    first everywhere). A link or anything that is not a regular file is Skip('symlink') or Skip('unsupported')."""
    if is_link(path):
        raise Skip("symlink")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Skip("unsupported", "not a regular file")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def read_file(path, cap, budget):
    """The bytes of a regular file, at most cap (real bytes counted; more is Skip('too-large')), never through a
    link. Counts the bytes in budget and the file as scanned."""
    try:
        f = _open_no_follow(path)
    except Skip:
        raise
    except FileNotFoundError:
        raise Skip(MISSING) from None
    except OSError as e:
        raise Skip("error", f"cannot be read ({e.strerror or type(e).__name__})") from None
    out, total = [], 0
    with f:
        try:
            if os.fstat(f.fileno()).st_size > cap:
                raise Skip("too-large")
            while True:
                chunk = f.read(min(1 << 20, cap + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                budget.bytes(len(chunk))
                out.append(chunk)
                if total > cap:
                    raise Skip("too-large")
        except OSError as e:
            raise Skip("error", f"cannot be read ({e.strerror or type(e).__name__})") from None
    budget.scanned()
    return b"".join(out)


# ------------------------------------------------------------------------------------------------ folders

def _inside(real, base):
    return real == base or real.startswith(base.rstrip(os.sep) + os.sep)


def walk(root, budget, skip=None, prune=()):
    """The files of a folder as sorted relative paths with forward slashes (6.2): os.walk without following links;
    symlinked files and folders are skipped (symlink), and so is any folder whose real path differs from its path
    (Windows junctions, loops); a path whose real path leaves the folder is unsafe-path; anything that is not a regular
    file is unsupported. skip(rel, reason, note) is told about each. Folders named in prune (relative paths) are
    left out silently. Every file counts in budget.file(); when the file limit stops the walk, the files found so far
    are yielded (sorted) and then Stop is raised."""
    base = os.path.realpath(root)
    base_key = os.path.normcase(base)
    found, stop = [], None

    def report(rel, reason, note=None):
        if skip is not None:
            skip(rel, reason, note)

    def onerror(e):
        rel = os.path.relpath(getattr(e, "filename", None) or base, base).replace(os.sep, "/")
        report(rel, "error", f"cannot be listed ({e.strerror or type(e).__name__})")

    try:
        for dirpath, dirnames, filenames in os.walk(base, topdown=True, onerror=onerror, followlinks=False):
            keep = []
            for d in sorted(dirnames):
                full = os.path.join(dirpath, d)
                rel = os.path.relpath(full, base).replace(os.sep, "/")
                if rel in prune:
                    continue
                if is_link(full) or getattr(os.path, "isjunction", lambda p: False)(full):
                    report(rel, "symlink")
                    continue
                real = os.path.normcase(os.path.realpath(full))
                if real != os.path.normcase(os.path.abspath(full)):
                    report(rel, "symlink")
                    continue
                if not _inside(real, base_key):
                    report(rel, "unsafe-path")
                    continue
                keep.append(d)
            dirnames[:] = keep
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, base).replace(os.sep, "/")
                try:
                    st = os.lstat(full)
                except OSError as e:
                    report(rel, "error", f"cannot be read ({e.strerror or type(e).__name__})")
                    continue
                if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
                    report(rel, "symlink")
                    continue
                if not stat.S_ISREG(st.st_mode):
                    report(rel, "unsupported", "not a regular file")
                    continue
                if not _inside(os.path.normcase(os.path.realpath(full)), base_key):
                    report(rel, "unsafe-path")
                    continue
                budget.file()
                found.append(rel)
    except Stop as e:
        stop = e
    yield from sorted(found)
    if stop is not None:
        raise stop
