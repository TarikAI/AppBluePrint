"""W2-FX1: the archive fixture builders in fixtures_archives.py.

Nothing here uses lane code: AXML, proto, dex, zip and asar structures are re-read with `struct` (and `zipfile`,
`plistlib`, `hashlib`, `zlib`, `json`) written in this file, and compared with what the builders were asked to encode.
The goldens in tests/data/android/ were made by aapt2 (see tests/data/README.md); tests that need the Android SDK tools
are skipped when the tools are absent.
"""
import copy
import hashlib
import io
import json
import os
import plistlib
import re
import shutil
import struct
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

import support  # noqa: F401  (puts scripts/ on sys.path; the tests folder itself is on it already)
import fixtures_archives as F

DATA = Path(__file__).resolve().parent / "data" / "android"
ANDROID_NS = "http://schemas.android.com/apk/res/android"

# sha256 of each builder's output for the fixed inputs in CASES; the same on Windows and Linux, Python 3.10 to 3.14
PINNED = {
    "axml": "50bf79cf36fe07a14883e24b887d4c2b3cb4bca9d06c5ccf20ab61787cbf8462",
    "proto": "a48e9e26ef46b6cc93faf34c60d47901ef66362eae31299de6031e3ca0fe7a71",
    "dex": "dc403a7a59ef5905d32665f679fc11e86ead9b61ebbbe73f5fd1a847c5911c12",
    "apk": "5f95b3de9c2407524ce57f8d9ae7463ba3822a2a4154be5daa2cd5b2330a678d",
    "aab": "3f11ba54e5d6004d8e78a458b3233a6a46fba1c1214a757659ec5dd5a74a38e4",
    "xapk": "efa13c7a528a3edcc3d325f6b5d84f90583ef6ddccef78745bf5c54c876337f5",
    "ipa": "c55d95db75724a6d9a5903049f241d18942c37869f72b9c58b9479f622f00ed7",
    "asar": "0e2c43c68f289124242ec2c3a1d1d12e2ab22ea90a6befa987fd7872bf372441",
    "zip_slip": "7b1deb4c469dc44819583fad6f372da8e57ec0aeae19b25c6e56ab4725d95e00",
    "absolute": "335a599f0d823fd646a635fcf0eb8cf18fde6e2d8d6173589867091c3962cddf",
    "bomb": "7010de18603298b7a47017d03ddd26013087fbc44839f34f391010f1a87198d2",
    "many_entries": "7fb96d822b6ae0668914584c2ab39730286e16720290ecb1a9266e69ac14ee58",
    "nested": "5129194284ace448ce943237ac8904ea7a7d3e1cb269b0888a91549c8fc91f86",
    "symlink": "e4f9f89c0609dbcc05bfce896b963cf93c4d3f7a90ace8fa8ecd3ea95bcd4367",
    "encrypted": "a4782cc586b6e2ef4bba0a29eab02cf386013a02c2a271b0e0d185db2111174c",
    "duplicates": "b1baebf263c959d8e9d4f3b0dd465cb93faa43b65bfb1bdb0548c84d7baca422",
    "truncated": "54c9208b2d5901bf185a8e41c48f8a1ebc21f94662ce56c0ceaa478b1b66359d",
    "nul_name": "648f8961bced06c0bf8a69f82a3fcc8db0fcff4b308203dfc5ce62d807495107",
    "unsupported_method": "58b7bf1ef8caed2e48ff6550bb7d0f2f0714c88690f46d00134ce69ae24c6dcb",
    "corrupt_deflate": "4b5044b081bf4f7f1a4782813c73b103549335d9284723ddfe81ec70e3ee18bc",
    "truncated_member": "b7b18409f42a4c768ed59144ec3a601e1720fc5c94970451272eddfac0547840",
    "many_directories": "a1cb37a713f5b591b5dce86443a9aea40896e4c48b966ab1fab82bd890fd0751",
    "oversized": "74f491b978469ceb9adff4151f98d030a6ac60d7c9ed3e26b0f622cab458b110",
    "electron_asar": "ca6590f7d5b33cf1899d75cd5ef954da56c110a07fc07bad47d718b2bc5f91f2",
    "dex039": "1fc7c7b1fe0dd9dc357cb6cd648a4d293b482b7052e9eacf8574c832a9d4bfa2",
    "axml_utf8": "458d19ba14cf0b5d02356bdd62e04598dd863a33baaafba31fc72be331478377",
    "asar_traversal": "99fc3def3cc68317caa8f3bfe5d4fe351cc9580e39785ae8084759b9b5ab4921",
    "asar_separator": "43ba0a1bb2960ee032efad1bdefa8d5991863a44140d8d11ad11738d6706ae68",
    "asar_slash": "2885baf3af740b595f8778fed6d0c2dd5b22d16f01a3cacc11f4bfdde48850ec",
    "asar_nul_name": "d23d30f7fab453c4cf068f40e6e9cd32bc1d35e9458476eae7788537335f75c1",
    "asar_offset_oob": "cb117c43695a2c2afb9a5ff65a013a5a1fd9881fbb87ff70e137508fb5cf03e2",
    "asar_size_oob": "edafc641bc77692c9ef3e2236704695eb129e84aeb9d2125f8d216b051cfd06c",
    "asar_json_too_long": "57635603031847e2a95a1ad9421ffd4f5107cd1dffbe8edaafbc31d199c766b7",
    "asar_offset_negative": "0e8171dc260d25e3297b248503f593af947160df13685ee0271786f57bfa5da4",
    "asar_offset_hex": "c1d663a7589bd8246dc7f388599b5f21433d37896fe5c669dfbc4bfb7108a89c",
    "asar_too_deep": "8d90406b70b97a0dc0ea3808955a9bc321cb64392e56eb85851abc7a0c25634a",
    "asar_link_outside": "461816b603f0fc91fee99aa4fba652726c7a646b48c5d87c57393dbe8d53bcf9",
    "asar_bad_json": "c3264100d0cda30174411d2ab58ca176f7f11d6134bc4ae8d7dd9de5ea34efed",
}


# ------------------------------------------------------------------------------------------------ test-side readers

def pool_strings(chunk):
    """The string pool of a 0x0001 chunk (bytes of the whole chunk, UTF-16 or UTF-8), with every size cross-checked."""
    ctype, hsize, size = struct.unpack_from("<HHI", chunk, 0)
    assert (ctype, hsize, size) == (1, 28, len(chunk))
    count, styles, flags, start, style_start = struct.unpack_from("<5I", chunk, 8)
    assert styles == 0 and style_start == 0 and flags & ~0x100 == 0  # no styles, no sorted flag
    utf8 = bool(flags & 0x100)
    assert start == 28 + 4 * count
    offsets = struct.unpack_from("<%dI" % count, chunk, 28)
    out, end = [], start
    for off in offsets:
        pos = start + off
        if utf8:  # two lengths (UTF-16 units, then bytes), each 1 byte, or 2 bytes when the high bit is set
            lengths = []
            for _ in range(2):
                b0 = chunk[pos]
                if b0 & 0x80:
                    lengths.append(((b0 & 0x7F) << 8) | chunk[pos + 1])
                    pos += 2
                else:
                    lengths.append(b0)
                    pos += 1
            text = chunk[pos:pos + lengths[1]].decode("utf-8")
            assert len(text.encode("utf-16-le")) // 2 == lengths[0]
            assert chunk[pos + lengths[1]] == 0
            out.append(text)
            end = max(end, pos + lengths[1] + 1)
            continue
        n = struct.unpack_from("<H", chunk, pos)[0]
        pos += 2
        if n & 0x8000:
            n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", chunk, pos)[0]
            pos += 2
        out.append(chunk[pos:pos + 2 * n].decode("utf-16-le"))
        assert chunk[pos + 2 * n:pos + 2 * n + 2] == b"\0\0"
        end = max(end, pos + 2 * n + 2)
    assert size - end < 4 and size % 4 == 0  # only alignment padding follows the last string
    return out


