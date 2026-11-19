"""Synthetic archive fixtures (contract 10.2, workstream W2-FX1). Standard library only; nothing is downloaded.

Every builder writes small, invented files (package `com.example.snagdesk`, endpoints under `api.example.test`) and is
deterministic: the same call gives the same bytes. To get that, the zip container is written by hand (fixed 1980-01-01
timestamps, fixed member order, no `zlib` output: members are stored or deflated by the tiny encoders below, whose bytes
do not depend on the zlib build). Verified so far: Windows with Python 3.11, 3.13 and 3.14 (same pinned hashes); CI adds
Linux and 3.10/3.12. No parser code is used here: the encoders follow the format documentation and the goldens in
`tests/data/android/` (made by aapt2 36.0.0).

Public API (the signatures below are final; 10.2 names are marked [10.2], additions are marked [+]):

  bytes builders
    make_axml(elements, *, utf8=False, attr_size=20, attr_start=20, extra_chunk=None) -> bytes   [10.2] AXML
    make_proto_manifest(elements, text=True) -> bytes    [10.2] aapt2 proto XmlNode (AAB manifest)
    make_dex(strings, types, version="035") -> bytes     [10.2] dex with adler32 and sha1 filled in
    apk_bytes / aab_bytes / ipa_bytes / xapk_bytes / asar_bytes                                  [+] same as make_*, returning bytes
    zip_bytes(entries, comment=b"") -> bytes             [+] the deterministic zip writer; entries are ZipEntry
  file builders (all return the Path they wrote)
    make_apk(path, manifest, dex_strings, dex_types, files=None)                                  [10.2]
    make_aab(path, manifest, dex_strings, dex_types, files=None)                                  [10.2]
    make_xapk(path, apks, files=None)                                                             [10.2]
    make_ipa(path, info, files=None)                                                              [10.2]
    make_asar(path, files, unpacked=(), links=None)                                               [10.2]
    hostile zips: make_zip_slip, make_absolute_path_zip, make_nul_name_zip, make_zip_bomb(size=2 MiB),
      make_many_entries_zip(count=50_001), make_many_directories_zip(count=50_001),
      make_nested_zip(depth=3, member="inner{level}.apk"), make_symlink_zip, make_encrypted_member_zip,
      make_duplicate_names_zip, make_unsupported_method_zip, make_corrupt_deflate_zip, make_truncated_member_zip,
      make_oversized_member_zip(size=64 MiB + 1, stored=False), make_truncated_zip                [10.2 / +]
    hostile asars and folders: make_asar_hostile(path, kind), make_folder_with_symlink(path)      [+]
  constants for the parser workstreams
    SNAGDESK_MANIFEST / _STRINGS / _TYPES (the golden source), AAPT2_ADDED_ATTRS (the four attributes aapt2 stamps into
    <manifest>), IPA_INFO, ELECTRON_FILES (use with make_asar), SAMPLE_BUNDLE, HERMES_MAGIC, HERMES_BUNDLE, RES_IDS,
    FAKE_AWS_KEY, FLUTTER_LIB

Choices where 10.2 left the signature open
  * `elements` is `[(depth, name, {attr: value})]`, depth 0 is the single root. Attribute keys: `"android:name"` is in the
    android namespace (and gets its resource id from RES_IDS); `"package"` has no namespace and no id; `"tools:x"` and
    `"app:x"` use the namespaces in NAMESPACES; `"android:@0x01010003"` writes an attribute whose name string is EMPTY
    and whose identity is only its resource id (what an obfuscated manifest looks like; the parser falls back to the
    resource map). Values: str, bool, int (decimal), Hex(int), Ref(id[, "@android:style/X"]).
  * Attributes are ordered like aapt2 orders them in AXML: those with a resource id first, by id, then the others in the
    order given. The string pool is aapt2's too: the names of id attributes first, in id order (so pool index = resource
    map index), then every other string, sorted. (The proto encoder keeps the order given; aapt2 orders those its own way.)
  * make_axml's keyword options write legal layouts aapt2 does not produce (UTF-8 pool, wider or offset attribute
    records, an unknown chunk): a parser must not hard-code what aapt2 does. See its docstring.
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
  * make_ipa(path, info, files): `info` is the Info.plist dict (written as a binary plist; IPA_INFO is a full example);
    `files` keys are relative to `Payload/Snag.app/`; `main.jsbundle` and `embedded.mobileprovision` get defaults unless
    `files` overrides them.
  * make_asar: `files` maps a slash-separated relative path to bytes or str; `unpacked` is a set of those paths whose data
    goes to the sibling `<asar>.unpacked/` folder instead of the archive; `links` maps a path to a link target. Each packed
    file carries an `integrity` record (SHA256, 4 MiB blocks) like Electron's own tool writes; a file of 4 MiB or more
    would also need the hash of the trailing partial block, which this builder does not add (fixtures are small), an
    empty file gets one block (the hash of nothing), and `unpacked` entries carry no `integrity`, as in real archives.

Which safety rule each hostile fixture is meant to trip (contract 6.2, 6.6)
  * `unsafe-path`: make_zip_slip, make_absolute_path_zip, make_nul_name_zip; asar kinds traversal, separator, slash,
    nul-name (the asar reader rule: names containing `/`, `\\`, `..` or NUL).
  * `ratio`: make_zip_bomb. `too-large`: make_oversized_member_zip (deliberately under the ratio limit; `stored=True`
    is the on-disk-heavy variant). Archive entries (50,000): make_many_entries_zip (also the files-per-study limit,
    every entry being a file) and make_many_directories_zip (entries only). Nested depth (2): make_nested_zip with
    depth 3 (named `.apk` members, outer file named `.xapk` by the caller; depth 1 and 2 are allowed).
  * `symlink`: make_symlink_zip; asar kinds link-outside and unpacked-escape; make_folder_with_symlink (folders).
    `encrypted`: make_encrypted_member_zip.
  * `malformed`: make_truncated_zip (whole archive), make_unsupported_method_zip, make_corrupt_deflate_zip,
    make_truncated_member_zip (one member each); asar kinds offset-oob, size-oob, json-too-long, offset-negative,
    offset-hex, bad-json, too-deep (64 folder limit).
  * None of these is a rule: make_duplicate_names_zip (a lane must not let the second member hide the first).
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
# the published AWS documentation example key, assembled so the source holds no key-shaped text and needs no scanner
# exemption (and so no exemption marker can travel into a fixture)
FAKE_AWS_KEY = "AKIA" + "IOSFODNN7" + "EXAMPLE"
APPLE_APP_DIR = "Payload/Snag.app/"

NAMESPACES = {
    "android": "http://schemas.android.com/apk/res/android",
    "app": "http://schemas.android.com/apk/res-auto",
    "tools": "http://schemas.android.com/tools",
}

# android: attribute resource ids. Every entry was checked against `aapt2 dump xmltree` output of a manifest using it
# (see test_res_ids_match_android_jar, skipped when the tool is absent); the 6.7 table is a subset of this one.
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

# the four attributes `aapt2 link` adds to <manifest> (values of build-tools 36.0.0 / platform 36). They come from the
# installed SDK, not from the source XML, so anything comparing fresh aapt2 output with the goldens must drop them.
AAPT2_ADDED_ATTRS = {"android:compileSdkVersion": 36, "android:compileSdkVersionCodename": "16",
                     "platformBuildVersionCode": 36, "platformBuildVersionName": 16}

# first 8 bytes of a Hermes bytecode bundle (contract 6.7); a lane reports it and does not decode it
HERMES_MAGIC = bytes([0xC6, 0x1F, 0xBC, 0x03, 0xC1, 0x03, 0x19, 0x1F])
HERMES_BUNDLE = HERMES_MAGIC + bytes(range(24))  # magic plus filler: a "compiled bundle" that is not JavaScript text

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
    f'var cfg={{awsKey:"{FAKE_AWS_KEY}",flag:"new-checkout"}};\n'
    'module.exports={load:load,login:login,cfg:cfg};\n'
)

IPA_INFO = {
    "CFBundleIdentifier": "com.example.snag", "CFBundleShortVersionString": "2.0.1", "CFBundleVersion": "7",
    "MinimumOSVersion": "15.0", "UIBackgroundModes": ["fetch", "remote-notification"],
    "NSCameraUsageDescription": "Scan a tag", "NSLocationWhenInUseUsageDescription": "Find nearby desks",
    "CFBundleURLTypes": [{"CFBundleURLName": "snagdesk", "CFBundleURLSchemes": ["snagdesk"]}],
    "NSAppTransportSecurity": {"NSAllowsArbitraryLoads": False, "NSExceptionDomains": {
        "legacy.example.test": {"NSTemporaryExceptionAllowsInsecureHTTPLoads": True}}},
}

# a small Electron app for make_asar: main process with IPC, a protocol client, an updater and insecure
# webPreferences; a preload exposing an API with contextBridge; a renderer page and script (contract 6.6)
ELECTRON_FILES = {
    "package.json": json.dumps({"name": "snagdesk-desktop", "version": "3.4.5", "main": "main.js",
                                "dependencies": {"electron-updater": "^6.1.0", "ws": "^8.0.0"}}, indent=2) + "\n",
    "main.js": (
        'const { app, BrowserWindow, ipcMain } = require("electron");\n'
        'const { autoUpdater } = require("electron-updater");\n'
        'app.setAsDefaultProtocolClient("snagdesk");\n'
        'function createWindow() {\n'
        '  const win = new BrowserWindow({ webPreferences: { preload: __dirname + "/preload.js", nodeIntegration: true,\n'
        '    contextIsolation: false, webSecurity: false } });\n'
        '  win.loadFile("renderer/index.html");\n'
        '  win.webContents.send("app:ready", { ok: true });\n'
        '}\n'
        'ipcMain.handle("tickets:list", async () => []);\n'
        'ipcMain.on("tickets:close", (event, id) => {});\n'
        'autoUpdater.setFeedURL({ url: "https://updates.example.test/snagdesk" });\n'
        'app.whenReady().then(createWindow);\n'),
    "preload.js": (
        'const { contextBridge, ipcRenderer } = require("electron");\n'
        'contextBridge.exposeInMainWorld("api", {\n'
        '  listTickets: () => ipcRenderer.invoke("tickets:list"),\n'
        '  closeTicket: (id) => ipcRenderer.send("tickets:close", id),\n'
        '});\n'),
    "renderer/index.html": '<!doctype html><html><body><div id="app"></div><script src="renderer.js"></script></body></html>\n',
    "renderer/renderer.js": 'fetch("https://api.example.test/v1/tickets").then((r) => r.json());\n',
}


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


def _len8(n):
    if n > 0x7FFF:
        raise ValueError("a UTF-8 pool string is limited to 32,767 characters")
    return bytes([n]) if n < 0x80 else bytes([0x80 | n >> 8, n & 0xFF])


def _utf8_string(s):
    """UTF-8 pool entry: length in UTF-16 units, length in bytes (each 1 byte, or 2 with the high bit set), bytes, NUL."""
    data = s.encode("utf-8")
    return _len8(len(s.encode("utf-16-le")) // 2) + _len8(len(data)) + data + b"\0"


def _string_pool(strings, utf8=False):
    blobs, offs, pos = [], [], 0
    for s in strings:
        offs.append(pos)
        blob = _utf8_string(s) if utf8 else _utf16_string(s)
        blobs.append(blob)
        pos += len(blob)
    body = b"".join(blobs)
    body += b"\0" * _pad4(len(body))
    start = 28 + 4 * len(strings)
    size = start + len(body)
    return (struct.pack("<HHI", _RES_STRING_POOL, 28, size) + _u32(len(strings), 0, 0x100 if utf8 else 0, start, 0)
            + _u32(*offs) + body)


def make_axml(elements, *, utf8=False, attr_size=20, attr_start=20, extra_chunk=None):
    """Binary AndroidManifest (AXML) for `elements`; layout as documented in contract 6.7 and as aapt2 36.0.0 writes it.

    The keyword options write layouts that are legal but that aapt2 does not produce, so a parser cannot get away with
    hard-coding what aapt2 does: `utf8=True` writes a UTF-8 string pool (flag 0x100; strings of 128 bytes or more use the
    two-byte length form); `attr_size` (>= 20) is the size of each attribute record, the extra bytes being zero;
    `attr_start` (>= 20) is the offset of the first attribute from the start of the element's extension (zero padding in
    between); `extra_chunk=(type, payload)` inserts a chunk of an unknown type after the resource map, which readers skip."""
    if attr_size < 20 or attr_start < 20:
        raise ValueError("attr_size and attr_start are at least 20")
    if extra_chunk is not None and (extra_chunk[0] in (0x0001, 0x0003, 0x0100, 0x0101, 0x0102, 0x0103, 0x0180)
                                    or not 0 < extra_chunk[0] <= 0xFFFF):
        raise ValueError("extra_chunk needs a chunk type this format does not use")
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

    chunks = [_string_pool(pool, utf8)]
    if ids_sorted:
        chunks.append(struct.pack("<HHI", _RES_XML_RESOURCE_MAP, 8, 8 + 4 * len(ids_sorted)) + _u32(*ids_sorted))
    if extra_chunk is not None:
        payload = _b(extra_chunk[1])
        payload += b"\0" * _pad4(len(payload))
        chunks.append(struct.pack("<HHI", extra_chunk[0], 8, 8 + len(payload)) + payload)
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
            body += (_u32(ns, name_idx(local, resid), raw) + struct.pack("<HBBI", 8, 0, dtype, data)
                     + b"\0" * (attr_size - 20))
        chunks.append(node(_RES_ELEM_START, line, 16 + attr_start + len(body), _NONE, index[name]) + struct.pack(
            "<HHHHHH", attr_start, attr_size, len(row), 0, 0, 0) + b"\0" * (attr_start - 20) + body)
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


def make_dex(strings, types, version="035"):
    """A dex file with `strings` and `types` (descriptors) and no classes; checksum and signature are valid.
    `version` is the three-digit magic version: 035 (default), 037, 038 or 039 (same header layout)."""
    if not (isinstance(version, str) and version in ("035", "037", "038", "039")):
        raise ValueError(f"unsupported dex version {version!r}")
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
    head[0:8] = b"dex\n" + version.encode("ascii") + b"\0"
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

    def align(self):  # pad with zero bits to a byte boundary
        if self.n:
            self.put(0, 8 - self.n)

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


def _deflate_zero_run(n, w=None, final=True):
    """A raw deflate stream for `n` zero bytes in about n/1000 bytes: one dynamic block holding literal 0 (code 10),
    length-258 matches at distance 1 (code 0 and distance code 0) and the end of block (code 11). With `w` (a _Bits
    writer) the block is appended to it and nothing is returned; `final=False` leaves room for more blocks."""
    own = w is None
    w = w or _Bits()
    w.put(1 if final else 0, 1)
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
    w.put(0, 2 * (left // 258))  # each match is code 0 (length 258) then code 0 (distance 1): two zero bits
    for _ in range(left % 258):
        w.code(2, 2)
    w.code(3, 2)  # end of block
    return w.done() if own else None


def _deflate_stored_then_zeros(literal, zeros):
    """Raw deflate: `literal` bytes in stored blocks (incompressible, 1:1), then `zeros` zero bytes as one final run block.
    Lets a member be large when expanded at a ratio chosen by the caller."""
    w = _Bits()
    for i in range(0, len(literal), 65535):
        chunk = literal[i:i + 65535]
        w.put(0, 1)  # not the final block
        w.put(0, 2)  # stored
        w.align()
        w.out += struct.pack("<HH", len(chunk), len(chunk) ^ 0xFFFF) + chunk
    _deflate_zero_run(zeros, w, final=True)
    return w.done()


# ------------------------------------------------------------------------------------------------- zip container

class ZipEntry:
    """One member for zip_bytes. `method=None` picks deflate when it is smaller, else stored. `compressed`/`usize`/`crc`
    override what is written to the headers (hostile fixtures; `csize` overrides only the size field, not the bytes);
    `symlink=True` sets the Unix link mode."""

    def __init__(self, name, data=b"", *, method=None, flags=0, mode=0o100644, symlink=False, compressed=None,
                 usize=None, crc=None, csize=None):
        self.name = name if isinstance(name, bytes) else name.encode("utf-8")
        self.data = _b(data)
        self.method, self.flags = method, flags
        self.external = ((0o120777 if symlink else mode) & 0xFFFF) << 16
        self.compressed, self.usize, self.crc, self.csize = compressed, usize, crc, csize  # csize: header only
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
        csize = len(payload) if e.csize is None else e.csize
        out += struct.pack("<IHHHHHIIIHH", 0x04034B50, need, e.flags, method, 0, 0x21, crc, csize, usize,
                           len(e.name), 0) + e.name + payload
        central += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 3 << 8 | 20, need, e.flags, method, 0, 0x21, crc,
                               csize, usize, len(e.name), 0, 0, 0, 0, e.external, offset) + e.name
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

def _asar_assemble(tree, data, json_length=None, header_size=None, raw=None):
    if raw is None:
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


class SymlinkUnavailable(Exception):
    """This platform (or this user) cannot create the symlink or junction a fixture needs; tests skip on it."""


def _link(link, target, is_dir):
    """Create `link` pointing at `target` (a symlink, or on Windows a directory junction); True when it worked."""
    link, target = Path(link), Path(target).resolve()
    try:
        os.symlink(target, link, target_is_directory=is_dir)
        return True
    except (OSError, NotImplementedError):
        pass
    if is_dir and os.name == "nt":
        import subprocess
        try:
            done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        except OSError:
            return False
        return done.returncode == 0 and link.exists()
    return False


def make_folder_with_symlink(path):
    """`path/project/` holds `ok.txt`, a directory link `dirlink` and a file link `filelink`, both pointing into
    `path/outside/` (which holds `secret.txt`). Raises SymlinkUnavailable if no link at all can be made. Returns
    {"root": project folder, "outside": outside folder, "links": [link paths that exist]}."""
    path = Path(path)
    root, outside = path / "project", path / "outside"
    root.mkdir(parents=True, exist_ok=True)
    outside.mkdir(parents=True, exist_ok=True)
    (root / "ok.txt").write_bytes(b"fine\n")
    (outside / "secret.txt").write_bytes(b"outside\n")
    links = []
    if _link(root / "dirlink", outside, True):
        links.append(root / "dirlink")
    if _link(root / "filelink", outside / "secret.txt", False):
        links.append(root / "filelink")
    if not links:
        raise SymlinkUnavailable("cannot create a symlink or junction here (Windows without developer mode?)")
    return {"root": root, "outside": outside, "links": links}


def make_asar_hostile(path, kind):
    """A small asar broken in one way (reader rules of contract 6.6). kind:
    traversal (name `..`), separator (name `a\\b`), slash (name `a/b`), nul-name (name with a NUL): the `unsafe-path` rule;
    offset-oob, size-oob (past the end of the file), json-too-long (jsonLength past the end): the bounds checks;
    offset-negative (`"-1"`), offset-hex (`"0x10"`): offsets that are not decimal strings;
    too-deep (70 nested folders; the limit is 64); link-outside (a link to `../../outside.txt`); bad-json (truncated
    header JSON with consistent lengths); unpacked-escape (an `unpacked` entry `dir/leak.txt` whose `dir` in
    `<path>.unpacked/` is a link to a folder outside it, so the real path escapes; raises SymlinkUnavailable where
    links cannot be made)."""
    entry = {"size": 5, "offset": "0"}
    data = b"hello"
    names = {"traversal": "..", "separator": "a\\b", "slash": "a/b", "nul-name": "a\0b"}
    if kind in names:
        return _write(path, _asar_assemble({"files": {names[kind]: entry}}, data))
    if kind == "offset-oob":
        return _write(path, _asar_assemble({"files": {"x.txt": {"size": 5, "offset": "99999"}}}, data))
    if kind == "size-oob":
        return _write(path, _asar_assemble({"files": {"x.txt": {"size": 99999, "offset": "0"}}}, data))
    if kind == "json-too-long":
        return _write(path, _asar_assemble({"files": {"x.txt": entry}}, data, json_length=0x7FFFFFFF))
    if kind in ("offset-negative", "offset-hex"):
        text = "-1" if kind == "offset-negative" else "0x10"
        return _write(path, _asar_assemble({"files": {"x.txt": {"size": 5, "offset": text}}}, data))
    if kind == "too-deep":
        node = {"files": {"f.txt": entry}}
        for _ in range(70):
            node = {"files": {"d": node}}
        return _write(path, _asar_assemble(node, data))
    if kind == "link-outside":
        return _write(path, _asar_assemble({"files": {"out": {"link": "../../outside.txt"}}}, b""))
    if kind == "bad-json":
        return _write(path, _asar_assemble(None, data, raw=b'{"files": {"x.txt": {"size": 5, "offs'))
    if kind == "unpacked-escape":
        path = Path(path)
        side, outside = Path(str(path) + ".unpacked"), Path(str(path) + ".outside")
        side.mkdir(parents=True, exist_ok=True)
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "leak.txt").write_bytes(b"outside\n")
        if not _link(side / "dir", outside, True):
            raise SymlinkUnavailable("cannot create a symlink or junction here")
        tree = {"files": {"dir": {"files": {"leak.txt": {"size": 8, "unpacked": True}}}}}
        return _write(path, _asar_assemble(tree, b""))
    raise ValueError(f"unknown kind {kind!r}")


# ----------------------------------------------------------------------------------------------- hostile archives

def make_zip_slip(path):
    """Members named with `..` parts: `../evil.txt`, `a/../../evil.txt`, `..\\evil.txt` (backslash form) and a clean one.
    Trips the `unsafe-path` rule."""
    return _write(path, zip_bytes([
        ZipEntry("ok.txt", "fine\n"), ZipEntry("../evil.txt", "slip\n"), ZipEntry("a/../../evil2.txt", "slip\n"),
        ZipEntry("..\\evil3.txt", "slip\n"), ZipEntry("dir/../../../evil4.txt", "slip\n")]))


def make_absolute_path_zip(path):
    """Members with absolute names: `/etc/evil.txt`, `C:/Windows/evil.txt`, `C:\\evil.txt`, `\\\\host\\share\\evil.txt`.
    Trips the `unsafe-path` rule."""
    return _write(path, zip_bytes([
        ZipEntry("ok.txt", "fine\n"), ZipEntry("/etc/evil.txt", "abs\n"), ZipEntry("C:/Windows/evil.txt", "abs\n"),
        ZipEntry("C:\\evil2.txt", "abs\n"), ZipEntry("\\\\host\\share\\evil3.txt", "abs\n")]))


def make_nul_name_zip(path):
    """A member whose name contains a NUL (`evil\\0.txt`), written into both the local and central headers. Python's
    `zipfile` cuts `filename` and `namelist()` at the NUL (seen on 3.11, 3.13 and 3.14) and keeps the full name only in
    `ZipInfo.orig_filename`. A safety layer must check `orig_filename` or the raw central directory, or the NUL rule
    never trips. The property
    is only reliable in the raw central directory. Trips the `unsafe-path` rule (NUL)."""
    return _write(path, zip_bytes([ZipEntry("ok.txt", "fine\n"), ZipEntry(b"evil\0.txt", "nul\n")]))


def make_zip_bomb(path, size=2 * 1024 * 1024):
    """One member `bomb.bin` of `size` zeros (2 MiB: about 2 KB on disk, ratio about 1000) and a small clean member.
    Trips the `ratio` rule (uncompressed over 1 MiB and over 100 times the compressed size)."""
    zeros_crc = zlib.crc32(b"\0" * size) & 0xFFFFFFFF
    return _write(path, zip_bytes([
        ZipEntry("readme.txt", "bomb below\n"),
        ZipEntry("bomb.bin", b"", method=8, compressed=_deflate_zero_run(size), usize=size, crc=zeros_crc)]))


def make_many_entries_zip(path, count=50_001):
    """`count` empty members `e/<n>.txt`. Trips the archive-entry limit (50,000) and, because every entry is a file, the
    files-per-study limit too; use make_many_directories_zip to trip only the entry limit."""
    return _write(path, zip_bytes([ZipEntry(f"e/{n}.txt", b"") for n in range(count)]))


def make_many_directories_zip(path, count=50_001):
    """`count` directory entries `d<n>/` (no files). Trips the archive-entry limit (50,000) without adding to the
    files-per-study count."""
    return _write(path, zip_bytes([ZipEntry(f"d{n}/", b"", mode=0o040755) for n in range(count)]))


def make_nested_zip(path, depth=3, member="inner{level}.apk"):
    """outer file -> `inner1.apk` -> `inner2.apk` -> ... `depth` levels below the outer file; the innermost is a real
    small apk (`AndroidManifest.xml`). Name the outer file `.xapk` (or `.apks`) so a lane treats it as a bundle of apks.
    The nesting limit is 2 (XAPK -> APK -> APK is allowed), so the default of 3 trips the nested-depth rule; `member`
    is the member name pattern with a `{level}` field."""
    blob = apk_bytes(SNAGDESK_MANIFEST, None, None)
    for level in range(depth, 0, -1):
        blob = zip_bytes([ZipEntry(member.format(level=level), blob)])
    return _write(path, blob)


def make_symlink_zip(path):
    """`link` is a symlink (Unix mode 0o120777, data = target `/etc/passwd`); `ok.txt` is a regular file. Trips the
    `symlink` rule."""
    return _write(path, zip_bytes([ZipEntry("ok.txt", "fine\n"), ZipEntry("link", "/etc/passwd", symlink=True),
                                   ZipEntry("dirlink", "../outside", symlink=True)]))


def make_encrypted_member_zip(path):
    """`secret.txt` has general-purpose flag bit 0 set (ZipCrypto marker; the payload is 12 header bytes plus junk).
    Trips the `encrypted` rule."""
    junk = bytes((n * 37 + 11) & 0xFF for n in range(12 + 16))
    return _write(path, zip_bytes([ZipEntry("ok.txt", "fine\n"),
                                   ZipEntry("secret.txt", b"", method=0, flags=1, compressed=junk, usize=16,
                                            crc=0x12345678)]))


def make_duplicate_names_zip(path):
    """Two members both named `app/config.json` with different content (later one wins in most readers). A lane must not
    let the second hide the first from its checks."""
    return _write(path, zip_bytes([ZipEntry("app/config.json", '{"which": "first"}\n'),
                                   ZipEntry("app/config.json", '{"which": "second"}\n'), ZipEntry("ok.txt", "fine\n")]))


def make_unsupported_method_zip(path):
    """`weird.bin` declares compression method 99 (AES marker; unsupported by `zipfile`); `ok.txt` is fine. Trips the
    `malformed` skip (NotImplementedError) for the member, not for the archive."""
    return _write(path, zip_bytes([ZipEntry("weird.bin", b"", method=99, compressed=bytes(range(32)), usize=64,
                                            crc=0xDEADBEEF), ZipEntry("ok.txt", "fine\n")]))


def make_corrupt_deflate_zip(path):
    """`bad.bin` is method 8 with data that is not a deflate stream (block type 3), under a valid central directory;
    `ok.txt` is fine. Trips the `malformed` skip (zlib.error) for the member."""
    return _write(path, zip_bytes([ZipEntry("bad.bin", b"", method=8, compressed=b"\xff" * 24, usize=100,
                                            crc=0x12345678), ZipEntry("ok.txt", "fine\n")]))


def make_truncated_member_zip(path):
    """`cut.bin` (stored, 4096 bytes declared) has only its first 2048 bytes in the file, so its declared size runs into
    the next member's header, while the central directory is intact; `ok.txt` follows. Trips the `malformed` skip
    for the member (on current Pythons `BadZipFile`: "Overlapped entries", because the declared size runs into the
    next member; a bad CRC or a short read on others)."""
    real = bytes((n * 7 + 3) & 0xFF for n in range(4096))
    return _write(path, zip_bytes([ZipEntry("cut.bin", b"", method=0, compressed=real[:2048], csize=4096, usize=4096,
                                            crc=zlib.crc32(real) & 0xFFFFFFFF), ZipEntry("ok.txt", "fine\n")]))


def make_oversized_member_zip(path, size=64 * 1024 * 1024 + 1, stored=False):
    """One member `big.bin` of `size` bytes, by default 1 byte over the 64 MiB member cap. It is deflated at a ratio near
    60 (a stored first 1/64 of pseudo-random bytes, then zeros: about 1 MiB on disk), so the `ratio` rule does NOT fire
    and the member-size cap (`too-large`) is what must stop a reader. `stored=True` writes it stored instead (`size`
    bytes on disk, build it only when that is the point)."""
    block = hashlib.sha256(b"snagdesk-big-member").digest() * 128  # 4 KiB
    if stored:
        data = (block * (size // len(block) + 1))[:size]
        return _write(path, zip_bytes([ZipEntry("big.bin", data, method=0)]))
    head = min(size // 64, size - 1)
    literal = (block * (head // len(block) + 1))[:head]
    crc = zlib.crc32(literal)
    zeros = bytes(1024 * 1024)
    left = size - head
    while left > 0:
        crc = zlib.crc32(zeros[:min(left, len(zeros))], crc)
        left -= len(zeros)
    return _write(path, zip_bytes([ZipEntry("big.bin", b"", method=8, usize=size, crc=crc & 0xFFFFFFFF,
                                            compressed=_deflate_stored_then_zeros(literal, size - head))]))


def make_truncated_zip(path):
    """A valid small zip cut in the middle of its central directory (no end-of-central-directory record). Trips
    the `malformed` skip for the whole archive."""
    blob = zip_bytes([ZipEntry("a.txt", "alpha\n"), ZipEntry("b.txt", "beta\n"), ZipEntry("c.txt", "gamma\n")])
    return _write(path, blob[:len(blob) - 22 - 40])
