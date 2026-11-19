"""W2-FX2: self-checks of the text fixture builders (contract 10.2).

Every check uses the standard library only and reuses no lane code and no fixture helper: the expected facts of each
fixture are re-derived here from the written bytes by small independent readers (json, plistlib, sqlite3,
ElementTree and the regexes of the lane tables in contract 6.4 to 6.10), and must equal the facts the builder returns
(completeness in both directions: nothing listed that is not in the file, nothing in the file that is not listed).
Meta-tests then add an unlisted fact to a file and require the same readers to notice it.
"""
import base64
import json
import plistlib
import posixpath
import re
import sqlite3
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
from xml.parsers.expat import ExpatError

import fixtures_text as F

CAP = 5 * 1024 * 1024
KIND_RE = re.compile(r"^[a-z]+(\.[a-z0-9-]+)+$")
ROUTE_RE = re.compile(r"^(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|ANY) /\S*$")
SEC = {   # contract 6.2, copied again here (independent of the module under test)
    "aws-key": r"AKIA[0-9A-Z]{16}", "google-api-key": r"AIza[0-9A-Za-z_\-]{35}",
    "private-key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "jwt": r"eyJ[\w-]{10,}\.eyJ[\w-]{10,}\.[\w-]{10,}",
    "stripe-live": r"[sr]k_live_[0-9A-Za-z]{16,}", "slack": r"xox[abprs]-[0-9A-Za-z-]{10,}",
    "github-token": r"gh[pousr]_[0-9A-Za-z]{30,}", "bearer": r"(?i)bearer\s+[\w.\-]{16,}",
}


def tree(root):
    """relpath -> bytes for every file under root (or {name: bytes} for one file)."""
    root = Path(root)
    if root.is_file():
        return {root.name: root.read_bytes()}
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def dec(data):
    """6.2 rule 1: BOM dropped, UTF-8 with replacement."""
    return (data[3:] if data.startswith(b"\xef\xbb\xbf") else data).decode("utf-8", "replace")


def jload(path):
    return json.loads(Path(path).read_bytes().decode("utf-8"))


def sec_kinds(texts):
    out = {}
    for path in sorted(texts):
        for name, rx in SEC.items():
            n = len(re.findall(rx, texts[path]))
            if n:
                out["%s@%s" % (name, path)] = {"count": n}
    return out


def shape(v, level=1, depth=4):
    """json_shape (6.3): the given value is level 1; a value at level depth + 1 is "..."."""
    if level > depth:
        return "..."
    if isinstance(v, dict):
        return {k: shape(v[k], level + 1) for k in sorted(v)[:50]}
    if isinstance(v, list):
        return [shape(v[0], level + 1)] if v else []
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    return "null" if v is None else "string"


def template(seg):
    if (seg.isdigit() or re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", seg)
            or re.fullmatch(r"[0-9a-fA-F]{16,}", seg) or re.fullmatch(r"[A-Za-z0-9_-]{20,}", seg)):
        return "{id}"
    return seg


def canon(path):
    """6.3 route templating for the forms the fixtures use."""
    path = re.sub(r"\[\[\.\.\.(\w+)\]\]|\[\.\.\.(\w+)\]", lambda m: "{%s*}" % (m.group(1) or m.group(2)), path)
    path = re.sub(r":(\w+)|<(?:\w+:)?(\w+)>|\[(\w+)\]", lambda m: "{%s}" % next(g for g in m.groups() if g), path)
    path = re.sub(r"/\*$", "/{rest*}", path)
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/") or "/"


def host_of(url):
    u = urlsplit(url)
    default = {"https": 443, "http": 80}.get(u.scheme)
    return u.hostname if u.port in (None, default) else "%s:%d" % (u.hostname, u.port)


def mime(m):
    return m.split(";")[0].strip().lower()


def har_key(method, url):
    segs = [template(s) for s in urlsplit(url).path.split("/") if s]
    return "%s %s/%s" % (method, host_of(url), "/".join(segs))


def har_derive(entries):
    """Every HAR kind of 6.5 read the plain way from the entries."""
    eps, shapes, auth, hosts, pages = {}, {}, [], {}, {}
    for e in sorted(entries, key=lambda x: x["startedDateTime"]):
        req, resp = e["request"], e["response"]
        k = har_key(req["method"], req["url"])
        d = eps.setdefault(k, {"statuses": set(), "requestTypes": set(), "responseTypes": set(), "count": 0, "queryParams": set()})
        d["statuses"].add(resp["status"])
        d["count"] += 1
        d["queryParams"].update(q["name"] for q in req["queryString"])
        post = req.get("postData")
        if post:
            d["requestTypes"].add(mime(post["mimeType"]))
        content = resp["content"]
        if content["mimeType"]:
            d["responseTypes"].add(mime(content["mimeType"]))
        bodies = [("request", post["mimeType"] if post else "", post["text"] if post else None),
                  ("response", content["mimeType"], content.get("text"))]
        for direction, mt, text in bodies:
            m = mime(mt) if mt else ""
            if (m == "application/json" or m.endswith("+json")) and text:
                try:                                  # 6.2 rule 4: a piece that fails is dropped on its own
                    if direction == "response" and content.get("encoding") == "base64":
                        text = base64.b64decode(text, validate=True).decode("utf-8")
                    value = json.loads(text)
                except (ValueError, RecursionError):
                    continue
                shapes.setdefault("%s %s" % (k, direction), {"shape": shape(value)})
        path = urlsplit(req["url"]).path
        if re.search(r"login|logout|auth|token|session|oauth|signin|signup|sso|2fa|otp", path):
            h = {x["name"].lower(): x["value"] for x in req["headers"]}
            scheme = h.get("authorization", "").split(" ")[0]
            auth.append((k, {"order": len(auth) + 1, "sets-cookie": any(x["name"].lower() == "set-cookie" for x in resp["headers"]),
                             "auth-scheme": scheme if scheme in ("Bearer", "Basic") else None}))
        h = host_of(req["url"])
        hosts[h] = hosts.get(h, 0) + 1
        if mime(content["mimeType"]) == "text/html":
            pages["%s/%s" % (h, "/".join(template(s) for s in urlsplit(req["url"]).path.split("/") if s))] = {}
    main = sorted(hosts, key=lambda h: (-hosts[h], h))[0]
    return {
        "web.endpoint": {k: {"statuses": sorted(v["statuses"]), "requestTypes": sorted(v["requestTypes"]),
                             "responseTypes": sorted(v["responseTypes"]), "count": v["count"],
                             "queryParams": sorted(v["queryParams"])} for k, v in eps.items()},
        "web.payload-shape": shapes, "web.auth-step": dict(auth),
        "web.host": {h: {"count": n} for h, n in hosts.items() if h != main}, "web.page": pages}


def har_leaks(entries):
    """Every header/cookie/query value, form value and JSON string leaf of 8+ characters, as a set (completeness
    oracle for `never_stored`)."""
    out = set()

    def leaves(v):
        if isinstance(v, dict):
            for x in v.values():
                leaves(x)
        elif isinstance(v, list):
            for x in v:
                leaves(x)
        elif isinstance(v, str) and len(v) >= 8:
            out.add(v)
    for e in entries:
        req, resp = e["request"], e["response"]
        u = urlsplit(req["url"])
        if u.password:
            out.add(u.password)
        for h in req["headers"] + resp["headers"]:
            if len(h["value"]) >= 8:
                out.add(h["value"])
        for c in req["cookies"] + resp["cookies"]:
            if len(c["value"]) >= 8:
                out.add(c["value"])
        for q in req["queryString"]:
            if len(q["value"]) >= 8:
                out.add(q["value"])
        post = req.get("postData")
        if post and "json" in post["mimeType"]:
            leaves(json.loads(post["text"]))
        if post:
            for p in post.get("params", []):
                if len(p["value"]) >= 8:
                    out.add(p["value"])
        c = resp["content"]
        if c.get("text") and "json" in c["mimeType"]:
            if c.get("encoding") == "base64":
                out.add(c["text"])
            try:
                t = base64.b64decode(c["text"], validate=True).decode() if c.get("encoding") == "base64" else c["text"]
                leaves(json.loads(t))
            except (ValueError, RecursionError):
                pass
    return out


def expected_never(leaks):
    """What `never_stored` must hold for a set of leaked values: the values, the JSON-escaped form of each value that
    JSON escapes, and the body of the fake private key (contract 10.1)."""
    out = set(leaks)
    for v in leaks:
        out.add(json.dumps(v)[1:-1])
        if F.PEM_BODY in v:
            out.add(F.PEM_BODY)
    return out


READ_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".rs", ".go", ".rb", ".php", ".java", ".kt", ".cs", ".sql",
            ".prisma", ".vue", ".svelte", ".swift", ".dart", ".ex", ".exs", ".scala", ".json", ".toml", ".yaml", ".yml",
            ".gradle", ".kts", ".xml", ".html", ".md"}      # contract 6.4: SCAN_EXT plus the listed extras


def is_read(rel):
    """Files the source lane reads as text: the extension list and the manifests of the `source.framework` row."""
    name = posixpath.basename(rel)
    return (posixpath.splitext(rel)[1] in READ_EXT or name in ("Gemfile", "go.mod", "pubspec.yaml")
            or re.match(r"requirements.*\.txt$", name) is not None or name.startswith("build.gradle"))


def scan_js(text, kinds=None, src="bundle"):
    """The jsscan rows of 6.5 over one text (bounded string classes, never `.*?`). Adds to ``kinds`` when given."""
    k = kinds if kinds is not None else {}
    for name in ("web.route", "web.api-call", "web.flag", "web.i18n-key", "web.framework"):
        k.setdefault(name, {})
    q = r"[^\"\\\n]{0,2048}"
    for m in re.finditer(r'path:\s*"(/%s)"' % q, text):
        k["web.route"][canon(m.group(1))] = {}
    for m in re.finditer(r'<Route path="(/%s)"' % q, text):
        k["web.route"][canon(m.group(1))] = {}
    for m in re.finditer(r"path:\s*'([^'\\\n]{0,2048})'", text):
        p = m.group(1)
        k["web.route"][canon(p)] = {} if p.startswith("/") else {"relative": True}
    if "__NEXT_DATA__" in text:
        for m in re.finditer(r'"page":\s*"(/%s)"' % q, text):
            k["web.route"][canon(m.group(1))] = {}
    if "__BUILD_MANIFEST" in text:
        for m in re.finditer(r'"(/%s)":\s*\[' % q, text):
            k["web.route"][canon(m.group(1))] = {}
    for m in re.finditer(r"axios\.(get|post|put|patch|delete)\(\s*[\"'`](/[^\"'`\\\n]{0,2048})[\"'`]", text):
        k["web.api-call"]["%s %s" % (m.group(1).upper(), canon(m.group(2)))] = {"host": None}
    for m in re.finditer(r'XMLHttpRequest\.open\(\s*"(\w+)",\s*"(/[^"\\\n]{0,2048})"', text):
        k["web.api-call"]["%s %s" % (m.group(1).upper(), canon(m.group(2)))] = {"host": None}
    for m in re.finditer(r"fetch\(\s*([\"'`])(/[^\"'`\\\n]{0,2048})\1", text):
        rest = text[m.end():text.find(")", m.end())]
        mm = re.search(r'method:\s*"(\w+)"', rest)
        k["web.api-call"]["%s %s" % (mm.group(1).upper() if mm else "ANY", canon(m.group(2)))] = {"host": None}
    for m in re.finditer(r'"https?://([^/"\s]+)(/(?:api|v[0-9])[^"\s]{0,2048})"', text):
        k["web.api-call"]["ANY " + canon(m.group(2))] = {"host": m.group(1)}
    for m in re.finditer(r'\b(?:isEnabled|useFlag|useFeatureFlag|getFeatureValue|variation)\("([^"\\\n]{1,200})"', text):
        k["web.flag"][m.group(1)] = {}
    if re.search(r"(?:from|require\()\s*[\"'][^\"']*(?:launchdarkly|unleash|growthbook)", text, re.I):
        for m in re.finditer(r"\bflags\.(\w+)", text):
            k["web.flag"][m.group(1)] = {}
    for rx in (r'(?<![\w$.])t\("([^"\\\n]{1,200})"', r'i18n\.t\("([^"\\\n]{1,200})"', r'\$t\("([^"\\\n]{1,200})"',
               r'formatMessage\(\{\s*id:\s*"([^"\\\n]{1,200})"'):
        for m in re.finditer(rx, text):
            k["web.i18n-key"][m.group(1)] = {"namespace": m.group(1).split(".")[0]}
    for marker, fw in (("__NEXT_DATA__", "next"), ("__NUXT__", "nuxt"), ("ng-version", "angular"),
                       ("data-reactroot", "react"), ("__SVELTEKIT", "sveltekit")):
        if marker in text:
            k["web.framework"][fw] = {"from": src}
    return k


def kinds_of(facts, *names):
    return {n: facts["kinds"].get(n, {}) for n in names}


JS_KINDS = ("web.route", "web.api-call", "web.flag", "web.i18n-key", "web.framework")


def scan_yaml(text):
    found, current = {}, None
    for line in text.replace("\r", "").split("\n"):
        if re.match(r"^\S", line) or re.match(r"^  \S", line):
            current = None
        m = re.match(r"^  (/\S*):\s*$", line)
        if m:
            current = m.group(1)
            continue
        m = re.match(r"^    (get|put|post|delete|patch|head|options|trace):", line)
        if m and current:
            found["%s %s" % (m.group(1).upper(), current)] = {"operationId": None, "tags": []}
    return found


def scan_sdl(text):
    text = re.sub(r"#[^\n]*", "", text)
    types = {}
    for m in re.finditer(r"^(type|input|enum|interface|union|scalar)\s+(\w+)", text, re.M):
        types[m.group(2)] = {"kind": m.group(1)}
    eps = {}
    for m in re.finditer(r"^(?:extend\s+)?type\s+(Query|Mutation|Subscription)\s*\{(.*?)(?:^\}|\Z)", text, re.M | re.S):
        for name in re.findall(r"^\s+(\w+)", m.group(2), re.M):
            eps["%s %s" % (m.group(1).lower(), name)] = {}
    return {"data.schema-type": types, "data.endpoint": eps}


def scan_proto(text):
    text = re.sub(r"/\*.*?\*/", "", re.sub(r"//[^\n]*", "", text), flags=re.S)
    types, rpcs, stack = {}, {}, []
    for m in re.finditer(r"\b(message|enum|service)\s+(\w+)\s*\{|\{|\}", text):
        if m.group(0) == "}":
            stack.pop()
        elif m.group(0) == "{":
            stack.append(None)
        else:
            kind, name = m.group(1), m.group(2)
            if kind == "service":
                stack.append(("service", name))
                body_start = m.end()
                end = text.find("\n}", body_start)
                end = len(text) if end < 0 else end
                for r in re.finditer(r"rpc\s+(\w+)\s*\(\s*(?:stream\s+)?([\w.]+)\s*\)\s*returns\s*\(\s*(?:stream\s+)?([\w.]+)\s*\)",
                                     text[body_start:end]):
                    rpcs["%s.%s" % (name, r.group(1))] = {"request": r.group(2), "response": r.group(3)}
            else:
                outer = [s[1] for s in stack if s and s[0] == "msg"]
                types[".".join(outer + [name])] = {"kind": kind}
                stack.append(("msg", name))
    return {"data.schema-type": types, "data.rpc": rpcs}


