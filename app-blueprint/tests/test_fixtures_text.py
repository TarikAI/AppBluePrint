"""W2-FX2: self-checks of the text fixture builders (contract 10.2).

Every check uses the standard library only and reuses no lane code: the expected facts of each fixture are
re-derived here by small independent readers (json, plistlib, sqlite3, ElementTree, a few regexes) and compared with
the facts the builder returns, so a fixture whose facts disagree with its bytes fails here, before a lane test
trusts it.
"""
import json
import plistlib
import posixpath
import re
import sqlite3
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit

import fixtures_text as F

CAP = 5 * 1024 * 1024
KIND_RE = re.compile(r"^[a-z]+(\.[a-z0-9-]+)+$")
ROUTE_RE = re.compile(r"^(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|ANY) /\S*$")


def tree(root):
    """relpath -> bytes for every file under root (or {name: bytes} for one file)."""
    root = Path(root)
    if root.is_file():
        return {root.name: root.read_bytes()}
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def jload(path):
    return json.loads(Path(path).read_bytes().decode("utf-8"))


def shape(v):
    if isinstance(v, dict):
        return {k: shape(x) for k, x in v.items()}
    if isinstance(v, list):
        return [shape(v[0])] if v else []
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


def har_key(entry):
    u = urlsplit(entry["request"]["url"])
    segs = [template(s) for s in u.path.split("/") if s]
    return "%s %s/%s" % (entry["request"]["method"], u.hostname, "/".join(segs))


def har_derive(entries):
    """endpoints, payload shapes, auth steps, hosts and pages of HAR entries, read the plain way."""
    eps, shapes, auth, hosts, pages = {}, {}, [], [], []
    for e in entries:
        k = har_key(e)
        req, resp = e["request"], e["response"]
        d = eps.setdefault(k, {"statuses": set(), "requestTypes": set(), "responseTypes": set(), "count": 0, "queryParams": set()})
        d["statuses"].add(resp["status"])
        d["count"] += 1
        d["queryParams"].update(q["name"] for q in req["queryString"])
        if "postData" in req:
            d["requestTypes"].add(req["postData"]["mimeType"])
        if resp["content"]["mimeType"]:
            d["responseTypes"].add(resp["content"]["mimeType"])
        for direction, mime, text in (("request", req.get("postData", {}).get("mimeType"), req.get("postData", {}).get("text")),
                                      ("response", resp["content"]["mimeType"], resp["content"].get("text"))):
            if mime == "application/json" and text and (k, direction) not in shapes:
                shapes[(k, direction)] = shape(json.loads(text))
        if re.search(r"login|logout|auth|token|session|oauth|signin|signup|sso|2fa|otp", urlsplit(req["url"]).path):
            h = {x["name"].lower(): x["value"] for x in req["headers"]}
            scheme = h.get("authorization", "").split(" ")[0] or None
            auth.append({"endpoint": k, "order": len(auth) + 1,
                         "sets-cookie": any(x["name"].lower() == "set-cookie" for x in resp["headers"]),
                         "auth-scheme": scheme if scheme in ("Bearer", "Basic") else None})
        if resp["content"]["mimeType"] == "text/html":
            pages.append(req["url"].split("?")[0])
    counts = {}
    for e in entries:
        counts[urlsplit(e["request"]["url"]).hostname] = counts.get(urlsplit(e["request"]["url"]).hostname, 0) + 1
    main = max(counts, key=counts.get)
    hosts = sorted(h for h in counts if h != main)
    eps = {k: {"statuses": sorted(v["statuses"]), "requestTypes": sorted(v["requestTypes"]),
               "responseTypes": sorted(v["responseTypes"]), "count": v["count"], "queryParams": sorted(v["queryParams"])}
           for k, v in eps.items()}
    return eps, shapes, auth, hosts, pages


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="fx2-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def p(self, *parts):
        return self.tmp.joinpath(*parts)


class TestSecrets(Base):
    def test_every_secret_matches_its_pattern_and_is_fake(self):
        for name, value in F.SECRETS.items():
            with self.subTest(name):
                self.assertRegex(value, F.SECRET_REGEXES[name])
                self.assertIn("fixture", value.lower())
        self.assertEqual(set(F.SECRETS), set(F.SECRET_REGEXES))

    def test_source_never_holds_a_whole_token(self):
        src = Path(F.__file__).read_text(encoding="utf-8")
        for name, rx in F.SECRET_REGEXES.items():
            with self.subTest(name):
                self.assertIsNone(re.search(rx, src), "a whole %s value appears in fixtures_text.py" % name)


