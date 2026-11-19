"""Synthetic archive fixtures (contract 10.2, workstream W2-FX1). Standard library only; nothing is downloaded.

Every builder writes small, invented files (package `com.example.snagdesk`, endpoints under `api.example.test`) and is
deterministic: the same call gives byte-identical output on every platform and Python version. To get that, the zip
container is written by hand (fixed 1980-01-01 timestamps, fixed member order, no `zlib` output: members are stored or
deflated by the tiny encoders below, whose bytes do not depend on the zlib build). No parser code is used here: the
encoders follow the format documentation and the goldens in `tests/data/android/` (made by aapt2 36.0.0).

Public API (the signatures below are final; 10.2 names are marked [10.2], additions are marked [+]):

  bytes builders
    make_axml(elements) -> bytes                         [10.2] binary AndroidManifest (AXML), UTF-16 pool
    make_proto_manifest(elements, text=True) -> bytes    [10.2] aapt2 proto XmlNode (AAB manifest)
    make_dex(strings, types) -> bytes                    [10.2] dex 035 with adler32 and sha1 filled in
    apk_bytes / aab_bytes / ipa_bytes / xapk_bytes / asar_bytes                                  [+] same as make_*, returning bytes
    zip_bytes(entries, comment=b"") -> bytes             [+] the deterministic zip writer; entries are ZipEntry
  file builders (all return the Path they wrote)
    make_apk(path, manifest, dex_strings, dex_types, files=None)                                  [10.2]
    make_aab(path, manifest, dex_strings, dex_types, files=None)                                  [10.2]
    make_xapk(path, apks, files=None)                                                             [10.2]
    make_ipa(path, info, files=None)                                                              [10.2]
    make_asar(path, files, unpacked=(), links=None)                                               [10.2]
    make_zip_slip(path)  make_absolute_path_zip(path)  make_zip_bomb(path, size=2 MiB)            [10.2 / +]
    make_many_entries_zip(path, count=50_001)  make_nested_zip(path, depth=3)                     [+]
    make_symlink_zip(path)  make_encrypted_member_zip(path)  make_duplicate_names_zip(path)       [10.2 / +]
    make_oversized_member_zip(path, size=64 MiB + 1)  make_truncated_zip(path)                    [+]
    make_asar_hostile(path, kind)                                                                 [+]

Choices where 10.2 left the signature open
  * `elements` is `[(depth, name, {attr: value})]`, depth 0 is the single root. Attribute keys: `"android:name"` is in the
    android namespace (and gets its resource id from RES_IDS); `"package"` has no namespace and no id; `"tools:x"` and
    `"app:x"` use the namespaces in NAMESPACES; `"android:@0x01010003"` writes an attribute whose name string is EMPTY
    and whose identity is only its resource id (what an obfuscated manifest looks like; the parser falls back to the
    resource map). Values: str, bool, int (decimal), Hex(int), Ref(id[, "@android:style/X"]).
  * Attributes are ordered like aapt2 orders them: those with a resource id first, by id, then the others in the order
    given. The string pool is aapt2's too: the names of id attributes first, in id order (so pool index = resource map
    index), then every other string, sorted.
  * `make_dex` returns the SORTED, de-duplicated union of `strings` and `types` as its string table (the dex format
    requires sorted ids), so `strings(dex)` is that union, not the input order. `types` must be type descriptors
    (`Lcom/example/Foo;`, `I`, `[B`); they are added to the string table automatically. No classes, methods or fields.
  * `manifest` of make_apk / make_aab is an `elements` list or ready-made bytes (use bytes to embed the aapt2 goldens).
    `files` maps member names to bytes (or str, UTF-8). make_apk's optional members are plain `files` entries, for
    example `{"assets/index.android.bundle": SAMPLE_BUNDLE, FLUTTER_LIB: b""}`. Setting `dex_strings=None` omits the dex.
  * Members are stored unless noted: the manifest, the dex and `main.jsbundle` are deflated (with this module's own
    literal-only deflate encoder, so the bytes never depend on the zlib build); every other member is stored.
  * make_xapk(path, apks): `apks` maps a member name to apk bytes or to the path of an apk; `files` adds other members
    (for example a `manifest.json`).
  * make_ipa(path, info, files): `info` is the Info.plist dict (written as a binary plist); `files` keys are relative to
    `Payload/Snag.app/`; `main.jsbundle` and `embedded.mobileprovision` get defaults unless `files` overrides them.
  * make_asar: `files` maps a slash-separated relative path to bytes or str; `unpacked` is a set of those paths whose data
    goes to the sibling `<asar>.unpacked/` folder instead of the archive; `links` maps a path to a link target.
  * Hostile zips: the zip bomb is one 2 MiB member of zeros of about 2 KB on disk (hand-built dynamic Huffman block).
    `make_nested_zip(path, depth=3)` nests `depth` zip levels below the outer file (the safety limit is 2, so 3 is over).
    `make_oversized_member_zip` is stored (not compressible, so the ratio rule does not fire first) and by default 1 byte
    over the 64 MiB member cap: it is big on disk, build it only when that is the point, or pass a smaller `size`.
"""
import hashlib
import json
import os
import plistlib
import struct
import zlib
from pathlib import Path