def plist_paths(value):
    out = {}

    def walk(v, path, segs):
        if segs > 6:
            return
        name = ("dict" if isinstance(v, dict) else "array" if isinstance(v, list) else "bool" if isinstance(v, bool)
                else "integer" if isinstance(v, int) else "real" if isinstance(v, float) else "data" if isinstance(v, bytes)
                else "date" if hasattr(v, "year") else "string")
        if path:
            out[path] = {"type": name}
        if isinstance(v, dict):
            for key, x in v.items():
                walk(x, path + "." + key if path else key, segs + 1)
        elif isinstance(v, list) and v:
            walk(v[0], path + "[]", segs + 1)
    walk(value, "", 0)
    return {k: out[k] for k in sorted(out)[:500]}


def parse_columns(sql):
    """Columns of a CREATE TABLE statement from its text (6.8): name unquoted, type as declared."""
    body = sql[sql.index("(") + 1:sql.rindex(")")]
    parts, depth, cur = [], 0, ""
    for ch in body:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    cols = []
    for p in parts:
        toks = p.strip().split()
        if toks[0].upper() in ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"):
            continue
        name = toks[0].strip('"`[]')
        typ = []
        for t in toks[1:]:
            if t.upper() in ("PRIMARY", "NOT", "NULL", "DEFAULT", "UNIQUE", "REFERENCES", "CHECK", "COLLATE", "CONSTRAINT"):
                break
            typ.append(t)
        cols.append({"name": name, "type": " ".join(typ)})
    return {"columns": cols}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="fx2-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def p(self, *parts):
        return self.tmp.joinpath(*parts)

    def check_secrets(self, path, facts, rel="."):
        """`secret.embedded` records and `secret_patterns` equal what the 6.2 regexes find in the decoded file."""
        got = sec_kinds({rel: dec(Path(path).read_bytes())})
        self.assertEqual(facts["kinds"].get("secret.embedded", {}), got)
        self.assertEqual(facts["secret_patterns"], sorted({k.split("@")[0] for k in got}))


class TestSecrets(Base):
    def test_every_secret_matches_its_pattern_and_is_fake(self):
        for name, value in F.SECRETS.items():
            with self.subTest(name):
                self.assertRegex(value, SEC[name])
                self.assertIn("fixture", value.lower())
        self.assertEqual(set(F.SECRETS), set(SEC))
        self.assertEqual(F.SECRET_REGEXES, SEC)

    def test_source_never_holds_a_whole_token_or_a_scan_marker(self):
        src = Path(F.__file__).read_text(encoding="utf-8")
        for name, rx in SEC.items():
            with self.subTest(name):
                self.assertIsNone(re.search(rx, src), "a whole %s value appears in fixtures_text.py" % name)
        self.assertNotIn("scan-private" + ": allow", src)
        self.assertNotRegex(src, r"[\w.%+-]+@[\w-]+\.[a-z]{2,}")      # no email-like text in the source

    def test_no_fixture_output_carries_a_scan_marker(self):
        d = self.tmp
        F.make_har(d / "a.har")
        F.make_repo(d / "r")
        F.make_jadx_dir(d / "j")
        F.make_apktool_dir(d / "a")
        F.make_openapi_yaml(d / "o.yaml")
        for rel, data in tree(d).items():
            self.assertNotIn(b"scan-private", data, rel)

    def test_the_private_key_is_multi_line_and_written_to_fixtures(self):
        self.assertGreaterEqual(F.SECRETS["private-key"].count("\n"), 2)
        har = F.make_har(self.p("a.har"))[1]
        self.assertIn("private-key@.", har["kinds"]["secret.embedded"])
        repo = F.make_repo(self.p("r"))[1]
        self.assertIn("private-key@src/config.js", repo["kinds"]["secret.embedded"])


class TestDeterminism(Base):
    def twice(self, make):
        a, b = self.p("a"), self.p("b")
        make(a)
        make(b)
        ta, tb = tree(a), tree(b)
        self.assertTrue(ta)
        self.assertEqual(ta, tb)

    def test_file_builders(self):
        makers = {
            "har": lambda d: F.make_har(d / "x.har"), "sourcemap": lambda d: F.make_sourcemap(d / "x.map"),
            "bundle": lambda d: F.write_bundle(d / "x.js"), "bundle-compact": lambda d: F.write_bundle(d / "x.js", style="compact"),
            "edge": lambda d: F.make_bundle_edge(d / "x.js"), "hermes": lambda d: F.make_hermes_bundle(d / "x.js"),
            "plist-xml": lambda d: F.make_plist(d / "x.plist"), "plist-bin": lambda d: F.make_plist(d / "x.plist", "binary"),
            "openapi": lambda d: F.make_openapi_json(d / "x.json"), "swagger": lambda d: F.make_openapi_json(d / "x.json", swagger=True),
            "yaml": lambda d: F.make_openapi_yaml(d / "x.yaml"), "graphql": lambda d: F.make_graphql(d / "x.graphql"),
            "introspection": lambda d: F.make_graphql(d / "x.json", introspection=True), "proto": lambda d: F.make_proto(d / "x.proto"),
            "capture": lambda d: F.make_capture(d / "x.json"), "rea": lambda d: F.make_rea_bundle(d / "x.json"),
            "rea-tolerant": lambda d: F.make_rea_bundle(d / "x.json", "tolerant"),
            "site": lambda d: F.make_site(d / "site"), "repo": lambda d: F.make_repo(d / "repo"),
            "repo-malformed": lambda d: F.make_repo(d / "repo", "malformed"),
            "jadx": lambda d: F.make_jadx_dir(d / "j"), "gradle": lambda d: F.make_jadx_dir(d / "j", "gradle"),
            "apktool": lambda d: F.make_apktool_dir(d / "a"),
            "pair-openapi": lambda d: F.make_version_pair(d / "p", "openapi"), "pair-har": lambda d: F.make_version_pair(d / "p", "har"),
            "pathological": lambda d: F.make_pathological_line(d / "x.js", 100000),
        }
        for name, make in makers.items():
            with self.subTest(name):
                self.twice(make)

    def test_facts_are_deterministic_too(self):
        self.assertEqual(json.dumps(F.make_har(self.p("a.har"))[1], sort_keys=True),
                         json.dumps(F.make_har(self.p("b.har"))[1], sort_keys=True))
        self.assertEqual(F.make_bundle()[0], F.make_bundle()[0])

    def test_every_variant_is_deterministic(self):
        """SQLite files embed the library version in their header, so the SQLite variants compare by length."""
        for kind, names in F.VARIANTS.items():
            for name in names:
                if name == "too_large":
                    continue   # exercised in TestVariants (5 MiB writes)
                with self.subTest("%s/%s" % (kind, name)):
                    if kind == "sqlite":
                        a = F.make_variant(self.p("a.db"), kind, name)[0].read_bytes()
                        b = F.make_variant(self.p("b.db"), kind, name)[0].read_bytes()
                        self.assertEqual(len(a), len(b))
                    else:
                        self.twice(lambda d, k=kind, n=name: F.make_variant(d / "f.bin", k, n))

    def test_sqlite_schema_and_rows_are_stable(self):
        views = []
        for n in ("a", "b"):
            path, _ = F.make_sqlite(self.p(n, "x.db"))
            con = sqlite3.connect(str(path))
            views.append((con.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name").fetchall(),
                          {t: con.execute("SELECT * FROM %s" % t).fetchall() for t in ("users", "items", "tags")}))
            con.close()
        self.assertEqual(views[0], views[1])

    def test_line_endings_are_lf(self):
        for rel, data in tree(F.make_repo(self.p("r"))[0]).items():
            if not rel.endswith(".png"):
                with self.subTest(rel):
                    self.assertNotIn(b"\r", data)
        self.assertIn(b"\r\n", F.make_openapi_yaml(self.p("y.yaml"), "crlf")[0].read_bytes())


class TestFactsShape(Base):
    def all_facts(self):
        d = self.tmp
        yield "har", F.make_har(d / "a.har")[1]
        yield "map", F.make_sourcemap(d / "a.map")[1]
        yield "bundle", F.make_bundle()[1]
        yield "edge", F.make_bundle_edge(d / "e.js")[1]
        yield "site", F.make_site(d / "site")[1]
        yield "sqlite", F.make_sqlite(d / "a.db")[1]
        yield "plist", F.make_plist(d / "a.plist")[1]
        yield "openapi", F.make_openapi_json(d / "a.json")[1]
        yield "swagger", F.make_openapi_json(d / "s.json", swagger=True)[1]
        yield "yaml", F.make_openapi_yaml(d / "a.yaml")[1]
        yield "graphql", F.make_graphql(d / "a.graphql")[1]
        yield "proto", F.make_proto(d / "a.proto")[1]
        yield "repo", F.make_repo(d / "repo")[1]
        yield "capture", F.make_capture(d / "c.json")[1]
        yield "rea", F.make_rea_bundle(d / "r.json")[1]
        yield "rea-tolerant", F.make_rea_bundle(d / "rt.json", "tolerant")[1]
        yield "jadx", F.make_jadx_dir(d / "j")[1]
        yield "apktool", F.make_apktool_dir(d / "ap")[1]

    def test_kinds_and_keys_are_canonical(self):
        for name, facts in self.all_facts():
            self.assertIsInstance(facts["kinds"], dict, name)
            for kind, keys in facts["kinds"].items():
                with self.subTest("%s %s" % (name, kind)):
                    self.assertRegex(kind, KIND_RE)
                    self.assertIsInstance(keys, dict)
                    for key, data in keys.items():
                        self.assertLessEqual(len(key), 200)
                        self.assertIsInstance(data, dict)
                        if kind == "source.route" or kind == "web.api-call":
                            self.assertRegex(key, ROUTE_RE)

    def test_facts_are_json_serialisable_and_name_their_secrets(self):
        for name, facts in self.all_facts():
            with self.subTest(name):
                json.dumps(facts)
                self.assertIn("variant", facts)
                self.assertNotIn("ambiguous", facts)        # the contract leaves nothing open now
                if "secret_patterns" in facts:
                    for pat in facts["secret_patterns"]:
                        self.assertIn(pat, SEC)


class TestHar(Base):
    def test_default_har_parses_and_every_kind_matches(self):
        path, facts = F.make_har(self.p("a.har"))
        doc = jload(path)
        self.assertEqual(doc["log"]["version"], "1.2")
        entries = doc["log"]["entries"]
        self.assertEqual(len(entries), facts["entry_count"])
        derived = har_derive(entries)
        for kind, got in derived.items():
            with self.subTest(kind):
                self.assertEqual(facts["kinds"][kind], got)
        self.check_secrets(path, facts)

    def test_never_stored_is_complete_and_every_value_is_in_the_file(self):
        path, facts = F.make_har(self.p("a.har"))
        entries = jload(path)["log"]["entries"]
        self.assertEqual(set(facts["never_stored"]), expected_never(har_leaks(entries)))
        text = path.read_text(encoding="utf-8")
        for value in facts["never_stored"]:
            once = json.dumps(value)[1:-1]
            self.assertTrue(once in text or json.dumps(once)[1:-1] in text, value[:30])
        self.assertIn(F.PEM_BODY, facts["never_stored"])
        self.assertIn(json.dumps(F.SECRETS["private-key"])[1:-1], facts["never_stored"])   # the JSON-escaped form
        for want in (F.SECRETS["google-api-key"], F.SECRETS["jwt"], F.SECRETS["stripe-live"], F.PASSWORD, F.COOKIE,
                     F.URL_PASSWORD, F.BASIC, F.TOKEN, F.PEM_BODY):
            self.assertTrue(any(want in v for v in facts["never_stored"]), want)

    def test_the_forms_the_contract_names_are_present(self):
        entries = jload(F.make_har(self.p("a.har"))[0])["log"]["entries"]
        urls = " ".join(e["request"]["url"] for e in entries)
        for raw in ("/items/123", "/items/124", "3f2b8c1e-5a4d", "0123456789abcdef", "Zm9vYmFyYmF6cXV4MTIzNA", ":8443/"):
            self.assertIn(raw, urls)
        self.assertIn("API.example.test", urls)                       # host lower-cased by the lane
        self.assertEqual(F.HAR_SHAPES["GET app.example.test/api/tree response"], {"a": {"b": {"c": {"d": "..."}}}})
        tree_body = json.loads(next(e for e in entries if e["request"]["url"].endswith("/api/tree"))["response"]["content"]["text"])
        self.assertEqual(shape(tree_body), {"a": {"b": {"c": {"d": "..."}}}})      # six levels deep, cut at level 5
        facts = F.make_har(self.p("lim.har"))[1]
        self.assertEqual(facts["record_limitations"],
                         {"web.payload-shape GET app.example.test/api/tree response":
                          ["nested deeper than 4 levels; deeper levels not read"]})      # 6.2 rule 3
        cut = sorted("web.payload-shape " + k for k, v in facts["kinds"]["web.payload-shape"].items() if "..." in json.dumps(v))
        self.assertEqual(cut, sorted(facts["record_limitations"]))       # exactly the cut shapes carry the limitation
        self.assertTrue(any(F.URL_PASSWORD in e["request"]["url"] for e in entries))
        self.assertTrue(any("; charset=" in e["response"]["content"]["mimeType"] for e in entries))
        self.assertTrue(any(e["response"]["content"].get("encoding") == "base64" for e in entries))
        self.assertTrue(any(e["response"]["content"]["mimeType"].endswith("+json") for e in entries))
        names = {h["name"] for e in entries for h in e["request"]["headers"] + e["response"]["headers"]}
        self.assertTrue({"Authorization", "Cookie", "Set-Cookie"} <= names)
        self.assertTrue(any(e["response"]["cookies"] for e in entries))
        for e in entries:
            self.assertTrue(e["startedDateTime"].startswith("2026-01-01T00:00:"))

    def test_custom_entries(self):
        e = F.har_entry("POST", "https://app.example.test/api/x?a=1", 201, req_body={"k": "value-long-enough"},
                        resp_body={"ok": True}, resp_mime="application/json")
        path, facts = F.make_har(self.p("c.har"), [e])
        self.assertEqual(facts["entry_count"], 1)
        self.assertEqual(jload(path)["log"]["entries"][0]["request"]["queryString"], [{"name": "a", "value": "1"}])
        self.assertIn("value-long-enough", facts["never_stored"])

    def test_empty_har(self):
        path, facts = F.make_variant(self.p("e.har"), "har", "empty")
        self.assertEqual(jload(path)["log"]["entries"], [])
        self.assertEqual(facts["kinds"], {})