def read_axml(blob, unknown=None):
    """-> (strings, resource_map, elements, lines). elements: [(depth, name, {key: (resid, typed)})] where key is
    `prefix:name` (`@0x01010003` as the name when the name string is empty) and typed is ('s'|'i'|'h'|'b'|'r', value)."""
    ctype, hsize, size = struct.unpack_from("<HHI", blob, 0)
    assert (ctype, hsize, size) == (3, 8, len(blob))
    off, strings, resmap, ns, elements, lines, depth = 8, None, [], {}, [], [], -1
    while off < len(blob):
        ctype, hsize, size = struct.unpack_from("<HHI", blob, off)
        assert size >= hsize and size % 4 == 0 and off + size <= len(blob), "chunk size out of range"
        chunk = blob[off:off + size]
        if ctype == 0x0001:
            assert strings is None
            strings = pool_strings(chunk)
        elif ctype == 0x0180:
            assert hsize == 8
            resmap = list(struct.unpack_from("<%dI" % ((size - 8) // 4), chunk, 8))
        elif ctype in (0x0100, 0x0101):
            assert (hsize, size) == (16, 24)
            prefix, uri = struct.unpack_from("<II", chunk, 16)
            if ctype == 0x0100:
                ns[strings[uri]] = strings[prefix]
        elif ctype == 0x0102:
            line, comment, nsi, name, astart, asize, count = struct.unpack_from("<IIIIHHH", chunk, 8)
            assert (hsize, comment) == (16, 0xFFFFFFFF) and nsi == 0xFFFFFFFF
            assert astart >= 20 and asize >= 20 and size == 16 + astart + asize * count
            attrs = {}
            for k in range(count):
                ans, aname, raw, vsize, res0, dtype, data = struct.unpack_from("<IIIHBBI", chunk, 16 + astart + asize * k)
                assert (vsize, res0) == (8, 0)
                resid = resmap[aname] if aname < len(resmap) else None
                local = strings[aname] or "@0x%08x" % resid
                key = (ns[strings[ans]] + ":" if ans != 0xFFFFFFFF else "") + local
                if dtype == 0x03:
                    assert raw == data
                    typed = ("s", strings[data])
                elif dtype == 0x10:
                    assert raw == 0xFFFFFFFF
                    typed = ("i", data - (1 << 32) if data >> 31 else data)
                elif dtype == 0x11:
                    typed = ("h", data)
                elif dtype == 0x12:
                    assert data in (0, 0xFFFFFFFF)
                    typed = ("b", data != 0)
                elif dtype == 0x01:
                    typed = ("r", data)
                else:
                    raise AssertionError(f"data type {dtype:#x}")
                attrs[key] = (resid, typed)
            depth += 1
            elements.append((depth, strings[name], attrs))
            lines.append(line)
        elif ctype == 0x0103:
            assert (hsize, size) == (16, 24) and strings[struct.unpack_from("<I", chunk, 20)[0]]
            depth -= 1
        else:  # a reader skips chunk types it does not know (the 4 header bytes say how far)
            assert unknown is not None, f"chunk {ctype:#x}"
            unknown.append(ctype)
        off += size
    assert off == len(blob) and depth == -1
    return strings, resmap, elements, lines


def typed(value):
    if isinstance(value, bool):
        return "b", value
    if isinstance(value, F.Ref):
        return "r", value.resid
    if isinstance(value, F.Hex):
        return "h", int(value)
    if isinstance(value, int):
        return "i", value
    return "s", value


def expected(elements):
    """Builder input -> the shape read_axml returns for elements (without resource ids)."""
    return [(d, n, {k: typed(v) for k, v in a.items()}) for d, n, a in elements]


def strip_ids(elements):
    return [(d, n, {k: t for k, (_, t) in a.items()}) for d, n, a in elements]


def mask_lines(blob):
    """The AXML bytes with the line number of every node chunk zeroed (aapt2 writes real source lines)."""
    data = bytearray(blob)
    off = 8
    while off < len(data):
        ctype, _, size = struct.unpack_from("<HHI", data, off)
        if ctype >= 0x0100 and ctype != 0x0180:
            struct.pack_into("<I", data, off + 8, 0)
        off += size
    return bytes(data)


def varint(b, i):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, i


def pb_fields(b):
    i, out = 0, []
    while i < len(b):
        key, i = varint(b, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            v, i = varint(b, i)
        elif wire == 2:
            n, i = varint(b, i)
            assert i + n <= len(b)
            v, i = b[i:i + n], i + n
        else:
            raise AssertionError(f"wire type {wire}")
        out.append((field, wire, v))
    return out


def pb_get(fields, number):
    return [v for f, _, v in fields if f == number]


def read_proto(blob):
    """-> (elements, texts) like read_axml's elements (key `prefix:name`, typed) from an aapt2 XmlNode."""
    elements, texts = [], []
    uri_prefix = {}

    def node(b, depth):
        f = pb_fields(b)
        if pb_get(f, 2):
            texts.append(pb_get(f, 2)[0].decode())
            assert pb_get(f, 3)  # source position
            return
        el = pb_fields(pb_get(f, 1)[0])
        for decl in pb_get(el, 1):
            d = pb_fields(decl)
            uri_prefix[pb_get(d, 2)[0].decode()] = pb_get(d, 1)[0].decode()
        attrs = {}
        for raw in pb_get(el, 4):
            a = pb_fields(raw)
            uri = pb_get(a, 1)[0].decode() if pb_get(a, 1) else None
            name, value = pb_get(a, 2)[0].decode(), pb_get(a, 3)[0].decode()
            resid = pb_get(a, 5)[0] if pb_get(a, 5) else None
            item = pb_fields(pb_get(a, 6)[0]) if pb_get(a, 6) else None
            if item is None:
                t = ("s", value)
            elif item[0][0] == 1:
                t = ("r", pb_get(pb_fields(item[0][2]), 2)[0])
            else:
                prim = pb_fields(item[0][2])
                (num, _, v), = prim
                t = {6: ("i", v - (1 << 64) if v >> 63 else v), 7: ("h", v), 8: ("b", bool(v))}[num]
                assert value == {"i": str(t[1]), "h": "0x%x" % v, "b": "true" if v else "false"}[t[0]]
            key = (uri_prefix[uri] + ":" if uri else "") + name
            attrs[key] = (resid, t)
        elements.append((depth, pb_get(el, 3)[0].decode(), attrs))
        for child in pb_get(el, 5):
            node(child, depth + 1)

    node(blob, 0)
    return elements, texts


MUTF8_NOTE = "decoded by hand: 1/2/3-byte forms, C0 80 is NUL"


def mutf8_decode(data):
    units, i = [], 0
    while i < len(data):
        c = data[i]
        if c < 0x80:
            units.append(c)
            i += 1
        elif c & 0xE0 == 0xC0:
            units.append((c & 0x1F) << 6 | data[i + 1] & 0x3F)
            i += 2
        else:
            assert c & 0xF0 == 0xE0
            units.append((c & 0x0F) << 12 | (data[i + 1] & 0x3F) << 6 | data[i + 2] & 0x3F)
            i += 3
    return struct.pack("<%dH" % len(units), *units).decode("utf-16-le", "surrogatepass"), len(units)


def read_dex(b):
    """Re-parse a dex and verify every structural field. -> (strings, types)."""
    assert re.fullmatch(rb"dex\n03[5-9]\0", b[:8])
    checksum, = struct.unpack_from("<I", b, 8)
    assert checksum == zlib.adler32(b[12:]) & 0xFFFFFFFF, "adler32"
    assert b[12:32] == hashlib.sha1(b[32:]).digest(), "sha1 signature"
    (file_size, header_size, endian, link_size, link_off, map_off, nstr, stroff, ntype, typeoff, nproto, protooff,
     nfield, fieldoff, nmeth, methoff, nclass, classoff, data_size, data_off) = struct.unpack_from("<20I", b, 0x20)
    assert (file_size, header_size, endian, link_size, link_off) == (len(b), 0x70, 0x12345678, 0, 0)
    assert (nproto, nfield, nmeth, nclass) == (0, 0, 0, 0)
    assert data_off + data_size == file_size and data_off % 4 == 0
    assert (stroff == 0x70) == (nstr > 0) and stroff + 4 * nstr + (4 * ntype if ntype else 0) <= data_off
    strings = []
    for i in range(nstr):
        off, = struct.unpack_from("<I", b, stroff + 4 * i)
        assert data_off <= off < file_size
        length, shift, pos = 0, 0, off
        while True:
            c = b[pos]
            pos += 1
            length |= (c & 0x7F) << shift
            shift += 7
            if c < 0x80:
                break
        end = b.index(b"\0", pos)
        text, units = mutf8_decode(b[pos:end])
        assert units == length, "uleb128 length is the UTF-16 unit count"
        strings.append(text)
    keys = [s.encode("utf-16-be", "surrogatepass") for s in strings]
    assert keys == sorted(set(keys)), "string ids sorted, no duplicates"
    type_idx = list(struct.unpack_from("<%dI" % ntype, b, typeoff)) if ntype else []
    assert type_idx == sorted(set(type_idx)) and all(i < nstr for i in type_idx)
    # the map list: sorted by offset, one entry per section present, ending with itself
    count, = struct.unpack_from("<I", b, map_off)
    entries = [struct.unpack_from("<HHII", b, map_off + 4 + 12 * k) for k in range(count)]
    assert map_off % 4 == 0 and map_off + 4 + 12 * count == file_size
    assert [e[3] for e in entries] == sorted(e[3] for e in entries)
    assert (0x0000, 1, 0) == (entries[0][0], entries[0][2], entries[0][3])
    assert entries[-1][0] == 0x1000 and entries[-1][3] == map_off
    assert {e[0] for e in entries} == {0x0000, 0x1000} | ({0x0001, 0x2002} if nstr else set()) | ({0x0002} if ntype else set())
    return strings, [strings[i] for i in type_idx]


def raw_zip(blob):
    """Central directory read by hand: [dict(name, flags, method, crc, csize, usize, external, system, offset)]."""
    pos = blob.rfind(b"PK\x05\x06")
    assert pos >= 0 and len(blob) - pos >= 22
    disk, cd_disk, here, total, cd_size, cd_off, comment_len = struct.unpack_from("<HHHHIIH", blob, pos + 4)
    assert here == total and pos + 22 + comment_len == len(blob) and cd_off + cd_size == pos
    out, p = [], cd_off
    for _ in range(total):
        (sig, made, need, flags, method, mtime, mdate, crc, csize, usize, nlen, elen, clen, dstart, iattr, eattr,
         off) = struct.unpack_from("<IHHHHHHIIIHHHHHII", blob, p)
        assert sig == 0x02014B50
        name = blob[p + 46:p + 46 + nlen]
        out.append(dict(name=name.decode("utf-8", "surrogateescape"), flags=flags, method=method, crc=crc, csize=csize,
                        usize=usize, external=eattr, system=made >> 8, offset=off, time=(mtime, mdate)))
        p += 46 + nlen + elen + clen
    assert p == pos
    for e in out:  # the local header agrees with the central directory
        sig, need, flags, method, mtime, mdate, crc, csize, usize, nlen, elen = struct.unpack_from(
            "<IHHHHHIIIHH", blob, e["offset"])
        assert sig == 0x04034B50 and (flags, method, crc, csize, usize) == (
            e["flags"], e["method"], e["crc"], e["csize"], e["usize"])
        assert blob[e["offset"] + 30:e["offset"] + 30 + nlen].decode("utf-8", "surrogateescape") == e["name"]
    return out


def read_asar(blob):
    """-> (tree, data_start, {path: bytes}) with the four header uint32 checked against each other."""
    four, header_size, payload, json_len = struct.unpack_from("<IIII", blob, 0)
    assert four == 4 and header_size == payload + 4 and payload == 4 + (json_len + 3) // 4 * 4
    tree = json.loads(blob[16:16 + json_len])
    data_start = 8 + header_size
    assert data_start % 4 == 0 and set(blob[16 + json_len:data_start]) <= {0}
    files = {}

    def walk(node, prefix):
        for name, entry in sorted(node["files"].items()):
            path = prefix + name
            if "files" in entry:
                walk(entry, path + "/")
            elif "link" in entry:
                files[path] = ("link", entry["link"])
            elif entry.get("unpacked"):
                files[path] = ("unpacked", entry["size"])
            else:
                start = data_start + int(entry["offset"])
                content = blob[start:start + entry["size"]]
                assert len(content) == entry["size"]
                integrity = entry["integrity"]
                assert integrity["hash"] == hashlib.sha256(content).hexdigest()
                files[path] = ("file", content)

    walk(tree, "")
    return tree, data_start, files


# ------------------------------------------------------------------------------------------------- tool discovery

def sdk_roots():
    roots = [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk") if os.environ.get("LOCALAPPDATA") else None,
             os.path.expanduser("~/Android/Sdk"), os.path.expanduser("~/Library/Android/sdk")]
    return [Path(r) for r in roots if r and Path(r).is_dir()]


def version_key(path):
    return [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", path.name)]


def find_tool(name):
    exe = name + (".exe" if os.name == "nt" else "")
    for root in sdk_roots():
        for bt in sorted((root / "build-tools").glob("*"), key=version_key, reverse=True):
            if (bt / exe).is_file():
                return str(bt / exe)
    return shutil.which(name)


def find_android_jar():
    for root in sdk_roots():
        for p in sorted((root / "platforms").glob("android-*"), key=version_key, reverse=True):
            if (p / "android.jar").is_file():
                return str(p / "android.jar")
    return None


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)


# --------------------------------------------------------------------------------------------------------- cases

def build(tmp, fn, *args, **kwargs):
    path = Path(tmp) / "out.bin"
    if path.exists():
        path.unlink()
    fn(path, *args, **kwargs)
    return path.read_bytes()


ASAR_KINDS = ("traversal", "separator", "slash", "nul-name", "offset-oob", "size-oob", "json-too-long",
              "offset-negative", "offset-hex", "too-deep", "link-outside", "bad-json")


def cases(tmp):
    """name -> bytes, for fixed inputs. Each call builds from scratch."""
    m, s, t = F.SNAGDESK_MANIFEST, F.SNAGDESK_STRINGS, F.SNAGDESK_TYPES
    apk_files = {"assets/index.android.bundle": F.SAMPLE_BUNDLE, F.FLUTTER_LIB: b""}
    info = {"CFBundleIdentifier": "com.example.snag", "CFBundleShortVersionString": "2.0.1", "CFBundleVersion": "7",
            "MinimumOSVersion": "15.0", "NSCameraUsageDescription": "Scan a tag",
            "CFBundleURLTypes": [{"CFBundleURLSchemes": ["snagdesk"]}]}
    return {
        "axml": lambda: F.make_axml(m),
        "proto": lambda: F.make_proto_manifest(m),
        "dex": lambda: F.make_dex(s, t),
        "apk": lambda: build(tmp, F.make_apk, m, s, t, apk_files),
        "aab": lambda: build(tmp, F.make_aab, m, s, t),
        "xapk": lambda: build(tmp, F.make_xapk, {"base.apk": F.apk_bytes(m, s, t), "config.en.apk": F.apk_bytes(
            [(0, "manifest", {"package": F.PACKAGE})], None, None)}, {"manifest.json": '{"package_name": "x"}'}),
        "ipa": lambda: build(tmp, F.make_ipa, info, {"Frameworks/Flutter.framework/Flutter": b"x"}),
        "asar": lambda: build(tmp, F.make_asar, {"package.json": '{"name": "snag", "main": "main.js"}',
                                                 "main.js": "console.log(1)\n", "lib/native.node": b"\x7fELF",
                                                 "empty.txt": b""}, {"lib/native.node"}, {"lib/alias": "native.node"}),
        "zip_slip": lambda: build(tmp, F.make_zip_slip),
        "absolute": lambda: build(tmp, F.make_absolute_path_zip),
        "bomb": lambda: build(tmp, F.make_zip_bomb),
        "many_entries": lambda: build(tmp, F.make_many_entries_zip),
        "nested": lambda: build(tmp, F.make_nested_zip),
        "symlink": lambda: build(tmp, F.make_symlink_zip),
        "encrypted": lambda: build(tmp, F.make_encrypted_member_zip),
        "duplicates": lambda: build(tmp, F.make_duplicate_names_zip),
        "truncated": lambda: build(tmp, F.make_truncated_zip),
        "nul_name": lambda: build(tmp, F.make_nul_name_zip),
        "unsupported_method": lambda: build(tmp, F.make_unsupported_method_zip),
        "corrupt_deflate": lambda: build(tmp, F.make_corrupt_deflate_zip),
        "truncated_member": lambda: build(tmp, F.make_truncated_member_zip),
        "many_directories": lambda: build(tmp, F.make_many_directories_zip),
        "oversized": lambda: build(tmp, F.make_oversized_member_zip),
        "electron_asar": lambda: build(tmp, F.make_asar, F.ELECTRON_FILES),
        "dex039": lambda: F.make_dex(s, t, version="039"),
        "axml_utf8": lambda: F.make_axml(m, utf8=True, attr_size=24, attr_start=24, extra_chunk=(0x0FF0, b"x")),
        **{"asar_" + k.replace("-", "_"): (lambda k=k: build(tmp, F.make_asar_hostile, k)) for k in ASAR_KINDS},
    }


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


# ------------------------------------------------------------------------------------------------- determinism

class TestDeterminism(Base):
    def test_two_calls_are_identical_and_pinned(self):
        first, second = cases(self.tmp), cases(self.tmp)
        self.assertEqual(set(first), set(PINNED))
        for name in sorted(first):
            a, b = first[name](), second[name]()
            self.assertEqual(a, b, name)
            self.assertEqual(hashlib.sha256(a).hexdigest(), PINNED[name], f"{name}: update PINNED if the change is intended")

    def test_zip_timestamps_and_order_are_fixed(self):
        blob = F.apk_bytes(F.SNAGDESK_MANIFEST, ["a"], [], {"z.txt": b"z", "a.txt": b"a"})
        entries = raw_zip(blob)
        self.assertEqual([e["name"] for e in entries], ["AndroidManifest.xml", "classes.dex", "a.txt", "z.txt"])
        self.assertTrue(all(e["time"] == (0, 0x21) and e["system"] == 3 for e in entries))  # 1980-01-01 00:00

    def test_small_fixtures_stay_small(self):
        big = {"bomb", "many_entries", "many_directories", "oversized"}
        for name, make in cases(self.tmp).items():
            if name not in big:
                self.assertLess(len(make()), 100 * 1024, name)
        self.assertLess(len(cases(self.tmp)["bomb"]()), 4 * 1024)


# ------------------------------------------------------------------------------------------------------ AXML

class TestAxml(Base):
    def test_round_trip_of_the_golden_source_manifest(self):
        blob = F.make_axml(F.SNAGDESK_MANIFEST)
        strings, resmap, elements, lines = read_axml(blob)
        self.assertEqual(strip_ids(elements), expected(F.SNAGDESK_MANIFEST))
        self.assertEqual(lines, sorted(lines))

    def test_pool_is_aapt2_ordered_and_utf16(self):
        strings, resmap, elements, _ = read_axml(F.make_axml(F.SNAGDESK_MANIFEST))
        self.assertEqual(resmap, sorted(set(resmap)))
        names = [strings[i] for i in range(len(resmap))]
        self.assertEqual(names[:3], ["theme", "label", "name"])
        self.assertEqual(strings[len(resmap):], sorted(strings[len(resmap):]))
        for _, _, attrs in elements:
            for key, (resid, _) in attrs.items():
                if key.startswith("android:"):
                    self.assertEqual(resid, F.RES_IDS[key[8:]], key)
                else:
                    self.assertIsNone(resid)

    def test_attributes_with_ids_come_first_in_id_order(self):
        blob = F.make_axml([(0, "manifest", {"package": "p", "android:versionName": "1", "android:versionCode": 3})])
        _, _, elements, _ = read_axml(blob)
        self.assertEqual(list(elements[0][2]), ["android:versionCode", "android:versionName", "package"])

    def test_value_types(self):
        values = {"android:exported": True, "android:enabled": False, "android:versionCode": -5,
                  "android:priority": 2147483647, "android:configChanges": F.Hex(0x4A0), "android:theme": F.Ref(0x7F010001),
                  "android:label": "caf\u00e9 \U0001f600"}
        _, _, elements, _ = read_axml(F.make_axml([(0, "x", values)]))
        self.assertEqual({k: t for k, (_, t) in elements[0][2].items()}, {k: typed(v) for k, v in values.items()})

    def test_empty_attribute_name_keeps_only_the_resource_id(self):
        blob = F.make_axml([(0, "manifest", {"android:@0x01010003": "x"})])
        strings, resmap, elements, _ = read_axml(blob)
        self.assertEqual(resmap, [0x01010003])
        self.assertEqual(strings[0], "")
        self.assertEqual(elements[0][2], {"android:@0x01010003": (0x01010003, ("s", "x"))})

    def test_long_string_uses_the_two_unit_length(self):
        text = "x" * 40000
        strings, _, elements, _ = read_axml(F.make_axml([(0, "m", {"v": text})]))
        self.assertIn(text, strings)
        self.assertEqual(elements[0][2]["v"][1], ("s", text))

    def test_namespaces_other_than_android(self):
        blob = F.make_axml([(0, "m", {"tools:ignore": "x", "app:y": "z"})])
        _, _, elements, _ = read_axml(blob)
        self.assertEqual(set(elements[0][2]), {"tools:ignore", "app:y"})

    def test_element_nesting_is_validated(self):
        for bad in ([], [(1, "a", {})], [(0, "a", {}), (2, "b", {})], [(0, "a", {}), (0, "b", {})]):
            with self.assertRaises(ValueError):
                F.make_axml(bad)

    def test_matches_the_aapt2_golden(self):
        golden = (DATA / "manifest.axml").read_bytes()
        g_strings, g_map, g_elements, _ = read_axml(golden)
        source = elements_from_xml(DATA / "AndroidManifest.xml")
        self.assertEqual(source, expected(F.SNAGDESK_MANIFEST))
        # aapt2 adds four attributes to <manifest>; everything else is identical
        extras = {k: typed(v) for k, v in F.AAPT2_ADDED_ATTRS.items()}
        g = strip_ids(g_elements)
        for key, value in extras.items():
            self.assertEqual(g[0][2].pop(key), value)
        self.assertEqual(g, expected(F.SNAGDESK_MANIFEST))
        # resource ids and the resource map agree with the encoder's, name by name
        ours = read_axml(F.make_axml(F.SNAGDESK_MANIFEST))
        ids_ours = {k: i for _, _, a in ours[2] for k, (i, _) in a.items()}
        ids_gold = {k: i for _, _, a in g_elements for k, (i, _) in a.items()}
        for key in ids_ours:
            self.assertEqual(ids_ours[key], ids_gold[key], key)

    def test_equals_the_golden_byte_for_byte_except_line_numbers(self):
        golden = (DATA / "manifest.axml").read_bytes()
        elements = copy.deepcopy(F.SNAGDESK_MANIFEST)
        elements[0][2].update(F.AAPT2_ADDED_ATTRS)
        ours = F.make_axml(elements)
        self.assertEqual(len(ours), len(golden))
        self.assertEqual(mask_lines(ours), mask_lines(golden))
        self.assertNotEqual(ours, golden)  # only the line numbers differ: ours are element numbers, aapt2's are source lines


# Attributes `aapt2 link` stamps into <manifest> from the installed SDK, not from the source XML: they change with the
# platform android.jar and the build-tools (36 and "16" for the goldens; a newer SDK writes its own values).
SDK_STAMPED = frozenset(F.AAPT2_ADDED_ATTRS)


def without_sdk_stamps(elements):
    """elements (read_axml / read_proto shape) without the SDK_STAMPED attributes; everything else is untouched."""
    return [(d, n, {k: v for k, v in a.items() if k not in SDK_STAMPED}) for d, n, a in elements]


def elements_from_xml(path):
    """The golden source XML read with ElementTree and typed with the schema knowledge a parser has (the test's own
    reading of the source, independent of the encoder)."""
    ints = {"versionCode", "minSdkVersion", "targetSdkVersion"}
    bools = {"exported", "allowBackup", "usesCleartextTraffic", "autoVerify"}
    refs = {"@android:style/Theme.Material": 0x01030224}
    out = []

    def walk(el, depth):
        attrs = {}
        for key, value in el.attrib.items():
            if key.startswith("{" + ANDROID_NS + "}"):
                local = key.split("}")[1]
                name = "android:" + local
            else:
                local, name = key, key
            if local in ints:
                attrs[name] = ("i", int(value))
            elif local in bools:
                attrs[name] = ("b", value == "true")
            elif value in refs:
                attrs[name] = ("r", refs[value])
            else:
                attrs[name] = ("s", value)
        out.append((depth, el.tag, attrs))
        for child in el:
            walk(child, depth + 1)

    walk(ET.parse(path).getroot(), 0)
    return out


# ------------------------------------------------------------------------------------------------------ proto

class TestProto(Base):
    def test_round_trip(self):
        elements, texts = read_proto(F.make_proto_manifest(F.SNAGDESK_MANIFEST))
        self.assertEqual(strip_ids(elements), expected(F.SNAGDESK_MANIFEST))
        self.assertTrue(texts and all(set(t) <= {"\n", " "} for t in texts))
        ids = {k: i for _, _, a in elements for k, (i, _) in a.items()}
        self.assertEqual(ids["android:name"], 0x01010003)
        self.assertIsNone(ids["package"])

    def test_without_text_nodes(self):
        elements, texts = read_proto(F.make_proto_manifest(F.SNAGDESK_MANIFEST, text=False))
        self.assertEqual(texts, [])
        self.assertEqual(len(elements), len(F.SNAGDESK_MANIFEST))

    def test_starts_with_the_element_field(self):
        self.assertEqual(F.make_proto_manifest(F.SNAGDESK_MANIFEST)[0], 0x0A)

    def test_value_types_and_negative_numbers(self):
        values = {"android:exported": False, "android:versionCode": -1, "android:configChanges": F.Hex(0x4A0),
                  "android:label": "x"}
        elements, _ = read_proto(F.make_proto_manifest([(0, "m", values)]))
        self.assertEqual({k: t for k, (_, t) in elements[0][2].items()}, {k: typed(v) for k, v in values.items()})

    def test_empty_attribute_names_are_refused(self):
        with self.assertRaises(ValueError):
            F.make_proto_manifest([(0, "m", {"android:@0x01010003": "x"})])

    def test_matches_the_aapt2_golden(self):
        g_elements, g_texts = read_proto((DATA / "manifest.proto").read_bytes())
        g = strip_ids(g_elements)
        extras = {k: typed(v) for k, v in F.AAPT2_ADDED_ATTRS.items()}
        for key, value in extras.items():
            self.assertEqual(g[0][2].pop(key), value)
        ours, _ = read_proto(F.make_proto_manifest(F.SNAGDESK_MANIFEST))
        self.assertEqual(g, strip_ids(ours))
        self.assertEqual({k: i for _, _, a in g_elements for k, (i, _) in a.items()},
                         {k: i for _, _, a in ours for k, (i, _) in a.items()} | {
                             "android:compileSdkVersion": 0x01010572, "android:compileSdkVersionCodename": 0x01010573,
                             "platformBuildVersionCode": None, "platformBuildVersionName": None})
        self.assertTrue(g_texts)  # aapt2 keeps the whitespace text nodes, like text=True does

    def test_golden_proto_and_axml_describe_the_same_manifest(self):
        proto, _ = read_proto((DATA / "manifest.proto").read_bytes())
        axml = read_axml((DATA / "manifest.axml").read_bytes())[2]
        self.assertEqual(strip_ids(proto), strip_ids(axml))


# -------------------------------------------------------------------------------------------------------- dex

class TestDex(Base):
    def test_structure_checksum_and_signature(self):
        strings, types = read_dex(F.make_dex(F.SNAGDESK_STRINGS, F.SNAGDESK_TYPES))
        self.assertEqual(set(strings), set(F.SNAGDESK_STRINGS) | set(F.SNAGDESK_TYPES))
        self.assertEqual(types, sorted(F.SNAGDESK_TYPES, key=lambda s: s.encode("utf-16-be")))

    def test_duplicates_are_merged_and_the_input_order_does_not_matter(self):
        a = F.make_dex(["b", "a", "a"], ["Lx/B;", "Lx/A;", "Lx/A;"])
        self.assertEqual(a, F.make_dex(["a", "b"], ["Lx/A;", "Lx/B;"]))
        read_dex(a)

    def test_empty_and_strings_only(self):
        self.assertEqual(read_dex(F.make_dex([], [])), ([], []))
        self.assertEqual(read_dex(F.make_dex(["only"], [])), (["only"], []))

    def test_mutf8_forms(self):
        odd = ["", "nul\0inside", "caf\u00e9", "\u20ac uro", "\U0001f600 smile", "x" * 200]
        strings, _ = read_dex(F.make_dex(odd, []))
        self.assertEqual(set(strings), set(odd))
        raw = F.make_dex(["a\0b"], [])
        self.assertIn(b"a\xc0\x80b\0", raw)  # NUL is the two-byte form, the terminator is a real NUL
        self.assertIn(b"\xed\xa0\xbd\xed\xb8\x80", F.make_dex(["\U0001f600"], []))  # surrogates, 3 bytes each

    def test_bad_type_descriptor_is_refused(self):
        with self.assertRaises(ValueError):
            F.make_dex([], ["com.example.Foo"])

    def test_header_offsets_a_parser_uses(self):
        b = F.make_dex(F.SNAGDESK_STRINGS, F.SNAGDESK_TYPES)
        file_size, = struct.unpack_from("<I", b, 0x20)
        nstr, stroff, ntype, typeoff = struct.unpack_from("<4I", b, 0x38)
        self.assertEqual((file_size, stroff, typeoff), (len(b), 0x70, 0x70 + 4 * nstr))
        self.assertEqual(ntype, len(F.SNAGDESK_TYPES))


# ------------------------------------------------------------------------------------------------------- zips

class TestApkAabXapkIpa(Base):
    def test_apk(self):
        path = F.make_apk(self.tmp / "a.apk", F.SNAGDESK_MANIFEST, F.SNAGDESK_STRINGS, F.SNAGDESK_TYPES,
                          {"assets/index.android.bundle": F.SAMPLE_BUNDLE, F.FLUTTER_LIB: b""})
        with zipfile.ZipFile(path) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(z.namelist(), ["AndroidManifest.xml", "classes.dex", "assets/index.android.bundle", F.FLUTTER_LIB])
            self.assertEqual(z.read(F.FLUTTER_LIB), b"")
            self.assertEqual(z.read("assets/index.android.bundle").decode(), F.SAMPLE_BUNDLE)
            blob = z.read("AndroidManifest.xml")
            self.assertEqual(blob[:4], bytes.fromhex("03000800"))
            self.assertEqual(strip_ids(read_axml(blob)[2]), expected(F.SNAGDESK_MANIFEST))
            read_dex(z.read("classes.dex"))
            self.assertEqual({i.compress_type for i in z.infolist()}, {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})

    def test_apk_accepts_manifest_bytes_and_no_dex(self):
        golden = (DATA / "manifest.axml").read_bytes()
        with zipfile.ZipFile(io.BytesIO(F.apk_bytes(golden, None, None))) as z:
            self.assertEqual(z.namelist(), ["AndroidManifest.xml"])
            self.assertEqual(z.read("AndroidManifest.xml"), golden)

    def test_aab(self):
        path = F.make_aab(self.tmp / "a.aab", F.SNAGDESK_MANIFEST, F.SNAGDESK_STRINGS, F.SNAGDESK_TYPES)
        with zipfile.ZipFile(path) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(sorted(z.namelist()), ["BundleConfig.pb", "base/dex/classes.dex", "base/manifest/AndroidManifest.xml"])
            self.assertEqual(z.read("BundleConfig.pb"), b"")
            proto = z.read("base/manifest/AndroidManifest.xml")
            self.assertEqual(proto[0], 0x0A)
            self.assertEqual(strip_ids(read_proto(proto)[0]), expected(F.SNAGDESK_MANIFEST))
            read_dex(z.read("base/dex/classes.dex"))

    def test_xapk_holds_apks(self):
        inner = F.apk_bytes(F.SNAGDESK_MANIFEST, ["x"], [])
        path = F.make_xapk(self.tmp / "a.xapk", {"com.example.snagdesk.apk": inner, "config.arm64.apk": inner}, {"manifest.json": "{}"})
        with zipfile.ZipFile(path) as z:
            self.assertEqual(z.namelist(), ["com.example.snagdesk.apk", "config.arm64.apk", "manifest.json"])
            with zipfile.ZipFile(io.BytesIO(z.read("config.arm64.apk"))) as apk:
                self.assertIn("AndroidManifest.xml", apk.namelist())

    def test_xapk_reads_apk_paths(self):
        apk = F.make_apk(self.tmp / "in.apk", F.SNAGDESK_MANIFEST, [], [])
        path = F.make_xapk(self.tmp / "b.xapk", {"base.apk": apk})
        with zipfile.ZipFile(path) as z:
            self.assertEqual(z.read("base.apk"), apk.read_bytes())

    def test_ipa(self):
        info = {"CFBundleIdentifier": "com.example.snag", "CFBundleShortVersionString": "2.0.1", "CFBundleVersion": "7",
                "MinimumOSVersion": "15.0", "UIBackgroundModes": ["fetch"], "NSCameraUsageDescription": "Scan a tag",
                "CFBundleURLTypes": [{"CFBundleURLSchemes": ["snagdesk"]}],
                "NSAppTransportSecurity": {"NSExceptionDomains": {"legacy.example.test": {"NSTemporaryExceptionAllowsInsecureHTTPLoads": True}}}}
        path = F.make_ipa(self.tmp / "a.ipa", info, {"Frameworks/Flutter.framework/Flutter": b"x"})
        with zipfile.ZipFile(path) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(z.namelist(), ["Payload/Snag.app/Frameworks/Flutter.framework/Flutter", "Payload/Snag.app/Info.plist",
                                            "Payload/Snag.app/embedded.mobileprovision", "Payload/Snag.app/main.jsbundle"])
            raw = z.read("Payload/Snag.app/Info.plist")
            self.assertTrue(raw.startswith(b"bplist00"))
            self.assertEqual(plistlib.loads(raw), info)
            profile = z.read("Payload/Snag.app/embedded.mobileprovision")
            start, end = profile.index(b"<?xml"), profile.index(b"</plist>") + len(b"</plist>")
            self.assertGreater(start, 0)
            self.assertGreater(len(profile), end)  # junk before and after the XML plist
            ents = plistlib.loads(profile[start:end])["Entitlements"]
            self.assertEqual(ents["aps-environment"], "development")
            self.assertEqual(ents["com.apple.developer.associated-domains"], ["applinks:snagdesk.example.test"])
            self.assertIn("get-task-allow", ents)  # keys a lane must drop
            self.assertIn(F.FAKE_AWS_KEY.encode(), z.read("Payload/Snag.app/main.jsbundle"))

    def test_ipa_files_override_defaults(self):
        blob = F.ipa_bytes({"CFBundleIdentifier": "x"}, {"main.jsbundle": b"// mine"})
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            self.assertEqual(z.read("Payload/Snag.app/main.jsbundle"), b"// mine")


class TestAsar(Base):
    def test_layout_and_content(self):
        files = {"package.json": '{"name": "snag"}', "src/main.js": "x=1", "src/deep/a.js": "y", "empty": b"",
                 "native/addon.node": b"\x7fELF-unpacked"}
        path = F.make_asar(self.tmp / "app.asar", files, {"native/addon.node"}, {"src/alias.js": "main.js"})
        blob = path.read_bytes()
        tree, start, got = read_asar(blob)
        self.assertEqual(got["package.json"], ("file", b'{"name": "snag"}'))
        self.assertEqual(got["src/deep/a.js"], ("file", b"y"))
        self.assertEqual(got["empty"], ("file", b""))
        self.assertEqual(got["src/alias.js"], ("link", "main.js"))
        self.assertEqual(got["native/addon.node"], ("unpacked", len(files["native/addon.node"])))
        self.assertEqual(len(blob) - start, sum(len(v) for k, v in files.items() if k != "native/addon.node"))
        self.assertTrue(tree["files"]["src"]["files"]["deep"]["files"]["a.js"]["offset"].isdigit())  # decimal string
        side = Path(str(path) + ".unpacked") / "native" / "addon.node"
        self.assertEqual(side.read_bytes(), files["native/addon.node"])
        self.assertNotIn(b"ELF-unpacked", blob)

    def test_offsets_are_unique_and_contiguous(self):
        blob = F.asar_bytes({"a": "aaa", "b": "bb", "c": "c"})[0]
        tree, start, _ = read_asar(blob)
        spans = sorted((int(e["offset"]), e["size"]) for e in tree["files"].values())
        self.assertEqual(spans, [(0, 3), (3, 2), (5, 1)])

    def test_unpacked_must_be_in_files(self):
        with self.assertRaises(ValueError):
            F.asar_bytes({"a": "x"}, {"b"})

    def test_hostile_kinds(self):
        for kind in ("traversal", "separator", "slash", "offset-oob", "size-oob", "json-too-long"):
            blob = F.make_asar_hostile(self.tmp / f"{kind}.asar", kind).read_bytes()
            four, header_size, payload, json_len = struct.unpack_from("<IIII", blob, 0)
            self.assertEqual(four, 4, kind)
            if kind == "json-too-long":
                self.assertGreater(16 + json_len, len(blob))
                continue
            tree = json.loads(blob[16:16 + json_len])
            data_len = len(blob) - (8 + header_size)
            (name, entry), = tree["files"].items()
            if kind in ("traversal", "separator", "slash"):
                self.assertIn(name, {"..", "a\\b", "a/b"})
            elif kind == "offset-oob":
                self.assertGreater(int(entry["offset"]), data_len)
            else:
                self.assertGreater(entry["size"], data_len)
        with self.assertRaises(ValueError):
            F.make_asar_hostile(self.tmp / "x.asar", "nope")


class TestHostileZips(Base):
    def entries(self, path):
        blob = Path(path).read_bytes()
        return blob, raw_zip(blob)

    def test_zip_slip_names(self):
        _, entries = self.entries(F.make_zip_slip(self.tmp / "slip.zip"))
        names = [e["name"] for e in entries]
        self.assertIn("../evil.txt", names)
        self.assertIn("..\\evil3.txt", names)
        self.assertTrue(any(".." in n.replace("\\", "/").split("/") for n in names))
        self.assertEqual(sum(".." in n.replace("\\", "/").split("/") for n in names), len(names) - 1)  # all but ok.txt
        with zipfile.ZipFile(self.tmp / "slip.zip") as z:  # the standard library lists them as written
            self.assertEqual([n.replace("\\", "/") for n in z.namelist()], [n.replace("\\", "/") for n in names])

    def test_absolute_names(self):
        _, entries = self.entries(F.make_absolute_path_zip(self.tmp / "abs.zip"))
        names = [e["name"] for e in entries]
        self.assertTrue({"/etc/evil.txt", "C:/Windows/evil.txt", "C:\\evil2.txt"} <= set(names))
        self.assertTrue(any(re.match(r"^[A-Za-z]:", n) for n in names))
        self.assertTrue(any(n.startswith("/") for n in names))

    def test_zip_bomb_ratio(self):
        blob, entries = self.entries(F.make_zip_bomb(self.tmp / "bomb.zip"))
        bomb = next(e for e in entries if e["name"] == "bomb.bin")
        self.assertEqual(bomb["usize"], 2 * 1024 * 1024)
        self.assertEqual(bomb["method"], 8)
        self.assertGreater(bomb["usize"], 1024 * 1024)
        self.assertGreater(bomb["usize"], 100 * bomb["csize"])
        self.assertLess(len(blob), 4096)
        start = bomb["offset"] + 30 + len(b"bomb.bin")
        data = zlib.decompress(blob[start:start + bomb["csize"]], -15)  # real inflate, independent of zipfile
        self.assertEqual(data, b"\0" * bomb["usize"])
        self.assertEqual(zlib.crc32(data), bomb["crc"])
        with zipfile.ZipFile(self.tmp / "bomb.zip") as z:
            self.assertIsNone(z.testzip())

    def test_zip_bomb_size_parameter(self):
        for size in (1, 257, 258, 259, 1000, 70000):
            blob = F.make_zip_bomb(self.tmp / "b.zip", size=size).read_bytes()
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                self.assertEqual(z.read("bomb.bin"), b"\0" * size)

    def test_many_entries(self):
        blob, entries = self.entries(F.make_many_entries_zip(self.tmp / "many.zip"))
        self.assertEqual(len(entries), 50_001)
        self.assertEqual(len({e["name"] for e in entries}), 50_001)
        self.assertEqual(struct.unpack_from("<H", blob, blob.rfind(b"PK\x05\x06") + 10)[0], 50_001)
        self.assertEqual(len(F.make_many_entries_zip(self.tmp / "few.zip", count=7).read_bytes()) > 0, True)
        with zipfile.ZipFile(self.tmp / "few.zip") as z:
            self.assertEqual(len(z.namelist()), 7)

    def descend(self, blob):
        """names of the nested zip members, outermost first, and the innermost archive's member list"""
        chain = []
        while True:
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                inner = [n for n in z.namelist() if n.endswith(".apk")]
                if not inner:
                    return chain, z.namelist()
                self.assertEqual(z.namelist(), inner)  # a level holds only the next one
                chain.append(inner[0])
                blob = z.read(inner[0])

    def test_nested_apks_in_an_xapk_beyond_the_depth_limit(self):
        # contract 6.2: depth 2 (XAPK -> APK) is allowed, anything deeper is not; nested members are .apk files
        for depth, allowed in ((1, True), (2, True), (3, False)):
            path = F.make_nested_zip(self.tmp / f"nest{depth}.xapk", depth=depth)
            chain, innermost = self.descend(path.read_bytes())
            self.assertEqual(chain, [f"inner{n}.apk" for n in range(1, depth + 1)])
            self.assertIn("AndroidManifest.xml", innermost)  # the innermost is a real apk a lane could read
            self.assertEqual(len(chain) <= 2, allowed)

    def test_nested_member_name_is_a_parameter(self):
        path = F.make_nested_zip(self.tmp / "n.zip", depth=2, member="level-{level}.zip")
        with zipfile.ZipFile(path) as z:
            self.assertEqual(z.namelist(), ["level-1.zip"])
        self.assertEqual(F.make_nested_zip.__defaults__[1], "inner{level}.apk")

    def test_symlink_member(self):
        _, entries = self.entries(F.make_symlink_zip(self.tmp / "sym.zip"))
        by = {e["name"]: e for e in entries}
        self.assertEqual((by["link"]["external"] >> 16) & 0o170000, 0o120000)
        self.assertEqual((by["dirlink"]["external"] >> 16) & 0o170000, 0o120000)
        self.assertEqual((by["ok.txt"]["external"] >> 16) & 0o170000, 0o100000)
        self.assertEqual(by["link"]["system"], 3)
        with zipfile.ZipFile(self.tmp / "sym.zip") as z:
            self.assertEqual(z.read("link"), b"/etc/passwd")

    def test_encrypted_member(self):
        _, entries = self.entries(F.make_encrypted_member_zip(self.tmp / "enc.zip"))
        by = {e["name"]: e for e in entries}
        self.assertEqual(by["secret.txt"]["flags"] & 1, 1)
        self.assertEqual(by["ok.txt"]["flags"] & 1, 0)
        with zipfile.ZipFile(self.tmp / "enc.zip") as z:
            self.assertTrue(z.getinfo("secret.txt").flag_bits & 1)
            with self.assertRaises(RuntimeError):
                z.read("secret.txt")
            self.assertEqual(z.read("ok.txt"), b"fine\n")

    def test_duplicate_names(self):
        _, entries = self.entries(F.make_duplicate_names_zip(self.tmp / "dup.zip"))
        names = [e["name"] for e in entries]
        self.assertEqual(names.count("app/config.json"), 2)
        self.assertEqual(len({e["crc"] for e in entries if e["name"] == "app/config.json"}), 2)  # different content

    def test_oversized_member(self):
        size = 3 * 1024 * 1024 + 5
        blob, entries = self.entries(F.make_oversized_member_zip(self.tmp / "big.zip", size=size, stored=True))
        (big,) = entries
        self.assertEqual((big["method"], big["usize"], big["csize"]), (0, size, size))  # stored: no ratio to trip
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            self.assertIsNone(z.testzip())
        self.assertEqual(F.make_oversized_member_zip.__defaults__, (64 * 1024 * 1024 + 1, False))

    def test_truncated_zip(self):
        blob = F.make_truncated_zip(self.tmp / "cut.zip").read_bytes()
        self.assertEqual(blob.find(b"PK\x05\x06"), -1)
        self.assertGreater(blob.find(b"PK\x01\x02"), 0)  # cut inside the central directory
        with self.assertRaises(zipfile.BadZipFile):
            zipfile.ZipFile(io.BytesIO(blob))

    def test_every_hostile_zip_is_a_valid_container_otherwise(self):
        for fn in (F.make_zip_slip, F.make_absolute_path_zip, F.make_symlink_zip, F.make_duplicate_names_zip):
            path = fn(self.tmp / (fn.__name__ + ".zip"))
            raw_zip(path.read_bytes())
            with zipfile.ZipFile(path) as z:
                self.assertTrue(z.namelist())

    def test_zip_writer_stored_and_deflated_members_inflate(self):
        payloads = [b"", b"a", bytes(range(256)) * 4, b"hello world " * 50, bytes(300)]
        blob = F.zip_bytes([F.ZipEntry(f"m{i}", p) for i, p in enumerate(payloads)] +
                           [F.ZipEntry("forced", b"abc", method=8), F.ZipEntry("unicode-\u00e9", b"u")])
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            self.assertIsNone(z.testzip())
            for i, p in enumerate(payloads):
                self.assertEqual(z.read(f"m{i}"), p)
            self.assertEqual(z.getinfo("forced").compress_type, zipfile.ZIP_DEFLATED)
            self.assertTrue(z.getinfo("unicode-\u00e9").flag_bits & 0x800)
        with self.assertRaises(ValueError):
            F.zip_bytes([F.ZipEntry(f"{i}", b"") for i in range(0x10000)])


# ------------------------------------------------------------------------------------------ the SDK, when present

AAPT2 = find_tool("aapt2")
DEXDUMP = find_tool("dexdump")
ANDROID_JAR = find_android_jar()


@unittest.skipUnless(AAPT2, "aapt2 not found (Android SDK build-tools)")
class TestWithAapt2(Base):
    def test_aapt2_reads_our_axml(self):
        apk = F.make_apk(self.tmp / "a.apk", F.SNAGDESK_MANIFEST, F.SNAGDESK_STRINGS, F.SNAGDESK_TYPES)
        out = run([AAPT2, "dump", "xmltree", str(apk), "--file", "AndroidManifest.xml"])
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("com.example.snagdesk", out.stdout)
        self.assertIn("snagdesk.example.test", out.stdout)
        self.assertRegex(out.stdout, r"versionCode\(0x0101021b\)=(\(type 0x10\))?(0x)?(2a|42)(?!\d)")

    def test_aapt2_badging_accepts_the_apk(self):
        apk = F.make_apk(self.tmp / "a.apk", F.SNAGDESK_MANIFEST, F.SNAGDESK_STRINGS, F.SNAGDESK_TYPES)
        out = run([AAPT2, "dump", "badging", str(apk)])
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertRegex(out.stdout, r"package: name='com\.example\.snagdesk' versionCode='42' versionName='1\.2\.3'")

    def test_aapt2_reads_the_axml_variants_like_the_default(self):
        # same tool, same SDK: the dump of each variant must equal the dump of the default layout
        def dump(**options):
            apk = F.make_apk(self.tmp / "v.apk", F.make_axml(F.SNAGDESK_MANIFEST, **options), None, None)
            out = run([AAPT2, "dump", "xmltree", str(apk), "--file", "AndroidManifest.xml"])
            self.assertEqual(out.returncode, 0, f"{options}: {out.stderr}")
            return out.stdout
        base = dump()
        for options in ({"utf8": True}, {"attr_size": 24, "attr_start": 28}, {"extra_chunk": (0x0FF0, b"abcd")},
                        {"utf8": True, "attr_size": 24, "attr_start": 24, "extra_chunk": (0x0FF0, b"abcd")}):
            self.assertEqual(dump(**options), base, options)

    def test_aapt2_converts_our_proto_manifest_back_to_axml(self):
        proto_apk = F.make_apk(self.tmp / "p.apk", F.make_proto_manifest(F.SNAGDESK_MANIFEST), None, None,
                               {"resources.pb": b""})
        out = run([AAPT2, "convert", "--output-format", "binary", "-o", str(self.tmp / "c.apk"), str(proto_apk)])
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        with zipfile.ZipFile(self.tmp / "c.apk") as z:
            converted = z.read("AndroidManifest.xml")
        self.assertEqual(strip_ids(read_axml(converted)[2]), expected(F.SNAGDESK_MANIFEST))
        self.assertEqual(mask_lines(converted), mask_lines(F.make_axml(F.SNAGDESK_MANIFEST)))

    def link(self, name, *extra, proto=False):
        out = run([AAPT2, "link", *(["--proto-format"] if proto else []), "--manifest", str(DATA / "AndroidManifest.xml"),
                   "-I", ANDROID_JAR, *extra, "-o", str(self.tmp / name)])
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        with zipfile.ZipFile(self.tmp / name) as z:
            return z.read("AndroidManifest.xml")

    @unittest.skipUnless(ANDROID_JAR, "android.jar not found")
    def test_aapt2_links_the_golden_source_to_the_golden(self):
        golden = without_sdk_stamps(read_axml((DATA / "manifest.axml").read_bytes())[2])
        # the installed SDK is whatever it is; a second run forces different stamps to prove they are ignored
        for extra in ((), ("--compile-sdk-version-code", "37", "--compile-sdk-version-name", "17"),
                      ("--no-compile-sdk-metadata",)):
            fresh = without_sdk_stamps(read_axml(self.link("l.apk", *extra))[2])
            self.assertEqual(strip_ids(fresh), strip_ids(golden), extra)
            self.assertEqual(fresh, golden, extra)  # resource ids too

    @unittest.skipUnless(ANDROID_JAR, "android.jar not found")
    def test_aapt2_proto_link_matches_the_proto_golden(self):
        golden = without_sdk_stamps(read_proto((DATA / "manifest.proto").read_bytes())[0])
        for extra in ((), ("--compile-sdk-version-code", "37", "--compile-sdk-version-name", "17")):
            fresh = without_sdk_stamps(read_proto(self.link("p.apk", *extra, proto=True))[0])
            self.assertEqual(fresh, golden, extra)

    @unittest.skipUnless(ANDROID_JAR, "android.jar not found")
    def test_res_ids_match_android_jar(self):
        out = run([AAPT2, "dump", "resources", ANDROID_JAR])
        self.assertEqual(out.returncode, 0, out.stderr)
        real = {m.group(2): int(m.group(1), 16) for m in re.finditer(r"resource (0x[0-9a-f]{8}) attr/(\w+)", out.stdout)}
        if not real:
            self.skipTest("this aapt2 cannot list the attr table of android.jar")
        for name, resid in F.RES_IDS.items():
            if name in real:  # an attribute missing from an older platform is not a failure; a different id is
                self.assertEqual(real[name], resid, name)


@unittest.skipUnless(DEXDUMP, "dexdump not found (Android SDK build-tools)")
class TestWithDexdump(Base):
    def test_dexdump_accepts_our_dex(self):
        path = self.tmp / "classes.dex"
        path.write_bytes(F.make_dex(F.SNAGDESK_STRINGS, F.SNAGDESK_TYPES))
        out = run([DEXDUMP, "-f", str(path)])
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("035", out.stdout)  # the format version; the layout of the other lines varies with the tool
        self.assertRegex(out.stdout, r"string_ids_size\s*:\s*%d\b" % len(set(F.SNAGDESK_STRINGS) | set(F.SNAGDESK_TYPES)))
        self.assertRegex(out.stdout, r"type_ids_size\s*:\s*%d\b" % len(F.SNAGDESK_TYPES))

    def test_dexdump_accepts_the_small_shapes(self):
        for n, (s, t) in enumerate([([], []), (["x"], []), ([], ["I"]), (["caf\u00e9", "\U0001f600"], ["[B"])]):
            path = self.tmp / f"{n}.dex"
            path.write_bytes(F.make_dex(s, t))
            out = run([DEXDUMP, "-f", str(path)])
            self.assertEqual(out.returncode, 0, f"{n}: {out.stdout}{out.stderr}")

    def test_dexdump_checks_the_checksum(self):
        # `dexdump -c` verifies the checksum and exits: success on ours, failure once a byte is flipped
        good = self.tmp / "good.dex"
        good.write_bytes(F.make_dex(F.SNAGDESK_STRINGS, F.SNAGDESK_TYPES))
        self.assertEqual(run([DEXDUMP, "-c", str(good)]).returncode, 0)
        raw = good.read_bytes()
        bad = self.tmp / "bad.dex"
        bad.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))  # breaks adler32 and sha1
        self.assertNotEqual(run([DEXDUMP, "-c", str(bad)]).returncode, 0)

    def test_dexdump_accepts_the_later_versions(self):
        for version in ("037", "038", "039"):
            path = self.tmp / f"{version}.dex"
            path.write_bytes(F.make_dex(F.SNAGDESK_STRINGS, F.SNAGDESK_TYPES, version=version))
            out = run([DEXDUMP, "-f", str(path)])
            self.assertEqual(out.returncode, 0, f"{version}: {out.stdout}{out.stderr}")

    def test_dexdump_accepts_the_apk_dex(self):
        path = self.tmp / "a.apk"
        F.make_apk(path, F.SNAGDESK_MANIFEST, F.SNAGDESK_STRINGS, F.SNAGDESK_TYPES)
        with zipfile.ZipFile(path) as z:
            (self.tmp / "classes.dex").write_bytes(z.read("classes.dex"))
        self.assertEqual(run([DEXDUMP, "-c", str(self.tmp / "classes.dex")]).returncode, 0)


# ----------------------------------------------------------------------------------------- review round: new tests

class TestScannerMarkerAndConstants(Base):
    def test_no_fixture_carries_the_scanner_marker(self):
        # a line with the marker is skipped by learn.py scan-private, so a marker inside fixture bytes would hide a leak
        self.assertNotIn("scan-private", F.SAMPLE_BUNDLE)
        for name, make in cases(self.tmp).items():
            self.assertNotIn(b"scan-private", make(), name)
        for path in DATA.iterdir():
            self.assertNotIn(b"scan-private", path.read_bytes(), path.name)

    def test_fake_key_is_built_from_pieces_and_reaches_the_bundles(self):
        self.assertEqual(F.FAKE_AWS_KEY, "AKIA" + "IOSFODNN7" + "EXAMPLE")
        self.assertRegex(F.FAKE_AWS_KEY, r"^AKIA[0-9A-Z]{16}$")  # the shape lanes must scrub (contract 6.2 aws-key)
        self.assertIn(F.FAKE_AWS_KEY, F.SAMPLE_BUNDLE)
        source = (Path(__file__).resolve().parent / "fixtures_archives.py").read_text(encoding="utf-8")
        self.assertNotIn(F.FAKE_AWS_KEY, source)  # the repo scanner needs no exemption

    def test_exported_constants(self):
        self.assertEqual(set(F.AAPT2_ADDED_ATTRS), {"android:compileSdkVersion", "android:compileSdkVersionCodename",
                                                    "platformBuildVersionCode", "platformBuildVersionName"})
        self.assertEqual(F.HERMES_MAGIC, bytes([0xC6, 0x1F, 0xBC, 0x03, 0xC1, 0x03, 0x19, 0x1F]))
        self.assertTrue(F.HERMES_BUNDLE.startswith(F.HERMES_MAGIC))
        with zipfile.ZipFile(io.BytesIO(F.ipa_bytes(F.IPA_INFO))) as z:
            self.assertEqual(plistlib.loads(z.read("Payload/Snag.app/Info.plist")), F.IPA_INFO)
        for key in ("CFBundleIdentifier", "CFBundleShortVersionString", "CFBundleVersion", "MinimumOSVersion",
                    "UIBackgroundModes", "CFBundleURLTypes", "NSAppTransportSecurity", "NSCameraUsageDescription"):
            self.assertIn(key, F.IPA_INFO)

    def test_electron_file_set(self):
        _, _, got = read_asar(F.asar_bytes(F.ELECTRON_FILES)[0])
        text = {k: v[1].decode() for k, v in got.items() if v[0] == "file"}
        main = json.loads(text["package.json"])["main"]
        self.assertIn(main, text)
        for needle in ('ipcMain.handle("', 'ipcMain.on("', "webContents.send(", "nodeIntegration: true",
                       "contextIsolation: false", "webSecurity: false", 'setAsDefaultProtocolClient("snagdesk")',
                       "preload:", "autoUpdater"):
            self.assertIn(needle, text[main], needle)
        preload = next(v for k, v in text.items() if "contextBridge" in v)
        self.assertIn('contextBridge.exposeInMainWorld("api"', preload)
        self.assertIn('ipcRenderer.invoke("', preload)
        self.assertIn('ipcRenderer.send("', preload)
        self.assertTrue(any(k.endswith(".html") for k in text))


class TestAxmlVariants(Base):
    ELEMENTS = [(0, "manifest", {"package": F.PACKAGE, "android:versionName": "café " + "x" * 130,
                                 "android:label": "\U0001f600 emoji", "android:versionCode": 7}),
                (1, "application", {"android:name": ".App", "android:exported": True})]

    def roundtrip(self, **options):
        blob = F.make_axml(self.ELEMENTS, **options)
        unknown = []
        strings, resmap, elements, _ = read_axml(blob, unknown)
        self.assertEqual(strip_ids(elements), expected(self.ELEMENTS))
        return blob, strings, unknown

    def test_utf8_pool_with_two_byte_lengths(self):
        blob, strings, _ = self.roundtrip(utf8=True)
        self.assertEqual(struct.unpack_from("<I", blob, 8 + 16)[0] & 0x100, 0x100)  # pool flags
        long_text = "café " + "x" * 130  # 135 UTF-16 units, 136 bytes: both lengths need the 2-byte form
        self.assertIn(bytes([0x80, 135, 0x80, 136]) + long_text.encode("utf-8") + b"\0", blob)
        # short strings keep the one-byte form; non-ASCII has different unit and byte counts
        self.assertIn(bytes([4, 5]) + "café".encode() + b"\0", F.make_axml([(0, "m", {"v": "café"})], utf8=True))
        self.assertIn(bytes([2, 4]) + "\U0001f600".encode() + b"\0", F.make_axml([(0, "m", {"v": "\U0001f600"})], utf8=True))

    def test_utf8_and_utf16_describe_the_same_manifest(self):
        a = read_axml(F.make_axml(F.SNAGDESK_MANIFEST, utf8=True))
        b = read_axml(F.make_axml(F.SNAGDESK_MANIFEST))
        self.assertEqual(a[0], b[0])
        self.assertEqual(a[1:3], b[1:3])
        self.assertLess(len(F.make_axml(F.SNAGDESK_MANIFEST, utf8=True)), len(F.make_axml(F.SNAGDESK_MANIFEST)))

    def test_wide_attributes_and_attribute_start(self):
        for start, size in ((20, 24), (24, 20), (28, 32)):
            blob, _, _ = self.roundtrip(attr_start=start, attr_size=size)
            off = blob.index(struct.pack("<HH", 0x0102, 16))
            astart, asize = struct.unpack_from("<HH", blob, off + 8 + 16)
            self.assertEqual((astart, asize), (start, size))
        self.assertNotEqual(F.make_axml(self.ELEMENTS, attr_size=24), F.make_axml(self.ELEMENTS))

    def test_unknown_chunk_is_skippable(self):
        blob, _, unknown = self.roundtrip(extra_chunk=(0x0FF0, b"opaque-payload"))
        self.assertEqual(unknown, [0x0FF0])
        self.assertEqual(struct.unpack_from("<I", blob, 4)[0], len(blob))
        self.assertIn(struct.pack("<HHI", 0x0FF0, 8, 24) + b"opaque-payload\0\0", blob)

    def test_all_options_together_and_defaults_unchanged(self):
        self.roundtrip(utf8=True, attr_start=24, attr_size=28, extra_chunk=(0x0FF1, b""))
        self.assertEqual(F.make_axml(self.ELEMENTS), F.make_axml(self.ELEMENTS, utf8=False, attr_size=20, attr_start=20,
                                                                  extra_chunk=None))

    def test_bad_options(self):
        for bad in ({"attr_size": 19}, {"attr_start": 16}, {"extra_chunk": (0x0102, b"")}, {"extra_chunk": (0x0001, b"")}):
            with self.assertRaises(ValueError):
                F.make_axml(self.ELEMENTS, **bad)


class TestMoreHostileZips(Base):
    def test_nul_in_a_member_name(self):
        blob = F.make_nul_name_zip(self.tmp / "nul.zip").read_bytes()
        entries = raw_zip(blob)  # read from the raw central directory: zipfile treats the NUL differently by version
        names = [e["name"] for e in entries]
        self.assertTrue(any("\0" in n for n in names), names)
        self.assertIn("ok.txt", names)
        self.assertEqual(blob.count(b"evil\0.txt"), 2)  # local header and central directory

    def test_unsupported_compression_method(self):
        blob = F.make_unsupported_method_zip(self.tmp / "m.zip").read_bytes()
        by = {e["name"]: e for e in raw_zip(blob)}
        self.assertEqual(by["weird.bin"]["method"], 99)
        self.assertEqual(by["ok.txt"]["method"], 0)
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            with self.assertRaises(NotImplementedError):
                z.read("weird.bin")
            self.assertEqual(z.read("ok.txt"), b"fine\n")

    def test_corrupt_deflate_data_with_a_valid_central_directory(self):
        blob = F.make_corrupt_deflate_zip(self.tmp / "c.zip").read_bytes()
        by = {e["name"]: e for e in raw_zip(blob)}
        bad = by["bad.bin"]
        self.assertEqual(bad["method"], 8)
        start = bad["offset"] + 30 + len(b"bad.bin")
        with self.assertRaises(zlib.error):
            zlib.decompress(blob[start:start + bad["csize"]], -15)
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            self.assertEqual(z.namelist(), ["bad.bin", "ok.txt"])
            with self.assertRaises((zlib.error, zipfile.BadZipFile, EOFError)):
                z.read("bad.bin")
            self.assertEqual(z.read("ok.txt"), b"fine\n")

    def test_member_data_cut_short_with_an_intact_central_directory(self):
        blob = F.make_truncated_member_zip(self.tmp / "t.zip").read_bytes()
        entries = raw_zip(blob)
        cut, nxt = entries[0], entries[1]
        self.assertEqual(cut["name"], "cut.bin")
        data_start = cut["offset"] + 30 + len(cut["name"])
        self.assertGreater(cut["csize"], nxt["offset"] - data_start)  # the declared size runs into the next member
        self.assertTrue(blob[-22:].startswith(b"PK\x05\x06"))
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            with self.assertRaises((zipfile.BadZipFile, EOFError, zlib.error)):
                z.read("cut.bin")

    def test_oversized_member_is_cheap_and_under_the_ratio_limit_by_default(self):
        blob = F.make_oversized_member_zip(self.tmp / "big.zip").read_bytes()
        (big,) = raw_zip(blob)
        self.assertEqual(big["usize"], 64 * 1024 * 1024 + 1)
        self.assertEqual(big["method"], 8)
        self.assertLessEqual(big["usize"], 100 * big["csize"])  # the ratio rule does not fire: the size cap must
        self.assertLess(len(blob), 4 * 1024 * 1024)
        start = big["offset"] + 30 + len(b"big.bin")
        inflater, total, crc = zlib.decompressobj(-15), 0, 0
        data = blob[start:start + big["csize"]]
        for i in range(0, len(data), 65536):
            chunk = inflater.decompress(data[i:i + 65536])
            total, crc = total + len(chunk), zlib.crc32(chunk, crc)
        tail = inflater.flush()
        total, crc = total + len(tail), zlib.crc32(tail, crc)
        self.assertEqual((total, crc & 0xFFFFFFFF), (big["usize"], big["crc"]))
        self.assertTrue(inflater.eof)

    def test_oversized_member_stored_variant(self):
        (big,) = raw_zip(F.make_oversized_member_zip(self.tmp / "s.zip", size=1000, stored=True).read_bytes())
        self.assertEqual((big["method"], big["csize"], big["usize"]), (0, 1000, 1000))

    def test_many_entries_as_directories_do_not_count_as_files(self):
        entries = raw_zip(F.make_many_directories_zip(self.tmp / "d.zip").read_bytes())
        self.assertEqual(len(entries), 50_001)
        self.assertTrue(all(e["name"].endswith("/") and e["usize"] == 0 for e in entries))
        with zipfile.ZipFile(self.tmp / "d.zip") as z:
            self.assertEqual(sum(not i.is_dir() for i in z.infolist()), 0)


class TestMoreHostileAsars(Base):
    def header(self, path):
        blob = Path(path).read_bytes()
        four, header_size, payload, json_len = struct.unpack_from("<IIII", blob, 0)
        return blob, four, header_size, json_len

    def test_nul_in_a_name(self):
        blob, _, _, jl = self.header(F.make_asar_hostile(self.tmp / "a.asar", "nul-name"))
        (name,) = json.loads(blob[16:16 + jl])["files"]
        self.assertIn("\0", name)

    def test_folders_nested_beyond_64(self):
        blob, _, _, jl = self.header(F.make_asar_hostile(self.tmp / "a.asar", "too-deep"))
        node, depth = json.loads(blob[16:16 + jl]), 0
        while "files" in node:
            (name, node), = node["files"].items()
            depth += 1
        self.assertGreater(depth, 64)

    def test_link_pointing_outside(self):
        blob, _, _, jl = self.header(F.make_asar_hostile(self.tmp / "a.asar", "link-outside"))
        (entry,) = json.loads(blob[16:16 + jl])["files"].values()
        self.assertTrue(entry["link"].startswith("../"))

    def test_malformed_json_with_consistent_lengths(self):
        blob, four, header_size, jl = self.header(F.make_asar_hostile(self.tmp / "a.asar", "bad-json"))
        self.assertEqual(four, 4)
        self.assertLessEqual(16 + jl, len(blob))
        with self.assertRaises(ValueError):
            json.loads(blob[16:16 + jl])

    def test_non_decimal_offsets(self):
        for kind, text in (("offset-negative", "-1"), ("offset-hex", "0x10")):
            blob, _, _, jl = self.header(F.make_asar_hostile(self.tmp / f"{kind}.asar", kind))
            (entry,) = json.loads(blob[16:16 + jl])["files"].values()
            self.assertEqual(entry["offset"], text)
            self.assertFalse(entry["offset"].isdigit())

    def test_unpacked_entry_whose_real_path_escapes(self):
        try:
            path = F.make_asar_hostile(self.tmp / "app.asar", "unpacked-escape")
        except F.SymlinkUnavailable as e:
            self.skipTest(str(e))
        blob, _, _, jl = self.header(path)
        tree = json.loads(blob[16:16 + jl])
        entry = tree["files"]["dir"]["files"]["leak.txt"]
        self.assertTrue(entry.get("unpacked"))
        side = Path(str(path) + ".unpacked")
        real = os.path.normcase(os.path.realpath(side / "dir" / "leak.txt"))
        self.assertFalse(real.startswith(os.path.normcase(os.path.realpath(side)) + os.sep))
        self.assertEqual(Path(os.path.realpath(side / "dir" / "leak.txt")).read_bytes(), b"outside\n")


class TestSymlinkFolder(Base):
    def test_folder_with_a_link_pointing_outside(self):
        try:
            made = F.make_folder_with_symlink(self.tmp / "tree")
        except F.SymlinkUnavailable as e:
            self.skipTest(str(e))
        root = os.path.normcase(os.path.realpath(made["root"]))
        self.assertTrue((made["root"] / "ok.txt").is_file())
        self.assertTrue(made["links"])
        for link in made["links"]:
            real = os.path.normcase(os.path.realpath(link))
            self.assertFalse(real.startswith(root + os.sep), link)  # resolves outside the folder
            self.assertNotEqual(os.path.normcase(os.path.abspath(link)), real)
        self.assertEqual((made["outside"] / "secret.txt").read_bytes(), b"outside\n")


class TestDexVersions(Base):
    def test_versions(self):
        for version in ("035", "037", "038", "039"):
            blob = F.make_dex(F.SNAGDESK_STRINGS, F.SNAGDESK_TYPES, version=version)
            self.assertEqual(blob[:8], b"dex\n" + version.encode() + b"\0")
            read_dex(blob)
        self.assertEqual(F.make_dex(["a"], []), F.make_dex(["a"], [], version="035"))
        for bad in ("034", "04", "abc", 35):
            with self.assertRaises(ValueError):
                F.make_dex(["a"], [], version=bad)


# the pinned hashes are filled in by the first successful run of this module with FX1_PRINT_HASHES=1
if os.environ.get("FX1_PRINT_HASHES"):  # pragma: no cover
    with tempfile.TemporaryDirectory() as _d:
        for _name, _make in cases(_d).items():
            print(f'    "{_name}": "{hashlib.sha256(_make()).hexdigest()}",')


if __name__ == "__main__":
    unittest.main()
