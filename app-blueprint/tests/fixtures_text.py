"""Synthetic text and data fixtures for the study lanes (contract 10.2, workstream W2-FX2).

Everything here is invented: names, hosts (`example.test`, `vendor.test`) and values. Nothing is downloaded or read
from a real app. The module uses the standard library only and runs on Python 3.10+ (Windows and Linux). Every
secret-looking value is obviously fake (it says "fixture") and is assembled in code from pieces, so no whole token
appears in this source file.

Conventions (read these before using a builder)
-----------------------------------------------
* A file builder takes the destination path first and returns ``(path, facts)``; a folder builder returns
  ``(root, facts)``. ``path`` is a ``pathlib.Path``. Parent folders are created. Files are written as bytes with
  LF line endings and fixed timestamps, so two calls give byte-identical files on every platform and Python
  version. The one exception is SQLite: the file bytes can vary with the SQLite library version, so tests must
  compare schema and rows, never bytes (see ``make_sqlite``).
* ``facts`` is a plain dict of literals. Its common keys:

  - ``"kinds"``: ``{evidence kind: {key: data}}`` in the canonical key forms of contract 6.3 (``METHOD /path``,
    ``{name}`` parameters, ``{slug*}`` catch-alls). ``data`` is ``{}`` when the contract fixes no data for the kind;
    lane tests then assert the key only. Where the contract fixes ``data`` (HAR endpoints, SQLite columns, plist
    types) the dict holds it. Lists inside ``data`` are sorted and unique.
  - ``"never_stored"``: values that must appear in no stored record, excerpt, claim or data (contract 6.1, 6.2, 6.5).
  - ``"secret_patterns"``: the ``SECRET_PATTERNS`` names (contract 6.2) whose values really occur in the file, so a
    lane must report ``secret.embedded`` for it and redact the value everywhere.
  - ``"ambiguous"``: facts the contract does not pin down (for example the method of a bare ``fetch("/x")``). A lane
    test may assert them only after the contract is made exact; they are kept out of ``"kinds"`` on purpose.
  - ``"expect"`` (malformed variants only): ``{"skip": [acceptable skip reasons]}``. ``None`` in the list means
    "emitting nothing, without a skip, is also fine". Whatever the variant, no exception may escape the lane.
  - ``"variant"``: the variant name.
* ``make_variant(path, kind, name)`` builds the empty and malformed cases of every kind (``VARIANTS`` lists them):
  ``zero_bytes``, ``empty`` (valid but nothing to extract), ``truncated``, ``invalid_utf8``, ``binary``,
  ``minified_1mb`` (one 1 MB line with a real fact at each end), ``too_large`` (exactly 5 MiB + 1 byte, the only
  fixture over the text-read cap), and kind-specific cases such as a source map pointing outside its root or a HAR with
  a gigantic or deeply nested body.
* Folder builders (``make_repo``, ``make_jadx_dir``, ``make_apktool_dir``, ``make_site``) take ``variant=``.

Signatures chosen where contract 10.2 leaves them open
------------------------------------------------------
``make_har(path, entries=None)``                 entries: list of ``har_entry(...)`` dicts; None = the default flow
``make_sourcemap(path, sources=None, contents=None)``   sources/contents: lists, None = the defaults
``make_bundle(text="", style="spaced")``         returns ``(js_text, facts)`` and writes nothing; ``write_bundle`` writes
``make_sqlite(path, ddl=None)``                  ddl: list of SQL statements (the last ones may be INSERTs)
``make_plist(path, fmt="xml", data=None)``       fmt: "xml" or "binary"
``make_openapi_json(path, doc=None, swagger=False)``, ``make_openapi_yaml(path, variant="normal")``
``make_graphql(path, introspection=False)``, ``make_proto(path)``
``make_repo(path, variant="normal")``            variants: normal, empty, malformed
``make_version_pair(root, kind="openapi")``      returns ``(path_v1, path_v2, facts)``; kinds: openapi, har
``make_capture(path)``, ``make_rea_bundle(path, variant)``, ``make_jadx_dir(root, variant)``,
``make_apktool_dir(root, variant)``
"""
import base64
import datetime
import json
import plistlib
import sqlite3
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

TEXT_CAP = 5 * 1024 * 1024          # contract 6.2: text file read size; larger is `too-large`
ONE_MB = 1000 * 1000

# ----------------------------------------------------------------------------- fake secrets


def _b64url(text):
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


SECRETS = {
    "aws-key": "AKIA" + "FIXTURE0000000AB",
    "google-api-key": "AIza" + ("FixtureNotARealKey_" + "0" * 40)[:35],
    "private-key": "-----BEGIN RSA " + "PRIVATE KEY-----\nFIXTURE-NOT-A-KEY\n-----END RSA PRIVATE KEY-----",
    "jwt": ".".join([_b64url('{"alg":"none","typ":"JWT"}'), _b64url('{"sub":"fixture-user","fixture":true}'),
                     "fixturesignature0000"]),
    "stripe-live": "sk_live_" + "FIXTURE0000000000000000",
    "slack": "xox" + "b-0000000000-FIXTURE-fake",
    "github-token": "gh" + "p_FIXTURE" + "0" * 30,
    "bearer": "Bearer " + "fixture-token-" + "0" * 16,
}
TOKEN = SECRETS["bearer"].split(" ", 1)[1]
PASSWORD = "fixture-password-not-real"
COOKIE = "fixture-session-cookie-not-real"
URL_PASSWORD = "fixture-url-password"
BASIC = base64.b64encode(b"fixture-user:fixture-basic-secret").decode("ascii")

# Contract 6.2 minimum patterns, copied here so tests can prove every secret above would be caught.
SECRET_REGEXES = {
    "aws-key": r"AKIA[0-9A-Z]{16}",
    "google-api-key": r"AIza[0-9A-Za-z_\-]{35}",
    "private-key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "jwt": r"eyJ[\w-]{10,}\.eyJ[\w-]{10,}\.[\w-]{10,}",
    "stripe-live": r"[sr]k_live_[0-9A-Za-z]{16,}",
    "slack": r"xox[abprs]-[0-9A-Za-z-]{10,}",
    "github-token": r"gh[pousr]_[0-9A-Za-z]{30,}",
    "bearer": r"(?i)bearer\s+[\w.\-]{16,}",
}

# ----------------------------------------------------------------------------- small helpers