class TestSourcemapAndBundle(Base):
    def test_sourcemap_parses_and_every_kind_matches(self):
        path, facts = F.make_sourcemap(self.p("a.map"))
        doc = jload(path)
        self.assertEqual(doc["version"], 3)
        self.assertEqual(len(doc["sources"]), len(doc["sourcesContent"]))
        mods = {}
        for s in doc["sources"]:
            if s.startswith("/") or re.match(r"[A-Za-z]:[\\/]", s) or ".." in re.split(r"[\\/]", s):
                key = "(outside)"
            else:
                s = re.sub(r"^[a-z]+://", "", s).lstrip("/")
                s = s[2:] if s.startswith("./") else s
                folders = s.split("/")[:-1]
                key = "/".join(folders[:2]) if folders else "(root)"
            mods[key] = {"sources": mods.get(key, {"sources": 0})["sources"] + 1}
        self.assertEqual(facts["kinds"]["web.source-module"], mods)
        self.assertIn("(root)", mods)
        fw = {}
        for s in doc["sources"]:
            for marker, name in (("node_modules/react", "react"), ("next/dist", "next"), ("node_modules/vue", "vue"),
                                 ("@angular", "angular"), ("node_modules/svelte", "svelte")):
                if marker in s:
                    fw[name] = {"from": "source-map"}
        self.assertEqual(facts["kinds"]["web.framework"], fw)
        merged = {}
        for content in doc["sourcesContent"]:
            scan_js(content, merged)
        for kind in ("web.route", "web.api-call", "web.i18n-key", "web.flag"):
            self.assertEqual(facts["kinds"].get(kind, {}), merged[kind], kind)
        self.assertEqual(merged["web.framework"], {})
        self.check_secrets(path, facts)
        self.assertIn(F.MAP_MARKER, doc["sourcesContent"][0])

    def test_custom_sources(self):
        path, facts = F.make_sourcemap(self.p("c.map"), ["a/b/c/d.js", "a/b/e.js", "top.js", "../x.js"], ["1", "2", "3", "4"])
        self.assertEqual(facts["kinds"]["web.source-module"],
                         {"a/b": {"sources": 2}, "(root)": {"sources": 1}, "(outside)": {"sources": 1}})
        self.assertEqual(jload(path)["sourcesContent"], ["1", "2", "3", "4"])

    def test_bundle_kinds_match_an_independent_scan(self):
        js, facts = F.make_bundle()
        got = scan_js(js)
        for kind in JS_KINDS:
            with self.subTest(kind):
                self.assertEqual(facts["kinds"][kind], got[kind])
        for kind, keys in facts["must_not"].items():
            for key in keys:
                self.assertNotIn(key, got[kind])
        for value in facts["never_stored"]:
            self.assertIn(value, js)
        self.assertIn("fetch(`/api/export`)", js)
        self.assertIn('method: "PUT"', js)

    def test_bundle_secrets_are_counted(self):
        path, facts = F.write_bundle(self.p("b.js"))
        self.check_secrets(path, facts)
        self.assertEqual(set(facts["secret_patterns"]), {"aws-key", "bearer", "github-token", "slack", "stripe-live"})

    def test_compact_style_is_one_line_with_the_same_facts(self):
        js, facts = F.make_bundle(style="compact")
        self.assertEqual(js.count("\n"), 1)
        self.assertIn('path:"/dashboard"', js)
        self.assertEqual(facts["kinds"], F.make_bundle()[1]["kinds"])
        got = scan_js(js)
        for kind in JS_KINDS:
            self.assertEqual(facts["kinds"][kind], got[kind], kind)

    def test_bundle_extra_text_and_file(self):
        path, _ = F.write_bundle(self.p("b.js"), "// tail\n")
        self.assertTrue(path.read_text(encoding="utf-8").endswith("// tail\n"))

    def test_edge_file_markers_and_negatives(self):
        path, facts = F.make_bundle_edge(self.p("e.js"))
        got = scan_js(path.read_text(encoding="utf-8"))
        for kind in JS_KINDS:
            self.assertEqual(facts["kinds"].get(kind, {}), got[kind], kind)
        text = path.read_text(encoding="utf-8")
        for needle in ("__vite__", "webpackChunk", "flags.notAFlag", 'split("a.b")'):
            self.assertIn(needle, text)
        self.assertNotIn("launchdarkly", text.lower())
        self.assertEqual(set(facts["kinds"]["web.framework"]), {"nuxt", "angular", "sveltekit"})

    def test_hermes_magic_and_limitation(self):
        path, facts = F.make_hermes_bundle(self.p("h.js"))
        self.assertTrue(path.read_bytes().startswith(bytes([0xC6, 0x1F, 0xBC, 0x03, 0xC1, 0x03, 0x19, 0x1F])))
        self.assertEqual(facts["kinds"]["web.framework"], {"hermes": {"from": "bundle"}})
        self.assertEqual(facts["limitations"], ["Hermes bytecode present; not decoded (tier C: study import --from rea)"])
        self.assertIn(b"\x00", path.read_bytes()[:8192])

    def test_site_folder_records_by_path(self):
        root, facts = F.make_site(self.p("site"))
        files = tree(root)
        self.assertEqual(sorted(files), facts["files"])
        per = {}
        for rel, data in files.items():
            k = scan_js(dec(data), None, "html" if rel.endswith(".html") else "bundle")
            k = {n: v for n, v in k.items() if v}
            sec = sec_kinds({rel: dec(data)})
            if sec:
                k["secret.embedded"] = sec
            if k:
                per[rel] = k
        self.assertEqual(facts["records_by_path"], per)
        seen = {}
        for rel, ks in per.items():
            for kind, keys in ks.items():
                for key in keys:
                    seen.setdefault((kind, key), []).append(rel)
        unique = {}
        for rel, ks in per.items():
            for kind, keys in ks.items():
                for key, data in keys.items():
                    if len(seen[(kind, key)]) == 1:
                        unique.setdefault(kind, {})[key] = data
        self.assertEqual(facts["kinds"], unique)
        # the collisions the contract asks records_by_path for
        self.assertEqual(sorted(seen[("web.route", "/")]), ["_next/static/abc/_buildManifest.js", "static/main.js"])
        self.assertEqual(per["static/main.js"]["web.route"]["/"], {"relative": True})
        self.assertEqual(per["_next/static/abc/_buildManifest.js"]["web.route"]["/"], {})
        self.assertNotIn("/", facts["kinds"]["web.route"])
        self.assertEqual(per["index.html"]["web.framework"], {"next": {"from": "html"}})
        self.assertEqual(per["static/main.js"]["web.framework"]["next"], {"from": "bundle"})
        self.assertEqual(facts["kinds"]["secret.embedded"], sec_kinds({k: dec(v) for k, v in files.items()}))
        _, empty = F.make_site(self.p("empty"), "empty")
        self.assertEqual(tree(self.p("empty")), {})
        self.assertEqual(empty["records_by_path"], {})


class TestSqlite(Base):
    def test_schema_rows_and_facts(self):
        path, facts = F.make_sqlite(self.p("a.db"))
        self.assertTrue(path.read_bytes().startswith(b"SQLite format 3\x00"))
        self.assertEqual(facts["kinds"], F.sqlite_schema_facts(path))
        con = sqlite3.connect(str(path))
        self.addCleanup(con.close)
        for t, n in facts["row_counts"].items():
            self.assertEqual(con.execute("SELECT count(*) FROM %s" % t).fetchone()[0], n)
        rows = repr([con.execute("SELECT * FROM %s" % t).fetchall() for t in ("users", "items", "tags")])
        for value in facts["never_stored"]:
            self.assertIn(value, rows)
        self.assertEqual({r[0] for r in con.execute("SELECT type FROM sqlite_master")}, {"table", "index", "trigger", "view"})
        internal = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE name LIKE 'sqlite_%'")}
        self.assertEqual(internal, set(F.SQLITE_INTERNAL))
        self.assertEqual(facts["secret_patterns"], [])
        self.assertIn(F.SECRETS["aws-key"].encode(), path.read_bytes())    # in the raw bytes, never reported

    def test_columns_parse_from_the_stored_sql(self):
        path, facts = F.make_sqlite(self.p("a.db"))
        con = sqlite3.connect(str(path))
        self.addCleanup(con.close)
        for name, sql in con.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"):
            self.assertEqual(parse_columns(sql), facts["kinds"]["data.table"][name], name)
        cols = {c["name"]: c["type"] for c in facts["kinds"]["data.table"]["items"]["columns"]}
        self.assertEqual(cols["price"], "NUMERIC(10,2)")
        self.assertEqual(cols["extra"], "")
        self.assertEqual([c["name"] for c in facts["kinds"]["data.table"]["audit"]["columns"]], ["event", "when", "by"])

    def test_index_and_trigger_data_name_their_table(self):
        facts = F.make_sqlite(self.p("a.db"))[1]
        self.assertEqual(facts["kinds"]["data.index"], {"idx_items_owner": {"table": "items"}})
        self.assertEqual(facts["kinds"]["data.trigger"], {"trg_items_done": {"table": "items"}})

    def test_custom_ddl_and_overwrite(self):
        path, facts = F.make_sqlite(self.p("c.db"), ["CREATE TABLE a (x INT, y TEXT)", "CREATE INDEX ia ON a (x)"])
        self.assertEqual(facts["kinds"]["data.table"], {"a": {"columns": [{"name": "x", "type": "INT"}, {"name": "y", "type": "TEXT"}]}})
        self.assertEqual(facts["kinds"]["data.index"], {"ia": {"table": "a"}})
        F.make_sqlite(self.p("c.db"))
        self.assertNotIn("a", F.sqlite_schema_facts(self.p("c.db"))["data.table"])


class TestPlist(Base):
    def test_both_formats_parse_equal_and_paths_match(self):
        x, facts = F.make_plist(self.p("x.plist"))
        b, bfacts = F.make_plist(self.p("b.plist"), "binary")
        self.assertTrue(x.read_bytes().startswith(b"<?xml"))
        self.assertTrue(b.read_bytes().startswith(b"bplist00"))
        vx, vb = plistlib.loads(x.read_bytes()), plistlib.loads(b.read_bytes())
        self.assertEqual(vx, vb)
        self.assertEqual(plist_paths(vx), facts["kinds"]["data.plist-key"])
        self.assertEqual(bfacts["kinds"]["data.plist-key"], facts["kinds"]["data.plist-key"])
        self.assertIn("UIBackgroundModes[]", facts["kinds"]["data.plist-key"])
        self.assertIn("CFBundleURLTypes[].CFBundleURLSchemes[]", facts["kinds"]["data.plist-key"])
        for value in facts["never_stored"]:
            self.assertIn(value, x.read_text(encoding="utf-8"))

    def test_secrets_xml_scanned_binary_not(self):
        x, xf = F.make_plist(self.p("x.plist"))
        self.check_secrets(x, xf)
        self.assertEqual(xf["kinds"]["secret.embedded"], {"aws-key@.": {"count": 1}})
        b, bf = F.make_plist(self.p("b.plist"), "binary")
        self.assertNotIn("secret.embedded", bf["kinds"])
        self.assertEqual(bf["secret_patterns"], [])
        self.assertIn(F.SECRETS["aws-key"].encode(), b.read_bytes())

    def test_empty_plist(self):
        path, facts = F.make_variant(self.p("e.plist"), "plist", "empty")
        self.assertEqual(plistlib.loads(path.read_bytes()), {})
        self.assertEqual(facts["kinds"], {})