# --------------------------------------------------------------------------------------------------------- constants

PACKAGE = "com.example.snagdesk"
FLUTTER_LIB = "lib/arm64-v8a/libflutter.so"
FAKE_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"  # scan-private: allow  (the published AWS documentation example, not a credential)
APPLE_APP_DIR = "Payload/Snag.app/"

NAMESPACES = {
    "android": "http://schemas.android.com/apk/res/android",
    "app": "http://schemas.android.com/apk/res-auto",
    "tools": "http://schemas.android.com/tools",
}

# android: attribute resource ids. Every entry was checked against `aapt2 dump xmltree` output of a manifest using it
# (see test_res_ids_match_aapt2, skipped when the tool is absent); the 6.7 table is a subset of this one.
RES_IDS = {
    "theme": 0x01010000, "label": 0x01010001, "icon": 0x01010002, "name": 0x01010003, "permission": 0x01010006,
    "protectionLevel": 0x01010009, "debuggable": 0x0101000F, "enabled": 0x0101000E, "exported": 0x01010010,
    "process": 0x01010011, "taskAffinity": 0x01010012, "authorities": 0x01010018, "priority": 0x0101001C,
    "launchMode": 0x0101001D, "screenOrientation": 0x0101001E, "configChanges": 0x0101001F, "mimeType": 0x01010026,
    "scheme": 0x01010027, "host": 0x01010028, "port": 0x01010029, "path": 0x0101002A, "pathPrefix": 0x0101002B,
    "pathPattern": 0x0101002C, "value": 0x01010024, "resource": 0x01010025, "minSdkVersion": 0x0101020C,
    "versionCode": 0x0101021B, "versionName": 0x0101021C, "maxSdkVersion": 0x01010271, "required": 0x0101028E,
    "targetSdkVersion": 0x01010270, "allowBackup": 0x01010280, "extractNativeLibs": 0x010104EA,
    "usesCleartextTraffic": 0x010104EC, "autoVerify": 0x010104EE, "networkSecurityConfig": 0x01010527,
    "roundIcon": 0x0101052C, "compileSdkVersion": 0x01010572, "compileSdkVersionCodename": 0x01010573,
}

# the golden source manifest (tests/data/android/AndroidManifest.xml) as `elements`, before aapt2 adds its attributes
SNAGDESK_MANIFEST = None  # defined after Ref / Hex below
SNAGDESK_STRINGS = None
SNAGDESK_TYPES = None

SAMPLE_BUNDLE = None  # defined below

# RES_XML_TYPE chunk types and value types (public AOSP constants)
_RES_XML = 0x0003
_RES_STRING_POOL = 0x0001
_RES_XML_RESOURCE_MAP = 0x0180
_RES_NS_START, _RES_NS_END = 0x0100, 0x0101
_RES_ELEM_START, _RES_ELEM_END = 0x0102, 0x0103
_T_REFERENCE, _T_STRING, _T_INT_DEC, _T_INT_HEX, _T_BOOL = 0x01, 0x03, 0x10, 0x11, 0x12
_NONE = 0xFFFFFFFF


class Ref:
    """A resource reference value; `name` (like "@android:style/Theme.Material") is only used by the proto encoder."""

    def __init__(self, resid, name=None):
        self.resid, self.name = resid, name

    def __repr__(self):
        return f"Ref({self.resid:#010x})"


class Hex(int):
    """An integer written as hexadecimal (data type 0x11), for example android:configChanges."""


SNAGDESK_MANIFEST = [
    (0, "manifest", {"package": PACKAGE, "android:versionCode": 42, "android:versionName": "1.2.3"}),
    (1, "uses-sdk", {"android:minSdkVersion": 24, "android:targetSdkVersion": 34}),
    (1, "uses-permission", {"android:name": "android.permission.INTERNET"}),
    (1, "uses-permission", {"android:name": "android.permission.CAMERA"}),
    (1, "application", {"android:label": "SnagDesk", "android:allowBackup": False,
                        "android:usesCleartextTraffic": True,
                        "android:theme": Ref(0x01030224, "@android:style/Theme.Material")}),
    (2, "activity", {"android:name": ".MainActivity", "android:exported": True}),
    (3, "intent-filter", {}),
    (4, "action", {"android:name": "android.intent.action.MAIN"}),
    (4, "category", {"android:name": "android.intent.category.LAUNCHER"}),
    (3, "intent-filter", {"android:autoVerify": True}),
    (4, "action", {"android:name": "android.intent.action.VIEW"}),
    (4, "category", {"android:name": "android.intent.category.DEFAULT"}),
    (4, "category", {"android:name": "android.intent.category.BROWSABLE"}),
    (4, "data", {"android:scheme": "https", "android:host": "snagdesk.example.test", "android:pathPrefix": "/t"}),
    (2, "activity", {"android:name": "com.example.snagdesk.ui.SettingsActivity", "android:exported": False}),
    (2, "service", {"android:name": ".SyncService", "android:exported": False}),
    (2, "receiver", {"android:name": ".BootReceiver", "android:exported": True}),
    (2, "provider", {"android:name": ".FilesProvider", "android:authorities": "com.example.snagdesk.files",
                     "android:exported": False}),
]
SNAGDESK_TYPES = ["Lcom/example/snagdesk/MainActivity;", "Lcom/example/snagdesk/ui/SettingsActivity;",
                  "Lcom/example/snagdesk/SyncService;", "Lokhttp3/OkHttpClient;", "Lretrofit2/Retrofit;",
                  "Lio/sentry/Sentry;", "Lcom/google/firebase/FirebaseApp;"]