def _write(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    p.write_bytes(data)
    return p


def _dump(obj, indent=2):
    """Deterministic JSON bytes: insertion order, ASCII only, one trailing LF."""
    return (json.dumps(obj, indent=indent, ensure_ascii=True) + "\n").encode("ascii")


def _lines(*lines):
    return "\n".join(lines) + "\n"


def _lcg_bytes(n, seed=12345):
    """Deterministic pseudo-random bytes that always contain NULs (binary data where text is expected)."""
    out = bytearray()
    x = seed
    while len(out) < n:
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        out.append((x >> 16) & 0xFF)
    out[0:4] = b"\x00\x01\xfe\xff"
    return bytes(out)


def _deep_json(n):
    """A JSON document nested n arrays deep: json.loads raises RecursionError for the n used here."""
    return "[" * n + "]" * n


def _iso(seconds):
    t = datetime.datetime(2026, 1, 1, 0, 0, 0) + datetime.timedelta(seconds=seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _facts(kinds=None, **extra):
    f = {"kinds": kinds or {}}
    f.update(extra)
    return f


# ----------------------------------------------------------------------------- HAR


def _hdrs(h):
    if not h:
        return []
    pairs = h.items() if isinstance(h, dict) else h
    return [{"name": k, "value": v} for k, v in pairs]


def har_entry(method, url, status=200, *, t=0, req_headers=None, req_body=None, req_mime=None, resp_headers=None,
              resp_body=None, resp_mime="", req_cookies=None, resp_cookies=None):
    """One HAR 1.2 entry. A dict body is JSON-encoded; a str body is used as it is."""
    parts = urlsplit(url)
    request = {"method": method, "url": url, "httpVersion": "HTTP/1.1", "headers": _hdrs(req_headers),
               "queryString": [{"name": k, "value": v} for k, v in parse_qsl(parts.query, keep_blank_values=True)],
               "cookies": req_cookies or [], "headersSize": -1, "bodySize": 0}
    if req_body is not None:
        text = req_body if isinstance(req_body, str) else json.dumps(req_body, separators=(",", ":"))
        mime = req_mime or ("application/json" if not isinstance(req_body, str) else "text/plain")
        request["postData"] = {"mimeType": mime, "text": text}
        if mime == "application/x-www-form-urlencoded":
            request["postData"]["params"] = [{"name": k, "value": v} for k, v in parse_qsl(text, keep_blank_values=True)]
        request["bodySize"] = len(text)
    text = "" if resp_body is None else (resp_body if isinstance(resp_body, str) else json.dumps(resp_body, separators=(",", ":")))
    content = {"size": len(text), "mimeType": resp_mime}
    if text:
        content["text"] = text
    response = {"status": status, "statusText": "OK" if status < 400 else "Error", "httpVersion": "HTTP/1.1",
                "headers": _hdrs(resp_headers), "cookies": resp_cookies or [], "content": content,
                "redirectURL": "", "headersSize": -1, "bodySize": len(text)}
    return {"startedDateTime": _iso(t), "time": 12, "request": request, "response": response, "cache": {},
            "timings": {"send": 0, "wait": 12, "receive": 0}}


def _html(title):
    return '<!doctype html><html><head><title>%s</title></head><body><div id="root"></div></body></html>' % title


_ORIGIN = "https://app.example.test"
_ITEM = {"id": "i-1", "title": "Cracked-tile-fixture-value", "tags": ["wall-fixture-value"], "done": False,
         "owner": {"id": "u-1"}}


def default_har_entries():
    cookie_hdr = {"Cookie": "session=" + COOKIE}
    auth = {"Authorization": SECRETS["bearer"]}
    both = dict(cookie_hdr, **auth)
    session_cookie = [{"name": "session", "value": COOKIE, "path": "/", "httpOnly": True, "secure": True}]
    return [
        har_entry("GET", _ORIGIN + "/", 200, t=0, req_headers=cookie_hdr, resp_body=_html("Snag"), resp_mime="text/html"),
        har_entry("POST", _ORIGIN + "/api/session/login", 201, t=1,
                  req_body={"email": "tester-one-fixture-value", "password": PASSWORD, "remember": True},
                  resp_headers=[("Set-Cookie", "session=%s; Path=/; HttpOnly; Secure" % COOKIE)],
                  resp_cookies=session_cookie, resp_mime="application/json",
                  resp_body={"user": {"id": "u-1", "name": "Tester-One-fixture-value", "roles": ["admin", "user"]},
                             "token": SECRETS["jwt"], "expiresIn": 3600}),
        har_entry("GET", _ORIGIN + "/api/items?page=2&q=fixture-query-value&api_key=" + SECRETS["google-api-key"], 200,
                  t=2, req_headers=both, req_cookies=[{"name": "session", "value": COOKIE}], resp_mime="application/json",
                  resp_body={"items": [_ITEM], "total": 1, "next": None}),
        har_entry("GET", _ORIGIN + "/api/items/123", 200, t=3, req_headers=both, resp_mime="application/json", resp_body=_ITEM),
        har_entry("GET", _ORIGIN + "/api/items/124", 200, t=4, req_headers=both, resp_mime="application/json", resp_body=_ITEM),
        har_entry("GET", _ORIGIN + "/api/items/3f2b8c1e-5a4d-4e7b-9c1a-0d2e4f6a8b0c/comments", 200, t=5, req_headers=both,
                  resp_mime="application/json", resp_body=[{"id": "c-1", "text": "Looks-bad-fixture-value", "at": "2026-01-01"}]),
        har_entry("GET", _ORIGIN + "/api/files/0123456789abcdef", 200, t=6, req_headers=both,
                  resp_mime="application/octet-stream", resp_body="AAEC"),
        har_entry("GET", _ORIGIN + "/api/blobs/Zm9vYmFyYmF6cXV4MTIzNA", 200, t=7, req_headers=both,
                  resp_mime="text/plain", resp_body="ok-fixture-value"),
        har_entry("PUT", _ORIGIN + "/api/items/123", 204, t=8, req_headers=both,
                  req_body={"title": "Fix-tile-fixture-value", "done": True}),
        har_entry("DELETE", _ORIGIN + "/api/items/123", 404, t=9, req_headers=both, resp_mime="application/json",
                  resp_body={"error": {"code": "not_found", "message": "Gone-fixture-value"}}),
        har_entry("POST", _ORIGIN + "/api/auth/token", 200, t=10, req_headers={"Authorization": "Basic " + BASIC},
                  req_body="grant_type=client_credentials&client_secret=" + SECRETS["stripe-live"],
                  req_mime="application/x-www-form-urlencoded", resp_mime="application/json",
                  resp_body={"access_token": SECRETS["jwt"], "token_type": "Bearer"}),
        har_entry("GET", "https://analytics.vendor.test/collect?cid=abc123-fixture&v=1", 200, t=11, resp_mime="text/plain"),
        har_entry("GET", "https://svc:%s@api.example.test/v1/status" % URL_PASSWORD, 200, t=12, resp_mime="application/json",  # scan-private: allow
                  resp_body={"ok": True, "version": "1.0.0-fixture-value"}),
        har_entry("GET", _ORIGIN + "/settings", 200, t=13, req_headers=cookie_hdr, resp_body=_html("Settings"),
                  resp_mime="text/html"),
        har_entry("GET", _ORIGIN + "/api/session/logout", 200, t=14, req_headers=both,
                  resp_headers=[("Set-Cookie", "session=; Max-Age=0; Path=/")], resp_mime="application/json",
                  resp_body={"ok": True}),
    ]


def _ep(statuses, req, resp, count=1, query=()):
    return {"statuses": list(statuses), "requestTypes": list(req), "responseTypes": list(resp), "count": count,
            "queryParams": list(query)}


_JSON = "application/json"
_APP = "app.example.test"
_API = _APP + "/api"
HAR_ENDPOINTS = {
    "GET " + _APP + "/": _ep([200], [], ["text/html"]),
    "POST " + _API + "/session/login": _ep([201], [_JSON], [_JSON]),
    "GET " + _API + "/items": _ep([200], [], [_JSON], 1, ["api_key", "page", "q"]),
    "GET " + _API + "/items/{id}": _ep([200], [], [_JSON], 2),
    "GET " + _API + "/items/{id}/comments": _ep([200], [], [_JSON]),
    "GET " + _API + "/files/{id}": _ep([200], [], ["application/octet-stream"]),
    "GET " + _API + "/blobs/{id}": _ep([200], [], ["text/plain"]),
    "PUT " + _API + "/items/{id}": _ep([204], [_JSON], []),
    "DELETE " + _API + "/items/{id}": _ep([404], [], [_JSON]),
    "POST " + _API + "/auth/token": _ep([200], ["application/x-www-form-urlencoded"], [_JSON]),
    "GET analytics.vendor.test/collect": _ep([200], [], ["text/plain"], 1, ["cid", "v"]),
    "GET api.example.test/v1/status": _ep([200], [], [_JSON]),
    "GET " + _APP + "/settings": _ep([200], [], ["text/html"]),
    "GET " + _API + "/session/logout": _ep([200], [], [_JSON]),
}
_ITEM_SHAPE = {"id": "string", "title": "string", "tags": ["string"], "done": "boolean", "owner": {"id": "string"}}
HAR_SHAPES = [
    {"endpoint": "POST " + _API + "/session/login", "direction": "request",
     "shape": {"email": "string", "password": "string", "remember": "boolean"}},
    {"endpoint": "POST " + _API + "/session/login", "direction": "response",
     "shape": {"user": {"id": "string", "name": "string", "roles": ["string"]}, "token": "string", "expiresIn": "number"}},
    {"endpoint": "GET " + _API + "/items", "direction": "response",
     "shape": {"items": [_ITEM_SHAPE], "total": "number", "next": "null"}},
    {"endpoint": "GET " + _API + "/items/{id}", "direction": "response", "shape": _ITEM_SHAPE},
    {"endpoint": "GET " + _API + "/items/{id}/comments", "direction": "response",
     "shape": [{"id": "string", "text": "string", "at": "string"}]},
    {"endpoint": "PUT " + _API + "/items/{id}", "direction": "request", "shape": {"title": "string", "done": "boolean"}},
    {"endpoint": "DELETE " + _API + "/items/{id}", "direction": "response",
     "shape": {"error": {"code": "string", "message": "string"}}},
    {"endpoint": "POST " + _API + "/auth/token", "direction": "response",
     "shape": {"access_token": "string", "token_type": "string"}},
    {"endpoint": "GET api.example.test/v1/status", "direction": "response", "shape": {"ok": "boolean", "version": "string"}},
    {"endpoint": "GET " + _API + "/session/logout", "direction": "response", "shape": {"ok": "boolean"}},
]
HAR_AUTH_STEPS = [
    {"endpoint": "POST " + _API + "/session/login", "order": 1, "sets-cookie": True, "auth-scheme": None},
    {"endpoint": "POST " + _API + "/auth/token", "order": 2, "sets-cookie": False, "auth-scheme": "Basic"},
    {"endpoint": "GET " + _API + "/session/logout", "order": 3, "sets-cookie": True, "auth-scheme": "Bearer"},
]
HAR_NEVER_STORED = [
    SECRETS["jwt"], SECRETS["google-api-key"], SECRETS["stripe-live"], TOKEN, PASSWORD, COOKIE, URL_PASSWORD, BASIC,
    "tester-one-fixture-value", "Tester-One-fixture-value", "fixture-query-value", "abc123-fixture", "Cracked-tile-fixture-value",
    "wall-fixture-value", "Looks-bad-fixture-value", "Fix-tile-fixture-value", "Gone-fixture-value", "ok-fixture-value",
    "1.0.0-fixture-value",
]


def _har_facts():
    return _facts(
        {"web.endpoint": HAR_ENDPOINTS,
         "web.host": {"analytics.vendor.test": {}, "api.example.test": {}}},
        payload_shapes=HAR_SHAPES, auth_steps=HAR_AUTH_STEPS,
        pages=["https://app.example.test/", "https://app.example.test/settings"],
        secret_patterns=["bearer", "google-api-key", "jwt", "stripe-live"], never_stored=HAR_NEVER_STORED,
        entry_count=15, variant="normal")


def _har_doc(entries):
    return {"log": {"version": "1.2", "creator": {"name": "fixture-recorder", "version": "1.0"},
                    "pages": [{"startedDateTime": _iso(0), "id": "page_1", "title": "Snag", "pageTimings": {}}],
                    "entries": entries}}


def make_har(path, entries=None):
    """HAR 1.2 with auth headers, cookies, a password in a URL and JSON bodies carrying secret fixture values.

    With ``entries=None`` the default flow of ``default_har_entries()`` is written and the facts describe it
    (host `app.example.test` is the page host; `analytics.vendor.test` and `api.example.test` are third parties).
    With your own entries the facts only carry ``entry_count`` (derive nothing from custom entries).
    """
    if entries is None:
        return _write(path, _dump(_har_doc(default_har_entries()))), _har_facts()
    return _write(path, _dump(_har_doc(entries))), _facts(entry_count=len(entries), variant="custom")


# ----------------------------------------------------------------------------- source map

DEFAULT_MAP_SOURCES = [
    "webpack:///./src/index.tsx",
    "webpack:///./src/components/ItemList.tsx",
    "webpack:///./src/components/Header.tsx",
    "webpack:///./src/pages/Home.tsx",
    "webpack:///./node_modules/react/index.js",
    "webpack:///./node_modules/react-dom/index.js",
    "webpack:///./node_modules/next/dist/client/index.js",
    "webpack:///./node_modules/vue/dist/vue.runtime.js",
]
MAP_MARKER = "SOURCESCONTENT_MARKER_7f3a_fixture"


def default_map_contents():
    first = _lines(
        "// " + MAP_MARKER,
        'import React from "react";',
        'const routes = [{ path: "/dashboard", element: null }];',
        'axios.get("/api/items");',
        'const label = t("home.title");',
        'const key = "%s"; // fixture' % SECRETS["aws-key"],
    )
    return [first] + ["export const x%d = %d;\n" % (i, i) for i in range(1, len(DEFAULT_MAP_SOURCES))]


MAP_MODULES = {"src": {"count": 1}, "src/components": {"count": 2}, "src/pages": {"count": 1},
               "node_modules/react": {"count": 1}, "node_modules/react-dom": {"count": 1},
               "node_modules/next": {"count": 1}, "node_modules/vue": {"count": 1}}


def _module_key(source):
    s = source.split("://", 1)[-1].lstrip("/") if source.startswith("webpack:") else source
    s = s[2:] if s.startswith("./") else s
    return "/".join(s.split("/")[:-1][:2])


def make_sourcemap(path, sources=None, contents=None):
    """Source map v3. ``web.source-module`` key = the first two folder levels of a source (file name excluded; a file
    directly in `src/` gives `src`), data = `{"count": n}`. `sourcesContent` carries a marker and a secret that
    must never be stored (it goes to the bundle scanner in memory only)."""
    default = sources is None and contents is None
    sources = list(DEFAULT_MAP_SOURCES if sources is None else sources)
    contents = default_map_contents()[:len(sources)] if contents is None else list(contents)
    doc = {"version": 3, "file": "main.js", "sourceRoot": "", "sources": sources, "sourcesContent": contents,
           "names": ["React", "axios", "t"], "mappings": "AAAA;AACA,SAAS"}
    if default:
        facts = _facts({"web.source-module": MAP_MODULES,
                        "web.framework": {"next": {}, "react": {}, "vue": {}},
                        "web.api-call": {"GET /api/items": {}}, "web.i18n-key": {"home.title": {}},
                        "web.route": {"/dashboard": {}}},
                       secret_patterns=["aws-key"], never_stored=[MAP_MARKER, SECRETS["aws-key"]],
                       sources=len(sources), variant="normal")
    else:
        mods = {}
        for s in sources:
            k = _module_key(s)
            mods[k] = {"count": mods.get(k, {"count": 0})["count"] + 1}
        facts = _facts({"web.source-module": mods}, sources=len(sources), variant="custom")
    return _write(path, _dump(doc)), facts


# ----------------------------------------------------------------------------- bundle

BUNDLE_KINDS = {
    "web.route": {"/dashboard": {}, "/settings/profile": {}, "/reports": {}, "/vue-page": {}},
    "web.api-call": {"GET /api/items": {}, "POST /api/items": {}, "GET /api/legacy/ping": {}},
    "web.flag": {"new-checkout": {}, "dark-mode": {}, "beta-banner": {}, "promo-copy": {}, "fast-sync": {},
                 "legacyExport": {}},
    "web.i18n-key": {"home.title": {}, "home.subtitle": {}, "nav.settings": {}, "items.empty": {}},
    "web.framework": {"next": {}, "react": {}},
}
BUNDLE_AMBIGUOUS = {
    "fetch calls have no method in the literal pattern (key method unknown)": ["/api/search", "/api/export"],
    "absolute literal without a method": ["https://api.example.test/v2/orders"],
    "route with a parameter (templated or raw path?)": ["/items/:itemId"],
    "Angular relative path": ["admin"],
}
BUNDLE_MUST_NOT = {"web.i18n-key": ["x.y"], "web.api-call": ["GET /static/logo.png", "GET https://cdn.example.test/static/logo.png"]}
BUNDLE_SECRETS = ["aws-key", "bearer", "github-token", "slack", "stripe-live"]


def make_bundle(text="", style="spaced"):
    """Minified-looking JavaScript with routes, axios and fetch calls, flags, i18n keys and secrets.

    Returns ``(js_text, facts)``; nothing is written (use ``write_bundle``). ``text`` is appended verbatim.
    ``style="spaced"`` writes the literal forms of contract 6.5 (``path: "/x"``); ``"compact"`` removes the
    spaces after ``:`` and ``,`` and joins everything on one line (a tolerance fixture with the same facts).
    """
    sp = " " if style == "spaced" else ""
    lines = [
        "/*! synthetic bundle for tests */",
        "(function () {",
        'var routes = [{ path:%s"/dashboard", element:%sDashboard }, { path:%s"/settings/profile" }, { path:%s"/items/:itemId" }];' % (sp, sp, sp, sp),
        'var jsx = <Route path="/reports" element={Reports} />;',
        "var vueRoutes = [{ path:%s'/vue-page', component:%sVuePage }];" % (sp, sp),
        "var ngRoutes = [{ path:%s'admin', component:%sAdmin }];" % (sp, sp),
        'axios.get("/api/items"); axios.post("/api/items", body);',
        'XMLHttpRequest.open("GET", "/api/legacy/ping");',
        'fetch("/api/search"); fetch("/api/export");',
        'var abs = "https://api.example.test/v2/orders"; var logo = "https://cdn.example.test/static/logo.png";',
        'import * as LaunchDarkly from "launchdarkly-js-client-sdk";',
        'isEnabled("new-checkout"); useFlag("dark-mode"); useFeatureFlag("beta-banner"); getFeatureValue("promo-copy");',
        'client.variation("fast-sync", false); if (flags.legacyExport) { run(); }',
        't("home.title"); i18n.t("home.subtitle"); $t("nav.settings"); formatMessage({ id: "items.empty" });',
        'cat("x.y"); str.split("a.b");',
        'window.__NEXT_DATA__ = {};',
        'var el = "<div data-reactroot>";',
        'var cfg = { aws: "%s", stripe: "%s", auth: "%s", slack: "%s", gh: "%s" };' % (
            SECRETS["aws-key"], SECRETS["stripe-live"], SECRETS["bearer"], SECRETS["slack"], SECRETS["github-token"]),
        "})();",
    ]
    js = ("\n".join(lines) if style == "spaced" else "".join(lines)) + "\n"
    js += text
    facts = _facts(BUNDLE_KINDS, ambiguous=BUNDLE_AMBIGUOUS, must_not=BUNDLE_MUST_NOT, secret_patterns=BUNDLE_SECRETS,
                   never_stored=[SECRETS[k] for k in ("aws-key", "stripe-live", "slack", "github-token")] + [TOKEN],
                   style=style, variant="normal")
    return js, facts


def write_bundle(path, text="", style="spaced"):
    js, facts = make_bundle(text, style)
    return _write(path, js), facts


HERMES_MAGIC = bytes([0xC6, 0x1F, 0xBC, 0x03, 0xC1, 0x03, 0x19, 0x1F])


def make_hermes_bundle(path):
    """Compiled Hermes bytecode stand-in: the magic then junk. Reported present, never decoded (decision 5)."""
    return _write(path, HERMES_MAGIC + _lcg_bytes(256, seed=7)), _facts(
        {"web.framework": {"hermes": {}}, "mobile.framework": {"hermes": {}}},
        ambiguous={"expected limitation": "Hermes bytecode present, not decoded"}, variant="hermes")


# ----------------------------------------------------------------------------- saved site (folder of web files)

SITE_FILES_FACTS = {
    "web.route": {"/": {}, "/about": {}},
    "web.framework": {"next": {}},
}


def make_site(root, variant="normal"):
    """A saved Next.js site: index.html with `__NEXT_DATA__`, `_next/static/abc/_buildManifest.js`, a bundle and
    its map. Variants: normal, empty (an empty folder)."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if variant == "empty":
        return root, _facts(variant="empty")
    html = _lines(
        "<!doctype html>", "<html><head><title>Snag</title></head><body>",
        '<div id="__next"></div>',
        '<script id="__NEXT_DATA__" type="application/json">{"page":"/blog/[slug]","query":{},"buildId":"abc"}</script>',
        '<script src="/static/main.js"></script>', "</body></html>")
    manifest = ('self.__BUILD_MANIFEST = {"/": ["static/chunks/pages/index.js"], "/about": ["static/chunks/pages/about.js"], '
                '"/blog/[slug]": ["static/chunks/pages/blog.js"], sortedPages: ["/", "/about", "/blog/[slug]"]};\n')
    _write(root / "index.html", html)
    _write(root / "_next" / "static" / "abc" / "_buildManifest.js", manifest)
    _, bundle_facts = write_bundle(root / "static" / "main.js")
    _, map_facts = make_sourcemap(root / "static" / "main.js.map")
    kinds = {k: dict(v) for k, v in bundle_facts["kinds"].items()}
    for k, v in SITE_FILES_FACTS.items():
        kinds.setdefault(k, {}).update(v)
    kinds["web.framework"].update({"react": {}, "vue": {}})
    for k, v in map_facts["kinds"].items():
        kinds.setdefault(k, {}).update(v)
    return root, _facts(kinds, files=["_next/static/abc/_buildManifest.js", "index.html", "static/main.js", "static/main.js.map"],
                        ambiguous={"Next page keys with a parameter": ["/blog/[slug] (templated as /blog/{slug}?)"]},
                        variant="normal")


# ----------------------------------------------------------------------------- SQLite

DEFAULT_DDL = [
    "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, api_token TEXT, created_at TEXT DEFAULT 'none')",
    "CREATE TABLE items (id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id), title TEXT NOT NULL, "
    "done BOOLEAN NOT NULL DEFAULT 0, price NUMERIC(10,2), extra)",
    "CREATE TABLE tags (item_id INTEGER NOT NULL, tag VARCHAR(40) NOT NULL, FOREIGN KEY (item_id) REFERENCES items(id))",
    "CREATE INDEX idx_items_owner ON items (owner_id)",
    "CREATE TRIGGER trg_items_done AFTER UPDATE OF done ON items BEGIN UPDATE users SET created_at = created_at "
    "WHERE id = NEW.owner_id; END",
    "CREATE VIEW open_items AS SELECT id, title FROM items WHERE done = 0",
    "INSERT INTO users (id, name, api_token) VALUES (1, 'fixture-row-user', '%s')" % SECRETS["aws-key"],
    "INSERT INTO items (id, owner_id, title, done, price, extra) VALUES (1, 1, 'SECRET-ROW-TITLE-fixture', 0, 9.5, 'x')",
    "INSERT INTO tags (item_id, tag) VALUES (1, 'row-tag-fixture')",
]
SQLITE_KINDS = {
    "data.table": {
        "users": {"columns": [{"name": "id", "type": "INTEGER"}, {"name": "name", "type": "TEXT"},
                              {"name": "api_token", "type": "TEXT"}, {"name": "created_at", "type": "TEXT"}]},
        "items": {"columns": [{"name": "id", "type": "INTEGER"}, {"name": "owner_id", "type": "INTEGER"},
                              {"name": "title", "type": "TEXT"}, {"name": "done", "type": "BOOLEAN"},
                              {"name": "price", "type": "NUMERIC(10,2)"}, {"name": "extra", "type": ""}]},
        "tags": {"columns": [{"name": "item_id", "type": "INTEGER"}, {"name": "tag", "type": "VARCHAR(40)"}]},
    },
    "data.index": {"idx_items_owner": {}},
    "data.trigger": {"trg_items_done": {}},
    "data.view": {"open_items": {}},
}
SQLITE_ROWS = {"users": 1, "items": 1, "tags": 1}
SQLITE_NEVER = [SECRETS["aws-key"], "fixture-row-user", "SECRET-ROW-TITLE-fixture", "row-tag-fixture"]


def sqlite_schema_facts(path):
    """Schema facts read with the sqlite3 module itself (never with lane code): the oracle for custom DDL."""
    con = sqlite3.connect(str(path))
    try:
        kinds = {"data.table": {}, "data.index": {}, "data.trigger": {}, "data.view": {}}
        for typ, name in con.execute("SELECT type, name FROM sqlite_master ORDER BY name"):
            if name.startswith("sqlite_"):
                continue
            if typ == "table":
                cols = [{"name": c[1], "type": c[2]} for c in con.execute('PRAGMA table_info("%s")' % name)]
                kinds["data.table"][name] = {"columns": cols}
            else:
                kinds["data." + typ][name] = {}
        return kinds
    finally:
        con.close()


def make_sqlite(path, ddl=None):
    """SQLite database: three tables (one column with a precision type and one with no type), an index, a trigger, a
    view and one row per table holding fake secret values that must never appear in output.

    The file bytes can vary with the SQLite library version (header version numbers, free-page layout), so tests must
    compare the schema (`sqlite_schema_facts`) and the rows, never the bytes. With custom ``ddl`` the facts are read
    back from the database by ``sqlite_schema_facts``.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.unlink()
    con = sqlite3.connect(str(p))
    try:
        con.execute("PRAGMA page_size=4096")
        con.execute("PRAGMA journal_mode=DELETE")
        for stmt in (DEFAULT_DDL if ddl is None else ddl):
            con.execute(stmt)
        con.commit()
    finally:
        con.close()
    if ddl is None:
        return p, _facts(SQLITE_KINDS, row_counts=SQLITE_ROWS, never_stored=SQLITE_NEVER, variant="normal")
    return p, _facts(sqlite_schema_facts(p), variant="custom")


# ----------------------------------------------------------------------------- plist

DEFAULT_PLIST = {
    "CFBundleIdentifier": "test.example.snag",
    "CFBundleName": "Snag",
    "CFBundleShortVersionString": "1.4.2",
    "CFBundleVersion": "142",
    "MinimumOSVersion": "15.0",
    "NSCameraUsageDescription": "Fixture text: the camera photographs defects.",
    "UIBackgroundModes": ["fetch", "remote-notification"],
    "CFBundleURLTypes": [{"CFBundleURLName": "test.example.snag", "CFBundleURLSchemes": ["snag"]}],
    "NSAppTransportSecurity": {"NSAllowsArbitraryLoads": True,
                               "NSExceptionDomains": {"localhost-fixture": {"NSExceptionAllowsInsecureHTTPLoads": True}}},
    "ApiKey": SECRETS["aws-key"],
    "LaunchedAt": datetime.datetime(2026, 1, 1, 0, 0, 0),
    "Blob": b"\x00\x01\x02",
    "Ratio": 0.5,
    "Count": 3,
}
PLIST_KINDS = {"data.plist-key": {
    "ApiKey": {"type": "string"}, "Blob": {"type": "data"}, "CFBundleIdentifier": {"type": "string"},
    "CFBundleName": {"type": "string"}, "CFBundleShortVersionString": {"type": "string"},
    "CFBundleURLTypes": {"type": "array"}, "CFBundleVersion": {"type": "string"}, "Count": {"type": "integer"},
    "LaunchedAt": {"type": "date"}, "MinimumOSVersion": {"type": "string"},
    "NSAppTransportSecurity": {"type": "dict"},
    "NSAppTransportSecurity.NSAllowsArbitraryLoads": {"type": "bool"},
    "NSAppTransportSecurity.NSExceptionDomains": {"type": "dict"},
    "NSAppTransportSecurity.NSExceptionDomains.localhost-fixture": {"type": "dict"},
    "NSAppTransportSecurity.NSExceptionDomains.localhost-fixture.NSExceptionAllowsInsecureHTTPLoads": {"type": "bool"},
    "NSCameraUsageDescription": {"type": "string"}, "Ratio": {"type": "real"}, "UIBackgroundModes": {"type": "array"},
}}
PLIST_AMBIGUOUS = {
    "paths below an array (the notation for array elements is not fixed; chosen here: first element, `[]` suffix)":
        ["UIBackgroundModes[]", "CFBundleURLTypes[]", "CFBundleURLTypes[].CFBundleURLName",
         "CFBundleURLTypes[].CFBundleURLSchemes", "CFBundleURLTypes[].CFBundleURLSchemes[]"],
}
PLIST_NEVER = [SECRETS["aws-key"], "Fixture text: the camera photographs defects.", "test.example.snag"]


def make_plist(path, fmt="xml", data=None):
    """Property list, XML (`fmt="xml"`) or binary (`"binary"`). Key paths use `.` between dict keys; no key in the
    default data contains a dot. Values (a fake key, usage text) must never be stored."""
    f = plistlib.FMT_XML if fmt == "xml" else plistlib.FMT_BINARY
    raw = plistlib.dumps(DEFAULT_PLIST if data is None else data, fmt=f, sort_keys=True)
    if data is None:
        facts = _facts(PLIST_KINDS, ambiguous=PLIST_AMBIGUOUS, never_stored=PLIST_NEVER,
                       secret_patterns=["aws-key"], format=fmt, variant="normal")
    else:
        facts = _facts(format=fmt, variant="custom")
    return _write(path, raw), facts


# ----------------------------------------------------------------------------- OpenAPI (JSON and YAML)


def _op(op_id, tags, summary="fixture"):
    return {"operationId": op_id, "tags": list(tags), "summary": summary, "responses": {"200": {"description": "ok"}}}


def default_openapi_doc():
    item = {"type": "object", "properties": {"id": {"type": "string"}, "title": {"type": "string"},
                                              "token": {"type": "string", "example": SECRETS["jwt"]}}}
    return {
        "openapi": "3.0.3",
        "info": {"title": "Snag fixture API", "version": "1.0.0", "description": "Synthetic. Key " + SECRETS["aws-key"]},
        "servers": [{"url": "https://svc:%s@api.example.test/v1" % URL_PASSWORD}],  # scan-private: allow
        "paths": {
            "/items": {"parameters": [{"name": "X-Trace", "in": "header", "schema": {"type": "string"}}],
                       "get": _op("listItems", ["items"]), "post": _op("createItem", ["items"])},
            "/items/{itemId}": {"summary": "One item", "get": _op("getItem", ["items"]),
                                "put": _op("updateItem", ["items"]), "delete": _op("deleteItem", ["items"])},
            "/session": {"post": _op("createSession", ["auth"])},
            "/health": {"get": _op("health", [])},
        },
        "components": {
            "schemas": {"Item": item, "NewItem": {"type": "object"}, "Error": {"type": "object"}},
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"},
                                "apiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}},
        },
    }


def _endpoints(doc):
    out = {}
    for p, item in doc["paths"].items():
        for m, op in item.items():
            if m in ("get", "put", "post", "delete", "patch", "head", "options"):
                out["%s %s" % (m.upper(), p)] = {"operationId": op["operationId"], "tags": list(op["tags"])}
    return out


OPENAPI_KINDS = {
    "data.endpoint": {
        "GET /items": {"operationId": "listItems", "tags": ["items"]},
        "POST /items": {"operationId": "createItem", "tags": ["items"]},
        "GET /items/{itemId}": {"operationId": "getItem", "tags": ["items"]},
        "PUT /items/{itemId}": {"operationId": "updateItem", "tags": ["items"]},
        "DELETE /items/{itemId}": {"operationId": "deleteItem", "tags": ["items"]},
        "POST /session": {"operationId": "createSession", "tags": ["auth"]},
        "GET /health": {"operationId": "health", "tags": []},
    },
    "data.schema-type": {"Error": {}, "Item": {}, "NewItem": {}},
    "data.security": {"apiKeyAuth": {"type": "apiKey"}, "bearerAuth": {"type": "http"}},
}
OPENAPI_NEVER = [URL_PASSWORD, SECRETS["jwt"], SECRETS["aws-key"]]
OPENAPI_NOT_ENDPOINTS = ["/items parameters", "/items/{itemId} summary"]   # path-item keys that are not methods


def make_openapi_json(path, doc=None, swagger=False):
    """OpenAPI 3 JSON (or Swagger 2 with ``swagger=True``: `definitions`, no security). The server URL carries a
    password and an example carries a fake JWT. Path-level `parameters` and `summary` are not methods."""
    if doc is not None:
        return _write(path, _dump(doc)), _facts({"data.endpoint": _endpoints(doc)}, variant="custom")
    d = default_openapi_doc()
    if swagger:
        d = {"swagger": "2.0", "info": d["info"], "host": "api.example.test", "basePath": "/v1", "paths": d["paths"],
             "definitions": d["components"]["schemas"]}
        kinds = {"data.endpoint": OPENAPI_KINDS["data.endpoint"], "data.schema-type": OPENAPI_KINDS["data.schema-type"]}
        return _write(path, _dump(d)), _facts(kinds, never_stored=OPENAPI_NEVER, variant="swagger")
    return _write(path, _dump(d)), _facts(OPENAPI_KINDS, never_stored=OPENAPI_NEVER, not_endpoints=OPENAPI_NOT_ENDPOINTS,
                                          variant="normal")


YAML_ENDPOINT_KEYS = ["GET /items", "POST /items", "GET /items/{itemId}", "PUT /items/{itemId}", "DELETE /items/{itemId}",
                      "POST /session", "GET /health"]


def _yaml_text():
    return _lines(
        "openapi: 3.0.3",
        "info:",
        "  title: Snag fixture API",
        "  version: 1.0.0",
        "servers:",
        "  - url: https://svc:%s@api.example.test/v1" % URL_PASSWORD,  # scan-private: allow
        "paths:",
        "  /items:",
        "    parameters:",
        "      - name: X-Trace",
        "        in: header",
        "    get:",
        "      operationId: listItems",
        "      tags: [items]",
        "    post:",
        "      operationId: createItem",
        "  /items/{itemId}:",
        "    summary: One item",
        "    get:",
        "      operationId: getItem",
        "    put:",
        "      operationId: updateItem",
        "    delete:",
        "      operationId: deleteItem",
        "  /session:",
        "    post:",
        "      operationId: createSession",
        '  "/quoted":',
        "    get:",
        "      operationId: quoted",
        "  /health:",
        "    get:",
        "      operationId: health",
        "components:",
        "  schemas:",
        "    get:",
        "      type: object",
        "    delete:",
        "      type: object",
        "  securitySchemes:",
        "    bearerAuth:",
        "      type: http",
        "x-secret-example: " + SECRETS["bearer"],
    )


def make_openapi_yaml(path, variant="normal"):
    """OpenAPI YAML for the line scanner of 6.8. Traps: path-level `parameters:` and `summary:` (not methods); schemas
    named `get` and `delete` under `components` (must not become methods of the last path); a quoted path key
    (`"/quoted":`) that the line scanner does not match. Variants: normal, crlf (same facts, CRLF line endings),
    empty (`paths: {}`)."""
    if variant == "empty":
        return _write(path, "openapi: 3.0.3\ninfo:\n  title: Empty\n  version: 1.0.0\npaths: {}\n"), _facts(variant="empty")
    text = _yaml_text()
    if variant == "crlf":
        text = text.replace("\n", "\r\n")
    kinds = {"data.endpoint": {k: {} for k in YAML_ENDPOINT_KEYS}}
    return _write(path, text), _facts(
        kinds, never_stored=[URL_PASSWORD, TOKEN], secret_patterns=["bearer"],
        not_extracted=["GET /quoted"], not_endpoints=["GET /items/{itemId} via components.schemas.get"],
        limitations=["YAML read by a line scanner"], variant=variant)


# ----------------------------------------------------------------------------- GraphQL

GRAPHQL_SDL = _lines(
    "schema {", "  query: Query", "  mutation: Mutation", "  subscription: Subscription", "}",
    "", '"""Synthetic schema."""', "scalar DateTime", "",
    "interface Node {", "  id: ID!", "}", "",
    "type Item implements Node {", "  id: ID!", "  title: String!", "  done: Boolean!", "  createdAt: DateTime", "}", "",
    "type User implements Node {", "  id: ID!", "  name: String!", "}", "",
    "input NewItem {", "  title: String!", "}", "",
    "enum Status {", "  OPEN", "  DONE", "}", "",
    "union SearchResult = Item | User", "",
    "type Query {", "  items(first: Int = 10, status: Status): [Item!]!", "  item(id: ID!): Item", "  me: User",
    "  search(q: String!): [SearchResult!]!", "}", "",
    "type Mutation {", "  createItem(input: NewItem!): Item", "  deleteItem(id: ID!): Boolean!", "}", "",
    "type Subscription {", "  itemChanged(id: ID!): Item", "}",
)
GRAPHQL_KINDS = {
    "data.schema-type": {k: {} for k in ("Item", "Mutation", "NewItem", "Node", "Query", "SearchResult", "Status",
                                         "Subscription", "User")},
    "data.endpoint": {k: {} for k in ("mutation createItem", "mutation deleteItem", "query item", "query items",
                                      "query me", "query search", "subscription itemChanged")},
}


def _introspection():
    def t(kind, name, fields=None):
        d = {"kind": kind, "name": name}
        if fields is not None:
            d["fields"] = [{"name": f} for f in fields]
        return d
    types = [t("INTERFACE", "Node", ["id"]), t("OBJECT", "Item", ["id", "title", "done", "createdAt"]),
             t("OBJECT", "User", ["id", "name"]), t("INPUT_OBJECT", "NewItem"), t("ENUM", "Status"),
             t("UNION", "SearchResult"), t("SCALAR", "DateTime"),
             t("OBJECT", "Query", ["items", "item", "me", "search"]), t("OBJECT", "Mutation", ["createItem", "deleteItem"]),
             t("OBJECT", "Subscription", ["itemChanged"])]
    return {"data": {"__schema": {"queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"},
                                  "subscriptionType": {"name": "Subscription"}, "types": types}}}


def make_graphql(path, introspection=False):
    """GraphQL SDL (`.graphql`) or introspection JSON (`__schema`); both give the same facts. `scalar DateTime` and
    the `schema { }` block are not schema types in the 6.8 rule (`type|input|enum|interface|union`)."""
    if introspection:
        return _write(path, _dump(_introspection())), _facts(GRAPHQL_KINDS, variant="introspection")
    return _write(path, GRAPHQL_SDL), _facts(GRAPHQL_KINDS, not_types=["DateTime"], variant="normal")


# ----------------------------------------------------------------------------- proto

PROTO_TEXT = _lines(
    'syntax = "proto3";', "package example.snag.v1;", "",
    "message Item {", "  string id = 1;", "  string title = 2;", "}", "",
    "message NewItem {", "  string title = 1;", "}", "",
    "message GetItemRequest {", "  string id = 1;", "}", "",
    "message ListItemsRequest {", "  int32 page = 1;", "}", "",
    "enum Status {", "  STATUS_UNSPECIFIED = 0;", "  OPEN = 1;", "}", "",
    "service ItemService {",
    "  rpc GetItem(GetItemRequest) returns (Item);",
    "  rpc ListItems(ListItemsRequest) returns (stream Item);",
    "  rpc CreateItem(NewItem) returns (Item) {}",
    "  rpc Watch(stream GetItemRequest) returns (stream Item);",
    "}", "",
    "service HealthService {", "  rpc Check(GetItemRequest) returns (Item);", "}",
)
PROTO_KINDS = {
    "data.schema-type": {k: {} for k in ("GetItemRequest", "Item", "ListItemsRequest", "NewItem", "Status")},
    "data.rpc": {k: {} for k in ("HealthService.Check", "ItemService.CreateItem", "ItemService.GetItem",
                                 "ItemService.ListItems", "ItemService.Watch")},
}
PROTO_SIGNATURES = {"ItemService.GetItem": ("GetItemRequest", "Item"), "ItemService.ListItems": ("ListItemsRequest", "Item"),
                    "ItemService.CreateItem": ("NewItem", "Item"), "ItemService.Watch": ("GetItemRequest", "Item"),
                    "HealthService.Check": ("GetItemRequest", "Item")}


def make_proto(path):
    """proto3 file: four messages, an enum, two services; one rpc streams the response, one both ways, one has an
    empty body. Key of an rpc is `Service.Method`; `rpc_signatures` gives (input, output) for tests that want it."""
    return _write(path, PROTO_TEXT), _facts(PROTO_KINDS, rpc_signatures=PROTO_SIGNATURES, variant="normal")


# ----------------------------------------------------------------------------- source repository

_LANG_BY_EXT = {".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript",
                ".cjs": "JavaScript", ".py": "Python", ".rs": "Rust", ".go": "Go", ".rb": "Ruby", ".sql": "SQL",
                ".prisma": "Prisma"}


def _repo_files():
    """relpath -> text for the normal repository. Express and Fastify (JavaScript), Next.js (TypeScript, app router and
    pages), FastAPI, Flask and Django (Python), Axum (Rust), Rails (Ruby) and a Go module, plus tests, an import cycle,
    an alias import, unresolved imports, env reads and embedded fake secrets."""
    f = {}
    f["package.json"] = _dump({
        "name": "snag-fixture", "version": "0.1.0", "main": "src/server.js", "bin": {"snag": "bin/cli.js"},
        "scripts": {"start": "node src/server.js", "test": "node --test tests/"},
        "dependencies": {"express": "^4.18.0", "fastify": "^4.0.0", "next": "^14.0.0", "react": "^18.2.0",
                         "react-dom": "^18.2.0"},
        "devDependencies": {"typescript": "^5.0.0"}}).decode("ascii")
    f["tsconfig.json"] = _dump({"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}).decode("ascii")
    f["README.md"] = "# snag-fixture\n\nA synthetic repository for study-lane tests.\n"
    f["bin/cli.js"] = _lines("#!/usr/bin/env node", 'require("../src/server");')
    f["src/server.js"] = _lines(
        'const express = require("express");',
        'const itemsRouter = require("./routes/items");',
        'const { connect } = require("./db");',
        'const events = require("./events");',
        'const config = require("./config");',
        'const loadLazy = () => import("./lazy");',
        "const app = express();",
        "app.use(express.json());",
        'app.get("/health", (req, res) => res.json({ ok: true }));',
        "app.use(itemsRouter);",
        "connect(process.env.DATABASE_URL);",
        "app.listen(process.env.PORT || 3000);",
        "module.exports = { app, events, config, loadLazy };")
    f["src/routes/items.js"] = _lines(
        'const express = require("express");',
        'const { query } = require("../db");',
        'const { validateItem } = require("../lib/validate");',
        "const router = express.Router();",
        'router.get("/items", async (req, res) => res.json(await query("SELECT * FROM items")));',
        'router.post("/items", validateItem, async (req, res) => {',
        '  await query("INSERT INTO items (title) VALUES ($1)", [req.body.title]);',
        "  res.status(201).end();",
        "});",
        'router.get("/items/:itemId", async (req, res) => res.json(await query("SELECT * FROM items WHERE id = $1")));',
        'router.delete("/items/:itemId", async (req, res) => {',
        '  await query("DELETE FROM items WHERE id = $1", [req.params.itemId]);',
        "  res.status(204).end();",
        "});",
        "module.exports = router;")
    f["src/db.js"] = _lines(
        'const { Pool } = require("pg");',
        "const pool = new Pool({ connectionString: process.env.DATABASE_URL, max: Number(process.env[\"DB_POOL_SIZE\"]) });",
        "async function query(sql, params) { return pool.query(sql, params); }",
        'async function connect(url) { return query("SELECT id FROM users"); }',
        "module.exports = { query, connect };")
    f["src/events.js"] = _lines(
        'const fastify = require("fastify");',
        "const app = fastify();",
        'app.post("/events", async () => ({ ok: true }));',
        'app.get("/events/:eventId", async () => ({ id: 1 }));',
        "module.exports = app;")
    f["src/lazy.js"] = "module.exports = { lazy: true };\n"
    f["src/config.js"] = _lines(
        "module.exports = {",
        '  stripeKey: "%s",' % SECRETS["stripe-live"],
        '  awsKey: "%s",' % SECRETS["aws-key"],
        "  lowercase: process.env.lowercase_var,",
        "};")
    f["src/lib/validate.js"] = _lines(
        "function validateItem(req, res, next) {",
        "  if (process.env.STRICT_VALIDATION && !req.body.title) { return res.status(400).end(); }",
        "  next();",
        "}",
        "module.exports = { validateItem };")
    f["src/lib/util.ts"] = "export const slug = (s: string) => s.toLowerCase();\n"
    f["src/types.ts"] = "export interface Item { id: string; title: string }\n"
    f["src/api-client.ts"] = _lines(
        'import { slug } from "@/lib/util";',
        'import type { Item } from "./types";',
        'import "./missing-module";',
        "const BASE = process.env.NEXT_PUBLIC_API_URL;",
        "const VITE = import.meta.env.VITE_API_BASE;",
        'const AUTH = "%s";' % SECRETS["bearer"],
        "export async function fetchItems(): Promise<Item[]> {",
        '  return fetch(BASE + "/items", { headers: { Authorization: AUTH } }).then((r) => r.json());',
        "}",
        "export const itemSlug = (i: Item) => slug(i.id) + VITE;")
    f["src/cyc/a.js"] = 'const b = require("./b");\nmodule.exports = { b };\n'
    f["src/cyc/b.js"] = 'const a = require("./a");\nmodule.exports = { a };\n'
    f["src/routes/items.spec.js"] = 'const router = require("./items");\ntest("router", () => router);\n'
    f["tests/items.test.js"] = 'const router = require("../src/routes/items");\ntest("items", () => router);\n'
    f["tests/health.test.js"] = 'const { app } = require("../src/server");\ntest("health", () => app);\n'
    f["__tests__/page.test.tsx"] = 'import Home from "../app/page";\ntest("home", () => Home);\n'
    f["app/layout.tsx"] = "export default function RootLayout({ children }) { return <html><body>{children}</body></html>; }\n"
    f["app/page.tsx"] = _lines(
        'import { slug } from "@/lib/util";',
        'import { fetchItems } from "../src/api-client";',
        'const MAPS_KEY = "%s";' % SECRETS["google-api-key"],
        "export default async function Home() {",
        "  const items = await fetchItems();",
        "  return <main>{items.map((i) => slug(i.id))}</main>;",
        "}")
    for p, name in (("app/items/[id]/page.tsx", "Item"), ("app/(marketing)/about/page.tsx", "About"),
                    ("app/@modal/login/page.tsx", "Login"), ("app/docs/[...slug]/page.tsx", "Docs"),
                    ("app/shop/[[...parts]]/page.tsx", "Shop"), ("pages/legacy.tsx", "Legacy")):
        f[p] = "export default function %s() { return null; }\n" % name
    f["app/api/items/route.ts"] = _lines(
        "export async function GET() { return Response.json([]); }",
        "export async function POST(req: Request) { return Response.json({}, { status: 201 }); }")
    f["app/api/items/[id]/route.ts"] = "export async function DELETE() { return new Response(null, { status: 204 }); }\n"
    f["pages/api/ping.ts"] = "export default function handler(req, res) { res.status(200).json({ pong: true }); }\n"
    f["prisma/schema.prisma"] = _lines("model Note {", "  id    Int    @id", "  title String", '  @@map("notes")', "}")
    f["db/schema.sql"] = _lines(
        "CREATE TABLE users (id SERIAL PRIMARY KEY, name TEXT);",
        "CREATE TABLE items (id SERIAL PRIMARY KEY, title TEXT, owner_id INT);",
        "CREATE TABLE widgets (id SERIAL PRIMARY KEY, name TEXT);")
    f["config/settings.json"] = _dump({"slackToken": SECRETS["slack"], "region": "test-1"}).decode("ascii")
    f["pylib/__init__.py"] = ""
    f["pylib/mod.py"] = "def helper():\n    return 1\n"
    f["services/py/requirements.txt"] = "fastapi==0.110.0\nflask>=3.0\ndjango>=4.2\nuvicorn\n"
    f["services/py/api/__init__.py"] = ""
    f["services/py/api/models.py"] = "class Widget:\n    name: str\n"
    f["services/py/api/store.py"] = "def all_rows(sql):\n    return []\n"
    f["services/py/api/main.py"] = _lines(
        "import os",
        "import pylib.mod",
        "from fastapi import APIRouter, FastAPI",
        "from .models import Widget",
        "from .store import all_rows",
        "",
        "app = FastAPI()",
        "router = APIRouter()",
        'DB_URL = os.environ["WIDGET_DB_URL"]',
        'CACHE = os.environ.get("WIDGET_CACHE")',
        'DEBUG = os.getenv("PY_DEBUG")',
        "",
        "",
        '@app.get("/widgets")',
        "def list_widgets():",
        '    return all_rows("SELECT * FROM widgets")',
        "",
        "",
        '@router.post("/widgets")',
        "def create_widget(widget: Widget):",
        '    return all_rows("INSERT INTO widgets (name) VALUES (?)")',
        "",
        "",
        '@app.get("/widgets/{widget_id}")',
        "def get_widget(widget_id: int):",
        '    return {"id": widget_id, "helper": pylib.mod.helper()}',
        "",
        "",
        'if __name__ == "__main__":',
        "    import uvicorn",
        "    uvicorn.run(app)")
    f["services/py/flask_app.py"] = _lines(
        "import os",
        "from flask import Flask",
        "",
        "app = Flask(__name__)",
        'app.secret_key = os.environ["FLASK_SECRET_KEY"]',
        "",
        "",
        '@app.route("/flask/ping", methods=["GET", "POST"])',
        "def ping():",
        '    return "pong"',
        "",
        "",
        '@app.route("/flask/items/<int:item_id>", methods=["DELETE"])',
        "def drop(item_id):",
        '    return "", 204')
    f["services/py/polls/urls.py"] = _lines(
        "from django.urls import path",
        "from .views import detail, index, results",
        "",
        "urlpatterns = [",
        '    path("polls/", index),',
        '    path("polls/<int:poll_id>/", detail),',
        '    path("polls/<slug:name>/results/", results),',
        "]")
    f["services/py/polls/views.py"] = "def index(request): ...\n\n\ndef detail(request, poll_id): ...\n\n\ndef results(request, name): ...\n"
    f["services/py/settings.py"] = 'GITHUB_TOKEN = "%s"\n' % SECRETS["github-token"]
    f["services/py/test_widgets.py"] = "from .api.main import app\n\n\ndef test_app():\n    assert app\n"
    f["services/rust/Cargo.toml"] = _lines("[package]", 'name = "snag-fixture"', 'version = "0.1.0"', "", "[dependencies]",
                                           'axum = "0.7"')
    f["services/rust/src/main.rs"] = _lines(
        "use axum::{routing::{get, post}, Router};",
        "mod handlers;",
        "mod ghost;",
        "",
        "fn main() {",
        '    let port = std::env::var("AXUM_PORT").unwrap_or_default();',
        '    let tag = env!("BUILD_TAG");',
        "    let app = Router::new()",
        '        .route("/rust/health", get(handlers::health))',
        '        .route("/rust/users/:id", get(handlers::user))',
        '        .route("/rust/users", post(handlers::create));',
        "}")
    f["services/rust/src/handlers.rs"] = "pub async fn health() {}\npub async fn user() {}\npub async fn create() {}\n"
    f["services/rails/Gemfile"] = 'source "https://rubygems.org"\ngem "rails", "~> 7.1"\n'
    f["services/rails/config/routes.rb"] = _lines(
        "Rails.application.routes.draw do",
        '  get "/rails/widgets", to: "widgets#index"',
        '  post "/rails/widgets", to: "widgets#create"',
        '  get "/rails/widgets/:id", to: "widgets#show"',
        "end")
    f["services/rails/config/application.rb"] = 'SECRET = ENV["RAILS_MASTER_FIXTURE"]\n'
    f["services/go/go.mod"] = "module example.test/svc\n\ngo 1.21\n\nrequire github.com/gin-gonic/gin v1.9.1\n"
    f["services/go/main.go"] = _lines(
        "package main", "", "import (", '\t"example.test/svc/internal/util"', '\t"github.com/gin-gonic/gin"', ")", "",
        "func main() {", "\t_ = gin.New()", '\t_ = util.Slug("x")', "}")
    f["services/go/internal/util/util.go"] = "package util\n\nfunc Slug(s string) string { return s }\n"
    f["services/go/internal/util/util_test.go"] = "package util\n\nimport \"testing\"\n\nfunc TestSlug(t *testing.T) {}\n"
    return f


REPO_BINARY = {"assets/logo.png": b"\x89PNG\r\n\x1a\n" + _lcg_bytes(512, seed=99)}

REPO_ROUTES_BY_FILE = {
    "src/server.js": ["GET /health"],
    "src/routes/items.js": ["GET /items", "POST /items", "GET /items/{itemId}", "DELETE /items/{itemId}"],
    "src/events.js": ["POST /events", "GET /events/{eventId}"],
    "app/page.tsx": ["GET /"],
    "app/items/[id]/page.tsx": ["GET /items/{id}"],
    "app/(marketing)/about/page.tsx": ["GET /about"],
    "app/@modal/login/page.tsx": ["GET /login"],
    "app/docs/[...slug]/page.tsx": ["GET /docs/{slug*}"],
    "app/shop/[[...parts]]/page.tsx": ["GET /shop/{parts*}"],
    "app/api/items/route.ts": ["GET /api/items", "POST /api/items"],
    "app/api/items/[id]/route.ts": ["DELETE /api/items/{id}"],
    "pages/legacy.tsx": ["GET /legacy"],
    "pages/api/ping.ts": ["ANY /api/ping"],
    "services/py/api/main.py": ["GET /widgets", "POST /widgets", "GET /widgets/{widget_id}"],
    "services/py/flask_app.py": ["GET /flask/ping", "POST /flask/ping", "DELETE /flask/items/{item_id}"],
    "services/py/polls/urls.py": ["ANY /polls", "ANY /polls/{poll_id}", "ANY /polls/{name}/results"],
    "services/rust/src/main.rs": ["GET /rust/health", "GET /rust/users/{id}", "POST /rust/users"],
    "services/rails/config/routes.rb": ["GET /rails/widgets", "POST /rails/widgets", "GET /rails/widgets/{id}"],
}
REPO_IMPORTS = {
    "bin/cli.js": ["src/server.js"],
    "src/server.js": ["src/config.js", "src/db.js", "src/events.js", "src/lazy.js", "src/routes/items.js"],
    "src/routes/items.js": ["src/db.js", "src/lib/validate.js"],
    "src/api-client.ts": ["src/lib/util.ts", "src/types.ts"],
    "src/cyc/a.js": ["src/cyc/b.js"],
    "src/cyc/b.js": ["src/cyc/a.js"],
    "src/routes/items.spec.js": ["src/routes/items.js"],
    "tests/items.test.js": ["src/routes/items.js"],
    "tests/health.test.js": ["src/server.js"],
    "__tests__/page.test.tsx": ["app/page.tsx"],
    "app/page.tsx": ["src/api-client.ts", "src/lib/util.ts"],
    "services/py/api/main.py": ["pylib/mod.py", "services/py/api/models.py", "services/py/api/store.py"],
    "services/py/polls/urls.py": ["services/py/polls/views.py"],
    "services/py/test_widgets.py": ["services/py/api/main.py"],
    "services/rust/src/main.rs": ["services/rust/src/handlers.rs"],
}
REPO_UNRESOLVED = {"src/api-client.ts": ["./missing-module"], "services/rust/src/main.rs": ["ghost"]}
REPO_ENV_BY_FILE = {
    "src/server.js": ["DATABASE_URL", "PORT"], "src/db.js": ["DATABASE_URL", "DB_POOL_SIZE"],
    "src/lib/validate.js": ["STRICT_VALIDATION"], "src/api-client.ts": ["NEXT_PUBLIC_API_URL", "VITE_API_BASE"],
    "services/py/api/main.py": ["PY_DEBUG", "WIDGET_CACHE", "WIDGET_DB_URL"], "services/py/flask_app.py": ["FLASK_SECRET_KEY"],
    "services/rust/src/main.rs": ["AXUM_PORT", "BUILD_TAG"], "services/rails/config/application.rb": ["RAILS_MASTER_FIXTURE"],
}
REPO_TABLES_BY_FILE = {"src/routes/items.js": {"items": "write"}, "src/db.js": {"users": "read"},
                       "services/py/api/main.py": {"widgets": "write"}}
REPO_TESTS = ["__tests__/page.test.tsx", "services/go/internal/util/util_test.go", "services/py/test_widgets.py",
              "src/routes/items.spec.js", "tests/health.test.js", "tests/items.test.js"]
REPO_SECRETS = {"src/config.js": ["aws-key", "stripe-live"], "src/api-client.ts": ["bearer"], "app/page.tsx": ["google-api-key"],
                "services/py/settings.py": ["github-token"], "config/settings.json": ["slack"]}
REPO_TRACE = {
    "GET /items": {
        "seed_files": ["src/routes/items.js"],
        "files": [{"path": "src/routes/items.js", "role": "seed", "depth": 0},
                  {"path": "src/db.js", "role": "uses", "depth": 1},
                  {"path": "src/lib/validate.js", "role": "uses", "depth": 1},
                  {"path": "src/server.js", "role": "used-by", "depth": 1},
                  {"path": "bin/cli.js", "role": "used-by", "depth": 2}],
        "tests": ["src/routes/items.spec.js", "tests/health.test.js", "tests/items.test.js"],
        "env": ["DATABASE_URL", "DB_POOL_SIZE", "PORT", "STRICT_VALIDATION"],
        "tables": ["items", "users"],
    },
}
REPO_SEEDS = {"route": "GET /items", "path": "src/routes/items.js", "symbol": "validateItem",
              "literal": "SELECT * FROM items WHERE id", "missing": "GET /does-not-exist"}
REPO_SEED_FILES = {"route": ["src/routes/items.js"], "path": ["src/routes/items.js"],
                   "symbol": ["src/lib/validate.js"], "literal": ["src/routes/items.js"], "missing": []}


def _repo_kinds(files):
    langs = {}
    for p in sorted(files):
        lang = _LANG_BY_EXT.get(Path(p).suffix)
        if lang:
            langs[lang] = {"files": langs.get(lang, {"files": 0})["files"] + 1}
    routes = {k: {} for keys in REPO_ROUTES_BY_FILE.values() for k in keys}
    env = {k: {} for keys in REPO_ENV_BY_FILE.values() for k in keys}
    return {
        "source.language": langs,
        "source.framework": {k: {} for k in ("axum", "django", "express", "fastapi", "fastify", "flask", "gin", "next",
                                             "rails", "react")},
        "source.entry": {k: {} for k in ("app/layout.tsx", "bin/cli.js", "services/go/main.go", "services/py/api/main.py",
                                         "services/rust/src/main.rs", "src/server.js")},
        "source.route": routes,
        "source.env": env,
        "source.table": {k: {} for k in ("items", "notes", "users", "widgets")},
        "source.test": {t: {} for t in REPO_TESTS},
    }


def make_repo(path, variant="normal"):
    """A small multi-framework repository (see ``_repo_files``). Variants: ``normal``, ``empty`` (a README only) and
    ``malformed`` (truncated package.json and tsconfig, invalid UTF-8 and binary source, a one-line 1 MB script with a
    route at each end, a self-import).

    Facts of ``normal``: ``kinds`` (all the 6.4 kinds), ``graph`` (`imports`, `unresolved`, `routes`, `tests` as in
    graph.json; only entries with something to list), ``env_by_file``, ``tables_by_file``, ``secrets``
    (`secret.embedded` patterns per file), ``trace`` (expected `study trace "GET /items"`), ``seeds`` and
    ``seed_files`` (one seed of each form, 6.4 step 1), ``files`` (every path), ``binary`` (counted, not read).
    ``ambiguous``: bare package specifiers (`express`, `pg`) may or may not be listed in `unresolved`; the Go import
    `example.test/svc/internal/util` resolves to a package folder (which file?); trace depth of test files.
    """
    root = Path(path)
    root.mkdir(parents=True, exist_ok=True)
    if variant == "empty":
        _write(root / "README.md", "# empty\n")
        return root, _facts(files=["README.md"], variant="empty")
    if variant == "malformed":
        return _make_repo_malformed(root)
    if variant != "normal":
        raise KeyError(variant)
    files = _repo_files()
    for rel in sorted(files):
        _write(root / rel, files[rel])
    for rel in sorted(REPO_BINARY):
        _write(root / rel, REPO_BINARY[rel])
    graph = {"imports": REPO_IMPORTS, "unresolved": REPO_UNRESOLVED, "routes": REPO_ROUTES_BY_FILE, "tests": REPO_TESTS}
    never = [SECRETS[k] for k in ("aws-key", "stripe-live", "google-api-key", "github-token", "slack")] + [TOKEN]
    return root, _facts(
        _repo_kinds(files), graph=graph, env_by_file=REPO_ENV_BY_FILE, tables_by_file=REPO_TABLES_BY_FILE,
        secrets=REPO_SECRETS, secret_patterns=sorted({p for v in REPO_SECRETS.values() for p in v}), never_stored=never,
        trace=REPO_TRACE, seeds=REPO_SEEDS, seed_files=REPO_SEED_FILES, files=sorted(list(files) + list(REPO_BINARY)),
        binary=sorted(REPO_BINARY), must_not={"source.env": ["lowercase_var"]},
        ambiguous={"bare package specifiers in unresolved": ["express", "pg", "fastify", "fastapi", "flask", "axum"],
                   "Go import of a package folder": ["example.test/svc/internal/util"],
                   "trace depth of test files": REPO_TRACE["GET /items"]["tests"]},
        variant="normal")


def _make_repo_malformed(root):
    _write(root / "package.json", '{"name": "broken", "dependencies": {')
    _write(root / "tsconfig.json", '{"compilerOptions": {"paths": ')
    _write(root / "pyproject.toml", "[project\nname = ")
    _write(root / "go.mod", "this is not a go.mod\n")
    _write(root / "src" / "bad_utf8.js", b'const x = "\xff\xfe\x80";\napp.get("/after-bad-bytes", h);\n')
    _write(root / "src" / "binary.ts", _lcg_bytes(2048, seed=5))
    _write(root / "src" / "self.js", 'require("./self");\nrequire("./self.js");\n')
    filler = "".join("var a%d=function(){return %d};" % (i, i) for i in range(1, 30000))
    line = 'app.get("/start-of-line", h);' + filler + 'app.get("/end-of-line", h);'
    _write(root / "src" / "huge-minified.js", line)
    return root, _facts(
        {"source.route": {"GET /start-of-line": {}, "GET /end-of-line": {}}},
        files=["go.mod", "package.json", "pyproject.toml", "src/bad_utf8.js", "src/binary.ts", "src/huge-minified.js",
               "src/self.js", "tsconfig.json"],
        expect={"skip": ["malformed", "binary", "unsupported", None]},
        ambiguous={"route after invalid UTF-8 bytes": ["GET /after-bad-bytes"]},
        variant="malformed")


# ----------------------------------------------------------------------------- compare pair


def make_version_pair(root, kind="openapi"):
    """Two versions of one artifact for the compare lane: returns ``(path_v1, path_v2, facts)``.

    ``facts`` lists, by ``(kind, key)`` as in 6.9: ``added`` (v2 only), ``removed`` (v1 only), ``changed`` (both, data
    differs; with ``before`` and ``after``) and ``unchanged`` (a few facts present and equal in both). The two
    versions differ in nothing else that produces a fact, so a compare study has exactly these records and no more.
    Kinds: ``openapi`` (data lane) and ``har`` (web lane).
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if kind == "openapi":
        d1 = default_openapi_doc()
        d2 = default_openapi_doc()
        del d2["paths"]["/items/{itemId}"]["delete"]
        d2["paths"]["/items/{itemId}/archive"] = {"post": _op("archiveItem", ["items"])}
        d2["paths"]["/items"]["get"]["operationId"] = "listAllItems"
        d2["paths"]["/items"]["get"]["tags"] = ["items", "v2"]
        d2["components"]["schemas"]["ArchiveRequest"] = {"type": "object"}
        del d2["components"]["schemas"]["Error"]
        a, _ = make_openapi_json(root / "api-v1.json", doc=d1)
        b, _ = make_openapi_json(root / "api-v2.json", doc=d2)
        # make_openapi_json(doc=...) writes the doc as given; schemas and security are listed from the docs here
        facts = _facts(
            added=[{"kind": "data.endpoint", "key": "POST /items/{itemId}/archive"},
                   {"kind": "data.schema-type", "key": "ArchiveRequest"}],
            removed=[{"kind": "data.endpoint", "key": "DELETE /items/{itemId}"},
                     {"kind": "data.schema-type", "key": "Error"}],
            changed=[{"kind": "data.endpoint", "key": "GET /items",
                      "before": {"operationId": "listItems", "tags": ["items"]},
                      "after": {"operationId": "listAllItems", "tags": ["items", "v2"]}}],
            unchanged=[{"kind": "data.endpoint", "key": "POST /items"}, {"kind": "data.schema-type", "key": "Item"},
                       {"kind": "data.security", "key": "bearerAuth"}],
            artifact_kind="openapi", variant="pair")
        return a, b, facts
    if kind == "har":
        e1 = default_har_entries()
        e2 = [e for e in default_har_entries() if "/api/blobs/" not in e["request"]["url"]]
        e2.append(har_entry("GET", _ORIGIN + "/api/reports", 200, t=20, resp_mime="text/csv", resp_body="a,b\n"))
        e2.append(har_entry("GET", _ORIGIN + "/api/items?page=2&q=fixture-query-value&api_key=" + SECRETS["google-api-key"], 500,
                            t=21, resp_mime=""))
        a, _ = make_har(root / "flow-v1.har", e1)
        b, _ = make_har(root / "flow-v2.har", e2)
        before = dict(HAR_ENDPOINTS["GET " + _API + "/items"])
        after = dict(before, statuses=[200, 500], count=2)
        facts = _facts(
            added=[{"kind": "web.endpoint", "key": "GET " + _API + "/reports"}],
            removed=[{"kind": "web.endpoint", "key": "GET " + _API + "/blobs/{id}"}],
            changed=[{"kind": "web.endpoint", "key": "GET " + _API + "/items", "before": before, "after": after}],
            unchanged=[{"kind": "web.endpoint", "key": "POST " + _API + "/session/login"},
                       {"kind": "web.host", "key": "analytics.vendor.test"}],
            artifact_kind="har", variant="pair",
            note="the new reports response is CSV and the 500 has no body, so neither adds a payload shape")
        return a, b, facts
    raise ValueError("unknown pair kind: %r" % kind)


# ----------------------------------------------------------------------------- tier B: capture

CAPTURE_TYPED = ["tester-one-fixture-value", PASSWORD, "Option-fixture-value"]
CAPTURE_KINDS = {
    "flow.step": {"2 type field 'Email'": {}, "3 type field 'Password'": {}, "4 click button 'Sign in'": {},
                  "5 click link 'Items'": {}, "6 select dropdown 'Sort'": {}, "7 observe table 'Items'": {}},
    "flow.screen": {"Sign in": {}, "Dashboard": {}, "Items": {}},
    "web.endpoint": {"POST app.example.test/api/session": {}, "GET app.example.test/api/items/{id}": {}},
}
CAPTURE_SHAPES = [
    {"endpoint": "POST app.example.test/api/session", "direction": "request", "shape": {"email": "string", "password": "string"}},
    {"endpoint": "POST app.example.test/api/session", "direction": "response", "shape": {"user": {"id": "string"}}},
    {"endpoint": "GET app.example.test/api/items/{id}", "direction": "response", "shape": {"items": [{"id": "string"}]}},
]
CAPTURE_REPLACED = {"2": "<typed:%d chars>" % len("tester-one-fixture-value"), "3": "<typed:%d chars>" % len(PASSWORD),
                    "6": "<typed:%d chars>" % len("Option-fixture-value")}


def _capture_doc():
    return {
        "format": "app-blueprint-capture/1",
        "target": {"label": "Acme web app", "origin": "https://app.example.test"},
        "recordedWith": "fixture-recorder",
        "steps": [
            {"n": 1, "action": "navigate", "url": "https://app.example.test/login?next=fixture-query-value", "screen": "Sign in"},
            {"n": 2, "action": "type", "target": "field 'Email'", "value": "tester-one-fixture-value"},
            {"n": 3, "action": "type", "target": "field 'Password'", "value": PASSWORD},
            {"n": 4, "action": "click", "target": "button 'Sign in'", "screen": "Dashboard",
             "requests": [{"method": "POST", "url": "https://app.example.test/api/session?ref=fixture-query-value",
                           "status": 201, "requestShape": {"email": "string", "password": "string"},
                           "responseShape": {"user": {"id": "string"}}}],
             "note": "Lands on the dashboard"},
            {"n": 5, "action": "click", "target": "link 'Items'", "screen": "Items",
             "requests": [{"method": "GET", "url": "https://app.example.test/api/items/123", "status": 200,
                           "responseShape": {"items": [{"id": "string"}]}}]},
            {"n": 6, "action": "select", "target": "dropdown 'Sort'", "value": "Option-fixture-value"},
            {"n": 7, "action": "observe", "target": "table 'Items'", "screen": "Items"},
        ],
    }


def make_capture(path):
    """Tier B capture file (`app-blueprint-capture/1`, 6.10) with real-looking typed values (an email-like name and a
    password) that the importer must replace with `<typed:N chars>` (`replaced`), URLs with query values that must
    be dropped, and request/response shapes. ``ambiguous``: the key of the `navigate` step (URL or path)."""
    return _write(path, _dump(_capture_doc())), _facts(
        CAPTURE_KINDS, payload_shapes=CAPTURE_SHAPES, replaced=CAPTURE_REPLACED,
        never_stored=CAPTURE_TYPED + ["fixture-query-value"],
        ambiguous={"flow.step key of step 1 (navigate)": ["1 navigate /login", "1 navigate https://app.example.test/login"],
                   "flow.step data": ["screen", "next"]},
        variant="normal")


def _capture_variant(name):
    d = _capture_doc()
    if name == "wrong_format":
        d["format"] = "app-blueprint-capture/9"
    elif name == "bad_action":
        d["steps"][1]["action"] = "teleport"
    elif name == "steps_not_list":
        d["steps"] = {"n": 1}
    elif name == "step_missing_n":
        del d["steps"][0]["n"]
    elif name == "empty":
        d["steps"] = []
    else:
        raise KeyError(name)
    return d


# ----------------------------------------------------------------------------- tier C: rea bundle

REA_SHA = "0123456789abcdef" * 4          # 64 hex digits; obviously not a real digest
REA_NOTE = "imported from rea; not re-checked by app-blueprint"
REA_ABS_PATH = "/fixture/abs/Acme.apk"


def _rea_rec(evidence_id, operation, location, confidence, result, links=()):
    subject = {"name": "Acme.apk", "identity": {"sha256": REA_SHA}, "local_path": REA_ABS_PATH}
    return {"evidence_id": evidence_id, "operation": operation, "subject": subject, "authority": "static",
            "provider": {"name": "rea-fixture", "version": "0.0.0"}, "confidence": confidence,
            "normalized_result": result, "locations": [location], "limitations": ["fixture limitation"],
            "evidence_links": list(links)}


def _rea_doc():
    r1 = _rea_rec("ev-0001", "inspect_artifact", {"kind": "artifact-path", "path": "AndroidManifest.xml"}, "observed",
                  {"format": "apk", "entries": 42})
    r2 = _rea_rec("ev-0002", "search_strings", {"kind": "file-offset", "offset": 4096}, "high",
                  {"strings": ["https://api.example.test/v1/items"]}, [{"evidence_id": "ev-0001", "relation": "supports"}])
    r3 = _rea_rec("ev-0003", "procedure_info", {"kind": "address", "address": "0x401000"}, "derived",
                  {"name": "main", "size": 64}, [{"evidence_id": "ev-0002"}, {"evidence_id": "ev-9999"}])
    return {"evidence_format": 2, "records": [r1, r2, r3],
            "unknowns": [{"unknown_id": "u-1", "question": "What does the native library do?", "severity": "high",
                          "status": "open"}]}


REA_KINDS = {
    "rea.inspect-artifact": {"inspect_artifact:AndroidManifest.xml": {}},
    "rea.search-strings": {"search_strings:4096": {}},
    "rea.procedure-info": {"procedure_info:0x401000": {}},
    "rea.unknown": {"u-1": {"severity": "high", "status": "open"}},
}
REA_RECORDS = [   # expected mapping of 5.8, one entry per imported record
    {"kind": "rea.inspect-artifact", "key": "inspect_artifact:AndroidManifest.xml", "basis": "observed",
     "source": {"artifact": "Acme.apk", "digest": "sha256:" + REA_SHA, "path": "AndroidManifest.xml", "offset": None},
     "links": [], "limitations": ["fixture limitation", REA_NOTE], "coverage": "unknown", "by": "import:rea"},
    {"kind": "rea.search-strings", "key": "search_strings:4096", "basis": "inferred",
     "source": {"artifact": "Acme.apk", "digest": "sha256:" + REA_SHA, "path": None, "offset": 4096},
     "links": ["rea.inspect-artifact:inspect_artifact:AndroidManifest.xml"], "limitations": ["fixture limitation", REA_NOTE],
     "coverage": "unknown", "by": "import:rea"},
    {"kind": "rea.procedure-info", "key": "procedure_info:0x401000", "basis": "derived",
     "source": {"artifact": "Acme.apk", "digest": "sha256:" + REA_SHA, "path": None, "offset": None},
     "data_address": "0x401000", "links": ["rea.search-strings:search_strings:4096"],
     "links_dropped": ["ev-9999"], "limitations": ["fixture limitation", REA_NOTE], "coverage": "unknown", "by": "import:rea"},
]


def _rea_tolerant():
    big = {"blob": ["x" * 100] * 60}
    ok = _rea_rec("ev-t1", "inspect_artifact", {"kind": "artifact-path", "path": "res/values/strings.xml"}, "observed",
                  {"strings": 3})
    ok["future_field"] = {"anything": [1, 2, 3]}
    no_subject = _rea_rec("ev-t2", "list_strings", {"kind": "artifact-path", "path": "x"}, "observed", {})
    del no_subject["subject"]
    no_operation = _rea_rec("ev-t3", "x", {"kind": "artifact-path", "path": "x"}, "observed", {})
    del no_operation["operation"]
    bare = {"evidence_id": "ev-t4", "operation": "list_strings", "subject": {"name": "Acme.apk"}, "confidence": "weird",
            "normalized_result": {"n": 1}}
    huge = _rea_rec("ev-t5", "search_bytes", {"kind": "artifact-path", "path": "classes.dex"}, "observed", big)
    huge["locations"] = []
    str_links = _rea_rec("ev-t6", "xrefs", {"kind": "address", "address": "0x10"}, "observed", {"n": 2}, ["ev-t1"])
    return {"evidence_format": 3, "records": [ok, no_subject, no_operation, bare, huge, str_links], "extra": True}


def make_rea_bundle(path, variant="normal"):
    """rea evidence bundle (5.8; the record layout is partly unverified, so tolerant variants exist).

    Variants: ``normal`` (three records and one unknown; `records` holds the expected mapping per record),
    ``empty`` (no records), ``tolerant`` (`evidence_format` 3 and six records: unknown extra fields, a record without
    `subject`, one without `operation`, one with almost nothing and a confidence of ours not matching, one with a
    result far over 4 KB and no location, links given as plain strings; four import, two are `malformed`),
    ``not_rea`` (no `evidence_format`: not a rea bundle). The expected `kind` assumes `slug` turns `_` into `-`.
    """
    if variant == "normal":
        return _write(path, _dump(_rea_doc())), _facts(
            REA_KINDS, records=REA_RECORDS, counts={"imported": 3, "malformed": 0, "unknowns": 1},
            never_stored=[REA_ABS_PATH], variant="normal")
    if variant == "empty":
        return _write(path, _dump({"evidence_format": 2, "records": []})), _facts(
            counts={"imported": 0, "malformed": 0, "unknowns": 0}, variant="empty")
    if variant == "tolerant":
        kinds = {"rea.inspect-artifact": {"inspect_artifact:res/values/strings.xml": {}},
                 "rea.list-strings": {"list_strings:Acme.apk": {}}, "rea.search-bytes": {"search_bytes:Acme.apk": {}},
                 "rea.xrefs": {"xrefs:0x10": {}}}
        return _write(path, _dump(_rea_tolerant())), _facts(
            kinds, counts={"imported": 4, "malformed": 2, "unknowns": 0}, note="unrecognised evidence_format 3",
            basis={"rea.list-strings": "inferred"}, never_stored=[REA_ABS_PATH],
            ambiguous={"evidence_links given as plain strings": ["ev-t6 -> ev-t1"]}, variant="tolerant")
    if variant == "not_rea":
        doc = _rea_doc()
        del doc["evidence_format"]
        return _write(path, _dump(doc)), _facts(variant="not_rea")
    raise KeyError(variant)


# ----------------------------------------------------------------------------- tier C: JADX and apktool folders

_ANDROID_NS = "http://schemas.android.com/apk/res/android"


def _manifest_xml(doctype=False):
    head = '<?xml version="1.0" encoding="utf-8"?>\n'
    if doctype:
        head += '<!DOCTYPE manifest [<!ENTITY fixture "x">]>\n'
    return head + _lines(
        '<manifest xmlns:android="%s" android:versionCode="12" android:versionName="1.4.2" package="com.example.snag">' % _ANDROID_NS,
        '    <uses-sdk android:minSdkVersion="24" android:targetSdkVersion="34"/>',
        '    <uses-permission android:name="android.permission.INTERNET"/>',
        '    <uses-permission android:name="android.permission.CAMERA"/>',
        '    <application android:label="@string/app_name" android:usesCleartextTraffic="true" android:debuggable="true"'
        ' android:allowBackup="false" android:networkSecurityConfig="@xml/network_security_config">',
        '        <activity android:name=".MainActivity" android:exported="true">',
        "            <intent-filter>",
        '                <action android:name="android.intent.action.MAIN"/>',
        '                <category android:name="android.intent.category.LAUNCHER"/>',
        "            </intent-filter>",
        "        </activity>",
        '        <activity android:name="com.example.snag.DetailActivity" android:exported="false"/>',
        '        <activity android:name=".LinkActivity" android:exported="true">',
        '            <intent-filter android:autoVerify="true">',
        '                <action android:name="android.intent.action.VIEW"/>',
        '                <category android:name="android.intent.category.DEFAULT"/>',
        '                <category android:name="android.intent.category.BROWSABLE"/>',
        '                <data android:scheme="snag" android:host="items" android:pathPrefix="/open"/>',
        "            </intent-filter>",
        "        </activity>",
        '        <service android:name=".SyncService" android:exported="false"/>',
        '        <receiver android:name=".BootReceiver" android:exported="true"/>',
        '        <provider android:name=".FileProvider" android:authorities="com.example.snag.files" android:exported="false"/>',
        "    </application>",
        "</manifest>")


MANIFEST_KINDS = {
    "mobile.package": {"com.example.snag": {}},
    "mobile.permission": {"android.permission.CAMERA": {}, "android.permission.INTERNET": {}},
    "mobile.screen": {"com.example.snag.DetailActivity": {"exported": False, "launcher": False},
                      "com.example.snag.LinkActivity": {"exported": True, "launcher": False},
                      "com.example.snag.MainActivity": {"exported": True, "launcher": True}},
    "mobile.service": {"com.example.snag.BootReceiver": {"exported": True}, "com.example.snag.FileProvider": {"exported": False},
                       "com.example.snag.SyncService": {"exported": False}},
    "mobile.deeplink": {"snag://items/open": {"activity": "com.example.snag.LinkActivity", "autoVerify": True}},
}
MANIFEST_SETTINGS = {"usesCleartextTraffic": "true", "debuggable": "true", "allowBackup": "false",
                     "networkSecurityConfig": "@xml/network_security_config"}
ENDPOINT_LITERALS = ["https://api.example.test/v1/items", "/api/v2/ping", "https://example.test/docs/privacy",
                     "https://api.example.test/v1/session", "/v1/health"]
PACKAGE_HISTOGRAM = {"com.example.snag": 2, "com.example.snag.net": 1, "com.google.firebase": 1, "io.sentry": 1}


def _java_files(prefix):
    p = prefix
    return {
        p + "com/example/snag/MainActivity.java": _lines(
            "package com.example.snag;", "", "import com.google.firebase.FirebaseApp;", "",
            "public class MainActivity extends android.app.Activity {",
            '    static final String BASE = "https://api.example.test/v1/items";',
            '    static final String PING = "/api/v2/ping";',
            '    static final String DOCS = "https://example.test/docs/privacy";',
            '    static final String KEY = "%s";' % SECRETS["aws-key"], "}"),
        p + "com/example/snag/net/ApiClient.java": _lines(
            "package com.example.snag.net;", "", "public class ApiClient {",
            '    static final String SESSION = "https://api.example.test/v1/session";', "}"),
        p + "com/example/snag/Util.kt": _lines("package com.example.snag", "", 'const val HEALTH = "/v1/health"', "class Util"),
        p + "com/google/firebase/FirebaseApp.java": "package com.google.firebase;\n\npublic class FirebaseApp {}\n",
        p + "io/sentry/Sentry.java": "package io.sentry;\n\npublic class Sentry {}\n",
    }


def _locations(files, literals):
    """literal -> {"path", "line"} (first occurrence, 1-based) in a dict of relpath -> text."""
    out = {}
    for lit in literals:
        for rel in sorted(files):
            text = files[rel]
            if isinstance(text, str) and lit in text:
                line = next(i for i, ln in enumerate(text.split("\n"), 1) if lit in ln)
                out[lit] = {"path": rel, "line": line}
                break
    return out


_AXML_STUB = b"\x03\x00\x08\x00" + _lcg_bytes(64, seed=3)   # starts like a binary manifest; not text


def make_jadx_dir(root, variant="normal"):
    """JADX output folder (5.8). Variants: ``normal`` (`resources/AndroidManifest.xml` text manifest, `sources/**`),
    ``gradle`` (export-gradle layout `app/src/main/...`, same facts), ``empty`` (empty `sources/` and `resources/`),
    ``no_manifest``, ``no_sources``, ``doctype`` (manifest holds `<!DOCTYPE`/`<!ENTITY`: must be refused, sources
    still read), ``bad_manifest`` (truncated XML), ``binary_manifest`` (not text). Facts: manifest `kinds`,
    ``endpoints`` with ``locations`` (path and line of each literal), ``package_histogram`` (dotted names) and
    ``settings``; ``missing`` lists the expected parts a tolerant importer should report as not found.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if variant == "empty":
        (root / "sources").mkdir(exist_ok=True)
        (root / "resources").mkdir(exist_ok=True)
        return root, _facts(missing=["AndroidManifest.xml"], variant="empty")
    gradle = variant == "gradle"
    src_prefix = "app/src/main/java/" if gradle else "sources/"
    man_path = "app/src/main/AndroidManifest.xml" if gradle else "resources/AndroidManifest.xml"
    files = {} if variant == "no_sources" else _java_files(src_prefix)
    if variant in ("normal", "gradle", "no_sources"):
        files[man_path] = _manifest_xml()
    elif variant == "doctype":
        files[man_path] = _manifest_xml(doctype=True)
    elif variant == "bad_manifest":
        files[man_path] = _manifest_xml()[:300]
    elif variant == "binary_manifest":
        files[man_path] = _AXML_STUB
    elif variant != "no_manifest":
        raise KeyError(variant)
    for rel in sorted(files):
        _write(root / rel, files[rel])
    has_manifest = variant in ("normal", "gradle", "no_sources")
    has_sources = variant != "no_sources"
    kinds = {k: dict(v) for k, v in MANIFEST_KINDS.items()} if has_manifest else {}
    missing = [] if has_manifest else ["AndroidManifest.xml"]
    if not has_sources:
        missing.append("sources")
    facts = _facts(kinds, missing=missing, never_stored=[SECRETS["aws-key"]], variant=variant)
    if has_manifest:
        facts["settings"] = MANIFEST_SETTINGS
    if has_sources:
        facts["endpoints"] = ENDPOINT_LITERALS
        facts["locations"] = _locations(files, ENDPOINT_LITERALS)
        facts["package_histogram"] = PACKAGE_HISTOGRAM
        facts["secret_patterns"] = ["aws-key"]
        facts["ambiguous"] = {"mobile.sdk key form (dotted here; dex uses slashes)": sorted(PACKAGE_HISTOGRAM),
                              "mobile.setting key names": sorted(MANIFEST_SETTINGS) if has_manifest else []}
    return root, facts


APKTOOL_YML = _lines(
    "!!brut.androlib.meta.MetaInfo", "apkFileName: snag.apk", "compressionType: false", "doNotCompress:", "- resources.arsc",
    "isFrameworkApk: false", "packageInfo:", "  forcedPackageId: '127'", "sdkInfo:", "  minSdkVersion: '24'",
    "  targetSdkVersion: '34'", "sharedLibrary: false", "sparseResources: false", "usesFramework:", "  ids:", "  - 1",
    "version: 2.9.3", "versionInfo:", "  versionCode: '12'", "  versionName: 1.4.2")
APKTOOL_VALUES = {"minSdkVersion": "24", "targetSdkVersion": "34", "versionCode": "12", "versionName": "1.4.2"}
STRINGS_XML = _lines('<?xml version="1.0" encoding="utf-8"?>', "<resources>",
                     '    <string name="app_name">Snag-fixture-text</string>',
                     '    <string name="welcome">Welcome-fixture-text</string>',
                     '    <string name="empty_state">Nothing-here-fixture-text</string>', "</resources>")
SMALI_HISTOGRAM = {"com/example/snag": 1, "com/google/firebase": 1, "io/sentry": 1}


def _smali_files():
    main = _lines(
        ".class public Lcom/example/snag/MainActivity;", ".super Landroid/app/Activity;", "",
        ".method public onCreate()V", "    .locals 3",
        '    const-string v0, "https://api.example.test/v1/items"',
        '    const-string v1, "/api/v2/ping"',
        '    const-string v2, "%s"' % SECRETS["aws-key"],
        "    return-void", ".end method")
    return {
        "smali/com/example/snag/MainActivity.smali": main,
        "smali_classes2/com/google/firebase/FirebaseApp.smali": ".class public Lcom/google/firebase/FirebaseApp;\n.super Ljava/lang/Object;\n",
        "smali/io/sentry/Sentry.smali": ".class public Lio/sentry/Sentry;\n.super Ljava/lang/Object;\n",
    }


def make_apktool_dir(root, variant="normal"):
    """apktool output folder (5.8). Variants: ``normal`` (`apktool.yml`, text manifest, `res/values/strings.xml`, smali
    in two folders), ``empty`` (only an `apktool.yml` with no keys), ``no_manifest``, ``binary_manifest``,
    ``no_smali``, ``doctype``, ``bad_yml`` (garbage instead of YAML), ``unquoted`` (versions without quotes: same
    values). Facts: ``yml`` (the four values), ``strings_count`` (counts only, no UI text), ``endpoints`` with
    ``locations``, ``package_histogram`` (slash form, from `.class` lines)."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if variant == "empty":
        _write(root / "apktool.yml", "!!brut.androlib.meta.MetaInfo\n")
        return root, _facts(yml={}, missing=["AndroidManifest.xml", "smali", "res/values/strings.xml"], variant="empty")
    files = {"apktool.yml": APKTOOL_YML, "res/values/strings.xml": STRINGS_XML}
    if variant == "unquoted":
        files["apktool.yml"] = APKTOOL_YML.replace("'", "")
    elif variant == "bad_yml":
        files["apktool.yml"] = "{{{ not: [yaml\n"
    if variant not in ("no_manifest", "binary_manifest", "doctype"):
        files["AndroidManifest.xml"] = _manifest_xml()
    elif variant == "binary_manifest":
        files["AndroidManifest.xml"] = _AXML_STUB
    elif variant == "doctype":
        files["AndroidManifest.xml"] = _manifest_xml(doctype=True)
    if variant != "no_smali":
        files.update(_smali_files())
    if variant not in ("normal", "no_manifest", "binary_manifest", "doctype", "no_smali", "bad_yml", "unquoted"):
        raise KeyError(variant)
    for rel in sorted(files):
        _write(root / rel, files[rel])
    has_manifest = variant not in ("no_manifest", "binary_manifest", "doctype")
    kinds = {k: dict(v) for k, v in MANIFEST_KINDS.items()} if has_manifest else {}
    missing = [] if has_manifest else ["AndroidManifest.xml"]
    facts = _facts(kinds, yml={} if variant == "bad_yml" else APKTOOL_VALUES, strings_count=3, missing=missing,
                   never_stored=[SECRETS["aws-key"], "Snag-fixture-text", "Welcome-fixture-text", "Nothing-here-fixture-text"],
                   variant=variant)
    if variant != "no_smali":
        eps = ENDPOINT_LITERALS[:2]
        facts.update(endpoints=eps, locations=_locations(files, eps), package_histogram=SMALI_HISTOGRAM,
                     secret_patterns=["aws-key"])
    else:
        missing.append("smali")
    return root, facts


# ----------------------------------------------------------------------------- empty and malformed variants

_JSON_KINDS = ("har", "sourcemap", "openapi_json", "capture", "rea_bundle")
_COMMON = ["zero_bytes", "empty", "truncated", "invalid_utf8", "binary", "minified_1mb", "too_large"]
VARIANTS = {
    "har": _COMMON + ["deep", "deep_body", "huge_body", "bad_entries"],
    "sourcemap": _COMMON + ["deep", "outside_root", "bad_version"],
    "bundle": _COMMON + ["pathological_1mb"],
    "sqlite": ["zero_bytes", "empty", "truncated", "binary", "header_only"],
    "plist": ["zero_bytes", "empty", "truncated", "truncated_binary", "invalid_utf8", "binary", "deep", "many_keys",
              "minified_1mb", "too_large"],
    "openapi_json": _COMMON + ["deep"],
    "openapi_yaml": _COMMON + ["crlf"],
    "graphql": list(_COMMON),
    "proto": list(_COMMON),
    "capture": _COMMON + ["deep", "wrong_format", "bad_action", "steps_not_list", "step_missing_n"],
    "rea_bundle": _COMMON + ["deep", "tolerant", "not_rea"],
}


def _good(kind, path):
    if kind == "har":
        return make_har(path)
    if kind == "sourcemap":
        return make_sourcemap(path)
    if kind == "bundle":
        return write_bundle(path)
    if kind == "sqlite":
        return make_sqlite(path)
    if kind == "plist":
        return make_plist(path)
    if kind == "openapi_json":
        return make_openapi_json(path)
    if kind == "openapi_yaml":
        return make_openapi_yaml(path)
    if kind == "graphql":
        return make_graphql(path)
    if kind == "proto":
        return make_proto(path)
    if kind == "capture":
        return make_capture(path)
    if kind == "rea_bundle":
        return make_rea_bundle(path)
    raise KeyError(kind)


def _bad(variant, skips, **extra):
    """Facts of a variant that must emit nothing."""
    return _facts(variant=variant, expect={"skip": skips}, **extra)


def _empty_variant(kind, path):
    if kind == "har":
        return make_har(path, [])[0], _facts(variant="empty", entry_count=0)
    if kind == "sourcemap":
        return make_sourcemap(path, [], [])[0], _facts(variant="empty")
    if kind == "bundle":
        return _write(path, ""), _facts(variant="empty")
    if kind == "sqlite":
        return make_sqlite(path, ["CREATE TABLE gone (a)", "DROP TABLE gone"])[0], _facts(variant="empty")
    if kind == "plist":
        return make_plist(path, "xml", {})[0], _facts(variant="empty")
    if kind == "openapi_json":
        return make_openapi_json(path, {"openapi": "3.0.3", "info": {"title": "Empty", "version": "1"}, "paths": {}})[0], \
            _facts(variant="empty")
    if kind == "openapi_yaml":
        return make_openapi_yaml(path, "empty")
    if kind == "graphql":
        return _write(path, "# nothing here\n"), _facts(variant="empty")
    if kind == "proto":
        return _write(path, 'syntax = "proto3";\n'), _facts(variant="empty")
    if kind == "capture":
        return _write(path, _dump(_capture_variant("empty"))), _facts(variant="empty")
    if kind == "rea_bundle":
        return make_rea_bundle(path, "empty")
    raise KeyError(kind)


def _padded_good(kind, path, name):
    """Good fixture plus one 1 MB line of padding that carries no facts: the facts of the good fixture still hold."""
    pad = "a" * ONE_MB
    if kind in _JSON_KINDS:
        _, facts = _good(kind, path)
        doc = json.loads(Path(path).read_bytes().decode("utf-8"))
        (doc["log"] if kind == "har" else doc)["x-padding"] = pad
        _write(path, json.dumps(doc, separators=(",", ":")) + "\n")
    elif kind == "openapi_yaml":
        _, facts = _good(kind, path)
        _write(path, Path(path).read_bytes() + b"x-padding: " + pad.encode() + b"\n")
    elif kind == "graphql":
        _, facts = make_graphql(path)
        _write(path, Path(path).read_bytes() + b"# " + pad.encode() + b"\n")
    elif kind == "proto":
        _, facts = make_proto(path)
        _write(path, Path(path).read_bytes() + b"// " + pad.encode() + b"\n")
    elif kind == "plist":
        data = dict(DEFAULT_PLIST, Padding=pad)
        make_plist(path, "xml", data)
        facts = _facts({"data.plist-key": dict(PLIST_KINDS["data.plist-key"], Padding={"type": "string"})},
                       never_stored=PLIST_NEVER)
    elif kind == "bundle":
        js, facts = make_bundle()
        filler = "".join("var a%d=function(){return %d};" % (i, i) for i in range(1, 30000))
        js = js.rstrip("\n").replace("\n", " ") + filler + ' var z = { path: "/end-of-line" };'
        _write(path, js)
        facts = _facts(dict(facts["kinds"], **{"web.route": dict(facts["kinds"]["web.route"], **{"/end-of-line": {}})}),
                       ambiguous=facts["ambiguous"], never_stored=facts["never_stored"], secret_patterns=BUNDLE_SECRETS)
    else:
        raise KeyError(kind)
    facts = dict(facts, variant=name, single_line_bytes=Path(path).stat().st_size)
    return Path(path), facts


def make_variant(path, kind, name):
    """The empty and malformed cases of ``kind`` (see ``VARIANTS``). Returns ``(path, facts)``; ``facts["expect"]
    ["skip"]`` lists the skip reasons a lane may report (``None``: nothing emitted and no skip is also fine), and
    ``facts["kinds"]`` is ``{}`` unless the variant is valid and still holds facts (``minified_1mb``, ``crlf``,
    ``compact``, ``deep_body``, ``huge_body``, ``many_keys``, ``deep`` plist, ``outside_root`` ...)."""
    if name not in VARIANTS[kind]:
        raise KeyError("%s has no variant %r" % (kind, name))
    path = Path(path)
    json_kind = kind in _JSON_KINDS
    strict = ["malformed"] if json_kind else [None, "malformed"]
    if name == "empty":
        return _empty_variant(kind, path)
    if name == "zero_bytes":
        return _write(path, b""), _bad(name, ["malformed"] if json_kind or kind == "plist" else [None, "malformed", "unsupported"])
    if name == "binary":
        return _write(path, _lcg_bytes(4096)), _bad(name, ["binary", "malformed", "unsupported"] + ([None] if kind == "sqlite" else []))
    if name == "minified_1mb":
        return _padded_good(kind, path, name)
    if name == "pathological_1mb":
        p, facts = make_pathological_line(path, ONE_MB)
        return p, dict(facts, variant=name)
    if name == "too_large":
        if kind == "sqlite":
            raise KeyError(name)
        _, _ = _good(kind, path)
        data = Path(path).read_bytes()
        _write(path, data + b" " * (TEXT_CAP + 1 - len(data)))
        return path, _bad(name, ["too-large"], size=TEXT_CAP + 1)
    if name in ("truncated", "truncated_binary"):
        if kind == "plist" and name == "truncated_binary":
            make_plist(path, "binary")
        elif kind == "plist":
            make_plist(path, "xml")
        else:
            _good(kind, path)
        data = Path(path).read_bytes()
        cut = 2000 if kind == "sqlite" else max(len(data) * 6 // 10, 1)
        _write(path, data[:cut])
        skips = ["malformed"] if json_kind or kind in ("plist", "sqlite") else [None]
        return path, _bad(name, skips)
    if name == "invalid_utf8":
        _good(kind, path)
        data = Path(path).read_bytes()
        at = data.find(b"example")
        at = len(data) // 2 if at < 0 else at
        _write(path, data[:at] + b"\xff\xfe\xfa" + data[at:])
        return path, _bad(name, strict)
    if name == "deep":
        if kind == "plist":
            d = {"leaf": "x"}
            for i in range(7, -1, -1):
                d = {"k%d" % i: d}
            make_plist(path, "xml", d)
            paths = {".".join("k%d" % j for j in range(n)): {"type": "dict"} for n in range(1, 7)}
            return path, _facts({"data.plist-key": paths}, variant=name, max_depth=6,
                                must_not={"data.plist-key": ["k0.k1.k2.k3.k4.k5.k6"]})
        _write(path, _deep_json(50000))
        return path, _bad(name, ["malformed"], nesting=50000)
    if name == "many_keys":
        data = {"key%03d" % i: i for i in range(600)}
        make_plist(path, "xml", data)
        kept = {"key%03d" % i: {"type": "integer"} for i in range(500)}
        return path, _facts({"data.plist-key": kept}, variant=name, max_keys=500, total_keys=600)
    if name == "header_only":
        return _write(path, b"SQLite format 3\x00" + b"\x00" * 4080), _bad(name, ["malformed"])
    if name == "crlf":
        return make_openapi_yaml(path, "crlf")
    if name == "outside_root":
        sources = ["../../../outside-root/secret.js", "/srv/outside/app.js", "C:\\outside\\app.js", "webpack:///./src/ok.js"]
        doc = {"version": 3, "file": "main.js", "sourceRoot": "../../..", "sources": sources,
               "sourcesContent": ["// %s\n" % MAP_MARKER] + [None] * 3, "names": [], "mappings": "AAAA"}
        _write(path, _dump(doc))
        return path, _facts({"web.source-module": {"src": {"count": 1}}}, unsafe_sources=sources[:3], variant=name,
                            never_stored=[MAP_MARKER],
                            ambiguous={"module keys of the unsafe sources": sources[:3]},
                            expect={"skip": [None, "unsafe-path"], "note": "no file outside the root may be opened"})
    if name == "bad_version":
        _write(path, _dump({"version": 2, "sources": "not-a-list", "sourcesContent": 7, "mappings": 1}))
        return path, _bad(name, ["malformed", None])
    if name == "deep_body":
        e = har_entry("GET", _ORIGIN + "/api/deep", 200, t=0, resp_mime=_JSON, resp_body=_deep_json(50000))
        make_har(path, [e])
        return path, _facts({"web.endpoint": {"GET " + _API + "/deep": _ep([200], [], [_JSON])}}, variant=name,
                            payload_shapes=[], entry_count=1, nesting=50000, expect={"skip": [None, "malformed"]})
    if name == "huge_body":
        e = har_entry("GET", _ORIGIN + "/api/huge", 200, t=0, resp_mime=_JSON, resp_body='{"blob":"%s"}' % ("a" * 3_000_000))
        make_har(path, [e])
        return path, _facts({"web.endpoint": {"GET " + _API + "/huge": _ep([200], [], [_JSON])}}, variant=name,
                            payload_shapes=[{"endpoint": "GET " + _API + "/huge", "direction": "response",
                                             "shape": {"blob": "string"}}], entry_count=1, body_bytes=3_000_000)
    if name == "bad_entries":
        good = default_har_entries()[2]
        doc = _har_doc([{"startedDateTime": _iso(0)}, {"request": "nope", "response": 7}, good,
                        {"request": {"method": "GET"}, "response": {}}, None, 42])
        _write(path, _dump(doc))
        return path, _facts({"web.endpoint": {"GET " + _API + "/items": HAR_ENDPOINTS["GET " + _API + "/items"]}},
                            variant=name, entry_count=6, valid_entries=1, expect={"skip": [None, "malformed"]})
    if name in ("wrong_format", "bad_action", "steps_not_list", "step_missing_n"):
        _write(path, _dump(_capture_variant(name)))
        return path, _bad(name, ["malformed", None])
    if name == "tolerant":
        return make_rea_bundle(path, "tolerant")
    if name == "not_rea":
        return make_rea_bundle(path, "not_rea")
    raise KeyError(name)


def make_pathological_line(path, size=TEXT_CAP):
    """One line of exactly ``size`` bytes (default 5 MiB, the largest text file a lane reads) made of fragments that
    punish a careless regex: opened-but-unterminated `fetch(` and `t(` calls, long whitespace runs, quote runs and
    `path:` with no value. It holds no fact. A lane must finish it within 10 s (contract 6.2)."""
    frag = ('fetch("' + "a" * 300 + ' fetch(' + " " * 500 + "t(" * 200 + '"' * 100 + " path:" + " " * 300 + "/" * 200
            + "'`" * 50 + " ")
    reps = size // len(frag) + 1
    data = (frag * reps)[:size].encode("ascii")
    _write(path, data)
    return Path(path), _facts(variant="pathological", size=size, single_line=True, expect={"skip": [None]})


def make_too_large_text(path, size=TEXT_CAP + 1):
    """A plain-text file one byte over the 5 MiB read cap (`too-large`)."""
    _write(path, b"a" * size)
    return Path(path), _facts(variant="too_large", size=size, expect={"skip": ["too-large"]})