class TestOpenApi(Base):
    METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")

    def derive(self, doc):
        kinds = {"data.endpoint": {}, "data.schema-type": {}, "data.security": {}}
        for p, item in doc["paths"].items():
            for m, op in item.items():
                if m in self.METHODS:
                    kinds["data.endpoint"]["%s %s" % (m.upper(), p)] = {"operationId": op.get("operationId"),
                                                                       "tags": op.get("tags", [])}
        for s in (doc.get("components", {}).get("schemas") or doc.get("definitions") or {}):
            kinds["data.schema-type"][s] = {"kind": "schema"}
        sec = doc.get("components", {}).get("securitySchemes") or doc.get("securityDefinitions") or {}
        kinds["data.security"] = {k: {"type": v["type"]} for k, v in sec.items()}
        return kinds

    def test_json_and_swagger_facts_and_secrets(self):
        for swagger in (False, True):
            with self.subTest(swagger=swagger):
                path, facts = F.make_openapi_json(self.p("o%d.json" % swagger), swagger=swagger)
                doc = jload(path)
                got = self.derive(doc)
                for kind in got:
                    self.assertEqual(facts["kinds"][kind], got[kind], kind)
                self.assertEqual({k.split(" ")[0] for k in got["data.endpoint"]},
                                 {"GET", "PUT", "POST", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE"})
                self.check_secrets(path, facts)
                self.assertEqual(facts["secret_patterns"], ["aws-key", "jwt"])
                self.assertEqual(("swagger" in doc), swagger)
                for value in facts["never_stored"]:
                    self.assertIn(value, path.read_text(encoding="utf-8"))
        item = jload(self.p("o0.json"))["paths"]
        self.assertIn("parameters", item["/items"])
        self.assertIn("summary", item["/items/{itemId}"])

    def test_yaml_line_scanner_finds_exactly_the_facts(self):
        for variant in ("normal", "crlf"):
            with self.subTest(variant):
                path, facts = F.make_openapi_yaml(self.p(variant + ".yaml"), variant)
                text = path.read_text(encoding="utf-8")
                self.assertEqual(scan_yaml(text), facts["kinds"]["data.endpoint"])
                self.assertEqual(b"\r\n" in path.read_bytes(), variant == "crlf")
                self.check_secrets(path, facts)
                self.assertEqual(facts["secret_patterns"], ["bearer"])
        self.assertEqual(sorted(F.YAML_ENDPOINT_KEYS), sorted(F.OPENAPI_KINDS["data.endpoint"]))

    def test_yaml_traps_are_present_and_a_scanner_without_reset_would_fail(self):
        text = F.make_openapi_yaml(self.p("t.yaml"))[0].read_text(encoding="utf-8")
        self.assertIn('  "/quoted":', text)
        self.assertIn("\n    get:\n      type: object", text)
        found, current = set(), None
        for line in text.split("\n"):                     # the careless scanner: no reset
            m = re.match(r"^  (/\S*):\s*$", line)
            current = m.group(1) if m else current
            m = re.match(r"^    (get|put|post|delete|patch|head|options|trace):", line)
            if m and current:
                found.add("%s %s" % (m.group(1).upper(), current))
        self.assertIn("GET /session", found)               # the quoted path's `get:` lands on /session
        self.assertNotIn("GET /session", F.make_openapi_yaml(self.p("u.yaml"))[1]["kinds"]["data.endpoint"])

    def test_empty_yaml(self):
        path, facts = F.make_openapi_yaml(self.p("e.yaml"), "empty")
        self.assertEqual(scan_yaml(path.read_text(encoding="utf-8")), {})
        self.assertEqual(facts["kinds"], {})


class TestGraphqlAndProto(Base):
    def intro(self, doc):
        schema = doc["data"]["__schema"]
        kind = {"OBJECT": "type", "INPUT_OBJECT": "input", "ENUM": "enum", "INTERFACE": "interface", "UNION": "union",
                "SCALAR": "scalar"}
        types = {t["name"]: {"kind": kind[t["kind"]]} for t in schema["types"]
                 if not t["name"].startswith("__") and t["name"] not in ("String", "Int", "Float", "Boolean", "ID")}
        eps = {"%s %s" % (t["name"].lower(), f["name"]): {} for t in schema["types"]
               if t["name"] in ("Query", "Mutation", "Subscription") for f in t["fields"]}
        return {"data.schema-type": types, "data.endpoint": eps}

    def test_sdl(self):
        path, facts = F.make_graphql(self.p("a.graphql"))
        got = scan_sdl(path.read_text(encoding="utf-8"))
        self.assertEqual(facts["kinds"]["data.schema-type"], got["data.schema-type"])
        self.assertEqual(facts["kinds"]["data.endpoint"], got["data.endpoint"])
        self.assertIn("extend type Query", path.read_text(encoding="utf-8"))
        self.assertNotIn("Commented", got["data.schema-type"])
        self.assertEqual(got["data.schema-type"]["DateTime"], {"kind": "scalar"})

    def test_introspection_equals_sdl_and_skips_built_ins(self):
        path, facts = F.make_graphql(self.p("i.json"), introspection=True)
        doc = jload(path)
        names = {t["name"] for t in doc["data"]["__schema"]["types"]}
        self.assertTrue({"__Schema", "__Type", "String", "Int", "Boolean", "ID", "Float"} <= names)
        self.assertEqual(self.intro(doc), {k: facts["kinds"][k] for k in ("data.schema-type", "data.endpoint")})
        self.assertEqual(facts["kinds"], F.make_graphql(self.p("s.graphql"))[1]["kinds"])

    def test_proto(self):
        path, facts = F.make_proto(self.p("a.proto"))
        text = path.read_text(encoding="utf-8")
        got = scan_proto(text)
        self.assertEqual(facts["kinds"]["data.schema-type"], got["data.schema-type"])
        self.assertEqual(facts["kinds"]["data.rpc"], got["data.rpc"])
        self.assertIn("Item.Meta", got["data.schema-type"])
        for bad in ("Commented", "InBlock", "Meta"):
            self.assertNotIn(bad, got["data.schema-type"])
        self.assertIn("returns (stream Item)", text)
        self.assertIn("(stream GetItemRequest)", text)


class TestRepo(Base):
    EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
    CODE = (".js", ".ts", ".tsx", ".py", ".rs", ".go")

    def setUp(self):
        super().setUp()
        self.root, self.facts = F.make_repo(self.p("repo"))
        self.files = tree(self.root)
        self.text = {k: v.decode("utf-8") for k, v in self.files.items() if not k.endswith(".png")}

    # --- independent readers (contract 6.4) -----------------------------------------------------------------
    def routes_in(self, rel):
        t = self.text[rel]
        parts = rel.split("/")
        stem = Path(rel).stem
        if parts[0] == "app" and stem in ("page", "route"):
            segs = parts[1:-1]
            if any(s.startswith("_") for s in segs):
                return []
            segs = [s for s in segs if not (s.startswith("(") or s.startswith("@"))]
            path = canon("/" + "/".join(segs))
            if stem == "page":
                return ["GET " + path]
            names = re.findall(r"export\s+(?:async\s+function|function|const)\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b", t)
            return ["%s %s" % (m, path) for m in names]
        if parts[0] == "pages":
            if stem in ("_app", "_document", "_error"):
                return []
            segs = parts[1:-1] + ([] if stem == "index" else [stem])
            return ["%s %s" % ("ANY" if parts[1] == "api" else "GET", canon("/" + "/".join(segs)))]
        out = []
        for m in re.finditer(r'(?<![@\w])(?:app|router)\.(get|post|put|patch|delete|all)\(\s*"([^"]+)"', t):
            out.append("%s %s" % ("ANY" if m.group(1) == "all" else m.group(1).upper(), canon(m.group(2))))
        for m in re.finditer(r'(?<![@\w])app\.use\(\s*"([^"]+)"', t):
            out.append("ANY " + canon(m.group(1)))
        for m in re.finditer(r'@(?:app|router)\.(get|post|put|patch|delete)\("([^"]+)"', t):
            out.append("%s %s" % (m.group(1).upper(), canon(m.group(2))))
        for m in re.finditer(r'@app\.route\("([^"]+)"(?:, methods=\[([^\]]+)\])?', t):
            for meth in (m.group(2).replace('"', "").replace(" ", "").split(",") if m.group(2) else ["GET"]):
                out.append("%s %s" % (meth, canon(m.group(1))))
        for m in re.finditer(r'(?<![\w])path\("([^"]*)"', t):
            out.append("ANY " + canon("/" + m.group(1)))
        for m in re.finditer(r'\.route\("([^"]+)", get\(', t):
            out.append("GET " + canon(m.group(1)))
        for m in re.finditer(r'^\s*get "([^"]+)"', t, re.M):
            out.append("GET " + canon(m.group(1)))
        return out

    def specifiers(self, rel):
        t = self.text[rel]
        suffix = Path(rel).suffix
        if suffix in self.EXTS:
            return re.findall(r'(?:\bfrom\s+|\bimport\s*\(\s*|\brequire\(\s*|\bimport\s+)"([^"]+)"', t)
        if suffix == ".py":
            return re.findall(r"^\s*import\s+([\w.]+)", t, re.M) + re.findall(r"^\s*from\s+([\w.]+)\s+import", t, re.M)
        if suffix == ".rs":
            return ["mod " + m for m in re.findall(r"^mod (\w+);", t, re.M)]
        if suffix == ".go":
            return re.findall(r'^\s*(?:import\s+)?"([^"]+)"\s*$', t, re.M)
        return []

    def resolve(self, rel, spec):
        d = posixpath.dirname(rel)
        suffix = Path(rel).suffix
        if suffix in self.EXTS:
            if spec.startswith("@/"):
                target = "src/" + spec[2:]
            elif spec.startswith("."):
                target = posixpath.normpath(posixpath.join(d, spec))
            else:
                return []
            for cand in [target] + [target + e for e in self.EXTS] + [target + "/index" + e for e in self.EXTS]:
                if cand in self.files:
                    return [cand]
            return []
        if suffix == ".py":
            if spec.startswith("."):
                dots = len(spec) - len(spec.lstrip("."))
                base = d
                for _ in range(dots - 1):
                    base = posixpath.dirname(base)
                rest = spec.lstrip(".").replace(".", "/")
                target = posixpath.join(base, rest) if rest else base
            else:
                target = spec.replace(".", "/")
            for cand in (target + ".py", target + "/__init__.py"):
                if cand in self.files:
                    return [cand]
            return []
        if suffix == ".rs":
            name = spec[4:]
            for cand in (posixpath.join(d, name + ".rs"), posixpath.join(d, name, "mod.rs")):
                if cand in self.files:
                    return [cand]
            return []
        if suffix == ".go":
            root = d
            while root and root + "/go.mod" not in self.files:
                root = posixpath.dirname(root)
            mod = re.search(r"^module (\S+)", self.text[root + "/go.mod"], re.M).group(1)
            if spec == mod or spec.startswith(mod + "/"):
                folder = posixpath.join(root, spec[len(mod) + 1:])
                return sorted(k for k in self.files if posixpath.dirname(k) == folder and k.endswith(".go") and not k.endswith("_test.go"))
            return []
        return []

    # --- tests ------------------------------------------------------------------------------------------------
    def test_every_file_in_the_facts_exists_and_the_binary_is_binary(self):
        self.assertEqual(sorted(self.files), self.facts["files"])
        self.assertIn(b"\x00", self.files["assets/logo.png"])
        with self.assertRaises(UnicodeDecodeError):
            self.files["assets/logo.png"].decode("utf-8")

    def test_routes_match_the_files_in_both_directions(self):
        derived = {}
        for rel in sorted(self.text):
            if Path(rel).suffix in (".js", ".ts", ".tsx", ".py", ".rs", ".rb") and rel not in self.facts["graph"]["tests"]:
                r = self.routes_in(rel)
                if r:
                    derived[rel] = r
        self.assertEqual(derived, self.facts["graph"]["routes"])
        keys = {k for v in derived.values() for k in v}
        self.assertEqual(keys, set(self.facts["kinds"]["source.route"]))
        for k in keys:
            self.assertRegex(k, ROUTE_RE)
        for k in ("GET /docs/{slug*}", "GET /shop/{parts*}", "ANY /static/{rest*}", "GET /flask/files/{path}", "GET /blog/{slug}",
                  "ANY /ping-any", "ANY /polls", "PATCH /api/things", "GET /flask/hello", "GET /login", "GET /about"):
            self.assertIn(k, keys)
        for rel in self.facts["no_route_files"]:
            self.assertEqual(self.routes_in(rel), [], rel)
            self.assertIn(rel, self.files)
        self.assertIn("re_path(", self.text["services/py/polls/urls.py"])

    def test_import_graph_matches_in_both_directions(self):
        imports, unresolved = {}, {}
        for rel in sorted(self.text):
            if Path(rel).suffix not in self.CODE:
                continue
            for spec in self.specifiers(rel):
                got = self.resolve(rel, spec)
                if got:
                    imports.setdefault(rel, set()).update(got)
                else:
                    unresolved.setdefault(rel, set()).add(spec)
        self.assertEqual({k: sorted(v) for k, v in imports.items()}, self.facts["graph"]["imports"])
        self.assertEqual({k: sorted(v) for k, v in unresolved.items()}, self.facts["graph"]["unresolved"])
        for needle in ("express", "os", "fmt", "testing", "mod ghost"):
            self.assertTrue(any(needle in v for v in self.facts["graph"]["unresolved"].values()), needle)
        # the resolution cases of 6.4
        self.assertIn("src/lib/index.js", self.facts["graph"]["imports"]["src/server.js"])           # require("./lib")
        self.assertIn("services/rust/src/nested/mod.rs", self.facts["graph"]["imports"]["services/rust/src/main.rs"])
        self.assertIn("src/polyfill.ts", self.facts["graph"]["imports"]["src/api-client.ts"])        # import "./x"
        self.assertIn("src/types.ts", self.facts["graph"]["imports"]["src/api-client.ts"])           # import type
        self.assertIn("src/lib/util.ts", self.facts["graph"]["imports"]["src/api-client.ts"])        # tsconfig alias
        self.assertIn("src/lazy.js", self.facts["graph"]["imports"]["src/server.js"])                # import("./lazy")
        self.assertEqual(self.facts["graph"]["imports"]["services/go/main.go"], ["services/go/internal/util/util.go"])

    def test_env_reads_match_the_patterns(self):
        pats = [r"process\.env\.([A-Z][A-Z0-9_]*)", r"process\.env\[\s*['\"]([A-Z][A-Z0-9_]*)['\"]", r"import\.meta\.env\.([A-Z][A-Z0-9_]*)",
                r"os\.environ(?:\.get)?\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]", r"os\.environ\[\s*['\"]([A-Z][A-Z0-9_]*)['\"]",
                r"getenv\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]", r"env::var(?:_os)?\(\s*\"([A-Z][A-Z0-9_]*)\"",
                r"env!\(\s*\"([A-Z][A-Z0-9_]*)\"", r"ENV\[\s*['\"]([A-Z][A-Z0-9_]*)['\"]"]
        got = {}
        for rel, t in self.text.items():
            names = sorted({n for p in pats for n in re.findall(p, t)})
            if names:
                got[rel] = names
        self.assertEqual(got, self.facts["env_by_file"])
        everywhere = {k for v in got.values() for k in v}
        self.assertEqual(everywhere, {k for ks in self.facts["records_by_path"].values() for k in ks.get("source.env", {})})
        # DATABASE_URL is read in two files, so it is only in records_by_path; the unique keys stay in kinds
        self.assertEqual(set(self.facts["kinds"]["source.env"]), everywhere - {"DATABASE_URL"})
        self.assertIn("process.env.lowercase_var", self.text["src/config.js"])
        self.assertNotIn("lowercase_var", self.facts["kinds"]["source.env"])

    def test_records_by_path_and_the_unique_keys_of_kinds(self):
        per = {}
        for rel in sorted(self.text):
            if Path(rel).suffix in (".js", ".ts", ".tsx", ".py", ".rs", ".rb") and rel not in self.facts["graph"]["tests"]:
                for k in self.routes_in(rel):
                    per.setdefault(rel, {}).setdefault("source.route", {})[k] = {}
        for rel, keys in self.facts["env_by_file"].items():
            for k in keys:
                per.setdefault(rel, {}).setdefault("source.env", {})[k] = {}
        for r in self.facts["table_records"]:
            per.setdefault(r["path"], {}).setdefault("source.table", {})[r["key"]] = {"access": r["access"]}
        for t in self.facts["graph"]["tests"]:
            per.setdefault(t, {}).setdefault("source.test", {})[t] = {"imports": self.facts["graph"]["imports"].get(t, [])}
        for key, via in F.REPO_ENTRIES.items():
            per.setdefault(key.split("#")[0], {}).setdefault("source.entry", {})[key] = {"via": via}
        for key, data in sec_kinds({k: v for k, v in self.text.items() if is_read(k)}).items():
            per.setdefault(key.split("@", 1)[1], {}).setdefault("secret.embedded", {})[key] = data
        self.assertEqual(self.facts["records_by_path"], per)
        seen = {}
        for rel, ks in per.items():
            for kind, keys in ks.items():
                for key in keys:
                    seen[(kind, key)] = seen.get((kind, key), 0) + 1
        for (kind, key), n in seen.items():
            self.assertEqual(key in self.facts["kinds"].get(kind, {}), n == 1, (kind, key))
        self.assertEqual(seen[("source.env", "DATABASE_URL")], 2)
        self.assertEqual(seen[("source.table", "items")], 2)

    def test_tests_by_basename_and_folder_patterns(self):
        def is_test(p):
            base = posixpath.basename(p)
            return bool(re.search(r"(^|/)(tests?|__tests__|spec)/", p) or re.search(r"\.(test|spec)\.[cm]?[jt]sx?$", base)
                        or re.match(r"test_.*\.py$", base) or re.search(r"_test\.(go|py)$", base))
        tests = sorted(k for k in self.files if is_test(k) and Path(k).suffix in self.CODE)
        self.assertEqual(tests, self.facts["graph"]["tests"])
        self.assertEqual(sorted(self.facts["kinds"]["source.test"]), tests)
        for t, d in self.facts["kinds"]["source.test"].items():
            self.assertEqual(d["imports"], self.facts["graph"]["imports"].get(t, []))
        self.assertIn("services/py/test_widgets.py", tests)          # `^test_.*\.py$` matched on the basename

    def test_tables_schema_and_code_references(self):
        sql = self.text["db/schema.sql"].lower()
        schema = {m: "db/schema.sql" for m in re.findall(r"create table (\w+)", sql)}
        schema.update({m: "prisma/schema.prisma" for m in re.findall(r'@@map\("(\w+)"\)', self.text["prisma/schema.prisma"])})
        recs = [{"key": k, "path": p, "access": None} for k, p in schema.items()]
        for rel, t in sorted(self.text.items()):
            if Path(rel).suffix in self.CODE and rel not in self.facts["graph"]["tests"]:
                for table in schema:
                    low = t.lower()
                    if re.search(r"\b(?:insert\s+into|update|delete\s+from)\s+%s\b" % table, low):
                        recs.append({"key": table, "path": rel, "access": "write"})
                    elif re.search(r"\b(?:from|join)\s+%s\b" % table, low):
                        recs.append({"key": table, "path": rel, "access": "read"})
        key = lambda r: (r["key"], r["path"])
        self.assertEqual(sorted(recs, key=key), sorted(self.facts["table_records"], key=key))
        self.assertEqual(set(self.facts["kinds"]["source.table"]), {"notes"})      # the other keys have several records
        self.assertEqual({k for ks in self.facts["records_by_path"].values() for k in ks.get("source.table", {})}, set(schema))

    def test_languages_with_bytes_frameworks_and_entries(self):
        langs = {}
        for rel, data in self.files.items():
            lang = {".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".py": "Python", ".rs": "Rust", ".go": "Go",
                    ".rb": "Ruby", ".sql": "SQL", ".prisma": "Prisma"}.get(Path(rel).suffix)
            if lang:
                d = langs.setdefault(lang, {"files": 0, "bytes": 0})
                d["files"] += 1
                d["bytes"] += len(data)
        self.assertEqual(self.facts["kinds"]["source.language"], langs)
        self.assertEqual(set(langs), {"JavaScript", "TypeScript", "Python", "Rust", "Go", "Ruby", "SQL", "Prisma"})
        rules = {  # framework id -> (manifest, text the manifest must hold)
            "express": ("package.json", '"express"'), "fastify": ("package.json", '"fastify"'), "next": ("package.json", '"next"'),
            "react": ("package.json", '"react"'), "vue": ("apps/vue/package.json", '"vue"'), "nuxt": ("apps/vue/package.json", '"nuxt"'),
            "svelte": ("apps/svelte/package.json", '"svelte"'), "sveltekit": ("apps/svelte/package.json", "@sveltejs/kit"),
            "angular": ("apps/ng/package.json", "@angular/core"), "nestjs": ("apps/nest/package.json", "@nestjs/core"),
            "electron": ("apps/desktop/package.json", '"electron"'), "react-native": ("apps/mobile/package.json", '"react-native"'),
            "expo": ("apps/mobile/package.json", '"expo"'), "django": ("services/py/pyproject.toml", "django"),
            "flask": ("services/py/requirements.txt", "flask"), "fastapi": ("services/py/requirements.txt", "fastapi"),
            "axum": ("services/rust/Cargo.toml", "axum"), "actix-web": ("services/rust/Cargo.toml", "actix-web"),
            "tauri": ("services/rust/Cargo.toml", "tauri"), "gin": ("services/go/go.mod", "github.com/gin-gonic/gin"),
            "echo": ("services/go/go.mod", "github.com/labstack/echo"), "chi": ("services/go/go.mod", "github.com/go-chi/chi"),
            "rails": ("services/rails/Gemfile", 'gem "rails"'), "flutter": ("mobile_flutter/pubspec.yaml", "flutter:"),
            "android": ("android/app/build.gradle", "com.android.application"),
        }
        self.assertEqual(self.facts["kinds"]["source.framework"], {k: {"from": [m]} for k, (m, _) in rules.items()})
        for fw, (manifest, needle) in rules.items():
            self.assertIn(needle, self.text[manifest], fw)
        pkg = json.loads(self.text["package.json"])
        entries = {pkg["main"]: "main", pkg["bin"]["snag"]: "bin"}
        start = pkg["scripts"]["start"].split()[1]
        self.assertNotIn(start, entries)                      # scripts.start names a file distinct from `main`
        entries[start] = "scripts.start"
        entries["app/layout.tsx"] = "next-layout"
        for rel, t in self.text.items():
            if 'if __name__ == "__main__"' in t:
                entries[rel] = "python-main"
            if "fn main(" in t:
                entries[rel] = "rust-main"
            if "func main(" in t:
                entries[rel] = "go-main"
        man = self.text["android/app/src/main/AndroidManifest.xml"]
        self.assertIn("android.intent.category.LAUNCHER", man)
        entries["android/app/src/main/AndroidManifest.xml#com.example.snag.MainActivity"] = "android-launcher"
        for rel in entries:
            self.assertIn(rel.split("#")[0], self.files)
        self.assertEqual(self.facts["kinds"]["source.entry"], {k: {"via": v} for k, v in entries.items()})
        self.assertEqual(json.loads(self.text["tsconfig.json"])["compilerOptions"]["paths"], {"@/*": ["src/*"]})

    def test_secrets_are_where_the_facts_say(self):
        texts = {k: v for k, v in self.text.items() if is_read(k)}
        got = sec_kinds(texts)
        self.assertEqual(self.facts["kinds"]["secret.embedded"], got)
        files = {}
        for key in got:
            pat, _, rel = key.partition("@")
            files.setdefault(rel, []).append(pat)
        self.assertEqual({k: sorted(v) for k, v in files.items()}, F.REPO_SECRETS)
        for value in self.facts["never_stored"]:
            escaped_secret = any(value == json.dumps(s)[1:-1] for s in F.SECRETS.values())     # the JSON-escaped listing
            self.assertTrue(escaped_secret or any(value in t for t in self.text.values()), value)
        self.assertEqual(self.facts["secret_patterns"], sorted({k.split("@")[0] for k in got}))
        self.assertIn("\n", F.SECRETS["private-key"])

    def test_trace_literal_is_consistent_with_the_graph(self):
        g = self.facts["graph"]
        seed = self.facts["seeds"]["route"]
        seeds = [f for f, keys in g["routes"].items() if seed in keys]
        self.assertEqual(seeds, F.REPO_TRACE[seed]["seed_files"])
        files = {f: ("seed", 0) for f in seeds}
        frontier = list(seeds)
        for depth in (1, 2, 3):
            nxt = []
            for f in frontier:
                for t in g["imports"].get(f, []):
                    if t not in files:
                        files[t] = ("uses", depth)
                        nxt.append(t)
            frontier = nxt
        frontier = list(seeds)
        for depth in (1, 2):
            nxt = []
            for f in frontier:
                for k, v in g["imports"].items():
                    if f in v and k not in g["tests"] and k not in files:
                        files[k] = ("used-by", depth)
                        nxt.append(k)
            frontier = nxt
        base = dict(files)
        for t in g["tests"]:
            if any(i in base for i in g["imports"].get(t, [])):
                files[t] = ("test", 1)
        got = [{"path": p, "role": r, "depth": d} for p, (r, d) in files.items()]
        order = {"seed": 0, "uses": 1, "used-by": 2, "test": 3}
        sk = lambda x: (order[x["role"]], x["depth"], x["path"])
        self.assertEqual(sorted(got, key=sk), sorted(F.REPO_TRACE[seed]["files"], key=sk))
        env = sorted({e for f in files for e in self.facts["env_by_file"].get(f, [])})
        self.assertEqual(env, F.REPO_TRACE[seed]["env"])
        tables = sorted({r["key"] for r in self.facts["table_records"] if r["path"] in files and r["access"]})
        self.assertEqual(tables, F.REPO_TRACE[seed]["tables"])

    def test_seeds_find_their_files(self):
        s, sf = self.facts["seeds"], self.facts["seed_files"]
        self.assertEqual([f for f, k in self.facts["graph"]["routes"].items() if s["route"] in k], sf["route"])
        self.assertIn(s["path"], self.files)
        sym = [f for f, t in self.text.items() if re.search(r"\b(def|function|fn|class|func|const|let|var)\s+%s\b" % s["symbol"], t)]
        self.assertEqual(sym, sf["symbol"])
        self.assertEqual([f for f, t in self.text.items() if s["literal"] in t], sf["literal"])
        self.assertNotIn(s["missing"], self.facts["kinds"]["source.route"])
        self.assertFalse(any(s["missing"].split(" ")[1] in t for t in self.text.values()))

    def test_import_cycle_and_alias_cases_exist(self):
        self.assertEqual(self.facts["graph"]["imports"]["src/cyc/a.js"], ["src/cyc/b.js"])
        self.assertEqual(self.facts["graph"]["imports"]["src/cyc/b.js"], ["src/cyc/a.js"])

    def test_empty_and_malformed_repos(self):
        root, facts = F.make_repo(self.p("e"), "empty")
        self.assertEqual(sorted(tree(root)), ["README.md"])
        root, facts = F.make_repo(self.p("m"), "malformed")
        files = tree(root)
        self.assertEqual(sorted(files), facts["files"])
        for rel in ("package.json", "tsconfig.json"):
            with self.assertRaises(ValueError):
                json.loads(files[rel].decode("utf-8"))
        self.assertEqual(facts["skips"]["src/binary.ts"], "binary")
        self.assertIn(b"\x00", files["src/binary.ts"][:8192])
        with self.assertRaises(UnicodeDecodeError):
            files["src/bad_utf8.js"].decode("utf-8")
        self.assertIn("/after-bad-bytes", dec(files["src/bad_utf8.js"]))
        line = files["src/huge-minified.js"]
        self.assertNotIn(b"\n", line)
        self.assertGreaterEqual(len(line), F.ONE_MB)
        self.assertLess(len(line), CAP)
        self.assertEqual(facts["kinds"]["source.route"], {"GET /start-of-line": {}, "GET /end-of-line": {}, "GET /after-bad-bytes": {}})
        for key in ("/start-of-line", "/end-of-line"):
            self.assertIn(key.encode(), line)
        with self.assertRaises(KeyError):
            F.make_repo(self.p("x"), "nope")

    def test_meta_an_unlisted_route_env_or_import_is_noticed(self):
        rel = "src/routes/items.js"
        self.text[rel] += '\nrouter.get("/ghost", h);\n'
        self.assertNotEqual(self.routes_in(rel), self.facts["graph"]["routes"][rel])
        self.text["src/db.js"] += '\nprocess.env.GHOST_VAR;\n'
        self.assertIn("GHOST_VAR", re.findall(r"process\.env\.([A-Z][A-Z0-9_]*)", self.text["src/db.js"]))
        self.assertNotIn("GHOST_VAR", self.facts["env_by_file"]["src/db.js"])
        self.text["src/lazy.js"] += '\nrequire("./db");\n'
        self.assertNotEqual(self.specifiers("src/lazy.js"), [])

    def test_nothing_in_the_repo_is_a_real_path_or_address(self):
        for rel, t in self.text.items():
            self.assertNotRegex(t, r"[A-Za-z]:\\Users|/home/|/Users/", rel)


class TestVersionPairs(Base):
    def per_path(self, root, single):
        """{(kind, key, path): data} for one side, read independently: a single file or a folder of scripts."""
        out = {}
        if single:
            return out
        for rel, data in tree(root).items():
            for kind, keys in scan_js(dec(data)).items():
                for key, d in keys.items():
                    out[(kind, key, rel)] = d
        return out

    def check(self, fa, fb, facts):
        key = lambda x: (x["kind"], x["key"], x["path"])
        self.assertEqual(sorted(k for k in fb if k not in fa), sorted(key(x) for x in facts["added"]))
        self.assertEqual(sorted(k for k in fa if k not in fb), sorted(key(x) for x in facts["removed"]))
        self.assertEqual(sorted(k for k in fa if k in fb and fa[k] != fb[k]), sorted(key(x) for x in facts["changed"]))
        for c in facts["changed"]:
            self.assertEqual(fa[key(c)], c["before"])
            self.assertEqual(fb[key(c)], c["after"])
        for u in facts["unchanged"]:
            self.assertEqual(fa[key(u)], fb[key(u)])

    def never_present(self, facts, *paths):
        texts = [dec(Path(p).read_bytes()) for p in paths]
        for value in facts["never_stored"]:
            self.assertTrue(any(value in t or json.dumps(value)[1:-1] in t or json.dumps(json.dumps(value)[1:-1])[1:-1] in t
                                for t in texts), value[:30])

    def test_openapi_pair_differs_exactly_as_listed(self):
        a, b, facts = F.make_version_pair(self.p("p"), "openapi")
        self.assertEqual((a.parent.name, b.parent.name, a.name), ("v1", "v2", b.name))
        t = TestOpenApi("derive")
        sides = []
        for p in (a, b):
            d = {}
            for kind, keys in t.derive(jload(p)).items():
                d.update({(kind, k, "."): v for k, v in keys.items()})
            d.update({("secret.embedded", k, "."): v for k, v in sec_kinds({".": dec(p.read_bytes())}).items()})
            sides.append(d)
        self.check(sides[0], sides[1], facts)
        self.assertTrue(any(u["kind"] == "secret.embedded" for u in facts["unchanged"]))
        self.assertTrue(facts["added"] and facts["removed"] and facts["changed"])
        self.never_present(facts, a, b)

    def test_har_pair_differs_exactly_as_listed(self):
        a, b, facts = F.make_version_pair(self.p("p"), "har")
        self.assertEqual((a.parent.name, b.parent.name, a.name), ("v1", "v2", b.name))
        sides = []
        for p in (a, b):
            d = {}
            for kind, keys in har_derive(jload(p)["log"]["entries"]).items():
                d.update({(kind, k, "."): v for k, v in keys.items()})
            d.update({("secret.embedded", k, "."): v for k, v in sec_kinds({".": dec(p.read_bytes())}).items()})
            sides.append(d)
        self.check(sides[0], sides[1], facts)
        self.assertTrue(any(u["kind"] == "secret.embedded" for u in facts["unchanged"]))
        self.never_present(facts, a, b)

    def test_a_moved_fact_is_removed_at_the_old_path_and_added_at_the_new_one(self):
        a, b, facts = F.make_version_pair(self.p("p"), "folder")
        sides = [self.per_path(r, False) for r in (a, b)]
        self.check(sides[0], sides[1], facts)
        self.assertEqual([x["path"] for x in facts["removed"]], ["a.js"])
        self.assertEqual([x["path"] for x in facts["added"]], ["b.js"])
        self.assertEqual(facts["removed"][0]["key"], facts["added"][0]["key"])

    def test_unknown_pair_kind(self):
        with self.assertRaises(ValueError):
            F.make_version_pair(self.p("p"), "nope")


class TestTierB(Base):
    def capture_expect(self, doc):
        actions = ("navigate", "click", "type", "select", "submit", "wait", "observe")
        steps = [s for s in doc["steps"] if isinstance(s, dict) and "n" in s and s.get("action") in actions]
        kinds = {"flow.step": {}, "flow.screen": {}, "web.endpoint": {}, "web.payload-shape": {}}
        for i, s in enumerate(steps):
            u = urlsplit(s["url"]) if s["action"] == "navigate" else None
            what = "%s%s" % (u.hostname, u.path) if u else s["target"]
            kinds["flow.step"]["%d %s %s" % (s["n"], s["action"], what)] = {
                "screen": s.get("screen"), "next": steps[i + 1]["n"] if i + 1 < len(steps) else None}
            if s.get("screen"):
                kinds["flow.screen"][s["screen"]] = {}
            for r in s.get("requests", []):
                key = har_key(r["method"], r["url"])
                d = kinds["web.endpoint"].setdefault(key, {"statuses": [], "requestTypes": [], "responseTypes": [], "count": 0,
                                                          "queryParams": []})
                d["statuses"] = sorted(set(d["statuses"]) | {r["status"]})
                d["count"] += 1
                d["queryParams"] = sorted(set(d["queryParams"]) | {k for k, _ in parse_qsl(urlsplit(r["url"]).query)})
                for direction, field in (("request", "requestShape"), ("response", "responseShape")):
                    if field in r:
                        kinds["web.payload-shape"]["%s %s" % (key, direction)] = {"shape": r[field]}
        return kinds

    def test_capture_parses_and_every_kind_matches(self):
        path, facts = F.make_capture(self.p("c.json"))
        doc = jload(path)
        self.assertEqual(doc["format"], "app-blueprint-capture/1")
        steps = doc["steps"]
        self.assertEqual([s["n"] for s in steps], list(range(1, len(steps) + 1)))
        self.assertTrue({s["action"] for s in steps} <= {"navigate", "click", "type", "select", "submit", "wait", "observe"})
        self.assertEqual(facts["kinds"], self.capture_expect(doc))
        self.assertIn("1 navigate app.example.test/login", facts["kinds"]["flow.step"])
        self.assertEqual(facts["kinds"]["flow.step"]["7 observe table 'Items'"], {"screen": "Items", "next": None})
        text = path.read_text(encoding="utf-8")
        for v in facts["never_stored"]:
            self.assertIn(v, text)
        values = {str(s["n"]): s["value"] for s in steps if "value" in s}
        self.assertEqual(facts["replaced"], {n: "<typed:%d chars>" % len(v) for n, v in values.items()})
        self.assertEqual(set(facts["never_stored"]), set(values.values()) | {"fixture-query-value"})
        self.assertEqual(facts["kinds"]["web.endpoint"]["POST app.example.test/api/session"]["queryParams"], ["ref"])

    def test_capture_variants_are_wrong_in_the_stated_way(self):
        def make(name):
            p, facts = F.make_variant(self.p(name + ".json"), "capture", name)
            return jload(p), facts
        doc, facts = make("wrong_format")
        self.assertNotEqual(doc["format"], "app-blueprint-capture/1")
        self.assertEqual(facts["expect"], {"error": "bad-capture"})
        doc, facts = make("steps_not_list")
        self.assertNotIsInstance(doc["steps"], list)
        self.assertEqual(facts["expect"], {"error": "bad-capture"})
        doc, facts = make("step_missing_n")
        self.assertNotIn("n", doc["steps"][0])
        self.assertEqual(facts["malformed_paths"], ["steps[0]"])
        self.assertEqual(facts["kinds"], self.capture_expect(doc))
        self.assertFalse(any("navigate" in k for k in facts["kinds"]["flow.step"]))
        doc, facts = make("bad_action")
        self.assertEqual(doc["steps"][1]["action"], "teleport")
        self.assertEqual(facts["malformed_paths"], ["steps[1]"])
        self.assertEqual(facts["expect"], {"skip": ["malformed"]})
        self.assertEqual(facts["kinds"], self.capture_expect(doc))
        self.assertEqual(facts["kinds"]["flow.step"]["1 navigate app.example.test/login"]["next"], 3)    # the next kept step
        self.assertNotIn("ambiguous", facts)
        doc, facts = make("empty")
        self.assertEqual(doc["steps"], [])
        self.assertEqual(facts["kinds"], {"flow.step": {}, "flow.screen": {}, "web.endpoint": {}, "web.payload-shape": {}})

    def test_rea_normal_mapping(self):
        path, facts = F.make_rea_bundle(self.p("r.json"))
        doc = jload(path)
        self.assertIsInstance(doc["evidence_format"], int)
        recs = doc["records"]
        self.assertEqual(len(recs), facts["counts"]["imported"])
        ids = {r["evidence_id"] for r in recs}
        by_id = {}
        for rec, exp in zip(recs, facts["records"]):
            loc = rec["locations"][0]
            ident = loc.get("path") or loc.get("address") or loc.get("offset")
            self.assertEqual(exp["key"], "%s:%s" % (rec["operation"], ident))
            self.assertEqual(exp["kind"], "rea." + re.sub(r"[^a-z0-9]+", "-", rec["operation"].lower()).strip("-"))
            self.assertEqual(exp["source"]["digest"], "sha256:" + rec["subject"]["identity"]["sha256"])
            self.assertEqual(exp["source"]["artifact"], rec["subject"]["name"])
            self.assertEqual(exp["source"]["path"], loc.get("path"))
            self.assertEqual(exp["source"]["offset"], loc.get("offset"))
            self.assertIn(exp["key"], facts["kinds"][exp["kind"]])
            self.assertEqual(exp["basis"], rec["confidence"] if rec["confidence"] in ("observed", "derived", "inferred") else "inferred")
            self.assertEqual(exp["limitations"][:2], rec["limitations"] + [F.REA_NOTE])
            dropped = [l["evidence_id"] for l in rec["evidence_links"] if l["evidence_id"] not in ids]
            self.assertEqual(exp["limitations"][2:], ["link %s not in this bundle; dropped" % d for d in dropped])
            by_id[rec["evidence_id"]] = "%s:%s" % (exp["kind"], exp["key"])
            self.assertEqual(exp["links"], [by_id[l["evidence_id"]] for l in rec["evidence_links"] if l["evidence_id"] in ids])
            self.assertIn(rec["subject"]["local_path"], facts["never_stored"])
        self.assertTrue(recs[0]["subject"]["local_path"].startswith("/"))
        self.assertEqual(doc["unknowns"][0]["unknown_id"], next(iter(facts["kinds"]["rea.unknown"])))
        self.assertTrue(any(r["evidence_links"] for r in recs))

    def test_rea_tolerant_and_other_variants(self):
        path, facts = F.make_rea_bundle(self.p("t.json"), "tolerant")
        doc = jload(path)
        self.assertEqual(doc["evidence_format"], 3)
        recs = doc["records"]
        bad = [i for i, r in enumerate(recs) if not isinstance(r, dict) or "subject" not in r or "operation" not in r]
        self.assertEqual(["records[%d]" % i for i in bad], facts["malformed_paths"])
        self.assertEqual(len(bad), facts["counts"]["malformed"])
        self.assertEqual(len(recs) - len(bad), facts["counts"]["imported"])
        self.assertTrue(any(isinstance(r, dict) and "future_field" in r for r in recs))
        self.assertTrue(any(len(json.dumps(r["normalized_result"])) > 4096 for r in recs if isinstance(r, dict) and "normalized_result" in r))
        self.assertTrue(any(isinstance(l, str) for r in recs if isinstance(r, dict) for l in r.get("evidence_links", [])))
        self.assertTrue(any(isinstance(l, dict) and "evidence_id" not in l for r in recs if isinstance(r, dict) for l in r.get("evidence_links", [])))
        self.assertTrue(any(isinstance(r, dict) and not r.get("locations", [1]) for r in recs))
        self.assertEqual(len(facts["records"]), facts["counts"]["imported"])
        self.assertNotIn("evidence_format", jload(F.make_rea_bundle(self.p("n.json"), "not_rea")[0]))
        self.assertEqual(F.make_rea_bundle(self.p("n2.json"), "not_rea")[1]["expect"], {"error": "target-unsupported"})
        self.assertEqual(jload(F.make_rea_bundle(self.p("e.json"), "empty")[0])["records"], [])
        with self.assertRaises(KeyError):
            F.make_rea_bundle(self.p("x.json"), "nope")


class TestAndroidFolders(Base):
    NS = "{http://schemas.android.com/apk/res/android}"
    WHOLE = r'^(https?://[^"\s]{1,2048}|/(?:api|v[0-9])/[^"\s]{0,2048})$'
    SDKS = ("com/google/firebase", "com/facebook", "io/sentry", "com/stripe", "okhttp3", "retrofit2", "com/onesignal", "io/branch")

    def manifest_kinds(self, path):
        m = ET.fromstring(Path(path).read_bytes())
        pkg = m.get("package")
        a = lambda e, n: e.get(self.NS + n)
        full = lambda n: pkg + n if n.startswith(".") else n
        sdk = m.find("uses-sdk")
        num = lambda v: int(v) if v is not None and v.isdigit() else v          # 6.7 value types: digits are integers
        kinds = {"mobile.package": {pkg: {"versionName": a(m, "versionName"), "versionCode": num(a(m, "versionCode")),
                                          "minSdk": num(a(sdk, "minSdkVersion")), "targetSdk": num(a(sdk, "targetSdkVersion"))}},
                 "mobile.permission": {a(e, "name"): {} for e in m.iter("uses-permission")},
                 "mobile.screen": {}, "mobile.service": {}, "mobile.deeplink": {}, "mobile.setting": {}}
        for e in m.iter("activity"):
            launcher = any(c.get(self.NS + "name") == "android.intent.category.LAUNCHER" for c in e.iter("category"))
            kinds["mobile.screen"][full(a(e, "name"))] = {"exported": a(e, "exported") == "true", "launcher": launcher}
            for f in e.iter("intent-filter"):
                if any(c.get(self.NS + "name") == "android.intent.category.BROWSABLE" for c in f.iter("category")):
                    for d in f.iter("data"):
                        key = "%s://%s%s" % (a(d, "scheme"), a(d, "host"), a(d, "pathPrefix") or "")
                        kinds["mobile.deeplink"][key] = {"activity": full(a(e, "name")), "autoVerify": a(f, "autoVerify") == "true"}
        for tag in ("service", "receiver", "provider"):
            for e in m.iter(tag):
                kinds["mobile.service"][full(a(e, "name"))] = {"type": tag, "exported": a(e, "exported") == "true"}
        app = m.find("application")
        for attr in ("usesCleartextTraffic", "debuggable", "allowBackup", "networkSecurityConfig"):
            if a(app, attr) is not None:
                kinds["mobile.setting"][attr] = {"value": a(app, attr)}
        return kinds

    def sdk_kinds(self, histogram):
        return {k: {"classes": n} for k, n in histogram.items() if k.startswith(self.SDKS)}

    def test_jadx_normal_and_gradle(self):
        for variant, man in (("normal", "resources/AndroidManifest.xml"), ("gradle", "app/src/main/AndroidManifest.xml")):
            with self.subTest(variant):
                root, facts = F.make_jadx_dir(self.p(variant), variant)
                files = tree(root)
                want = self.manifest_kinds(root / man)
                for kind, got in want.items():
                    self.assertEqual(facts["kinds"][kind], got, kind)
                hist, endpoints = {}, {}
                for rel, data in files.items():
                    if Path(rel).suffix in (".java", ".kt"):
                        text = data.decode("utf-8")
                        mm = re.search(r"^package ([\w.]+)", text, re.M)
                        hist[mm.group(1).replace(".", "/")] = hist.get(mm.group(1).replace(".", "/"), 0) + 1
                        for i, line in enumerate(text.split("\n"), 1):
                            for lit in re.findall(r'"([^"\\\n]{0,2048})"', line):       # whole literals only (5.8)
                                if re.match(self.WHOLE, lit):
                                    endpoints.setdefault(lit, {"path": rel, "line": i})
                self.assertEqual(facts["kinds"]["mobile.sdk"], self.sdk_kinds(hist))
                self.assertEqual(facts["kinds"]["mobile.endpoint"], {k: {"from": "source"} for k in endpoints})
                self.assertEqual(facts["locations"], endpoints)
                for lit, loc in endpoints.items():
                    self.assertLessEqual(len(files[loc["path"]].decode("utf-8").split("\n")[loc["line"] - 1]), 160)
                self.assertEqual(facts["kinds"]["secret.embedded"],
                                 sec_kinds({k: dec(v) for k, v in files.items() if Path(k).suffix in (".java", ".kt", ".xml")}))
                self.assertEqual(facts["missing"], [])

    def test_jadx_variants(self):
        root, facts = F.make_jadx_dir(self.p("e"), "empty")
        self.assertEqual(tree(root), {})
        self.assertTrue((root / "sources").is_dir())
        self.assertEqual(facts["missing"], ["resources/AndroidManifest.xml"])
        root, facts = F.make_jadx_dir(self.p("nm"), "no_manifest")
        self.assertFalse(any(p.endswith("AndroidManifest.xml") for p in tree(root)))
        self.assertEqual(facts["missing"], ["resources/AndroidManifest.xml"])
        self.assertNotIn("mobile.package", facts["kinds"])
        root, facts = F.make_jadx_dir(self.p("ns"), "no_sources")
        self.assertEqual(list(tree(root)), ["resources/AndroidManifest.xml"])
        self.assertEqual(facts["missing"], ["sources/"])
        root, facts = F.make_jadx_dir(self.p("dt"), "doctype")
        data = tree(root)["resources/AndroidManifest.xml"]
        self.assertIn(b"<!DOCTYPE", data)
        self.assertIn(b"<!ENTITY", data)
        self.assertIn(b"&fixture;", data)
        self.assertEqual(facts["skips"], {"resources/AndroidManifest.xml": "malformed"})
        self.assertEqual(facts["notes"], ["DOCTYPE or ENTITY refused"])
        self.assertEqual(facts["missing"], [])
        self.assertNotIn("mobile.package", facts["kinds"])
        self.assertIn("mobile.endpoint", facts["kinds"])
        root, facts = F.make_jadx_dir(self.p("um"), "unsupported_manifest")
        data = tree(root)["resources/AndroidManifest.xml"]
        self.assertNotIn(b"\x00", data[:8192])
        self.assertFalse(data.startswith((b"<?xml", b"<manifest")))
        self.assertEqual(facts["skips"], {"resources/AndroidManifest.xml": "unsupported"})
        self.assertEqual(facts["missing"], [])
        root, facts = F.make_jadx_dir(self.p("bm"), "bad_manifest")
        with self.assertRaises(ET.ParseError):
            ET.fromstring(tree(root)["resources/AndroidManifest.xml"])
        self.assertEqual(facts["skips"], {"resources/AndroidManifest.xml": "malformed"})
        root, facts = F.make_jadx_dir(self.p("bi"), "binary_manifest")
        data = tree(root)["resources/AndroidManifest.xml"]
        self.assertIn(b"\x00", data[:8192])
        self.assertFalse(data.startswith((b"<?xml", b"<manifest")))
        self.assertEqual(facts["skips"], {"resources/AndroidManifest.xml": "binary"})
        with self.assertRaises(KeyError):
            F.make_jadx_dir(self.p("x"), "nope")

    def test_apktool_normal(self):
        root, facts = F.make_apktool_dir(self.p("a"))
        files = tree(root)
        yml = files["apktool.yml"].decode("utf-8")
        num = lambda v: int(v) if v.isdigit() else v
        self.assertEqual({k: num(re.search(r"^\s*%s: '?([^'\n]+)'?$" % k, yml, re.M).group(1)) for k in facts["yml"]}, facts["yml"])
        self.assertTrue(all(isinstance(facts["yml"][k], int) for k in ("minSdkVersion", "targetSdkVersion", "versionCode")))
        self.assertIsInstance(facts["yml"]["versionName"], str)
        for value in facts["kinds"]["mobile.setting"].values():
            self.assertIsInstance(value["value"], str)
        self.assertEqual(facts["missing"], [])
        want = self.manifest_kinds(root / "AndroidManifest.xml")
        for kind, got in want.items():
            self.assertEqual(facts["kinds"][kind], got, kind)
        pkg = facts["kinds"]["mobile.package"]["com.example.snag"]
        self.assertEqual((pkg["versionName"], pkg["versionCode"], pkg["minSdk"], pkg["targetSdk"]),
                         (facts["yml"]["versionName"], facts["yml"]["versionCode"], facts["yml"]["minSdkVersion"],
                          facts["yml"]["targetSdkVersion"]))
        strings = ET.fromstring(files["res/values/strings.xml"])
        self.assertEqual(len(strings.findall("string")), facts["strings_count"])
        hist, endpoints = {}, {}
        for rel, data in files.items():
            if rel.endswith(".smali"):
                text = data.decode("utf-8")
                cls = re.search(r"^\.class [\w ]*L([\w/$]+);", text, re.M).group(1)
                pk = cls.rsplit("/", 1)[0]
                hist[pk] = hist.get(pk, 0) + 1
                for i, line in enumerate(text.split("\n"), 1):
                    m = re.search(r'const-string v\d+, "([^"\\\n]{0,2048})"', line)
                    if m and re.match(self.WHOLE, m.group(1)):
                        endpoints.setdefault(m.group(1), {"path": rel, "line": i})
        self.assertEqual(facts["kinds"]["mobile.sdk"], self.sdk_kinds(hist))
        self.assertEqual(facts["locations"], endpoints)
        self.assertEqual(set(facts["kinds"]["mobile.endpoint"]), set(endpoints))
        self.assertTrue(any(p.startswith("smali_classes2/") for p in files))
        text = "".join(v.decode("utf-8", "replace") for v in files.values())
        for v in facts["never_stored"]:
            self.assertIn(v, text)
        self.assertEqual(facts["kinds"]["secret.embedded"],
                         sec_kinds({k: dec(v) for k, v in files.items() if Path(k).suffix in (".smali", ".xml", ".yml")}))

    def test_apktool_variants(self):
        for variant in ("empty", "no_manifest", "binary_manifest", "no_smali", "doctype", "bad_yml", "unquoted",
                        "unsupported_manifest"):
            with self.subTest(variant):
                root, facts = F.make_apktool_dir(self.p(variant), variant)
                files = tree(root)
                self.assertIn("apktool.yml", files)
                if variant == "no_smali":
                    self.assertFalse(any(p.startswith("smali") for p in files))
                    self.assertEqual(facts["missing"], ["smali/"])
                if variant == "no_manifest":
                    self.assertNotIn("AndroidManifest.xml", files)
                    self.assertNotIn("mobile.package", facts["kinds"])           # apktool.yml alone gives no record
                    self.assertEqual(facts["missing"], ["AndroidManifest.xml"])
                if variant == "unsupported_manifest":
                    self.assertEqual(facts["skips"], {"AndroidManifest.xml": "unsupported"})
                    self.assertFalse(files["AndroidManifest.xml"].startswith((b"<?xml", b"<manifest")))
                    self.assertNotIn("mobile.package", facts["kinds"])
                if variant == "binary_manifest":
                    self.assertIn(b"\x00", files["AndroidManifest.xml"][:8192])
                    self.assertEqual(facts["skips"], {"AndroidManifest.xml": "binary"})
                if variant == "doctype":
                    self.assertIn(b"<!DOCTYPE", files["AndroidManifest.xml"])
                    self.assertEqual(facts["skips"], {"AndroidManifest.xml": "malformed"})
                    self.assertEqual(facts["notes"], ["DOCTYPE or ENTITY refused"])
                    self.assertEqual(facts["missing"], [])
                if variant == "unquoted":
                    self.assertNotIn(b"'", files["apktool.yml"])
                    self.assertIn(b"minSdkVersion: 24", files["apktool.yml"])
                if variant == "bad_yml":
                    self.assertNotIn(b"minSdkVersion", files["apktool.yml"])
                if variant == "empty":
                    self.assertEqual(list(files), ["apktool.yml"])
                    self.assertEqual(facts["missing"], ["AndroidManifest.xml", "smali/", "res/values/strings.xml"])
                    self.assertNotIn("apktool.yml", facts["missing"])
        with self.assertRaises(KeyError):
            F.make_apktool_dir(self.p("x"), "nope")


class TestVariants(Base):
    JSON_WHOLE = ("har", "sourcemap", "openapi_json")
    IMPORTS = {"capture": "bad-capture", "rea_bundle": "target-unsupported"}
    SCANNED = ("bundle", "openapi_yaml", "graphql", "proto")

    def build(self, kind, name):
        return F.make_variant(self.p("%s.%s" % (kind, name)), kind, name)

    def scan(self, kind, text):
        if kind == "bundle":
            k = scan_js(text)
            return {n: k[n] for n in JS_KINDS}
        if kind == "openapi_yaml":
            return {"data.endpoint": scan_yaml(text)}
        if kind == "graphql":
            return scan_sdl(text)
        return scan_proto(text)

    def good_kinds(self, kind):
        facts = F._good(kind, self.p("good." + kind))[1]
        return {k: v for k, v in facts["kinds"].items() if k != "secret.embedded"}

    def test_every_variant_builds_with_well_formed_facts(self):
        for kind, names in F.VARIANTS.items():
            for name in names:
                with self.subTest("%s/%s" % (kind, name)):
                    path, facts = self.build(kind, name)
                    self.assertTrue(Path(path).is_file())
                    self.assertEqual(facts["variant"], name)
                    self.assertNotIn("ambiguous", facts)
                    self.assertIsInstance(facts["kinds"], dict)
                    json.dumps(facts)
                    self.assertTrue("skip" in facts.get("expect", {}) or "error" in facts.get("expect", {}) or name in ("tolerant",))
                    size = Path(path).stat().st_size
                    if name == "too_large":
                        cap = F.CAPS.get(kind, CAP)                  # 5 MiB; a rea bundle: 64 MiB (6.2)
                        self.assertEqual(size, cap + 1)
                        self.assertEqual(facts["kinds"], {})
                        want = {"error": self.IMPORTS[kind]} if kind in self.IMPORTS else {"skip": ["too-large"]}
                        self.assertEqual(facts["expect"], want)
                    elif name == "oversized":
                        self.assertGreater(size, CAP)                # SQLite has no read cap
                    else:
                        self.assertLessEqual(size, CAP)
                    Path(path).unlink()

    def test_unknown_variant(self):
        with self.assertRaises(KeyError):
            F.make_variant(self.p("x"), "har", "nope")
        with self.assertRaises(KeyError):
            F.make_variant(self.p("x"), "sqlite", "too_large")
        self.assertEqual(F.CAPS, {"capture": CAP, "rea_bundle": 64 * 1024 * 1024})

    def test_zero_bytes_follow_the_62_table(self):
        want = {"har": ["malformed"], "sourcemap": ["malformed"], "openapi_json": ["malformed"], "bundle": [None],
                "graphql": [None], "proto": [None], "openapi_yaml": ["unsupported"], "sqlite": ["unsupported"],
                "plist": ["unsupported"]}
        for kind in F.VARIANTS:
            with self.subTest(kind):
                path, facts = self.build(kind, "zero_bytes")
                self.assertEqual(path.stat().st_size, 0)
                if kind in self.IMPORTS:
                    self.assertEqual(facts["expect"], {"error": self.IMPORTS[kind]})
                else:
                    self.assertEqual(facts["expect"], {"skip": want[kind]})
                self.assertEqual(facts["kinds"], {})

    def test_truncated_whole_parse_formats_are_malformed(self):
        for kind in self.JSON_WHOLE + tuple(self.IMPORTS):
            with self.subTest(kind):
                path, facts = self.build(kind, "truncated")
                with self.assertRaises(ValueError):
                    json.loads(path.read_bytes().decode("utf-8"))
                self.assertEqual(facts["expect"], {"error": self.IMPORTS[kind]} if kind in self.IMPORTS else {"skip": ["malformed"]})
        for name in ("truncated", "truncated_binary"):
            with self.subTest("plist " + name):
                path, facts = self.build("plist", name)
                with self.assertRaises((plistlib.InvalidFileException, ExpatError)):
                    plistlib.loads(path.read_bytes())
                self.assertEqual(facts["expect"], {"skip": ["malformed"]})
        path, facts = self.build("sqlite", "truncated")
        con = sqlite3.connect(str(path))
        self.addCleanup(con.close)
        with self.assertRaises(sqlite3.DatabaseError):
            con.execute("SELECT name FROM sqlite_master").fetchall()
        self.assertEqual(facts["expect"], {"skip": ["malformed"]})

    def test_truncated_scanned_kinds_keep_the_facts_of_the_prefix(self):
        for kind in self.SCANNED:
            with self.subTest(kind):
                path, facts = self.build(kind, "truncated")
                text = path.read_text(encoding="utf-8")
                got = self.scan(kind, text)
                for k, v in got.items():
                    self.assertEqual(facts["kinds"].get(k, {}), v, k)
                good = self.good_kinds(kind)
                for k, keys in facts["kinds"].items():
                    if k != "secret.embedded":
                        for key, data in keys.items():
                            self.assertEqual(good[k][key], data)          # a subset of the good facts
                        self.assertLess(len(keys), len(good[k]) + 1)
                self.assertTrue(any(len(facts["kinds"].get(k, {})) < len(good[k]) for k in good), "something is cut off")
                self.assertEqual(facts["expect"], {"skip": [None]})
                if kind in ("graphql", "proto"):
                    self.assertGreater(text.count("{"), text.count("}"))
                    self.assertIn("input ends inside an unclosed block", facts["limitations"])
                self.check_secrets(path, facts)

    def test_invalid_utf8_away_from_the_facts_is_partial(self):
        for kind in ("har", "sourcemap", "openapi_json", "plist", "bundle", "openapi_yaml", "graphql", "proto"):
            with self.subTest(kind):
                path, facts = self.build(kind, "invalid_utf8")
                data = path.read_bytes()
                with self.assertRaises(UnicodeDecodeError):
                    data.decode("utf-8")
                text = dec(data)
                self.assertIn("�", text)
                self.assertEqual(facts["coverage"], "partial")
                self.assertIn("not valid UTF-8; undecodable bytes replaced", facts["limitations"])
                self.assertEqual(facts["expect"], {"skip": [None]})
                self.check_secrets(path, facts)
                good = self.good_kinds(kind)
                got = {k: v for k, v in facts["kinds"].items() if k != "secret.embedded"}
                self.assertEqual(got, good)
                if kind in self.JSON_WHOLE:
                    json.loads(text)                                      # the damage is inside a string
                    if kind == "har":
                        for k, v in har_derive(json.loads(text)["log"]["entries"]).items():
                            self.assertEqual(got[k], v)
                elif kind == "plist":
                    self.assertEqual(plist_paths(plistlib.loads(text.encode("utf-8"))), got["data.plist-key"])
                else:
                    scanned = self.scan(kind, text)
                    for k, v in scanned.items():
                        self.assertEqual(got.get(k, {}), v, k)

    def test_invalid_utf8_inside_the_syntax_breaks_the_parse(self):
        for kind in self.JSON_WHOLE:
            with self.subTest(kind):
                path, facts = self.build(kind, "invalid_utf8_structure")
                with self.assertRaises(ValueError):
                    json.loads(dec(path.read_bytes()))
                self.assertEqual(facts["expect"], {"skip": ["malformed"]})
        path, facts = self.build("plist", "invalid_utf8_structure")
        with self.assertRaises((ExpatError, plistlib.InvalidFileException)):
            plistlib.loads(dec(path.read_bytes()).encode("utf-8"))
        for kind, err in self.IMPORTS.items():
            with self.subTest(kind):
                path, facts = self.build(kind, "invalid_utf8")
                with self.assertRaises(UnicodeDecodeError):
                    path.read_bytes().decode("utf-8")
                with self.assertRaises(ValueError):
                    json.loads(dec(path.read_bytes()))
                self.assertEqual(facts["expect"], {"error": err})

    def test_binary_needs_a_nul_in_the_first_8k(self):
        for kind in F.VARIANTS:
            with self.subTest(kind):
                path, facts = self.build(kind, "binary")
                data = path.read_bytes()
                self.assertIn(b"\x00", data[:8192])
                self.assertFalse(data.startswith(b"SQLite format 3") or data.startswith(b"bplist00"))
                if kind in self.IMPORTS:
                    self.assertEqual(facts["expect"], {"error": self.IMPORTS[kind]})
                elif kind in ("sqlite", "plist"):
                    self.assertEqual(facts["expect"], {"skip": ["unsupported"]})
                else:
                    self.assertEqual(facts["expect"], {"skip": ["binary"]})
                self.assertEqual(facts["kinds"], {})

    def test_minified_one_megabyte_line(self):
        for kind in ("har", "sourcemap", "openapi_json", "capture", "rea_bundle", "bundle", "openapi_yaml", "graphql", "proto", "plist"):
            with self.subTest(kind):
                path, facts = self.build(kind, "minified_1mb")
                data = path.read_bytes()
                self.assertGreaterEqual(max(len(l) for l in data.split(b"\n")), F.ONE_MB)
                self.assertLess(len(data), CAP)
                self.assertEqual(facts["expect"], {"skip": [None]})
                if kind in ("har", "sourcemap", "openapi_json", "capture", "rea_bundle"):
                    self.assertEqual(data.count(b"\n"), 1)
                    json.loads(data.decode("utf-8"))
                if kind == "plist":
                    self.assertEqual(plistlib.loads(data)["Padding"], "a" * F.ONE_MB)
                    self.assertEqual(plist_paths(plistlib.loads(data)), facts["kinds"]["data.plist-key"])
                if kind == "bundle":
                    self.assertEqual(data.count(b"\n"), 0)
                    self.assertTrue(data.endswith(b'path: "/end-of-line" };'))
                    got = scan_js(data.decode("utf-8"))
                    for k in JS_KINDS:
                        self.assertEqual(facts["kinds"][k], got[k], k)
                if kind == "har":
                    self.assertEqual(har_derive(json.loads(data)["log"]["entries"])["web.endpoint"], facts["kinds"]["web.endpoint"])
                if kind in ("openapi_yaml", "graphql", "proto", "bundle", "har", "sourcemap", "openapi_json", "plist"):
                    self.check_secrets(path, facts)

    def test_deep_json_is_deeper_than_the_parser_allows(self):
        for kind in self.JSON_WHOLE + tuple(self.IMPORTS):
            with self.subTest(kind):
                path, facts = self.build(kind, "deep")
                with self.assertRaises(RecursionError):
                    json.loads(path.read_bytes().decode("utf-8"))
                self.assertEqual(facts["expect"], {"error": self.IMPORTS[kind]} if kind in self.IMPORTS else {"skip": ["depth"]})

    def test_deep_and_huge_har_bodies(self):
        path, facts = self.build("har", "deep_body")
        entry = jload(path)["log"]["entries"][0]
        with self.assertRaises(RecursionError):
            json.loads(entry["response"]["content"]["text"])
        self.assertEqual(list(facts["kinds"]["web.endpoint"]), [har_key(entry["request"]["method"], entry["request"]["url"])])
        derived = har_derive([entry])                                  # the piece is dropped, the entry stays (6.2 rule 4)
        self.assertEqual(derived["web.endpoint"], facts["kinds"]["web.endpoint"])
        self.assertEqual(derived["web.payload-shape"], {})
        self.assertNotIn("web.payload-shape", facts["kinds"])
        lim = ["body nested deeper than the parser allows; not shaped"]
        self.assertEqual(facts["limitations"], lim)
        self.assertEqual(facts["record_limitations"], {"web.endpoint " + next(iter(facts["kinds"]["web.endpoint"])): lim})
        self.assertEqual(facts["expect"], {"skip": [None]})
        self.assertNotIn("ambiguous", facts)
        path, facts = self.build("har", "huge_body")
        entries = jload(path)["log"]["entries"]
        self.assertGreaterEqual(len(entries[0]["response"]["content"]["text"]), 3_000_000)
        derived = har_derive(entries)
        self.assertEqual(derived["web.payload-shape"], facts["kinds"]["web.payload-shape"])
        self.assertEqual(derived["web.endpoint"], facts["kinds"]["web.endpoint"])
        self.assertLess(path.stat().st_size, CAP)

    def test_bad_json_and_bad_base64_bodies_are_dropped_on_their_own(self):
        for name, limitation in (("bad_json_body", "body not parseable as JSON; not shaped"),
                                 ("bad_base64_body", "body not valid base64; not shaped")):
            with self.subTest(name):
                path, facts = self.build("har", name)
                entries = jload(path)["log"]["entries"]
                derived = har_derive(entries)
                self.assertEqual(derived["web.endpoint"], facts["kinds"]["web.endpoint"])
                self.assertEqual(derived["web.payload-shape"], facts["kinds"]["web.payload-shape"])
                self.assertEqual(list(facts["kinds"]["web.payload-shape"]), ["GET app.example.test/api/items response"])
                self.assertEqual(facts["limitations"], [limitation])
                self.assertEqual(facts["record_limitations"], {"web.endpoint GET app.example.test/api/broken": [limitation]})
                self.assertEqual(set(facts["never_stored"]), expected_never(har_leaks(entries)))
                self.check_secrets(path, facts)
        entries = jload(self.build("har", "bad_base64_body")[0])["log"]["entries"]
        body = entries[1]["response"]["content"]["text"]
        with self.assertRaises(ValueError):
            base64.b64decode(body, validate=True)
        with self.assertRaises(ValueError):                        # invalid in the lenient mode too
            base64.b64decode(body)
        with self.assertRaises(ValueError):
            json.loads(jload(self.build("har", "bad_json_body")[0])["log"]["entries"][1]["response"]["content"]["text"])

    def test_padding_is_real_spaces_written_in_chunks(self):
        path, _ = F.make_variant(self.p("t.json"), "openapi_json", "too_large")
        data = path.read_bytes()
        good = F.make_openapi_json(self.p("g.json"))[0].read_bytes()
        self.assertEqual(data[:len(good)], good)
        self.assertEqual(data[len(good):], b" " * (CAP + 1 - len(good)))

    def test_oversized_sqlite_is_read(self):
        path, facts = self.build("sqlite", "oversized")
        self.assertGreater(path.stat().st_size, CAP)
        self.assertEqual(F.sqlite_schema_facts(path), facts["kinds"])
        self.assertIn("padding", facts["kinds"]["data.table"])
        self.assertEqual(facts["expect"], {"skip": [None]})

    def test_pathological_fragment_keeps_its_shape(self):
        """The part that makes a lazy-dot pattern slow: `fetch("` followed by 300+ characters with no quote, repeated."""
        data = F.make_pathological_line(self.p("p.js"), 200000)[0].read_text(encoding="ascii")
        opens = data.count('fetch("')
        self.assertGreater(opens, 100)
        self.assertGreaterEqual(len(re.findall(r'fetch\("[^"]{300}', data)), opens - 1)
        self.assertGreater(len(re.findall(r"fetch\(\s{500}", data)), 100)         # whitespace runs after `fetch(`
        self.assertIn('"' * 100, data)
        self.assertNotIn("\n", data)

    def test_bad_har_entries(self):
        path, facts = self.build("har", "bad_entries")
        entries = jload(path)["log"]["entries"]
        self.assertEqual(len(entries), facts["entry_count"])
        valid = [e for e in entries if isinstance(e, dict) and isinstance(e.get("request"), dict) and "url" in e["request"]
                 and isinstance(e.get("response"), dict) and "content" in e["response"]]
        self.assertEqual(len(valid), facts["valid_entries"])
        derived = har_derive(valid)
        self.assertEqual(derived["web.endpoint"], facts["kinds"]["web.endpoint"])
        self.assertEqual(derived["web.payload-shape"], facts["kinds"]["web.payload-shape"])
        self.assertTrue(any(not isinstance(e, dict) for e in entries))
        self.assertEqual(set(facts["never_stored"]), expected_never(har_leaks(valid)))
        self.check_secrets(path, facts)

    def test_sourcemap_variants(self):
        path, facts = self.build("sourcemap", "outside_root")
        doc = jload(path)
        self.assertTrue(any(s.startswith("../") for s in doc["sources"]))
        self.assertTrue(any(s.startswith("/") for s in doc["sources"]))
        self.assertTrue(any(re.match(r"[A-Za-z]:\\", s) for s in doc["sources"]))
        self.assertEqual(doc["sourceRoot"], "../../..")
        self.assertEqual(facts["unsafe_sources"], doc["sources"][:3])
        self.assertEqual(facts["kinds"]["web.source-module"], {"(outside)": {"sources": 3}, "src": {"sources": 1}})
        for s in facts["unsafe_sources"]:                     # never in a stored key: the key says `(outside)`
            self.assertNotIn(s, json.dumps(facts["kinds"]))
            self.assertIn(s, facts["never_stored"])
        doc = jload(self.build("sourcemap", "bad_version")[0])
        self.assertNotEqual(doc["version"], 3)
        self.assertNotIsInstance(doc["sources"], list)

    def test_plist_variants(self):
        path, facts = self.build("plist", "deep")
        value = plistlib.loads(path.read_bytes())
        depth = 0
        while isinstance(value, dict) and "leaf" not in value:
            value = next(iter(value.values()))
            depth += 1
        self.assertEqual(depth, 8)
        self.assertEqual(plist_paths(plistlib.loads(path.read_bytes())), facts["kinds"]["data.plist-key"])
        self.assertEqual(max(k.count(".") + 1 for k in facts["kinds"]["data.plist-key"]), facts["max_depth"])
        self.assertEqual(facts["limitations"], ["nested deeper than 6 levels; deeper levels not read"])
        path, facts = self.build("plist", "many_keys")
        value = plistlib.loads(path.read_bytes())
        self.assertEqual(len(value), facts["total_keys"])
        self.assertEqual(plist_paths(value), facts["kinds"]["data.plist-key"])
        self.assertEqual(len(facts["kinds"]["data.plist-key"]), facts["max_keys"])
        self.assertEqual(facts["limitations"], ["more than 500 plist key paths; 100 not recorded"])

    def test_sqlite_variants(self):
        path, facts = self.build("sqlite", "empty")
        con = sqlite3.connect(str(path))
        self.addCleanup(con.close)
        self.assertEqual(con.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 0)
        self.assertTrue(path.read_bytes().startswith(b"SQLite format 3\x00"))
        path, facts = self.build("sqlite", "header_only")
        self.assertTrue(path.read_bytes().startswith(b"SQLite format 3\x00"))
        con2 = sqlite3.connect(str(path))
        self.addCleanup(con2.close)
        with self.assertRaises(sqlite3.DatabaseError):
            con2.execute("SELECT name FROM sqlite_master").fetchall()

    def test_empty_variants_hold_no_facts_and_parse(self):
        for kind in F.VARIANTS:
            with self.subTest(kind):
                path, facts = self.build(kind, "empty")
                if kind in ("capture",):
                    self.assertEqual(facts["kinds"], {"flow.step": {}, "flow.screen": {}, "web.endpoint": {}, "web.payload-shape": {}})
                else:
                    self.assertEqual(facts["kinds"], {})
                if kind in ("har", "sourcemap", "openapi_json", "capture", "rea_bundle"):
                    jload(path)
                if kind == "plist":
                    self.assertEqual(plistlib.loads(path.read_bytes()), {})
                if kind == "sqlite":
                    self.assertEqual(F.sqlite_schema_facts(path)["data.table"], {})

    def test_pathological_and_too_large_text(self):
        path, facts = F.make_pathological_line(self.p("p.js"))
        data = path.read_bytes()
        self.assertEqual(len(data), CAP)
        self.assertNotIn(b"\n", data)
        data.decode("ascii")
        self.assertEqual(F.make_pathological_line(self.p("q.js"), 1234)[0].stat().st_size, 1234)
        path, facts = F.make_too_large_text(self.p("t.txt"))
        self.assertEqual(path.stat().st_size, CAP + 1)
        path, facts = self.build("bundle", "pathological_1mb")
        self.assertEqual(path.stat().st_size, F.ONE_MB)
        self.assertEqual(facts["variant"], "pathological_1mb")

    def test_variant_names_are_listed_for_every_kind(self):
        self.assertEqual(set(F.VARIANTS), {"har", "sourcemap", "bundle", "sqlite", "plist", "openapi_json", "openapi_yaml",
                                           "graphql", "proto", "capture", "rea_bundle"})
        for kind, names in F.VARIANTS.items():
            self.assertIn("empty", names)
            self.assertEqual(len(names), len(set(names)))
            for must in ("zero_bytes", "truncated", "binary"):
                self.assertIn(must, names, kind)
            if kind != "sqlite":
                self.assertTrue(any(n.startswith("invalid_utf8") for n in names), kind)


class TestCompleteness(Base):
    """M8: an unlisted fact added to a file makes the independent readers disagree with the expectations."""

    def test_extra_flag_in_the_bundle_is_noticed(self):
        js, facts = F.make_bundle()
        self.assertEqual(scan_js(js)["web.flag"], facts["kinds"]["web.flag"])
        self.assertNotEqual(scan_js(js + ' useFlag("ghost-flag");')["web.flag"], facts["kinds"]["web.flag"])

    def test_extra_i18n_key_in_sourcescontent_is_noticed(self):
        facts = F.make_sourcemap(self.p("a.map"))[1]
        content = F.default_map_contents()[0]
        want = facts["kinds"]["web.i18n-key"]
        self.assertEqual(scan_js(content)["web.i18n-key"], want)
        self.assertNotEqual(scan_js(content + 't("ghost.key");')["web.i18n-key"], want)

    def test_removing_the_yaml_secret_is_noticed(self):
        path, facts = F.make_openapi_yaml(self.p("a.yaml"))
        text = dec(path.read_bytes())
        self.assertEqual(sec_kinds({".": text}), facts["kinds"]["secret.embedded"])
        without = "\n".join(l for l in text.split("\n") if "x-secret-example" not in l)
        self.assertNotEqual(sec_kinds({".": without}), facts["kinds"]["secret.embedded"])

    def test_dropping_a_value_from_never_stored_is_noticed(self):
        path, facts = F.make_har(self.p("a.har"))
        entries = jload(path)["log"]["entries"]
        self.assertEqual(expected_never(har_leaks(entries)), set(facts["never_stored"]))
        short = [v for v in facts["never_stored"] if F.SECRETS["google-api-key"] != v]
        self.assertNotEqual(expected_never(har_leaks(entries)), set(short))
        without_pem = [v for v in facts["never_stored"] if v != F.PEM_BODY]
        self.assertNotEqual(expected_never(har_leaks(entries)), set(without_pem))
        self.assertIn(F.SECRETS["google-api-key"], har_leaks(entries))     # the query value

    def test_other_unlisted_facts_are_noticed(self):
        path, facts = F.make_har(self.p("a.har"))
        entries = jload(path)["log"]["entries"]
        entries.append(F.har_entry("GET", "https://app.example.test/api/ghost", 200, t=99))
        self.assertNotEqual(har_derive(entries)["web.endpoint"], facts["kinds"]["web.endpoint"])
        sdl = F.GRAPHQL_SDL + "type Ghost {\n  id: ID\n}\n"
        self.assertNotEqual(scan_sdl(sdl)["data.schema-type"], F.GRAPHQL_KINDS["data.schema-type"])
        self.assertNotEqual(scan_proto(F.PROTO_TEXT.replace("service HealthService {", "service HealthService {\n  rpc Ghost(Item) returns (Item);"))["data.rpc"],
                            F.PROTO_KINDS["data.rpc"])
        value = dict(F.DEFAULT_PLIST, Ghost="x")
        self.assertNotEqual(plist_paths(value), F.PLIST_KINDS["data.plist-key"])
        extra = F.default_openapi_doc()
        extra["paths"]["/ghost"] = {"get": F._op("ghost", [])}
        self.assertNotIn("GET /ghost", F.OPENAPI_KINDS["data.endpoint"])
        self.assertIn("GET /ghost", TestOpenApi("derive").derive(extra)["data.endpoint"])
        con = sqlite3.connect(":memory:")
        for stmt in F.DEFAULT_DDL[:-3]:
            con.execute(stmt)
        con.execute("CREATE TABLE ghost (a)")
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        self.assertNotEqual(names, set(F.SQLITE_KINDS["data.table"]))
        con.close()


class TestSizesAndPrivacy(Base):
    def test_no_normal_fixture_exceeds_the_text_cap(self):
        biggest = 0
        for i, make in enumerate((lambda d: F.make_har(d / "a.har"), lambda d: F.make_repo(d / "r"), lambda d: F.make_site(d / "s"),
                                  lambda d: F.make_jadx_dir(d / "j"), lambda d: F.make_apktool_dir(d / "a"),
                                  lambda d: F.make_version_pair(d / "p", "har"))):
            d = self.p("x%d" % i)
            make(d)
            for data in tree(d).values():
                biggest = max(biggest, len(data))
        self.assertLess(biggest, 200_000)

    def test_no_fixture_holds_a_real_looking_home_path_or_email(self):
        d = self.tmp
        F.make_har(d / "a.har")
        F.make_repo(d / "r")
        F.make_jadx_dir(d / "j")
        F.make_apktool_dir(d / "a")
        F.make_capture(d / "c.json")
        F.make_rea_bundle(d / "rea.json")
        for rel, data in tree(d).items():
            if rel.endswith(".png"):
                continue
            text = data.decode("utf-8", "replace")
            with self.subTest(rel):
                self.assertNotRegex(text, r"(?i)[A-Za-z]:[\\/]+Users[\\/]")
                self.assertNotRegex(text, r"/(?:Users|home)/(?!you\b|user\b)\w+")
                self.assertNotRegex(text, r"[\w.+-]+@[\w-]+\.(?:com|org|net|io)\b")

    def test_hosts_are_reserved_test_names(self):
        d = self.tmp
        F.make_har(d / "a.har")
        F.make_openapi_json(d / "o.json")
        F.make_capture(d / "c.json")
        for rel, data in tree(d).items():
            for host in re.findall(r"https?://(?:[^@/\s\"']+@)?([A-Za-z0-9.-]+)", data.decode("utf-8")):
                with self.subTest((rel, host)):
                    self.assertTrue(host.lower().endswith(".test"), host)


if __name__ == "__main__":
    unittest.main()