SNAGDESK_STRINGS = ["https://api.example.test/v1/items", "https://api.example.test/v1/auth/login", "/api/tickets",
                    "/v2/search", "snagdesk://open", "Hello from SnagDesk"]
SAMPLE_BUNDLE = (
    '"use strict";var BASE="https://api.example.test/v1";\n'
    'function load(){return fetch(BASE+"/items",{method:"GET"})}\n'
    'function login(u){return fetch("https://api.example.test/v1/auth/login",{method:"POST",body:u})}\n'
    f'var cfg={{awsKey:"{FAKE_AWS_KEY}",flag:"new-checkout"}};  // scan-private: allow\n'
    'module.exports={load:load,login:login,cfg:cfg};\n'
)


# --------------------------------------------------------------------------------------------------- small helpers

def _b(data):
    return data.encode("utf-8") if isinstance(data, str) else bytes(data)


def _write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _u32(*v):
    return struct.pack("<%dI" % len(v), *v)


def _pad4(n):
    return (-n) % 4


def _check_elements(elements):
    out, prev = [], -1
    for el in elements:
        depth, name, attrs = el
        if not isinstance(depth, int) or depth < 0 or depth > prev + 1 or (prev == -1 and depth != 0):
            raise ValueError(f"element depth {depth!r} after {prev}: depth starts at 0 and grows by at most 1")
        if depth == 0 and out:
            raise ValueError("exactly one root element (depth 0)")
        out.append((depth, name, dict(attrs)))
        prev = depth
    if not out:
        raise ValueError("elements is empty")
    return out


def _attr_key(key):
    """'android:name' -> (prefix, local, resid or None, empty_name). See the module docstring."""
    prefix, _, local = key.rpartition(":")
    prefix = prefix or None
    if prefix is not None and prefix not in NAMESPACES:
        raise ValueError(f"unknown namespace prefix in {key!r}")
    if local.startswith("@0x"):
        return prefix, "", int(local[1:], 16), True
    resid = RES_IDS.get(local) if prefix == "android" else None
    return prefix, local, resid, False


# ----------------------------------------------------------------------------------------------------- AXML encoder

def _utf16_string(s):
    units = len(s.encode("utf-16-le")) // 2
    head = struct.pack("<H", units) if units < 0x8000 else struct.pack("<HH", 0x8000 | (units >> 16), units & 0xFFFF)
    return head + s.encode("utf-16-le") + b"\0\0"


def _string_pool(strings):
    blobs, offs, pos = [], [], 0
    for s in strings:
        offs.append(pos)
        blob = _utf16_string(s)
        blobs.append(blob)
        pos += len(blob)
    body = b"".join(blobs)
    body += b"\0" * _pad4(len(body))
    start = 28 + 4 * len(strings)
    size = start + len(body)
    return (struct.pack("<HHI", _RES_STRING_POOL, 28, size) + _u32(len(strings), 0, 0, start, 0)
            + _u32(*offs) + body)