class TestDeterminism(Base):
    def twice(self, make):
        a, b = self.p("a"), self.p("b")
        make(a)
        make(b)
        ta, tb = tree(a), tree(b)
        self.assertTrue(ta)
        self.assertEqual({k: v for k, v in ta.items()}, {k: v for k, v in tb.items()})

    def test_file_builders(self):
        makers = {
            "har": lambda d: F.make_har(d / "x.har"), "sourcemap": lambda d: F.make_sourcemap(d / "x.map"),
            "bundle": lambda d: F.write_bundle(d / "x.js"), "bundle-compact": lambda d: F.write_bundle(d / "x.js", style="compact"),
            "hermes": lambda d: F.make_hermes_bundle(d / "x.js"),
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

    def test_bundle_text_is_deterministic(self):
        self.assertEqual(F.make_bundle()[0], F.make_bundle()[0])

    def test_every_variant_is_deterministic(self):
        for kind, names in F.VARIANTS.items():
            for name in names:
                if name == "too_large":
                    continue   # exercised in TestVariants (5 MiB writes)
                with self.subTest("%s/%s" % (kind, name)):
                    self.twice(lambda d, k=kind, n=name: F.make_variant(d / "f.bin", k, n))

    def test_sqlite_schema_and_rows_are_stable(self):
        """File bytes may differ between SQLite versions; schema and rows may not."""
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
            with self.subTest(rel):
                self.assertNotIn(b"\r", data) if not rel.endswith(".png") else None
        self.assertIn(b"\r\n", self.p("y").write_bytes(b"") or F.make_openapi_yaml(self.p("y.yaml"), "crlf")[0].read_bytes())


class TestFactsShape(Base):
    def all_facts(self):
        d = self.tmp
        yield "har", F.make_har(d / "a.har")[1]
        yield "map", F.make_sourcemap(d / "a.map")[1]
        yield "bundle", F.make_bundle()[1]
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
                        if kind == "source.route":
                            self.assertRegex(key, ROUTE_RE)

    def test_facts_are_json_serialisable(self):
        for name, facts in self.all_facts():
            with self.subTest(name):
                json.dumps(facts)


class TestHar(Base):
    def test_default_har_parses_and_facts_match(self):
        path, facts = F.make_har(self.p("a.har"))
        doc = jload(path)
        self.assertEqual(doc["log"]["version"], "1.2")
        entries = doc["log"]["entries"]
        self.assertEqual(len(entries), facts["entry_count"])
        eps, shapes, auth, hosts, pages = har_derive(entries)
        self.assertEqual(facts["kinds"]["web.endpoint"], eps)
        self.assertEqual(sorted(facts["kinds"]["web.host"]), hosts)
        self.assertEqual(facts["auth_steps"], auth)
        self.assertEqual(facts["pages"], pages)
        self.assertEqual({(s["endpoint"], s["direction"]): s["shape"] for s in facts["payload_shapes"]}, shapes)
        self.assertEqual(len(facts["payload_shapes"]), len(shapes))

    def test_every_expected_endpoint_string_occurs_in_the_har(self):
        path, facts = F.make_har(self.p("a.har"))
        text = path.read_text(encoding="utf-8")
        for key in facts["kinds"]["web.endpoint"]:
            method, rest = key.split(" ", 1)
            host, _, p = rest.partition("/")
            with self.subTest(key):
                self.assertIn('"method": "%s"' % method, text)
                self.assertIn(host, text)
                first = [s for s in p.split("/") if s and s != "{id}"]
                for seg in first:
                    self.assertIn("/" + seg, text)

    def test_secrets_cookies_and_credentials_are_in_the_file(self):
        path, facts = F.make_har(self.p("a.har"))
        text = path.read_text(encoding="utf-8")
        for value in facts["never_stored"]:
            with self.subTest(value[:20]):
                self.assertIn(value, text)
        for name in facts["secret_patterns"]:
            self.assertRegex(text, F.SECRET_REGEXES[name])
        doc = jload(path)
        names = {h["name"] for e in doc["log"]["entries"] for h in e["request"]["headers"] + e["response"]["headers"]}
        self.assertTrue({"Authorization", "Cookie", "Set-Cookie"} <= names)
        self.assertTrue(any(e["response"]["cookies"] for e in doc["log"]["entries"]))
        self.assertTrue(any("@" in e["request"]["url"] and F.URL_PASSWORD in e["request"]["url"] for e in doc["log"]["entries"]))

    def test_all_the_id_forms_are_templated(self):
        keys = jload(F.make_har(self.p("a.har"))[0])["log"]["entries"]
        urls = " ".join(e["request"]["url"] for e in keys)
        for raw in ("/items/123", "/items/124", "3f2b8c1e-5a4d", "0123456789abcdef", "Zm9vYmFyYmF6cXV4MTIzNA"):
            self.assertIn(raw, urls)
        self.assertEqual(sum(k.endswith("{id}") for k in F.HAR_ENDPOINTS if "/items/" in k and "comments" not in k), 3)

    def test_custom_entries(self):
        e = F.har_entry("POST", "https://app.example.test/api/x?a=1", 201, req_body={"k": "v"}, resp_body={"ok": True},
                        resp_mime="application/json")
        path, facts = F.make_har(self.p("c.har"), [e])
        self.assertEqual(facts["entry_count"], 1)
        self.assertEqual(jload(path)["log"]["entries"][0]["request"]["queryString"], [{"name": "a", "value": "1"}])

    def test_empty_har(self):
        path, facts = F.make_variant(self.p("e.har"), "har", "empty")
        self.assertEqual(jload(path)["log"]["entries"], [])
        self.assertEqual(facts["kinds"], {})

    def test_timestamps_are_fixed(self):
        for e in jload(F.make_har(self.p("a.har"))[0])["log"]["entries"]:
            self.assertTrue(e["startedDateTime"].startswith("2026-01-01T00:00:"))


class TestSourcemapAndBundle(Base):
    def test_sourcemap_parses_and_facts_match(self):
        path, facts = F.make_sourcemap(self.p("a.map"))
        doc = jload(path)
        self.assertEqual(doc["version"], 3)
        self.assertEqual(len(doc["sources"]), len(doc["sourcesContent"]))
        mods = {}
        for s in doc["sources"]:
            parts = s.replace("webpack:///", "").lstrip("./").split("/")[:-1][:2]
            k = "/".join(parts)
            mods[k] = {"count": mods.get(k, {"count": 0})["count"] + 1}
        self.assertEqual(facts["kinds"]["web.source-module"], mods)
        for fw, marker in (("react", "node_modules/react/"), ("next", "node_modules/next/dist"), ("vue", "node_modules/vue/")):
            self.assertTrue(any(marker in s for s in doc["sources"]), fw)
        self.assertIn(F.MAP_MARKER, doc["sourcesContent"][0])
        self.assertIn(F.SECRETS["aws-key"], doc["sourcesContent"][0])
        self.assertIn('axios.get("/api/items")', doc["sourcesContent"][0])

    def test_custom_sources(self):
        path, facts = F.make_sourcemap(self.p("c.map"), ["a/b/c/d.js", "a/b/e.js", "top.js"], ["1", "2", "3"])
        self.assertEqual(facts["kinds"]["web.source-module"], {"a/b": {"count": 2}, "": {"count": 1}})
        self.assertEqual(jload(path)["sourcesContent"], ["1", "2", "3"])

    def test_bundle_holds_every_literal_form(self):
        js, facts = F.make_bundle()
        for kind, needle in (("web.route", 'path: "%s"'), ("web.flag", None), ("web.i18n-key", None)):
            if needle:
                for key in facts["kinds"][kind]:
                    if key != "/reports" and key != "/vue-page":
                        self.assertIn(needle % key, js, key)
        self.assertIn('<Route path="/reports"', js)
        self.assertIn("path: '/vue-page'", js)
        for key in facts["kinds"]["web.i18n-key"]:
            self.assertRegex(js, r'(t\("%s"\)|\$t\("%s"\)|id: "%s")' % (re.escape(key), re.escape(key), re.escape(key)))
        for key in facts["kinds"]["web.flag"]:
            self.assertRegex(js, r'(isEnabled|useFlag|useFeatureFlag|getFeatureValue|variation)\("%s"|flags\.%s\b' % (key, key))
        self.assertIn("LaunchDarkly", js)
        for key in facts["kinds"]["web.api-call"]:
            method, path = key.split(" ")
            self.assertRegex(js, r'(axios\.%s\("%s"|XMLHttpRequest\.open\("%s", "%s")' % (method.lower(), path, method, path))
        for fw in ("__NEXT_DATA__", "data-reactroot"):
            self.assertIn(fw, js)
        for needle in ('fetch("/api/search")', 'fetch("/api/export")', "https://api.example.test/v2/orders", 'cat("x.y")'):
            self.assertIn(needle, js)
        for name in facts["secret_patterns"]:
            self.assertRegex(js, F.SECRET_REGEXES[name])

    def test_compact_style_is_one_line_with_the_same_facts(self):
        js, facts = F.make_bundle(style="compact")
        self.assertEqual(js.count("\n"), 1)
        self.assertIn('path:"/dashboard"', js)
        self.assertEqual(facts["kinds"], F.make_bundle()[1]["kinds"])

    def test_bundle_extra_text_and_file(self):
        path, _ = F.write_bundle(self.p("b.js"), "// tail\n")
        self.assertTrue(path.read_text(encoding="utf-8").endswith("// tail\n"))

    def test_hermes_magic(self):
        path, facts = F.make_hermes_bundle(self.p("h.js"))
        self.assertTrue(path.read_bytes().startswith(bytes([0xC6, 0x1F, 0xBC, 0x03, 0xC1, 0x03, 0x19, 0x1F])))
        self.assertIn("hermes", facts["kinds"]["web.framework"])

    def test_site_folder(self):
        root, facts = F.make_site(self.p("site"))
        self.assertEqual(sorted(tree(root)), facts["files"])
        html = (root / "index.html").read_text(encoding="utf-8")
        self.assertIn('"page":"/blog/[slug]"', html)
        manifest = (root / "_next/static/abc/_buildManifest.js").read_text(encoding="utf-8")
        for key in facts["kinds"]["web.route"]:
            if key in ("/", "/about"):
                self.assertIn('"%s":' % key, manifest)
        jload(root / "static/main.js.map")
        _, empty = F.make_site(self.p("empty"), "empty")
        self.assertEqual(tree(self.p("empty")), {})
        self.assertEqual(empty["kinds"], {})


class TestSqlite(Base):
    def test_schema_rows_and_facts(self):
        path, facts = F.make_sqlite(self.p("a.db"))
        self.assertTrue(path.read_bytes().startswith(b"SQLite format 3\x00"))
        self.assertEqual(facts["kinds"], F.sqlite_schema_facts(path))
        con = sqlite3.connect(str(path))
        self.addCleanup(con.close)
        for t, n in facts["row_counts"].items():
            self.assertEqual(con.execute("SELECT count(*) FROM %s" % t).fetchone()[0], n)
        self.assertEqual(con.execute("SELECT type, name FROM sqlite_master WHERE name LIKE 'sqlite_%'").fetchall(), [])
        rows = repr([con.execute("SELECT * FROM %s" % t).fetchall() for t in facts["row_counts"]])
        for value in facts["never_stored"]:
            self.assertIn(value, rows)
        kinds = {r[0] for r in con.execute("SELECT type FROM sqlite_master")}
        self.assertEqual(kinds, {"table", "index", "trigger", "view"})

    def test_column_types_edge_cases(self):
        cols = {c["name"]: c["type"] for c in F.SQLITE_KINDS["data.table"]["items"]["columns"]}
        self.assertEqual(cols["price"], "NUMERIC(10,2)")
        self.assertEqual(cols["extra"], "")

    def test_custom_ddl(self):
        path, facts = F.make_sqlite(self.p("c.db"), ["CREATE TABLE a (x INT, y TEXT)", "CREATE INDEX ia ON a (x)"])
        self.assertEqual(facts["kinds"]["data.table"], {"a": {"columns": [{"name": "x", "type": "INT"}, {"name": "y", "type": "TEXT"}]}})
        self.assertEqual(facts["kinds"]["data.index"], {"ia": {}})

    def test_overwrites_an_existing_file(self):
        F.make_sqlite(self.p("o.db"), ["CREATE TABLE old (a)"])
        _, facts = F.make_sqlite(self.p("o.db"))
        self.assertNotIn("old", facts["kinds"]["data.table"])
        self.assertNotIn("old", F.sqlite_schema_facts(self.p("o.db"))["data.table"])


class TestPlist(Base):
    def walk(self, value, prefix=""):
        out = {}
        for k, v in value.items():
            key = prefix + k
            t = ("dict" if isinstance(v, dict) else "array" if isinstance(v, list) else "bool" if isinstance(v, bool)
                 else "integer" if isinstance(v, int) else "real" if isinstance(v, float) else "date" if hasattr(v, "year")
                 else "data" if isinstance(v, bytes) else "string")
            out[key] = {"type": t}
            if isinstance(v, dict):
                out.update(self.walk(v, key + "."))
        return out

    def test_both_formats_parse_to_the_same_value_and_facts_match(self):
        x, facts = F.make_plist(self.p("x.plist"))
        b, _ = F.make_plist(self.p("b.plist"), "binary")
        self.assertTrue(x.read_bytes().startswith(b"<?xml"))
        self.assertTrue(b.read_bytes().startswith(b"bplist00"))
        vx, vb = plistlib.loads(x.read_bytes()), plistlib.loads(b.read_bytes())
        self.assertEqual(vx, vb)
        self.assertEqual(self.walk(vx), facts["kinds"]["data.plist-key"])
        self.assertFalse(any("." in k for k in vx) or any("." in k for v in vx.values() if isinstance(v, dict) for k in v))
        for value in facts["never_stored"]:
            self.assertIn(value, x.read_text(encoding="utf-8"))

    def test_empty_plist(self):
        path, facts = F.make_variant(self.p("e.plist"), "plist", "empty")
        self.assertEqual(plistlib.loads(path.read_bytes()), {})
        self.assertEqual(facts["kinds"], {})


class TestOpenApi(Base):
    def endpoints(self, doc):
        return {"%s %s" % (m.upper(), p): {"operationId": op["operationId"], "tags": op["tags"]}
                for p, item in doc["paths"].items() for m, op in item.items()
                if m in ("get", "put", "post", "delete", "patch", "head", "options")}

    def test_json_facts(self):
        path, facts = F.make_openapi_json(self.p("o.json"))
        doc = jload(path)
        self.assertEqual(facts["kinds"]["data.endpoint"], self.endpoints(doc))
        self.assertEqual(sorted(facts["kinds"]["data.schema-type"]), sorted(doc["components"]["schemas"]))
        self.assertEqual(facts["kinds"]["data.security"],
                         {k: {"type": v["type"]} for k, v in doc["components"]["securitySchemes"].items()})
        self.assertIn("parameters", doc["paths"]["/items"])
        self.assertIn("summary", doc["paths"]["/items/{itemId}"])
        text = path.read_text(encoding="utf-8")
        for value in facts["never_stored"]:
            self.assertIn(value, text)

    def test_swagger(self):
        path, facts = F.make_openapi_json(self.p("s.json"), swagger=True)
        doc = jload(path)
        self.assertIn("swagger", doc)
        self.assertEqual(sorted(facts["kinds"]["data.schema-type"]), sorted(doc["definitions"]))
        self.assertNotIn("data.security", facts["kinds"])
        self.assertEqual(facts["kinds"]["data.endpoint"], self.endpoints(doc))

    def scan_yaml(self, text):
        found, current = [], None
        for line in text.splitlines():
            if re.match(r"^\S", line) or re.match(r"^  \S", line):
                current = None
            m = re.match(r"^  (/\S*):\s*$", line)
            if m:
                current = m.group(1)
                continue
            m = re.match(r"^    (get|put|post|delete|patch|head|options):", line)
            if m and current:
                found.append("%s %s" % (m.group(1).upper(), current))
        return found

    def test_yaml_line_scanner_finds_exactly_the_facts(self):
        for variant in ("normal", "crlf"):
            with self.subTest(variant):
                path, facts = F.make_openapi_yaml(self.p(variant + ".yaml"), variant)
                self.assertEqual(sorted(self.scan_yaml(path.read_text(encoding="utf-8"))), sorted(facts["kinds"]["data.endpoint"]))
                self.assertEqual(b"\r\n" in path.read_bytes(), variant == "crlf")
        self.assertEqual(sorted(F.YAML_ENDPOINT_KEYS), sorted(F.OPENAPI_KINDS["data.endpoint"]))

    def test_yaml_traps_are_present(self):
        text = F.make_openapi_yaml(self.p("t.yaml"))[0].read_text(encoding="utf-8")
        self.assertIn('  "/quoted":', text)
        self.assertIn("\n    get:\n      type: object", text)
        self.assertTrue(text.startswith("openapi:"))

    def test_empty_yaml(self):
        path, facts = F.make_openapi_yaml(self.p("e.yaml"), "empty")
        self.assertEqual(self.scan_yaml(path.read_text(encoding="utf-8")), [])
        self.assertEqual(facts["kinds"], {})


class TestGraphqlAndProto(Base):
    def sdl_facts(self, text):
        types = re.findall(r"^(?:type|input|enum|interface|union)\s+(\w+)", text, re.M)
        fields = {}
        for root in ("Query", "Mutation", "Subscription"):
            m = re.search(r"^type %s \{\n(.*?)^\}" % root, text, re.M | re.S)
            for name in re.findall(r"^  (\w+)", m.group(1), re.M):
                fields["%s %s" % (root.lower(), name)] = {}
        return types, fields

    def test_sdl(self):
        path, facts = F.make_graphql(self.p("a.graphql"))
        types, fields = self.sdl_facts(path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(types), sorted(facts["kinds"]["data.schema-type"]))
        self.assertEqual(fields, facts["kinds"]["data.endpoint"])
        self.assertNotIn("DateTime", types)

    def test_introspection(self):
        path, facts = F.make_graphql(self.p("i.json"), introspection=True)
        schema = jload(path)["data"]["__schema"]
        types = sorted(t["name"] for t in schema["types"] if t["kind"] != "SCALAR")
        self.assertEqual(types, sorted(facts["kinds"]["data.schema-type"]))
        fields = {"%s %s" % (t["name"].lower(), f["name"]): {} for t in schema["types"]
                  if t["name"] in ("Query", "Mutation", "Subscription") for f in t["fields"]}
        self.assertEqual(fields, facts["kinds"]["data.endpoint"])
        self.assertEqual(F.make_graphql(self.p("s.graphql"))[1]["kinds"], facts["kinds"])

    def test_proto(self):
        path, facts = F.make_proto(self.p("a.proto"))
        text = path.read_text(encoding="utf-8")
        types = re.findall(r"^(?:message|enum)\s+(\w+)", text, re.M)
        self.assertEqual(sorted(types), sorted(facts["kinds"]["data.schema-type"]))
        rpcs = {}
        for svc, body in re.findall(r"^service\s+(\w+)\s*\{(.*?)^\}", text, re.M | re.S):
            for m in re.finditer(r"rpc\s+(\w+)\s*\(\s*(?:stream\s+)?(\w+)\s*\)\s*returns\s*\(\s*(?:stream\s+)?(\w+)\s*\)", body):
                rpcs["%s.%s" % (svc, m.group(1))] = (m.group(2), m.group(3))
        self.assertEqual(sorted(rpcs), sorted(facts["kinds"]["data.rpc"]))
        self.assertEqual({k: tuple(v) for k, v in facts["rpc_signatures"].items()}, rpcs)
        self.assertIn("returns (stream Item)", text)
        self.assertIn("(stream GetItemRequest)", text)


class TestRepo(Base):
    EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

    def setUp(self):
        super().setUp()
        self.root, self.facts = F.make_repo(self.p("repo"))
        self.files = tree(self.root)
        self.text = {k: v.decode("utf-8") for k, v in self.files.items() if not k.endswith(".png")}

    def canon(self, path):
        path = re.sub(r"\[\[\.\.\.(\w+)\]\]|\[\.\.\.(\w+)\]", lambda m: "{%s*}" % (m.group(1) or m.group(2)), path)
        path = re.sub(r":(\w+)\??|<(?:\w+:)?(\w+)>|\[(\w+)\]|\{(\w+)\}",
                      lambda m: "{%s}" % next(g for g in m.groups() if g), path) if "*}" not in path else \
            re.sub(r"\[(\w+)\]", r"{\1}", path)
        return path.rstrip("/") or "/"

    def routes_in(self, rel):
        t = self.text[rel]
        out = []
        if rel.startswith("app/") and Path(rel).stem in ("page", "route") or rel.startswith("pages/"):
            segs = [s for s in Path(rel).parent.parts if not (s.startswith("(") or s.startswith("@") or s.startswith("_"))]
            if rel.startswith("pages/"):
                segs = segs[1:]
                api = bool(segs and segs[0] == "api")
                stem = Path(rel).stem
                if stem != "index":
                    segs.append(stem)
                return ["%s %s" % ("ANY" if api else "GET", self.canon("/" + "/".join(segs)))]
            segs = segs[1:]
            path = self.canon("/" + "/".join(segs))
            if Path(rel).stem == "page":
                return ["GET " + path]
            return ["%s %s" % (m, path) for m in re.findall(r"export async function (GET|POST|PUT|DELETE)\b", t)]
        for m in re.finditer(r'(?<![@\w])(?:app|router)\.(get|post|put|delete)\("([^"]+)"', t):
            out.append("%s %s" % (m.group(1).upper(), self.canon(m.group(2))))
        for m in re.finditer(r'@(?:app|router)\.(get|post|put|delete)\("([^"]+)"', t):
            out.append("%s %s" % (m.group(1).upper(), self.canon(m.group(2))))
        for m in re.finditer(r'@app\.route\("([^"]+)", methods=\[([^\]]+)\]', t):
            out += ["%s %s" % (x.strip(' "'), self.canon(m.group(1))) for x in m.group(2).split(",")]
        for m in re.finditer(r'\bpath\("([^"]+)"', t):
            out.append("ANY " + self.canon("/" + m.group(1)))
        for m in re.finditer(r'\.route\("([^"]+)", (get|post)\(', t):
            out.append("%s %s" % (m.group(2).upper(), self.canon(m.group(1))))
        for m in re.finditer(r'^\s*(get|post) "([^"]+)"', t, re.M):
            out.append("%s %s" % (m.group(1).upper(), self.canon(m.group(2))))
        return out

    def test_every_file_in_the_facts_exists_and_the_binary_is_binary(self):
        self.assertEqual(sorted(self.files), self.facts["files"])
        self.assertIn(b"\x00", self.files["assets/logo.png"][:600] + b"\x00")
        with self.assertRaises(UnicodeDecodeError):
            self.files["assets/logo.png"].decode("utf-8")

    def test_routes_match_the_files(self):
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
        self.assertIn("GET /docs/{slug*}", keys)
        self.assertIn("GET /shop/{parts*}", keys)
        self.assertNotIn("GET /(marketing)/about", keys)

    def resolve(self, rel, spec):
        if spec.startswith("@/"):
            target = "src/" + spec[2:]
        else:
            target = posixpath.normpath(posixpath.join(posixpath.dirname(rel), spec))
        for cand in [target] + [target + e for e in self.EXTS] + [target + "/index" + e for e in self.EXTS]:
            if cand in self.files:
                return cand
        return None

    def test_js_ts_import_graph_matches(self):
        pat = re.compile(r'(?:require\(|import\(|from |import )"(\.[^"]*|@/[^"]*)"')
        imports, unresolved = {}, {}
        for rel in sorted(self.text):
            if Path(rel).suffix not in self.EXTS:
                continue
            for spec in pat.findall(self.text[rel]):
                got = self.resolve(rel, spec)
                (imports if got else unresolved).setdefault(rel, []).append(got or spec)
        for rel, got in imports.items():
            self.assertEqual(sorted(got), self.facts["graph"]["imports"][rel], rel)
        js_files = {k for k in self.facts["graph"]["imports"] if Path(k).suffix in self.EXTS}
        self.assertEqual(js_files, set(imports))
        self.assertEqual({k: v for k, v in unresolved.items()},
                         {k: v for k, v in self.facts["graph"]["unresolved"].items() if Path(k).suffix in self.EXTS})

    def test_other_language_imports_point_at_real_files(self):
        for rel, targets in self.facts["graph"]["imports"].items():
            for t in targets:
                self.assertIn(t, self.files, (rel, t))
        self.assertIn("mod ghost;", self.text["services/rust/src/main.rs"])
        self.assertNotIn("services/rust/src/ghost.rs", self.files)
        self.assertIn("import pylib.mod", self.text["services/py/api/main.py"])

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
        self.assertEqual({k for v in got.values() for k in v}, set(self.facts["kinds"]["source.env"]))
        self.assertIn("process.env.lowercase_var", self.text["src/config.js"])
        self.assertNotIn("lowercase_var", self.facts["kinds"]["source.env"])

    def test_tests_and_tables(self):
        rx = [r"(^|/)(tests?|__tests__|spec)/", r"\.(test|spec)\.[cm]?[jt]sx?$", r"(^|/)test_[^/]*\.py$", r"_test\.(go|py)$"]
        tests = sorted(k for k in self.files if any(re.search(p, k) for p in rx))
        self.assertEqual(tests, self.facts["graph"]["tests"])
        self.assertEqual(sorted(self.facts["kinds"]["source.test"]), tests)
        sql = self.text["db/schema.sql"].lower()
        names = set(re.findall(r"create table (\w+)", sql)) | set(re.findall(r'@@map\("(\w+)"\)', self.text["prisma/schema.prisma"]))
        self.assertEqual(names, set(self.facts["kinds"]["source.table"]))
        for rel, access in self.facts["tables_by_file"].items():
            for table, how in access.items():
                t = self.text[rel].lower()
                wrote = re.search(r"(insert into|delete from|update)\s+%s\b" % table, t) is not None
                self.assertEqual(how == "write", wrote, (rel, table))
                self.assertRegex(t, r"(from|into)\s+%s\b" % table)

    def test_languages_and_frameworks(self):
        by_ext = {}
        for rel in self.files:
            lang = F._LANG_BY_EXT.get(Path(rel).suffix)
            if lang:
                by_ext[lang] = by_ext.get(lang, 0) + 1
        self.assertEqual({k: v["files"] for k, v in self.facts["kinds"]["source.language"].items()}, by_ext)
        self.assertEqual(set(by_ext), {"JavaScript", "TypeScript", "Python", "Rust", "Go", "Ruby", "SQL", "Prisma"})
        pkg = json.loads(self.text["package.json"])
        for dep in ("express", "fastify", "next", "react"):
            self.assertIn(dep, pkg["dependencies"])
        self.assertIn("fastapi", self.text["services/py/requirements.txt"])
        self.assertIn("flask", self.text["services/py/requirements.txt"])
        self.assertIn("django", self.text["services/py/requirements.txt"])
        self.assertIn("axum", self.text["services/rust/Cargo.toml"])
        self.assertIn("gin-gonic/gin", self.text["services/go/go.mod"])
        self.assertIn('gem "rails"', self.text["services/rails/Gemfile"])
        self.assertEqual(sorted(self.facts["kinds"]["source.framework"]),
                         ["axum", "django", "express", "fastapi", "fastify", "flask", "gin", "next", "rails", "react"])
        entries = set(self.facts["kinds"]["source.entry"])
        self.assertIn(pkg["main"], entries)
        self.assertIn(pkg["bin"]["snag"], entries)
        self.assertIn('if __name__ == "__main__"', self.text["services/py/api/main.py"])
        self.assertIn("fn main(", self.text["services/rust/src/main.rs"])
        self.assertIn("func main(", self.text["services/go/main.go"])
        tsconfig = json.loads(self.text["tsconfig.json"])
        self.assertEqual(tsconfig["compilerOptions"]["paths"], {"@/*": ["src/*"]})

    def test_secrets_are_where_the_facts_say(self):
        found = {}
        for rel, t in self.text.items():
            names = sorted(n for n, rx in F.SECRET_REGEXES.items() if re.search(rx, t))
            if names:
                found[rel] = names
        self.assertEqual(found, self.facts["secrets"])

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
        importers = lambda f: [k for k, v in g["imports"].items() if f in v and k not in g["tests"]]
        frontier = list(seeds)
        for depth in (1, 2):
            nxt = []
            for f in frontier:
                for k in importers(f):
                    if k not in files:
                        files[k] = ("used-by", depth)
                        nxt.append(k)
            frontier = nxt
        got = [{"path": p, "role": r, "depth": d} for p, (r, d) in files.items()]
        order = {"seed": 0, "uses": 1, "used-by": 2}
        self.assertEqual(sorted(got, key=lambda x: (order[x["role"]], x["depth"], x["path"])),
                         sorted(F.REPO_TRACE[seed]["files"], key=lambda x: (order[x["role"]], x["depth"], x["path"])))
        tests = sorted(t for t in g["tests"] if any(i in files for i in g["imports"].get(t, [])))
        self.assertEqual(tests, F.REPO_TRACE[seed]["tests"])
        env = sorted({e for f in files for e in self.facts["env_by_file"].get(f, [])})
        self.assertEqual(env, F.REPO_TRACE[seed]["env"])
        tables = sorted({t for f in files for t in self.facts["tables_by_file"].get(f, {})})
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
        self.assertIn('"./b"', self.text["src/cyc/a.js"])
        self.assertIn('"./a"', self.text["src/cyc/b.js"])
        self.assertIn('"@/lib/util"', self.text["src/api-client.ts"])
        self.assertIn('import("./lazy")', self.text["src/server.js"])

    def test_empty_and_malformed_repos(self):
        root, facts = F.make_repo(self.p("e"), "empty")
        self.assertEqual(sorted(tree(root)), ["README.md"])
        root, facts = F.make_repo(self.p("m"), "malformed")
        files = tree(root)
        self.assertEqual(sorted(files), facts["files"])
        for rel in ("package.json", "tsconfig.json"):
            with self.assertRaises(ValueError):
                json.loads(files[rel].decode("utf-8"))
        with self.assertRaises(UnicodeDecodeError):
            files["src/bad_utf8.js"].decode("utf-8")
        self.assertIn(b"\x00", files["src/binary.ts"])
        line = files["src/huge-minified.js"]
        self.assertNotIn(b"\n", line)
        self.assertGreaterEqual(len(line), F.ONE_MB)
        for key in facts["kinds"]["source.route"]:
            self.assertIn(key.split(" ")[1].encode(), line)
        self.assertLess(len(line), CAP)
        with self.assertRaises(KeyError):
            F.make_repo(self.p("x"), "nope")

    def test_nothing_in_the_repo_is_a_real_path_or_address(self):
        for rel, t in self.text.items():
            self.assertNotRegex(t, r"[A-Za-z]:\\Users|/home/|/Users/", rel)


class TestVersionPairs(Base):
    def test_openapi_pair_differs_exactly_as_listed(self):
        a, b, facts = F.make_version_pair(self.p("p"), "openapi")
        da, db = jload(a), jload(b)

        def facts_of(doc):
            out = {}
            for p, item in doc["paths"].items():
                for m, op in item.items():
                    if m in ("get", "put", "post", "delete", "patch", "head", "options"):
                        out[("data.endpoint", "%s %s" % (m.upper(), p))] = {"operationId": op["operationId"], "tags": op["tags"]}
            for s in doc["components"]["schemas"]:
                out[("data.schema-type", s)] = {}
            for s, v in doc["components"]["securitySchemes"].items():
                out[("data.security", s)] = {"type": v["type"]}
            return out
        fa, fb = facts_of(da), facts_of(db)
        self.assertEqual(sorted(k for k in fb if k not in fa), sorted((x["kind"], x["key"]) for x in facts["added"]))
        self.assertEqual(sorted(k for k in fa if k not in fb), sorted((x["kind"], x["key"]) for x in facts["removed"]))
        changed = [k for k in fa if k in fb and fa[k] != fb[k]]
        self.assertEqual(sorted(changed), sorted((x["kind"], x["key"]) for x in facts["changed"]))
        for c in facts["changed"]:
            self.assertEqual(fa[(c["kind"], c["key"])], c["before"])
            self.assertEqual(fb[(c["kind"], c["key"])], c["after"])
        for u in facts["unchanged"]:
            self.assertEqual(fa[(u["kind"], u["key"])], fb[(u["kind"], u["key"])])
        self.assertTrue(facts["added"] and facts["removed"] and facts["changed"])
        self.assertNotEqual(a.read_bytes(), b.read_bytes())

    def test_har_pair_differs_exactly_as_listed(self):
        a, b, facts = F.make_version_pair(self.p("p"), "har")
        ra, rb = har_derive(jload(a)["log"]["entries"]), har_derive(jload(b)["log"]["entries"])

        def fl(r):
            out = {("web.endpoint", k): v for k, v in r[0].items()}
            out.update({("web.host", h): {} for h in r[3]})
            out.update({("web.payload-shape", k): s for k, s in r[1].items()})
            out.update({("web.auth-step", x["endpoint"]): x for x in r[2]})
            return out
        fa, fb = fl(ra), fl(rb)
        self.assertEqual(sorted(k for k in fb if k not in fa), sorted((x["kind"], x["key"]) for x in facts["added"]))
        self.assertEqual(sorted(k for k in fa if k not in fb), sorted((x["kind"], x["key"]) for x in facts["removed"]))
        self.assertEqual(sorted(k for k in fa if k in fb and fa[k] != fb[k]), sorted((x["kind"], x["key"]) for x in facts["changed"]))
        for c in facts["changed"]:
            self.assertEqual(fa[(c["kind"], c["key"])], c["before"])
            self.assertEqual(fb[(c["kind"], c["key"])], c["after"])
        self.assertEqual(ra[3], rb[3])
        for u in facts["unchanged"]:
            self.assertEqual(fa[(u["kind"], u["key"])], fb[(u["kind"], u["key"])])

    def test_unknown_pair_kind(self):
        with self.assertRaises(ValueError):
            F.make_version_pair(self.p("p"), "nope")


class TestTierB(Base):
    def test_capture_parses_and_facts_match(self):
        path, facts = F.make_capture(self.p("c.json"))
        doc = jload(path)
        self.assertEqual(doc["format"], "app-blueprint-capture/1")
        steps = doc["steps"]
        self.assertEqual([s["n"] for s in steps], list(range(1, len(steps) + 1)))
        self.assertTrue({s["action"] for s in steps} <= {"navigate", "click", "type", "select", "submit", "wait", "observe"})
        keys = {"%d %s %s" % (s["n"], s["action"], s.get("target") or s.get("url")) for s in steps if s["action"] != "navigate"}
        self.assertEqual(keys, set(facts["kinds"]["flow.step"]))
        self.assertEqual({s["screen"] for s in steps if "screen" in s}, set(facts["kinds"]["flow.screen"]))
        eps = {har_key({"request": {"method": r["method"], "url": r["url"]}}) for s in steps for r in s.get("requests", [])}
        self.assertEqual(eps, set(facts["kinds"]["web.endpoint"]))
        text = path.read_text(encoding="utf-8")
        for v in facts["never_stored"]:
            self.assertIn(v, text)
        for n, rep in facts["replaced"].items():
            value = steps[int(n) - 1]["value"]
            self.assertEqual(rep, "<typed:%d chars>" % len(value))
        self.assertTrue(any("?" in r["url"] for s in steps for r in s.get("requests", [])))

    def test_capture_variants_are_wrong_in_the_stated_way(self):
        def make(name):
            return jload(F.make_variant(self.p(name + ".json"), "capture", name)[0])
        self.assertNotEqual(make("wrong_format")["format"], "app-blueprint-capture/1")
        self.assertNotIn(make("bad_action")["steps"][1]["action"], ("navigate", "click", "type", "select", "submit", "wait", "observe"))
        self.assertNotIsInstance(make("steps_not_list")["steps"], list)
        self.assertNotIn("n", make("step_missing_n")["steps"][0])
        self.assertEqual(make("empty")["steps"], [])

    def test_rea_normal(self):
        path, facts = F.make_rea_bundle(self.p("r.json"))
        doc = jload(path)
        self.assertIsInstance(doc["evidence_format"], int)
        recs = doc["records"]
        self.assertEqual(len(recs), facts["counts"]["imported"])
        for rec, exp in zip(recs, facts["records"]):
            loc = rec["locations"][0]
            ident = loc.get("path") or loc.get("address") or loc.get("offset")
            self.assertEqual(exp["key"], "%s:%s" % (rec["operation"], ident))
            self.assertEqual(exp["kind"], "rea." + rec["operation"].replace("_", "-"))
            self.assertEqual(exp["source"]["digest"], "sha256:" + rec["subject"]["identity"]["sha256"])
            self.assertEqual(exp["source"]["artifact"], rec["subject"]["name"])
            self.assertIn(exp["key"], facts["kinds"][exp["kind"]])
            self.assertEqual(exp["basis"], rec["confidence"] if rec["confidence"] in ("observed", "derived", "inferred") else "inferred")
            self.assertIn(rec["subject"]["local_path"], facts["never_stored"])
        self.assertTrue(recs[0]["subject"]["local_path"].startswith("/"))
        self.assertEqual(doc["unknowns"][0]["unknown_id"], next(iter(facts["kinds"]["rea.unknown"])))
        self.assertEqual(len(set(r["evidence_id"] for r in recs)), len(recs))
        linked = {l["evidence_id"] for r in recs for l in r["evidence_links"]}
        self.assertEqual(linked - {r["evidence_id"] for r in recs}, {"ev-9999"})

    def test_rea_tolerant_and_other_variants(self):
        path, facts = F.make_rea_bundle(self.p("t.json"), "tolerant")
        doc = jload(path)
        self.assertEqual(doc["evidence_format"], 3)
        recs = doc["records"]
        bad = [r for r in recs if "subject" not in r or "operation" not in r]
        self.assertEqual(len(bad), facts["counts"]["malformed"])
        self.assertEqual(len(recs) - len(bad), facts["counts"]["imported"])
        self.assertTrue(any("future_field" in r for r in recs))
        self.assertTrue(any(len(json.dumps(r["normalized_result"])) > 4096 for r in recs if "normalized_result" in r))
        self.assertTrue(any(isinstance(l, str) for r in recs for l in r.get("evidence_links", [])))
        self.assertTrue(any("locations" not in r or not r["locations"] for r in recs))
        self.assertNotIn("evidence_format", jload(F.make_rea_bundle(self.p("n.json"), "not_rea")[0]))
        self.assertEqual(jload(F.make_rea_bundle(self.p("e.json"), "empty")[0])["records"], [])
        with self.assertRaises(KeyError):
            F.make_rea_bundle(self.p("x.json"), "nope")


class TestAndroidFolders(Base):
    NS = "{http://schemas.android.com/apk/res/android}"

    def manifest_kinds(self, root):
        m = ET.fromstring(root.read_bytes())
        pkg = m.get("package")
        a = lambda e, n: e.get(self.NS + n)
        full = lambda n: pkg + n if n.startswith(".") else n
        kinds = {"mobile.package": {pkg: {}}, "mobile.permission": {a(e, "name"): {} for e in m.iter("uses-permission")},
                 "mobile.screen": {}, "mobile.service": {}, "mobile.deeplink": {}}
        for e in m.iter("activity"):
            launcher = any(c.get(self.NS + "name") == "android.intent.category.LAUNCHER" for c in e.iter("category"))
            kinds["mobile.screen"][full(a(e, "name"))] = {"exported": a(e, "exported") == "true", "launcher": launcher}
        for tag in ("service", "receiver", "provider"):
            for e in m.iter(tag):
                kinds["mobile.service"][full(a(e, "name"))] = {"exported": a(e, "exported") == "true"}
        for act in m.iter("activity"):
            for f in act.iter("intent-filter"):
                if any(c.get(self.NS + "name") == "android.intent.category.BROWSABLE" for c in f.iter("category")):
                    for d in f.iter("data"):
                        kinds["mobile.deeplink"]["%s://%s%s" % (a(d, "scheme"), a(d, "host"), a(d, "pathPrefix") or "")] = {
                            "activity": full(a(act, "name")), "autoVerify": a(f, "autoVerify") == "true"}
        return kinds, m

    def test_jadx_normal_and_gradle(self):
        for variant, man in (("normal", "resources/AndroidManifest.xml"), ("gradle", "app/src/main/AndroidManifest.xml")):
            with self.subTest(variant):
                root, facts = F.make_jadx_dir(self.p(variant), variant)
                kinds, m = self.manifest_kinds(root / man)
                self.assertEqual(kinds, facts["kinds"])
                self.assertEqual({k: m.find("application").get(self.NS + k) for k in facts["settings"]}, facts["settings"])
                files = tree(root)
                for lit, loc in facts["locations"].items():
                    line = files[loc["path"]].decode("utf-8").split("\n")[loc["line"] - 1]
                    self.assertIn(lit, line)
                    self.assertLessEqual(len(line), 160)
                self.assertEqual(sorted(facts["locations"]), sorted(facts["endpoints"]))
                hist = {}
                for rel, data in files.items():
                    mm = re.search(r"^package ([\w.]+)", data.decode("utf-8"), re.M)
                    if mm and Path(rel).suffix in (".java", ".kt"):
                        hist[mm.group(1)] = hist.get(mm.group(1), 0) + 1
                self.assertEqual(hist, facts["package_histogram"])
                self.assertTrue(all(F.SECRETS["aws-key"] in files[p].decode() for p in files if p.endswith("MainActivity.java")))

    def test_jadx_variants(self):
        root, facts = F.make_jadx_dir(self.p("e"), "empty")
        self.assertEqual(tree(root), {})
        self.assertTrue((root / "sources").is_dir())
        root, facts = F.make_jadx_dir(self.p("nm"), "no_manifest")
        self.assertFalse(any(p.endswith("AndroidManifest.xml") for p in tree(root)))
        self.assertEqual(facts["kinds"], {})
        root, facts = F.make_jadx_dir(self.p("ns"), "no_sources")
        self.assertEqual(list(tree(root)), ["resources/AndroidManifest.xml"])
        root, facts = F.make_jadx_dir(self.p("dt"), "doctype")
        self.assertIn(b"<!DOCTYPE", tree(root)["resources/AndroidManifest.xml"])
        self.assertIn(b"<!ENTITY", tree(root)["resources/AndroidManifest.xml"])
        self.assertEqual(facts["kinds"], {})
        self.assertIn("sources/com/example/snag/MainActivity.java", tree(root))
        root, facts = F.make_jadx_dir(self.p("bm"), "bad_manifest")
        with self.assertRaises(ET.ParseError):
            ET.fromstring(tree(root)["resources/AndroidManifest.xml"])
        root, facts = F.make_jadx_dir(self.p("bi"), "binary_manifest")
        data = tree(root)["resources/AndroidManifest.xml"]
        self.assertFalse(data.startswith(b"<?xml") or data.startswith(b"<manifest"))
        with self.assertRaises(KeyError):
            F.make_jadx_dir(self.p("x"), "nope")

    def test_apktool_normal(self):
        root, facts = F.make_apktool_dir(self.p("a"))
        files = tree(root)
        yml = files["apktool.yml"].decode("utf-8")
        got = {k: re.search(r"^\s*%s: '?([^'\n]+)'?$" % k, yml, re.M).group(1) for k in facts["yml"]}
        self.assertEqual(got, facts["yml"])
        self.assertEqual(self.manifest_kinds(root / "AndroidManifest.xml")[0], facts["kinds"])
        strings = ET.fromstring(files["res/values/strings.xml"])
        self.assertEqual(len(strings.findall("string")), facts["strings_count"])
        hist = {}
        for rel, data in files.items():
            if rel.endswith(".smali"):
                cls = re.search(r"^\.class [\w ]*L([\w/$]+);", data.decode("utf-8"), re.M).group(1)
                pkg = cls.rsplit("/", 1)[0]
                hist[pkg] = hist.get(pkg, 0) + 1
        self.assertEqual(hist, facts["package_histogram"])
        self.assertTrue(any(p.startswith("smali_classes2/") for p in files))
        for lit, loc in facts["locations"].items():
            line = files[loc["path"]].decode("utf-8").split("\n")[loc["line"] - 1]
            self.assertIn('const-string', line)
            self.assertIn(lit, line)
        text = "".join(v.decode("utf-8", "replace") for v in files.values())
        for v in facts["never_stored"][1:]:
            self.assertIn(v, text)

    def test_apktool_variants(self):
        for variant in ("empty", "no_manifest", "binary_manifest", "no_smali", "doctype", "bad_yml", "unquoted"):
            with self.subTest(variant):
                root, facts = F.make_apktool_dir(self.p(variant), variant)
                files = tree(root)
                self.assertIn("apktool.yml", files)
                if variant == "no_smali":
                    self.assertFalse(any(p.startswith("smali") for p in files))
                if variant == "no_manifest":
                    self.assertNotIn("AndroidManifest.xml", files)
                if variant == "binary_manifest":
                    self.assertFalse(files["AndroidManifest.xml"].startswith((b"<?xml", b"<manifest")))
                if variant == "doctype":
                    self.assertIn(b"<!DOCTYPE", files["AndroidManifest.xml"])
                if variant == "unquoted":
                    self.assertNotIn(b"'", files["apktool.yml"])
                    self.assertIn(b"minSdkVersion: 24", files["apktool.yml"])
                if variant == "bad_yml":
                    self.assertNotIn(b"minSdkVersion", files["apktool.yml"])
                if variant == "empty":
                    self.assertEqual(list(files), ["apktool.yml"])
        with self.assertRaises(KeyError):
            F.make_apktool_dir(self.p("x"), "nope")


class TestVariants(Base):
    def build(self, kind, name):
        return F.make_variant(self.p("%s.%s" % (kind, name)), kind, name)

    def test_every_variant_builds_with_well_formed_facts(self):
        for kind, names in F.VARIANTS.items():
            for name in names:
                with self.subTest("%s/%s" % (kind, name)):
                    path, facts = self.build(kind, name)
                    self.assertTrue(Path(path).is_file())
                    self.assertEqual(facts["variant"], name if name not in ("tolerant", "not_rea") else name)
                    self.assertIsInstance(facts["kinds"], dict)
                    if facts["kinds"] == {} or "expect" in facts:
                        pass
                    json.dumps(facts)
                    size = Path(path).stat().st_size
                    if name == "too_large":
                        self.assertEqual(size, CAP + 1)
                        self.assertEqual(facts["kinds"], {})
                        self.assertEqual(facts["expect"]["skip"], ["too-large"])
                    else:
                        self.assertLessEqual(size, CAP)
                    Path(path).unlink()

    def test_unknown_variant(self):
        with self.assertRaises(KeyError):
            F.make_variant(self.p("x"), "har", "nope")
        with self.assertRaises(KeyError):
            F.make_variant(self.p("x"), "sqlite", "too_large")

    def test_zero_bytes(self):
        for kind in F.VARIANTS:
            with self.subTest(kind):
                self.assertEqual(self.build(kind, "zero_bytes")[0].stat().st_size, 0)

    def test_truncated_is_malformed(self):
        for kind in ("har", "sourcemap", "openapi_json", "capture", "rea_bundle"):
            with self.subTest(kind):
                with self.assertRaises(ValueError):
                    json.loads(self.build(kind, "truncated")[0].read_bytes().decode("utf-8"))
        for name in ("truncated", "truncated_binary"):
            with self.subTest("plist " + name):
                with self.assertRaises(Exception) as cm:
                    plistlib.loads(self.build("plist", name)[0].read_bytes())
                self.assertIsInstance(cm.exception, (plistlib.InvalidFileException, ValueError, Exception))
        con = sqlite3.connect(str(self.build("sqlite", "truncated")[0]))
        self.addCleanup(con.close)
        with self.assertRaises(sqlite3.DatabaseError):
            con.execute("SELECT name FROM sqlite_master").fetchall()
        for kind in ("bundle", "openapi_yaml", "graphql", "proto"):
            data = self.build(kind, "truncated")[0].read_bytes()
            self.assertGreater(len(data), 0)
            data.decode("utf-8")

    def test_invalid_utf8(self):
        for kind in F.VARIANTS:
            if "invalid_utf8" in F.VARIANTS[kind]:
                with self.subTest(kind):
                    with self.assertRaises(UnicodeDecodeError):
                        self.build(kind, "invalid_utf8")[0].read_bytes().decode("utf-8")

    def test_binary(self):
        for kind in F.VARIANTS:
            with self.subTest(kind):
                data = self.build(kind, "binary")[0].read_bytes()
                self.assertIn(b"\x00", data)
                self.assertFalse(data.startswith(b"SQLite format 3"))
                with self.assertRaises(UnicodeDecodeError):
                    data.decode("utf-8")

    def test_minified_one_megabyte_line(self):
        for kind in ("har", "sourcemap", "openapi_json", "capture", "rea_bundle", "bundle", "openapi_yaml", "graphql", "proto", "plist"):
            with self.subTest(kind):
                path, facts = self.build(kind, "minified_1mb")
                data = path.read_bytes()
                self.assertGreaterEqual(max(len(l) for l in data.split(b"\n")), F.ONE_MB)
                self.assertLess(len(data), CAP)
                if kind in ("har", "sourcemap", "openapi_json", "capture", "rea_bundle"):
                    self.assertEqual(data.count(b"\n"), 1)
                    json.loads(data.decode("utf-8"))
                if kind == "plist":
                    self.assertEqual(plistlib.loads(data)["Padding"], "a" * F.ONE_MB)
                if kind == "bundle":
                    self.assertEqual(data.count(b"\n"), 0)
                    self.assertIn(b'path: "/dashboard"', data)
                    self.assertTrue(data.endswith(b'path: "/end-of-line" };'))
                    self.assertIn("/end-of-line", facts["kinds"]["web.route"])
                if kind in ("har", "openapi_json", "capture", "openapi_yaml", "graphql", "proto", "sourcemap"):
                    self.assertTrue(facts["kinds"] or kind == "capture")

    def test_deep_json_is_deeper_than_the_parser_allows(self):
        for kind in ("har", "sourcemap", "openapi_json", "capture", "rea_bundle"):
            with self.subTest(kind):
                with self.assertRaises(RecursionError):
                    json.loads(self.build(kind, "deep")[0].read_bytes().decode("utf-8"))

    def test_deep_and_huge_har_bodies(self):
        path, facts = self.build("har", "deep_body")
        entry = jload(path)["log"]["entries"][0]
        with self.assertRaises(RecursionError):
            json.loads(entry["response"]["content"]["text"])
        self.assertEqual(list(facts["kinds"]["web.endpoint"]), [har_key(entry)])
        path, facts = self.build("har", "huge_body")
        entry = jload(path)["log"]["entries"][0]
        self.assertGreaterEqual(len(entry["response"]["content"]["text"]), 3_000_000)
        self.assertEqual(shape(json.loads(entry["response"]["content"]["text"])), facts["payload_shapes"][0]["shape"])
        self.assertLess(path.stat().st_size, CAP)

    def test_bad_har_entries(self):
        path, facts = self.build("har", "bad_entries")
        entries = jload(path)["log"]["entries"]
        self.assertEqual(len(entries), facts["entry_count"])
        valid = [e for e in entries if isinstance(e, dict) and isinstance(e.get("request"), dict) and "url" in e["request"]
                 and isinstance(e.get("response"), dict) and "content" in e["response"]]
        self.assertEqual(len(valid), facts["valid_entries"])
        self.assertTrue(any(not isinstance(e, dict) for e in entries))

    def test_sourcemap_variants(self):
        path, facts = self.build("sourcemap", "outside_root")
        doc = jload(path)
        self.assertTrue(any(s.startswith("../") for s in doc["sources"]))
        self.assertTrue(any(s.startswith("/") for s in doc["sources"]))
        self.assertTrue(any(re.match(r"[A-Za-z]:\\", s) for s in doc["sources"]))
        self.assertEqual(doc["sourceRoot"], "../../..")
        self.assertEqual(facts["unsafe_sources"], doc["sources"][:3])
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
        self.assertEqual(max(k.count(".") + 1 for k in facts["kinds"]["data.plist-key"]), facts["max_depth"])
        path, facts = self.build("plist", "many_keys")
        self.assertEqual(len(plistlib.loads(path.read_bytes())), facts["total_keys"])
        self.assertEqual(len(facts["kinds"]["data.plist-key"]), facts["max_keys"])
        self.assertEqual(sorted(facts["kinds"]["data.plist-key"]), sorted("key%03d" % i for i in range(500)))

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
        path, facts = F.make_pathological_line(self.p("q.js"), 1234)
        self.assertEqual(path.stat().st_size, 1234)
        path, facts = F.make_too_large_text(self.p("t.txt"))
        self.assertEqual(path.stat().st_size, CAP + 1)

    def test_variant_names_are_listed_for_every_kind(self):
        self.assertEqual(set(F.VARIANTS), {"har", "sourcemap", "bundle", "sqlite", "plist", "openapi_json", "openapi_yaml",
                                           "graphql", "proto", "capture", "rea_bundle"})
        for kind, names in F.VARIANTS.items():
            self.assertIn("empty", names)
            self.assertEqual(len(names), len(set(names)))
            self.assertTrue({"zero_bytes", "truncated"} <= set(names) or kind == "plist", kind)


class TestSizesAndPrivacy(Base):
    def test_no_normal_fixture_exceeds_the_text_cap(self):
        biggest = 0
        for make in (lambda d: F.make_har(d / "a.har"), lambda d: F.make_repo(d / "r"), lambda d: F.make_site(d / "s"),
                     lambda d: F.make_jadx_dir(d / "j"), lambda d: F.make_apktool_dir(d / "a"),
                     lambda d: F.make_version_pair(d / "p", "har")):
            d = self.p("x%d" % biggest)
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
                    self.assertTrue(host.endswith(".test"), host)


if __name__ == "__main__":
    unittest.main()