def make_axml(elements):
    """Binary AndroidManifest (AXML) for `elements`; layout as documented in contract 6.7 and as aapt2 36.0.0 writes it."""
    elems = _check_elements(elements)
    prefixes, id_names, rest = set(), {}, set()
    parsed = []
    for depth, name, attrs in elems:
        rest.add(name)
        row = []
        for key, value in attrs.items():
            prefix, local, resid, _ = _attr_key(key)
            if prefix:
                prefixes.add(prefix)
            if resid is not None:
                if id_names.setdefault(resid, local) != local:
                    raise ValueError(f"resource id {resid:#x} used with two names")
            elif local:
                rest.add(local)
            if isinstance(value, str):
                rest.add(value)
            row.append((prefix, local, resid, value))
        # aapt2 order: attributes with a resource id first (by id), then the others as given
        row = sorted((r for r in row if r[2] is not None), key=lambda r: r[2]) + [r for r in row if r[2] is None]
        parsed.append((depth, name, row))
    ns_list = sorted(prefixes)
    for p in ns_list:
        rest.update((p, NAMESPACES[p]))
    ids_sorted = sorted(id_names)
    pool = [id_names[i] for i in ids_sorted]
    rest_sorted = sorted(rest)
    index = {s: len(pool) + n for n, s in enumerate(rest_sorted)}
    pool += rest_sorted
    id_index = {rid: n for n, rid in enumerate(ids_sorted)}

    def name_idx(local, resid):
        return id_index[resid] if resid is not None else index[local]

    def node(kind, line, size, *fields):
        return struct.pack("<HHI", kind, 16, size) + _u32(line, _NONE, *fields)

    chunks = [_string_pool(pool)]
    if ids_sorted:
        chunks.append(struct.pack("<HHI", _RES_XML_RESOURCE_MAP, 8, 8 + 4 * len(ids_sorted)) + _u32(*ids_sorted))
    for p in ns_list:
        chunks.append(node(_RES_NS_START, 2, 24, index[p], index[NAMESPACES[p]]))
    stack = []
    for n, (depth, name, row) in enumerate(parsed):
        line = 2 + n
        del stack[depth:]
        stack.append((name, line))
        body = b""
        for prefix, local, resid, value in row:
            ns = index[NAMESPACES[prefix]] if prefix else _NONE
            if isinstance(value, bool):
                raw, dtype, data = _NONE, _T_BOOL, _NONE if value else 0
            elif isinstance(value, Ref):
                raw, dtype, data = _NONE, _T_REFERENCE, value.resid
            elif isinstance(value, Hex):
                raw, dtype, data = _NONE, _T_INT_HEX, int(value) & _NONE
            elif isinstance(value, int):
                raw, dtype, data = _NONE, _T_INT_DEC, value & _NONE
            elif isinstance(value, str):
                raw, dtype, data = index[value], _T_STRING, index[value]
            else:
                raise TypeError(f"attribute {local!r}: unsupported value {value!r}")
            body += _u32(ns, name_idx(local, resid), raw) + struct.pack("<HBBI", 8, 0, dtype, data)
        chunks.append(node(_RES_ELEM_START, line, 36 + len(body), _NONE, index[name]) + struct.pack(
            "<HHHHHH", 20, 20, len(row), 0, 0, 0) + body)
        nxt = parsed[n + 1][0] if n + 1 < len(parsed) else 0
        # close every element that ends before the next one starts
        for d in range(depth, nxt - 1, -1):
            chunks.append(node(_RES_ELEM_END, line, 24, _NONE, index[stack[d][0]]))
    for p in reversed(ns_list):
        chunks.append(node(_RES_NS_END, 2 + len(parsed), 24, index[p], index[NAMESPACES[p]]))
    body = b"".join(chunks)
    return struct.pack("<HHI", _RES_XML, 8, 8 + len(body)) + body


# --------------------------------------------------------------------------------------------- proto (AAB) manifest

def _varint(n):
    n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _pv(field, n):  # varint field
    return _varint(field << 3) + _varint(n)


def _pl(field, data):  # length-delimited field
    data = _b(data)
    return _varint(field << 3 | 2) + _varint(len(data)) + data


def _source(line, col=0):
    return _pv(1, line) + (_pv(2, col) if col else b"")


def make_proto_manifest(elements, text=True):
    """aapt2 `XmlNode` bytes (what `aapt2 link --proto-format` writes for AndroidManifest.xml; field numbers in 6.7).

    Source positions are filled like aapt2 does (line = element number + 2, column = 4 x depth). With `text=True`
    whitespace text nodes are written between elements, as aapt2 does."""
    elems = _check_elements(elements)
    prefixes = sorted({_attr_key(k)[0] for _, _, a in elems for k in a if _attr_key(k)[0]})

    def attr(prefix, local, resid, empty, value, line):
        if empty:
            raise ValueError("an empty attribute name cannot be written in the proto format")
        if isinstance(value, bool):
            text_value, item = ("true" if value else "false"), _pl(7, _pv(8, int(value)))
        elif isinstance(value, Ref):
            text_value = value.name or f"@0x{value.resid:08x}"
            item = _pl(1, _pv(2, value.resid) + (_pl(3, value.name[1:]) + _pv(6, 1) if value.name else b""))
        elif isinstance(value, Hex):
            text_value, item = f"0x{int(value):x}", _pl(7, _pv(7, int(value)))
        elif isinstance(value, int):
            text_value, item = str(value), _pl(7, _pv(6, value))
        elif isinstance(value, str):
            text_value, item = value, b""
        else:
            raise TypeError(f"attribute {local!r}: unsupported value {value!r}")
        out = (_pl(1, NAMESPACES[prefix]) if prefix else b"") + _pl(2, local) + _pl(3, text_value) + _pl(4, _source(line))
        out += _pv(5, resid) if resid is not None else b""
        return out + (_pl(6, item) if item else b"")

    def encode(i):
        depth, name, attrs = elems[i]
        line = 2 + i
        el = b""
        if depth == 0:
            for p in prefixes:
                el += _pl(1, _pl(1, p) + _pl(2, NAMESPACES[p]) + _pl(3, _source(line)))
        el += _pl(3, name)
        for key, value in attrs.items():
            prefix, local, resid, empty = _attr_key(key)
            el += _pl(4, attr(prefix, local, resid, empty, value, line))
        j, children = i + 1, []
        while j < len(elems) and elems[j][0] > depth:
            if elems[j][0] == depth + 1:
                children.append(j)
            j += 1
        for c in children:
            if text:
                el += _pl(5, _pl(2, "\n" + "    " * (depth + 1)) + _pl(3, _source(2 + c - 1, 4 * (depth + 1))))
            el += _pl(5, encode(c))
        if text and children:
            el += _pl(5, _pl(2, "\n" + "    " * depth) + _pl(3, _source(2 + i, 4 * depth)))
        return _pl(1, el) + _pl(3, _source(line, 4 * depth))

    return encode(0)


# ------------------------------------------------------------------------------------------------------ dex writer

def _uleb(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _mutf8(s):
    """(utf16 unit count, MUTF-8 bytes): NUL is C0 80 and each surrogate is its own 3-byte sequence."""
    data = s.encode("utf-16-le", "surrogatepass")
    units = struct.unpack("<%dH" % (len(data) // 2), data)
    out = bytearray()
    for u in units:
        if 1 <= u <= 0x7F:
            out.append(u)
        elif u <= 0x7FF:
            out += bytes((0xC0 | u >> 6, 0x80 | u & 0x3F))
        else:
            out += bytes((0xE0 | u >> 12, 0x80 | (u >> 6) & 0x3F, 0x80 | u & 0x3F))
    return len(units), bytes(out)


def make_dex(strings, types):
    """A dex 035 file with `strings` and `types` (descriptors) and no classes; checksum and signature are valid."""
    for t in types:
        if not t or t[0] not in "LVZBSCIJFD[":
            raise ValueError(f"{t!r} is not a type descriptor")
    table = sorted(set(strings) | set(types), key=lambda s: s.encode("utf-16-be", "surrogatepass"))
    sidx = {s: i for i, s in enumerate(table)}
    type_ids = sorted({sidx[t] for t in types})
    n_str, n_type = len(table), len(type_ids)
    str_off = 0x70
    type_off = str_off + 4 * n_str
    data_off = type_off + 4 * n_type
    data, offsets = bytearray(), []
    for s in table:
        offsets.append(data_off + len(data))
        count, enc = _mutf8(s)
        data += _uleb(count) + enc + b"\0"
    data += b"\0" * _pad4(data_off + len(data))
    map_off = data_off + len(data)
    items = [(0x0000, 1, 0)]
    if n_str:
        items += [(0x0001, n_str, str_off)]
    if n_type:
        items += [(0x0002, n_type, type_off)]
    if n_str:
        items += [(0x2002, n_str, offsets[0])]
    items += [(0x1000, 1, map_off)]
    items.sort(key=lambda it: it[2])
    data += struct.pack("<I", len(items)) + b"".join(struct.pack("<HHII", t, 0, c, o) for t, c, o in items)
    file_size = data_off + len(data)
    head = bytearray(0x70)
    head[0:8] = b"dex\n035\0"
    struct.pack_into("<IIIII", head, 0x20, file_size, 0x70, 0x12345678, 0, 0)  # size, header size, endian, link
    struct.pack_into("<I", head, 0x34, map_off)
    struct.pack_into("<IIII", head, 0x38, n_str, str_off if n_str else 0, n_type, type_off if n_type else 0)
    struct.pack_into("<II", head, 0x68, file_size - data_off, data_off)
    body = bytes(head) + b"".join(struct.pack("<I", o) for o in offsets) + b"".join(
        struct.pack("<I", i) for i in type_ids) + bytes(data)
    sig = hashlib.sha1(body[0x20:]).digest()
    body = body[:0x0C] + sig + body[0x20:]
    return body[:8] + struct.pack("<I", zlib.adler32(body[0x0C:]) & 0xFFFFFFFF) + body[0x0C:]


# ----------------------------------------------------------------------------------------- deflate (no zlib output)

class _Bits:
    def __init__(self):
        self.out, self.acc, self.n = bytearray(), 0, 0

    def put(self, value, count):  # LSB first
        self.acc |= value << self.n
        self.n += count
        while self.n >= 8:
            self.out.append(self.acc & 0xFF)
            self.acc >>= 8
            self.n -= 8

    def code(self, value, count):  # Huffman codes are packed MSB first
        self.put(int(format(value, f"0{count}b")[::-1], 2) if count else 0, count)

    def done(self):
        if self.n:
            self.out.append(self.acc & 0xFF)
        return bytes(self.out)


def _deflate_literals(data):
    """A valid raw deflate stream: one final block, fixed Huffman codes, literals only (the output never varies)."""
    w = _Bits()
    w.put(1, 1)
    w.put(1, 2)
    for byte in data:
        if byte < 144:
            w.code(0x30 + byte, 8)
        else:
            w.code(0x190 + byte - 144, 9)
    w.code(0, 7)  # end of block
    return w.done()


def _deflate_zero_run(n):
    """A raw deflate stream for `n` zero bytes in about n/1000 bytes: one dynamic block holding literal 0 (code 10),
    length-258 matches at distance 1 (code 0 and distance code 0) and the end of block (code 11)."""
    w = _Bits()
    w.put(1, 1)
    w.put(2, 2)
    w.put(29, 5)  # HLIT: 286 literal/length codes
    w.put(0, 5)  # HDIST: 1 distance code
    w.put(14, 4)  # HCLEN: 18 code-length codes, down to symbol 1
    for sym_len in (0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 0, 2):  # order 16,17,18,0,8,7,9,6,10,5,11,4,12,3,13,2,14,1
        w.put(sym_len, 3)
    cl = {18: (0, 1), 1: (2, 2), 2: (3, 2)}  # code-length symbol -> (code, bits)

    def sym(s):
        w.code(*cl[s])

    sym(2)  # literal 0 has length 2
    sym(18), w.put(138 - 11, 7)  # symbols 1..138 unused
    sym(18), w.put(117 - 11, 7)  # symbols 139..255 unused
    sym(2)  # end of block (256) has length 2
    sym(18), w.put(28 - 11, 7)  # symbols 257..284 unused
    sym(1)  # symbol 285 (length 258) has length 1
    sym(1)  # the one distance code has length 1
    w.code(2, 2)  # literal 0
    left = n - 1
    while left >= 258:
        w.code(0, 1)  # length 258
        w.code(0, 1)  # distance 1
        left -= 258
    for _ in range(left):
        w.code(2, 2)
    w.code(3, 2)  # end of block
    return w.done()


# ------------------------------------------------------------------------------------------------- zip container

class ZipEntry:
    """One member for zip_bytes. `method=None` picks deflate when it is smaller, else stored. `compressed`/`usize`/`crc`
    override what is written to the headers (hostile fixtures); `symlink=True` sets the Unix link mode."""

    def __init__(self, name, data=b"", *, method=None, flags=0, mode=0o100644, symlink=False, compressed=None,
                 usize=None, crc=None):
        self.name = name if isinstance(name, bytes) else name.encode("utf-8")
        self.data = _b(data)
        self.method, self.flags = method, flags
        self.external = ((0o120777 if symlink else mode) & 0xFFFF) << 16
        self.compressed, self.usize, self.crc = compressed, usize, crc
        if not isinstance(name, bytes) and any(ord(c) > 127 for c in name):
            self.flags |= 0x800


def _zip_payload(e):
    crc = zlib.crc32(e.data) & 0xFFFFFFFF if e.crc is None else e.crc
    usize = len(e.data) if e.usize is None else e.usize
    if e.compressed is not None:
        return (e.method if e.method is not None else 8), e.compressed, crc, usize
    method = e.method
    if method in (None, 8) and e.data:
        packed = _deflate_literals(e.data)
        if method == 8 or len(packed) < len(e.data):
            return 8, packed, crc, usize
    return 0, e.data, crc, usize


def zip_bytes(entries, comment=b""):
    """A zip file from ZipEntry objects, in the order given; no Zip64 (at most 65,535 entries, 4 GiB)."""
    if len(entries) > 0xFFFF:
        raise ValueError("more than 65,535 entries needs Zip64")
    out, central = bytearray(), bytearray()
    for e in entries:
        method, payload, crc, usize = _zip_payload(e)
        need = 20 if method == 8 else 10
        offset = len(out)
        out += struct.pack("<IHHHHHIIIHH", 0x04034B50, need, e.flags, method, 0, 0x21, crc, len(payload), usize,
                           len(e.name), 0) + e.name + payload
        central += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 3 << 8 | 20, need, e.flags, method, 0, 0x21, crc,
                               len(payload), usize, len(e.name), 0, 0, 0, 0, e.external, offset) + e.name
    out += central
    out += struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(entries), len(entries), len(central),
                       len(out) - len(central), len(comment)) + comment
    return bytes(out)


def _members(files):
    return [ZipEntry(name, data) for name, data in sorted((files or {}).items())]


# ---------------------------------------------------------------------------------------- android and ios archives

def _manifest_bytes(manifest, encoder):
    return bytes(manifest) if isinstance(manifest, (bytes, bytearray)) else encoder(manifest)


def apk_bytes(manifest, dex_strings, dex_types, files=None):
    entries = [ZipEntry("AndroidManifest.xml", _manifest_bytes(manifest, make_axml), method=8)]
    if dex_strings is not None:
        entries.append(ZipEntry("classes.dex", make_dex(dex_strings, dex_types or []), method=8))
    return zip_bytes(entries + _members(files))


def make_apk(path, manifest, dex_strings, dex_types, files=None):
    """zip: AndroidManifest.xml (AXML), classes.dex, then `files` sorted by name."""
    return _write(path, apk_bytes(manifest, dex_strings, dex_types, files))


def aab_bytes(manifest, dex_strings, dex_types, files=None):
    entries = [ZipEntry("BundleConfig.pb", b""),
               ZipEntry("base/manifest/AndroidManifest.xml", _manifest_bytes(manifest, make_proto_manifest), method=8)]
    if dex_strings is not None:
        entries.append(ZipEntry("base/dex/classes.dex", make_dex(dex_strings, dex_types or []), method=8))
    return zip_bytes(entries + _members(files))


def make_aab(path, manifest, dex_strings, dex_types, files=None):
    """zip: BundleConfig.pb (empty), base/manifest/AndroidManifest.xml (proto), base/dex/classes.dex, then `files`."""
    return _write(path, aab_bytes(manifest, dex_strings, dex_types, files))


def xapk_bytes(apks, files=None):
    entries = []
    for name, apk in sorted(apks.items()):
        entries.append(ZipEntry(name, apk if isinstance(apk, (bytes, bytearray)) else Path(apk).read_bytes()))
    return zip_bytes(entries + _members(files))


def make_xapk(path, apks, files=None):
    """zip of apk files: `apks` maps member name to apk bytes or apk path; `files` adds other members."""
    return _write(path, xapk_bytes(apks, files))


def _mobileprovision():
    plist = plistlib.dumps({
        "Name": "Snag Example Profile", "TeamName": "Example Team", "AppIDName": "Snag",
        "Entitlements": {
            "application-identifier": "ABCDE12345.com.example.snag", "get-task-allow": False,
            "aps-environment": "development",
            "com.apple.developer.associated-domains": ["applinks:snagdesk.example.test"],
            "keychain-access-groups": ["ABCDE12345.*"],
        },
    }, fmt=plistlib.FMT_XML)
    return b"\x30\x82\x0b\x6e\x06\x09\x2a\x86\x48\x86\xf7\x0d\x01\x07\x02" + b"junk-cms-header" + plist + (
        b"\x00\x01cms-signature-junk\xff" * 3)


def ipa_bytes(info, files=None):
    members = {
        "Info.plist": plistlib.dumps(info, fmt=plistlib.FMT_BINARY),
        "main.jsbundle": SAMPLE_BUNDLE,
        "embedded.mobileprovision": _mobileprovision(),
    }
    members.update(files or {})
    return zip_bytes([ZipEntry(APPLE_APP_DIR + name, data, method=8 if name.endswith(".jsbundle") else None)
                      for name, data in sorted(members.items())])


def make_ipa(path, info, files=None):
    """zip: Payload/Snag.app/Info.plist (binary plist), main.jsbundle, embedded.mobileprovision, plus `files`."""
    return _write(path, ipa_bytes(info, files))


# ---------------------------------------------------------------------------------------------------------- asar

def _asar_assemble(tree, data, json_length=None, header_size=None):
    raw = json.dumps(tree, separators=(",", ":"), sort_keys=True).encode("utf-8")
    padded = raw + b"\0" * _pad4(len(raw))
    payload = 4 + len(padded)
    head = struct.pack("<IIII", 4, 4 + payload if header_size is None else header_size, payload,
                       len(raw) if json_length is None else json_length)
    return head + padded + data


def asar_bytes(files, unpacked=(), links=None):
    """Returns (asar bytes, {unpacked relative path: bytes})."""
    tree, data, side = {"files": {}}, bytearray(), {}
    for name in sorted(files):
        content = _b(files[name])
        node = tree
        parts = name.split("/")
        for part in parts[:-1]:
            node = node["files"].setdefault(part, {"files": {}})
        if name in unpacked:
            node["files"][parts[-1]] = {"size": len(content), "unpacked": True}
            side[name] = content
            continue
        blocks = [hashlib.sha256(content[i:i + 4194304]).hexdigest() for i in range(0, len(content), 4194304)]
        node["files"][parts[-1]] = {
            "size": len(content), "offset": str(len(data)),
            "integrity": {"algorithm": "SHA256", "hash": hashlib.sha256(content).hexdigest(),
                          "blockSize": 4194304, "blocks": blocks or [hashlib.sha256(b"").hexdigest()]}}
        data += content
    for name, target in sorted((links or {}).items()):
        node = tree
        parts = name.split("/")
        for part in parts[:-1]:
            node = node["files"].setdefault(part, {"files": {}})
        node["files"][parts[-1]] = {"link": target}
    missing = set(unpacked) - set(files)
    if missing:
        raise ValueError(f"unpacked names not in files: {sorted(missing)}")
    return _asar_assemble(tree, bytes(data)), side


def make_asar(path, files, unpacked=(), links=None):
    """asar per contract 6.6; unpacked members are written to `<path>.unpacked/` (sibling folder)."""
    blob, side = asar_bytes(files, set(unpacked), links)
    path = _write(path, blob)
    for name, content in side.items():
        _write(Path(str(path) + ".unpacked") / name, content)
    return path


def make_asar_hostile(path, kind):
    """A one-file asar broken in one way. kind: traversal (name `..`), separator (name `a\\b`), slash (name `a/b`),
    offset-oob (offset past the end), size-oob (size past the end), json-too-long (jsonLength past the end)."""
    entry = {"size": 5, "offset": "0"}
    data = b"hello"
    names = {"traversal": "..", "separator": "a\\b", "slash": "a/b"}
    if kind in names:
        return _write(path, _asar_assemble({"files": {names[kind]: entry}}, data))
    if kind == "offset-oob":
        return _write(path, _asar_assemble({"files": {"x.txt": {"size": 5, "offset": "99999"}}}, data))
    if kind == "size-oob":
        return _write(path, _asar_assemble({"files": {"x.txt": {"size": 99999, "offset": "0"}}}, data))
    if kind == "json-too-long":
        return _write(path, _asar_assemble({"files": {"x.txt": entry}}, data, json_length=0x7FFFFFFF))
    raise ValueError(f"unknown kind {kind!r}")


# ----------------------------------------------------------------------------------------------- hostile archives

def make_zip_slip(path):
    """Members named with `..` parts: `../evil.txt`, `a/../../evil.txt`, `..\\evil.txt` (backslash form) and a clean one."""
    return _write(path, zip_bytes([
        ZipEntry("ok.txt", "fine\n"), ZipEntry("../evil.txt", "slip\n"), ZipEntry("a/../../evil2.txt", "slip\n"),
        ZipEntry("..\\evil3.txt", "slip\n"), ZipEntry("dir/../../../evil4.txt", "slip\n")]))


def make_absolute_path_zip(path):
    """Members with absolute names: `/etc/evil.txt`, `C:/Windows/evil.txt`, `C:\\evil.txt`, `\\\\host\\share\\evil.txt`."""
    return _write(path, zip_bytes([
        ZipEntry("ok.txt", "fine\n"), ZipEntry("/etc/evil.txt", "abs\n"), ZipEntry("C:/Windows/evil.txt", "abs\n"),
        ZipEntry("C:\\evil2.txt", "abs\n"), ZipEntry("\\\\host\\share\\evil3.txt", "abs\n")]))


def make_zip_bomb(path, size=2 * 1024 * 1024):
    """One member `bomb.bin` of `size` zeros (2 MiB: about 2 KB on disk, ratio about 1000) and a small clean member."""
    zeros_crc = zlib.crc32(b"\0" * size) & 0xFFFFFFFF
    return _write(path, zip_bytes([
        ZipEntry("readme.txt", "bomb below\n"),
        ZipEntry("bomb.bin", b"", method=8, compressed=_deflate_zero_run(size), usize=size, crc=zeros_crc)]))


def make_many_entries_zip(path, count=50_001):
    """`count` empty members `e/<n>.txt` (the 6.2 limit is 50,000 archive entries)."""
    return _write(path, zip_bytes([ZipEntry(f"e/{n}.txt", b"") for n in range(count)]))


def make_nested_zip(path, depth=3):
    """outer zip -> `inner1.zip` -> `inner2.zip` -> ... `depth` levels below the outer file; the innermost holds
    `deep.txt`. (The nesting limit is 2, so the default of 3 is one too many.)"""
    blob = zip_bytes([ZipEntry("deep.txt", "bottom\n")])
    for level in range(depth, 0, -1):
        blob = zip_bytes([ZipEntry(f"inner{level}.zip", blob)])
    return _write(path, blob)


def make_symlink_zip(path):
    """`link` is a symlink (Unix mode 0o120777, data = target `/etc/passwd`); `ok.txt` is a regular file."""
    return _write(path, zip_bytes([ZipEntry("ok.txt", "fine\n"), ZipEntry("link", "/etc/passwd", symlink=True),
                                   ZipEntry("dirlink", "../outside", symlink=True)]))


def make_encrypted_member_zip(path):
    """`secret.txt` has general-purpose flag bit 0 set (ZipCrypto marker; the payload is 12 header bytes plus junk)."""
    junk = bytes((n * 37 + 11) & 0xFF for n in range(12 + 16))
    return _write(path, zip_bytes([ZipEntry("ok.txt", "fine\n"),
                                   ZipEntry("secret.txt", b"", method=0, flags=1, compressed=junk, usize=16,
                                            crc=0x12345678)]))


def make_duplicate_names_zip(path):
    """Two members both named `app/config.json` with different content (later one wins in most readers)."""
    return _write(path, zip_bytes([ZipEntry("app/config.json", '{"which": "first"}\n'),
                                   ZipEntry("app/config.json", '{"which": "second"}\n'), ZipEntry("ok.txt", "fine\n")]))


def make_oversized_member_zip(path, size=64 * 1024 * 1024 + 1):
    """One stored member `big.bin` of `size` bytes of repeating pseudo-random data (incompressible by design)."""
    block = hashlib.sha256(b"snagdesk-big-member").digest() * 128  # 4 KiB
    data = (block * (size // len(block) + 1))[:size]
    return _write(path, zip_bytes([ZipEntry("big.bin", data, method=0)]))


def make_truncated_zip(path):
    """A valid small zip cut in the middle of its central directory (no end-of-central-directory record)."""
    blob = zip_bytes([ZipEntry("a.txt", "alpha\n"), ZipEntry("b.txt", "beta\n"), ZipEntry("c.txt", "gamma\n")])
    return _write(path, blob[:len(blob) - 22 - 40])
