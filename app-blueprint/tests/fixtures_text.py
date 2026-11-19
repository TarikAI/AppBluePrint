"""Synthetic text and data fixtures for the study lanes (contract 10.2, workstream W2-FX2).

Everything here is invented: names, hosts (`example.test`, `vendor.test`) and values. Nothing is downloaded or read
from a real app. The module uses the standard library only and runs on Python 3.10+ (Windows and Linux). Every
secret-looking value is obviously fake (it says "fixture") and is assembled in code from pieces, so no whole token
and no `user:password@host` string appears in this source file (no line needs an allow-marker comment for the
private-data scan, and no marker is ever written into a fixture).

Conventions (read these before using a builder)
-----------------------------------------------
* A file builder takes the destination path first and returns ``(path, facts)``; a folder builder returns
  ``(root, facts)``. ``path`` is a ``pathlib.Path``. Parent folders are created. Files are written as bytes with
  LF line endings and fixed timestamps, so two calls give byte-identical files on every platform and Python
  version. The one exception is SQLite: the file bytes can vary with the SQLite library version (the header holds the
  library version number), so tests must compare schema and rows, never bytes.
* ``facts`` is a plain dict. Its common keys:

  - ``"kinds"``: ``{evidence kind: {key: data}}``, the keys and data exactly as the lane tables of contract 6.4 to 6.10
    define them (``METHOD /path``, ``{name}`` parameters, ``{slug*}`` catch-alls). ``secret.embedded`` is included
    (key ``<pattern>@<path>``, ``.`` for a single-file target; data ``{"count": n}``) for every text file that holds
    a pattern; the counts come from the contract-6.2 regexes run over the decoded text of the written file.
  - ``"never_stored"``: every value (header, cookie, query value, body leaf, secret, typed text) that must appear in no
    stored record, excerpt, claim or data.
  - ``"secret_patterns"``: the ``SECRET_PATTERNS`` names that occur in the file (``[]`` for SQLite and binary plists,
    which are not scanned: 6.2 rule 5).
  - ``"ambiguous"``: reserved for points the contract leaves open; no fixture has one at present.
  - ``"expect"`` (empty and malformed variants): the outcome of the 6.2 reading rules. ``{"skip": [reasons]}``
    (``None`` in the list: nothing emitted and no skip is also right), or ``{"error": "target-unsupported" |
    "bad-capture"}`` for importers. A readable but damaged text has ``"coverage": "partial"`` and
    ``"limitations": [...]`` in the facts. No variant may raise inside a lane.
  - ``"variant"``: the variant name.
* ``make_variant(path, kind, name)`` builds the empty and malformed cases of every kind (``VARIANTS`` lists them):
  ``zero_bytes``, ``empty``, ``truncated``, ``invalid_utf8`` (damage away from every fact),
  ``invalid_utf8_structure`` (damage inside the syntax), ``binary``, ``minified_1mb`` (one 1 MB line), ``too_large``
  (exactly 5 MiB + 1 byte: the only fixture over the cap) and kind-specific cases (a source map pointing outside its
  root, a HAR with a gigantic or deeply nested body, ...). Expectations of scanned text kinds (bundle, YAML, GraphQL,
  proto) are the facts readable in the prefix; whole-parse formats (JSON, plist, SQLite) give the skip of the 6.2
  worked-example table.
* Folder builders (``make_repo``, ``make_jadx_dir``, ``make_apktool_dir``, ``make_site``) take ``variant=``.

Signatures chosen where contract 10.2 leaves them open
------------------------------------------------------
``make_har(path, entries=None)``                 entries: list of ``har_entry(...)`` dicts; None = the default flow
``make_sourcemap(path, sources=None, contents=None)``   sources/contents: lists, None = the defaults
``make_bundle(text="", style="spaced")``         returns ``(js_text, facts)`` and writes nothing; ``write_bundle`` writes
``make_bundle_edge(path)``                       framework markers and the negatives of 6.5
``make_sqlite(path, ddl=None)``                  ddl: list of SQL statements (the last ones may be INSERTs)
``make_plist(path, fmt="xml", data=None)``       fmt: "xml" or "binary"
``make_openapi_json(path, doc=None, swagger=False)``, ``make_openapi_yaml(path, variant="normal")``
``make_graphql(path, introspection=False)``, ``make_proto(path)``
``make_repo(path, variant="normal")``            variants: normal, empty, malformed
``make_version_pair(root, kind="openapi")``      returns ``(path_v1, path_v2, facts)`` with ``root/v1/<name>`` and
                                                 ``root/v2/<name>`` (same file name, so single-file keys are equal)
``make_capture(path)``, ``make_rea_bundle(path, variant)``, ``make_jadx_dir(root, variant)``,
``make_apktool_dir(root, variant)``
"""
import base64
import datetime
import json
import plistlib
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

TEXT_CAP = 5 * 1024 * 1024          # contract 6.2: text file read size; larger is `too-large`
ONE_MB = 1000 * 1000
UTF8_NOTE = "not valid UTF-8; undecodable bytes replaced"
UNCLOSED_NOTE = "input ends inside an unclosed block"
DEPTH_NOTE = "nested deeper than 6 levels; deeper levels not read"
HERMES_NOTE = "Hermes bytecode present; not decoded (tier C: study import --from rea)"

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
PEM_BODY = "FIXTURE-NOT-A-KEY"
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
    """Deterministic pseudo-random bytes that always start with a NUL (binary data where text is expected)."""
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


def _decode(data):
    """The 6.2 reading rule: drop a BOM, decode UTF-8 with replacement."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8", "replace")


def _secret_kinds(texts):
    """``secret.embedded`` facts: {path: decoded text} -> {"<pattern>@<path>": {"count": n}} (6.2)."""
    out = {}
    for path in sorted(texts):
        for name, rx in SECRET_REGEXES.items():
            n = len(re.findall(rx, texts[path]))
            if n:
                out["%s@%s" % (name, path)] = {"count": n}
    return out


def escaped_forms(values):
    """``values`` plus, for every value that JSON escapes (a newline, a quote, a backslash, non-ASCII), its JSON-escaped
    form `json.dumps(v)[1:-1]` (stored records are JSON), plus the body of the fake private key when its value is
    listed (contract 10.1 privacy check)."""
    out = list(dict.fromkeys(values))
    for v in list(out):
        for extra in (json.dumps(v)[1:-1], PEM_BODY if PEM_BODY in v else None):
            if extra and extra not in out:
                out.append(extra)
    return out


def _facts(kinds=None, **extra):
    f = {"kinds": {k: dict(v) for k, v in (kinds or {}).items()}}
    f.update(extra)
    if "never_stored" in f:
        f["never_stored"] = escaped_forms(f["never_stored"])
    return f


def _split_records(path_records, other=None):
    """Folder fixtures (contract 10.2): ``path_records`` is `{path: {kind: {key: data}}}`; returns the `kinds` dict
    holding ``other`` (records without a file) and only the keys that have a record in exactly one file."""
    seen = {}
    for path, kinds in path_records.items():
        for kind, keys in kinds.items():
            for key in keys:
                seen.setdefault((kind, key), []).append(path)
    out = {k: dict(v) for k, v in (other or {}).items()}
    for path, kinds in sorted(path_records.items()):
        for kind, keys in kinds.items():
            for key, data in keys.items():
                if len(seen[(kind, key)]) == 1:
                    out.setdefault(kind, {})[key] = data
    return out


def _with_secrets(facts, texts):
    """Add the `secret.embedded` records (and the matching `secret_patterns`) found in ``texts``."""
    sec = _secret_kinds(texts)
    if sec:
        facts["kinds"]["secret.embedded"] = sec
    facts["secret_patterns"] = sorted({k.split("@", 1)[0] for k in sec})
    return facts


def _text_facts(path, kinds, **extra):
    """Facts of a single-file text fixture: ``kinds`` plus the secrets of the file as written (key path `.`)."""
    facts = _facts(kinds, **extra)
    return _with_secrets(facts, {".": _decode(Path(path).read_bytes())})


# ----------------------------------------------------------------------------- HAR


def _hdrs(h):
    if not h:
        return []
    pairs = h.items() if isinstance(h, dict) else h
    return [{"name": k, "value": v} for k, v in pairs]


def har_entry(method, url, status=200, *, t=0, req_headers=None, req_body=None, req_mime=None, resp_headers=None,
              resp_body=None, resp_mime="", req_cookies=None, resp_cookies=None, resp_b64=False):
    """One HAR 1.2 entry. A dict body is JSON-encoded; a str body is used as it is; ``resp_b64`` base64-encodes the
    response body and sets `content.encoding`."""
    parts = urlsplit(url)
    request = {"method": method, "url": url, "httpVersion": "HTTP/1.1", "headers": _hdrs(req_headers),
               "queryString": [{"name": k, "value": v} for k, v in parse_qsl(parts.query, keep_blank_values=True)],
               "cookies": req_cookies or [], "headersSize": -1, "bodySize": 0}
    if req_body is not None:
        text = req_body if isinstance(req_body, str) else json.dumps(req_body, separators=(",", ":"))
        mime = req_mime or ("application/json" if not isinstance(req_body, str) else "text/plain")
        request["postData"] = {"mimeType": mime, "text": text}
        if mime.startswith("application/x-www-form-urlencoded"):
            request["postData"]["params"] = [{"name": k, "value": v} for k, v in parse_qsl(text, keep_blank_values=True)]
        request["bodySize"] = len(text)
    text = "" if resp_body is None else (resp_body if isinstance(resp_body, str) else json.dumps(resp_body, separators=(",", ":")))
    size = len(text)
    if resp_b64 and text:
        text = base64.b64encode(text.encode("utf-8")).decode("ascii")
    content = {"size": size, "mimeType": resp_mime}
    if text:
        content["text"] = text
    if resp_b64 and text:
        content["encoding"] = "base64"
    response = {"status": status, "statusText": "OK" if status < 400 else "Error", "httpVersion": "HTTP/1.1",
                "headers": _hdrs(resp_headers), "cookies": resp_cookies or [], "content": content,
                "redirectURL": "", "headersSize": -1, "bodySize": size}
    return {"startedDateTime": _iso(t), "time": 12, "request": request, "response": response, "cache": {},
            "timings": {"send": 0, "wait": 12, "receive": 0}}


def _html(title):
    return '<!doctype html><html><head><title>%s</title></head><body><div id="root"></div></body></html>' % title


_ORIGIN = "https://app.example.test"
_ITEM = {"id": "i-1", "title": "Cracked-tile-fixture-value", "done": False, "tagCount": 1}
_JSON = "application/json"
_JSON_UTF8 = "application/json; charset=utf-8"
_TREE = {"a": {"b": {"c": {"d": {"e": {"f": "tree-leaf-fixture-value"}}}}}}      # 6 levels: `"..."` from level 5 (6.3)


def default_har_entries():
    cookie_hdr = {"Cookie": "session=" + COOKIE}
    auth = {"Authorization": SECRETS["bearer"]}
    both = dict(cookie_hdr, **auth)
    session_cookie = [{"name": "session", "value": COOKIE, "path": "/", "httpOnly": True, "secure": True}]
    return [
        har_entry("GET", _ORIGIN + "/", 200, t=0, req_headers=cookie_hdr, resp_body=_html("Snag"),
                  resp_mime="text/html; charset=utf-8"),
        har_entry("POST", _ORIGIN + "/api/session/login", 201, t=1,
                  req_body={"email": "tester-one-fixture-value", "password": PASSWORD, "remember": True},
                  resp_headers=[("Set-Cookie", "session=%s; Path=/; HttpOnly; Secure" % COOKIE)],
                  resp_cookies=session_cookie, resp_mime=_JSON_UTF8,
                  resp_body={"user": {"id": "u-1", "name": "Tester-One-fixture-value"}, "token": SECRETS["jwt"],
                             "expiresIn": 3600, "pem": SECRETS["private-key"]}),
        har_entry("GET", _ORIGIN + "/api/items?page=2&q=fixture-query-value&api_key=" + SECRETS["google-api-key"], 200,
                  t=2, req_headers=both, req_cookies=[{"name": "session", "value": COOKIE}], resp_mime=_JSON,
                  resp_body={"items": [_ITEM], "total": 1, "next": None}),
        har_entry("GET", _ORIGIN + "/api/items/123", 200, t=3, req_headers=both, resp_mime=_JSON, resp_body=_ITEM),
        har_entry("GET", _ORIGIN + "/api/items/124", 200, t=4, req_headers=both, resp_mime=_JSON, resp_body=_ITEM),
        har_entry("GET", _ORIGIN + "/api/items/3f2b8c1e-5a4d-4e7b-9c1a-0d2e4f6a8b0c/comments", 200, t=5, req_headers=both,
                  resp_mime="application/vnd.api+json",
                  resp_body=[{"id": "c-1", "text": "Looks-bad-fixture-value", "at": "2026-01-01"}]),
        har_entry("GET", _ORIGIN + "/api/files/0123456789abcdef", 200, t=6, req_headers=both,
                  resp_mime="application/octet-stream", resp_body="AAEC"),
        har_entry("GET", _ORIGIN + "/api/blobs/Zm9vYmFyYmF6cXV4MTIzNA", 200, t=7, req_headers=both,
                  resp_mime="text/plain", resp_body="ok-fixture-value"),
        har_entry("PUT", _ORIGIN + "/api/items/123", 204, t=8, req_headers=both,
                  req_body={"title": "Fix-tile-fixture-value", "done": True}),
        har_entry("DELETE", _ORIGIN + "/api/items/123", 404, t=9, req_headers=both, resp_mime=_JSON,
                  resp_body={"error": {"code": "not_found", "message": "Gone-fixture-value"}}),
        har_entry("POST", _ORIGIN + "/api/auth/token", 200, t=10, req_headers={"Authorization": "Basic " + BASIC},
                  req_body="grant_type=client_credentials&client_secret=" + SECRETS["stripe-live"],
                  req_mime="application/x-www-form-urlencoded", resp_mime=_JSON,
                  resp_body={"access_token": SECRETS["jwt"], "token_type": "Bearer"}),
        har_entry("GET", "https://analytics.vendor.test/collect?cid=abc123-fixture&v=1", 200, t=11, resp_mime="text/plain"),
        har_entry("GET", "https://svc:" + URL_PASSWORD + "@API.example.test:8443/v1/status", 200, t=12, resp_mime=_JSON,
                  resp_body={"ok": True, "version": "1.0.0-fixture-value"}),
        har_entry("GET", _ORIGIN + "/settings", 200, t=13, req_headers=cookie_hdr, resp_body=_html("Settings"),
                  resp_mime="text/html"),
        har_entry("GET", _ORIGIN + "/api/session/logout", 200, t=14, req_headers=both,
                  resp_headers=[("Set-Cookie", "session=; Max-Age=0; Path=/")], resp_mime=_JSON,
                  resp_body={"ok": True}, resp_b64=True),
        har_entry("GET", _ORIGIN + "/api/tree", 200, t=15, req_headers=both, resp_mime=_JSON, resp_body=_TREE),
    ]


def _ep(statuses, req, resp, count=1, query=()):
    return {"statuses": list(statuses), "requestTypes": list(req), "responseTypes": list(resp), "count": count,
            "queryParams": list(query)}


_APP = "app.example.test"
_API = _APP + "/api"
_STATUS_EP = "GET api.example.test:8443/v1/status"
HAR_ENDPOINTS = {
    "GET " + _APP + "/": _ep([200], [], ["text/html"]),
    "POST " + _API + "/session/login": _ep([201], [_JSON], [_JSON]),
    "GET " + _API + "/items": _ep([200], [], [_JSON], 1, ["api_key", "page", "q"]),
    "GET " + _API + "/items/{id}": _ep([200], [], [_JSON], 2),
    "GET " + _API + "/items/{id}/comments": _ep([200], [], ["application/vnd.api+json"]),
    "GET " + _API + "/files/{id}": _ep([200], [], ["application/octet-stream"]),
    "GET " + _API + "/blobs/{id}": _ep([200], [], ["text/plain"]),
    "PUT " + _API + "/items/{id}": _ep([204], [_JSON], []),
    "DELETE " + _API + "/items/{id}": _ep([404], [], [_JSON]),
    "POST " + _API + "/auth/token": _ep([200], ["application/x-www-form-urlencoded"], [_JSON]),
    "GET analytics.vendor.test/collect": _ep([200], [], ["text/plain"], 1, ["cid", "v"]),
    _STATUS_EP: _ep([200], [], [_JSON]),
    "GET " + _APP + "/settings": _ep([200], [], ["text/html"]),
    "GET " + _API + "/session/logout": _ep([200], [], [_JSON]),
    "GET " + _API + "/tree": _ep([200], [], [_JSON]),
}
_ITEM_SHAPE = {"id": "string", "title": "string", "done": "boolean", "tagCount": "number"}
HAR_SHAPES = {   # "<endpoint key> request|response" -> shape
    "POST " + _API + "/session/login request": {"email": "string", "password": "string", "remember": "boolean"},
    "POST " + _API + "/session/login response": {"user": {"id": "string", "name": "string"}, "token": "string",
                                                  "expiresIn": "number", "pem": "string"},
    "GET " + _API + "/items response": {"items": [_ITEM_SHAPE], "total": "number", "next": "null"},
    "GET " + _API + "/items/{id} response": _ITEM_SHAPE,
    "GET " + _API + "/items/{id}/comments response": [{"id": "string", "text": "string", "at": "string"}],
    "PUT " + _API + "/items/{id} request": {"title": "string", "done": "boolean"},
    "DELETE " + _API + "/items/{id} response": {"error": {"code": "string", "message": "string"}},
    "POST " + _API + "/auth/token response": {"access_token": "string", "token_type": "string"},
    _STATUS_EP + " response": {"ok": "boolean", "version": "string"},
    "GET " + _API + "/session/logout response": {"ok": "boolean"},
    "GET " + _API + "/tree response": {"a": {"b": {"c": {"d": "..."}}}},
}
HAR_AUTH_STEPS = {
    "POST " + _API + "/session/login": {"order": 1, "sets-cookie": True, "auth-scheme": None},
    "POST " + _API + "/auth/token": {"order": 2, "sets-cookie": False, "auth-scheme": "Basic"},
    "GET " + _API + "/session/logout": {"order": 3, "sets-cookie": True, "auth-scheme": "Bearer"},
}
HAR_HOSTS = {"analytics.vendor.test": {"count": 1}, "api.example.test:8443": {"count": 1}}
HAR_PAGES = {_APP + "/": {}, _APP + "/settings": {}}


def har_never_stored(entries):
    """Every value a lane must not store: header, cookie and query values, form values, JSON string leaves of both
    bodies (a base64 body is decoded first) of at least 8 characters, and the URL password."""
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
        if not isinstance(e, dict) or not isinstance(e.get("request"), dict) or not isinstance(e.get("response"), dict):
            continue
        req, resp = e["request"], e["response"]
        u = urlsplit(req.get("url", ""))
        if u.password:
            out.add(u.password)
        for h in req.get("headers", []) + resp.get("headers", []):
            if len(h["value"]) >= 8:
                out.add(h["value"])
        for c in req.get("cookies", []) + resp.get("cookies", []):
            if len(c.get("value", "")) >= 8:
                out.add(c["value"])
        for q in req.get("queryString", []):
            if len(q["value"]) >= 8:
                out.add(q["value"])
        post = req.get("postData")
        if post:
            if "json" in post["mimeType"]:
                leaves(json.loads(post["text"]))
            for p in post.get("params", []):
                if len(p["value"]) >= 8:
                    out.add(p["value"])
        content = resp.get("content", {})
        text = content.get("text")
        if text:
            if content.get("encoding") == "base64":
                out.add(text)
                try:
                    text = base64.b64decode(text).decode("utf-8")
                except ValueError:
                    text = ""
            if "json" in content.get("mimeType", ""):
                try:
                    leaves(json.loads(text))
                except (ValueError, RecursionError):
                    pass
    return sorted(out)


def _har_doc(entries):
    return {"log": {"version": "1.2", "creator": {"name": "fixture-recorder", "version": "1.0"},
                    "pages": [{"startedDateTime": _iso(0), "id": "page_1", "title": "Snag", "pageTimings": {}}],
                    "entries": entries}}


def _har_kinds():
    return {"web.endpoint": HAR_ENDPOINTS, "web.payload-shape": {k: {"shape": v} for k, v in HAR_SHAPES.items()},
            "web.auth-step": HAR_AUTH_STEPS, "web.host": HAR_HOSTS, "web.page": HAR_PAGES}


def make_har(path, entries=None):
    """HAR 1.2 with auth headers, cookies, a password in a URL, a private key and JWTs in JSON bodies, mime types with
    parameters, a base64 body and a host with a port. Page host `app.example.test`; third-party hosts
    `analytics.vendor.test` and `api.example.test:8443` (the URL is written with an upper-case host and userinfo).

    With ``entries=None`` the facts describe the default flow of ``default_har_entries()``. With your own entries the
    facts carry only ``entry_count``, ``never_stored`` and the secrets.
    """
    custom = entries is not None
    entries = entries if custom else default_har_entries()
    p = _write(path, _dump(_har_doc(entries)))
    facts = _text_facts(p, {} if custom else _har_kinds(), entry_count=len(entries), never_stored=har_never_stored(entries),
                        variant="custom" if custom else "normal")
    return p, facts


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
    "webpack:///./node_modules/@angular/core/fesm2022/core.mjs",
    "webpack:///./node_modules/svelte/src/index.js",
    "webpack:///./main.js",
]
MAP_MARKER = "SOURCESCONTENT_MARKER_7f3a_fixture"


def default_map_contents():
    first = _lines(
        "// " + MAP_MARKER,
        'import React from "react";',
        'const routes = [{ path: "/dashboard", element: null }];',
        'axios.get("/api/items");',
        'const label = t("home.title");',
        'const key = "%s";' % SECRETS["aws-key"],
    )
    return [first] + ["export const x%d = %d;\n" % (i, i) for i in range(1, len(DEFAULT_MAP_SOURCES))]


def map_module_key(source):
    """Contract 6.5 `web.source-module` key of one `sources` entry."""
    s = source
    if s.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", s) or ".." in re.split(r"[\\/]", s):
        return "(outside)"
    s = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", s).lstrip("/")
    if s.startswith("./"):
        s = s[2:]
    folders = s.split("/")[:-1]
    return "/".join(folders[:2]) if folders else "(root)"


def _module_facts(sources):
    mods = {}
    for s in sources:
        k = map_module_key(s)
        mods[k] = {"sources": mods.get(k, {"sources": 0})["sources"] + 1}
    return mods


MAP_CONTENT_KINDS = {"web.api-call": {"GET /api/items": {"host": None}}, "web.i18n-key": {"home.title": {"namespace": "home"}},
                     "web.route": {"/dashboard": {}}}
MAP_FRAMEWORKS = {k: {"from": "source-map"} for k in ("angular", "next", "react", "svelte", "vue")}


def make_sourcemap(path, sources=None, contents=None):
    """Source map v3. ``web.source-module`` key per `map_module_key` (`(root)` for a source with no folder, `(outside)`
    for an absolute, drive-letter or `..` source), data `{"sources": n}`. `sourcesContent` carries a marker, a route,
    an axios call, an i18n key and a secret; it is scanned in memory and never stored."""
    default = sources is None and contents is None
    sources = list(DEFAULT_MAP_SOURCES if sources is None else sources)
    contents = default_map_contents()[:len(sources)] if contents is None else list(contents)
    doc = {"version": 3, "file": "main.js", "sourceRoot": "", "sources": sources, "sourcesContent": contents,
           "names": ["React", "axios", "t"], "mappings": "AAAA;AACA,SAAS"}
    p = _write(path, _dump(doc))
    kinds = {"web.source-module": _module_facts(sources)}
    if default:
        kinds.update({k: dict(v) for k, v in MAP_CONTENT_KINDS.items()})
        kinds["web.framework"] = MAP_FRAMEWORKS
        return p, _text_facts(p, kinds, never_stored=[MAP_MARKER, SECRETS["aws-key"]], sources=len(sources), variant="normal")
    return p, _text_facts(p, kinds, sources=len(sources), variant="custom")


# ----------------------------------------------------------------------------- bundle

BUNDLE_KINDS = {
    "web.route": {"/dashboard": {}, "/settings/profile": {}, "/items/{itemId}": {}, "/reports": {}, "/vue-page": {},
                  "/admin": {"relative": True}, "/": {"relative": True}, "/blog/{slug}": {}},
    "web.api-call": {"GET /api/items": {"host": None}, "POST /api/items": {"host": None}, "DELETE /api/items": {"host": None},
                     "GET /api/legacy/ping": {"host": None}, "ANY /api/search": {"host": None},
                     "ANY /api/export": {"host": None}, "PUT /api/save": {"host": None},
                     "ANY /v2/orders": {"host": "api.example.test"}},
    "web.flag": {k: {} for k in ("new-checkout", "dark-mode", "beta-banner", "promo-copy", "fast-sync", "legacyExport")},
    "web.i18n-key": {"home.title": {"namespace": "home"}, "home.subtitle": {"namespace": "home"},
                     "nav.settings": {"namespace": "nav"}, "items.empty": {"namespace": "items"}},
    "web.framework": {"next": {"from": "bundle"}, "react": {"from": "bundle"}},
}
BUNDLE_MUST_NOT = {"web.i18n-key": ["x.y", "a.b", "/api/items", "fmt.key"],
                   "web.api-call": ["GET /static/logo.png", "ANY /static/logo.png"]}
BUNDLE_NEVER = [SECRETS[k] for k in ("aws-key", "stripe-live", "slack", "github-token")] + [TOKEN]


def make_bundle(text="", style="spaced"):
    """Minified-looking JavaScript: routes (React Router, JSX, Vue, Angular, Next `__NEXT_DATA__`), axios, XHR and
    fetch calls (a template literal, a literal method, an absolute URL), flags (with a flag-library import), i18n keys,
    framework markers, secrets and the negatives of 6.5 (`cat("x.y")`, `split("a.b")`, `format("fmt.key")`, a CDN
    URL that is not an API).

    Returns ``(js_text, facts)``; nothing is written (use ``write_bundle``). ``text`` is appended verbatim.
    ``style="spaced"`` writes the literal forms of contract 6.5 (``path: "/x"``); ``"compact"`` removes the spaces
    after ``:`` and ``,`` and joins everything on one line (the same facts).
    """
    sp = " " if style == "spaced" else ""
    lines = [
        "/*! synthetic bundle for tests */",
        "(function () {",
        'var routes = [{ path:%s"/dashboard", element:%sDashboard }, { path:%s"/settings/profile" }, { path:%s"/items/:itemId" }];' % (sp, sp, sp, sp),
        'var jsx = <Route path="/reports" element={Reports} />;',
        "var vueRoutes = [{ path:%s'/vue-page', component:%sVuePage }];" % (sp, sp),
        "var ngRoutes = [{ path:%s'admin', component:%sAdmin }, { path:%s'', component:%sHome }];" % (sp, sp, sp, sp),
        'axios.get("/api/items"); axios.post("/api/items", body); axios.delete("/api/items");',
        'XMLHttpRequest.open("GET", "/api/legacy/ping");',
        'fetch("/api/search"); fetch(`/api/export`); fetch("/api/save", { method:%s"PUT" });' % sp,
        'var abs = "https://api.example.test/v2/orders"; var logo = "https://cdn.example.test/static/logo.png";',
        'import * as LaunchDarkly from "launchdarkly-js-client-sdk";',
        'isEnabled("new-checkout"); useFlag("dark-mode"); useFeatureFlag("beta-banner"); getFeatureValue("promo-copy");',
        'client.variation("fast-sync", false); if (flags.legacyExport) { run(); }',
        't("home.title"); i18n.t("home.subtitle"); $t("nav.settings"); formatMessage({ id:%s"items.empty" });' % sp,
        'cat("x.y"); str.split("a.b"); format("fmt.key");',
        'window.__NEXT_DATA__ = {"page":"/blog/[slug]","query":{}};',
        'var el = "<div data-reactroot>";',
        'var cfg = { aws:%s"%s", stripe:%s"%s", auth:%s"%s", slack:%s"%s", gh:%s"%s" };' % (
            sp, SECRETS["aws-key"], sp, SECRETS["stripe-live"], sp, SECRETS["bearer"], sp, SECRETS["slack"], sp,
            SECRETS["github-token"]),
        "})();",
    ]
    js = ("\n".join(lines) if style == "spaced" else "".join(lines)) + "\n"
    js += text
    facts = _facts(BUNDLE_KINDS, must_not=BUNDLE_MUST_NOT, never_stored=BUNDLE_NEVER, style=style, variant="normal")
    return js, _with_secrets(facts, {".": js})


def write_bundle(path, text="", style="spaced"):
    js, facts = make_bundle(text, style)
    return _write(path, js), facts


def make_bundle_edge(path):
    """Edge cases of 6.5 in one file: the framework markers `__NUXT__`, `ng-version` and `__SVELTEKIT`; the bundler
    markers `__vite__` and `webpackChunk` (no framework); `flags.x` without a flag-library import (not a flag);
    `split("a.b")` and `cat("x.y")` (not i18n keys); empty literals in a flag, an i18n key, a `fetch` and an axios call
    (ignored: 6.2 rule 7); one real axios call."""
    js = _lines(
        "/*! edge cases */",
        'window.__NUXT__ = {}; document.body.setAttribute("ng-version", "17"); var s = "__SVELTEKIT";',
        'window.__vite__ = 1; (self["webpackChunkfixture"] = self["webpackChunkfixture"] || []);',
        "if (flags.notAFlag) { run(); }",
        'cat("x.y"); str.split("a.b");',
        'isEnabled(""); t(""); fetch(""); axios.get("");',
        'axios.get("/api/edge");')
    p = _write(path, js)
    kinds = {"web.framework": {k: {"from": "bundle"} for k in ("angular", "nuxt", "sveltekit")},
             "web.api-call": {"GET /api/edge": {"host": None}}}
    return p, _text_facts(p, kinds, must_not={"web.flag": ["notAFlag"], "web.i18n-key": ["x.y", "a.b"],
                                              "web.framework": ["vite", "webpack"]}, variant="edge")


HERMES_MAGIC = bytes([0xC6, 0x1F, 0xBC, 0x03, 0xC1, 0x03, 0x19, 0x1F])


def make_hermes_bundle(path):
    """Compiled Hermes bytecode stand-in: the magic then junk. Reported present with the exact limitation, never
    decoded (decision 5). The junk contains NULs, so it is not scanned as text."""
    p = _write(path, HERMES_MAGIC + _lcg_bytes(256, seed=7))
    return p, _facts({"web.framework": {"hermes": {"from": "bundle"}}}, limitations=[HERMES_NOTE], variant="hermes")


# ----------------------------------------------------------------------------- saved site (folder of web files)


def make_site(root, variant="normal"):
    """A saved Next.js site: index.html with `__NEXT_DATA__`, `_next/static/abc/_buildManifest.js` and a bundle.
    Variants: normal, empty (an empty folder). Keys with records in several files (`web.route "/"`: the bundle's
    Angular `path: ''` with `{"relative": true}` and the manifest with `{}`; `web.route "/blog/{slug}"`; the `next`
    framework from `html` and from `bundle`) are only in ``records_by_path``; ``kinds`` holds the unique keys."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if variant == "empty":
        return root, _facts(records_by_path={}, variant="empty")
    html = _lines(
        "<!doctype html>", "<html><head><title>Snag</title></head><body>",
        '<div id="__next"></div>',
        '<script id="__NEXT_DATA__" type="application/json">{"page":"/blog/[slug]","query":{},"buildId":"abc"}</script>',
        '<script src="/static/main.js"></script>', "</body></html>")
    manifest = ('self.__BUILD_MANIFEST = {"/": ["static/chunks/pages/index.js"], "/about": ["static/chunks/pages/about.js"], '
                '"/blog/[slug]": ["static/chunks/pages/blog.js"], sortedPages: ["/", "/about", "/blog/[slug]"]};\n')
    js, bundle_facts = make_bundle()
    _write(root / "index.html", html)
    _write(root / "_next" / "static" / "abc" / "_buildManifest.js", manifest)
    _write(root / "static" / "main.js", js)
    per_file = {
        "index.html": {"web.route": {"/blog/{slug}": {}}, "web.framework": {"next": {"from": "html"}}},
        "_next/static/abc/_buildManifest.js": {"web.route": {"/": {}, "/about": {}, "/blog/{slug}": {}}},
        "static/main.js": {k: dict(v) for k, v in bundle_facts["kinds"].items() if k != "secret.embedded"},
    }
    sec = _secret_kinds({"static/main.js": js})
    if sec:
        per_file["static/main.js"]["secret.embedded"] = sec
    facts = _facts(_split_records(per_file), records_by_path=per_file,
                   files=["_next/static/abc/_buildManifest.js", "index.html", "static/main.js"],
                   never_stored=BUNDLE_NEVER, secret_patterns=sorted({k.split("@")[0] for k in sec}), variant="normal")
    return root, facts


# ----------------------------------------------------------------------------- SQLite

DEFAULT_DDL = [
    "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE, api_token TEXT, "
    "created_at TEXT DEFAULT 'none')",
    "CREATE TABLE items (id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id), title TEXT NOT NULL, "
    "done BOOLEAN NOT NULL DEFAULT 0, price NUMERIC(10,2), extra)",
    "CREATE TABLE tags (item_id INTEGER NOT NULL, tag VARCHAR(40) NOT NULL, FOREIGN KEY (item_id) REFERENCES items(id))",
    'CREATE TABLE audit ("event" TEXT, [when] TEXT, `by` TEXT)',
    "CREATE TABLE kv (k TEXT PRIMARY KEY, v BLOB) WITHOUT ROWID",
    "CREATE TABLE counters (id INTEGER PRIMARY KEY AUTOINCREMENT, n INT)",
    "CREATE INDEX idx_items_owner ON items (owner_id)",
    "CREATE TRIGGER trg_items_done AFTER UPDATE OF done ON items BEGIN UPDATE users SET created_at = created_at "
    "WHERE id = NEW.owner_id; END",
    "CREATE VIEW open_items AS SELECT id, title FROM items WHERE done = 0",
    "INSERT INTO users (id, name, api_token) VALUES (1, 'fixture-row-user', '%s')" % SECRETS["aws-key"],
    "INSERT INTO items (id, owner_id, title, done, price, extra) VALUES (1, 1, 'SECRET-ROW-TITLE-fixture', 0, 9.5, 'x')",
    "INSERT INTO tags (item_id, tag) VALUES (1, 'row-tag-fixture')",
]


def _cols(*pairs):
    return {"columns": [{"name": n, "type": t} for n, t in pairs]}


SQLITE_KINDS = {
    "data.table": {
        "users": _cols(("id", "INTEGER"), ("name", "TEXT"), ("email", "TEXT"), ("api_token", "TEXT"), ("created_at", "TEXT")),
        "items": _cols(("id", "INTEGER"), ("owner_id", "INTEGER"), ("title", "TEXT"), ("done", "BOOLEAN"),
                       ("price", "NUMERIC(10,2)"), ("extra", "")),
        "tags": _cols(("item_id", "INTEGER"), ("tag", "VARCHAR(40)")),
        "audit": _cols(("event", "TEXT"), ("when", "TEXT"), ("by", "TEXT")),
        "kv": _cols(("k", "TEXT"), ("v", "BLOB")),
        "counters": _cols(("id", "INTEGER"), ("n", "INT")),
    },
    "data.index": {"idx_items_owner": {"table": "items"}},
    "data.trigger": {"trg_items_done": {"table": "items"}},
    "data.view": {"open_items": {}},
}
SQLITE_ROWS = {"users": 1, "items": 1, "tags": 1, "audit": 0, "kv": 0, "counters": 0}
SQLITE_NEVER = [SECRETS["aws-key"], "fixture-row-user", "SECRET-ROW-TITLE-fixture", "row-tag-fixture"]
SQLITE_INTERNAL = ["sqlite_autoindex_users_1", "sqlite_sequence"]     # in sqlite_master; ignored by the lane


def sqlite_schema_facts(path):
    """Schema facts read with the sqlite3 module itself (never with lane code): the oracle for custom DDL.
    Objects named `sqlite_%` are left out (6.8)."""
    con = sqlite3.connect(str(path))
    try:
        kinds = {"data.table": {}, "data.index": {}, "data.trigger": {}, "data.view": {}}
        for typ, name, tbl in con.execute("SELECT type, name, tbl_name FROM sqlite_master ORDER BY name"):
            if name.startswith("sqlite_"):
                continue
            if typ == "table":
                cols = [{"name": c[1], "type": c[2]} for c in con.execute('PRAGMA table_info("%s")' % name)]
                kinds["data.table"][name] = {"columns": cols}
            elif typ == "view":
                kinds["data.view"][name] = {}
            else:
                kinds["data." + typ][name] = {"table": tbl}
        return kinds
    finally:
        con.close()


def make_sqlite(path, ddl=None):
    """SQLite database: tables (a precision type, an untyped column, quoted identifiers, a UNIQUE column that creates
    `sqlite_autoindex_users_1`, an AUTOINCREMENT table that creates `sqlite_sequence`, a WITHOUT ROWID table), an index,
    a trigger, a view and one row per main table holding fake secrets that must never appear in output. SQLite files
    are not scanned for secrets (6.2 rule 5): ``secret_patterns`` is ``[]`` although the key is in the raw bytes.

    The file bytes can vary with the SQLite library version, so tests must compare the schema
    (`sqlite_schema_facts`) and the rows, never bytes. With custom ``ddl`` the facts are read back from the database.
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
        return p, _facts(SQLITE_KINDS, row_counts=SQLITE_ROWS, never_stored=SQLITE_NEVER, secret_patterns=[],
                         must_not={"secret.embedded": ["aws-key@."], "data.index": ["sqlite_autoindex_users_1"],
                                  "data.table": ["sqlite_sequence"]},
                         variant="normal")
    return p, _facts(sqlite_schema_facts(p), secret_patterns=[], variant="custom")


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


def _t(name):
    return {"type": name}


PLIST_KINDS = {"data.plist-key": {
    "ApiKey": _t("string"), "Blob": _t("data"), "CFBundleIdentifier": _t("string"), "CFBundleName": _t("string"),
    "CFBundleShortVersionString": _t("string"), "CFBundleURLTypes": _t("array"), "CFBundleURLTypes[]": _t("dict"),
    "CFBundleURLTypes[].CFBundleURLName": _t("string"), "CFBundleURLTypes[].CFBundleURLSchemes": _t("array"),
    "CFBundleURLTypes[].CFBundleURLSchemes[]": _t("string"), "CFBundleVersion": _t("string"), "Count": _t("integer"),
    "LaunchedAt": _t("date"), "MinimumOSVersion": _t("string"), "NSAppTransportSecurity": _t("dict"),
    "NSAppTransportSecurity.NSAllowsArbitraryLoads": _t("bool"), "NSAppTransportSecurity.NSExceptionDomains": _t("dict"),
    "NSAppTransportSecurity.NSExceptionDomains.localhost-fixture": _t("dict"),
    "NSAppTransportSecurity.NSExceptionDomains.localhost-fixture.NSExceptionAllowsInsecureHTTPLoads": _t("bool"),
    "NSCameraUsageDescription": _t("string"), "Ratio": _t("real"), "UIBackgroundModes": _t("array"),
    "UIBackgroundModes[]": _t("string"),
}}
PLIST_NEVER = [SECRETS["aws-key"], "Fixture text: the camera photographs defects.", "test.example.snag"]


def make_plist(path, fmt="xml", data=None):
    """Property list, XML (`fmt="xml"`) or binary (`"binary"`). Key paths per 6.8: dict keys joined by `.`, an array as
    the segment `[]` reduced to its first element (`UIBackgroundModes[]`); no key in the default data contains a dot.
    Values (a fake key, usage text, the bundle id) must never be stored. An XML plist is scanned for secrets; a binary
    plist is not (``secret_patterns`` is ``[]``, the key is in its bytes)."""
    f = plistlib.FMT_XML if fmt == "xml" else plistlib.FMT_BINARY
    raw = plistlib.dumps(DEFAULT_PLIST if data is None else data, fmt=f, sort_keys=True)
    p = _write(path, raw)
    kinds = PLIST_KINDS if data is None else {}
    extra = dict(never_stored=PLIST_NEVER) if data is None else {}
    facts = _facts(kinds, format=fmt, variant="normal" if data is None else "custom", **extra)
    if fmt == "xml":
        return p, _with_secrets(facts, {".": _decode(raw)})
    facts["secret_patterns"] = []
    facts["must_not"] = {"secret.embedded": ["aws-key@."]}
    return p, facts


# ----------------------------------------------------------------------------- OpenAPI (JSON and YAML)

_OP_LINES = [("/items", "get", "listItems", ["items"]), ("/items", "post", "createItem", ["items"]),
             ("/items/{itemId}", "get", "getItem", ["items"]), ("/items/{itemId}", "put", "updateItem", ["items"]),
             ("/items/{itemId}", "patch", "patchItem", ["items"]), ("/items/{itemId}", "delete", "deleteItem", ["items"]),
             ("/session", "post", "createSession", ["auth"]), ("/health", "get", "health", []),
             ("/health", "head", "healthHead", []), ("/health", "options", "healthOptions", []),
             ("/health", "trace", "healthTrace", [])]


def _op(op_id, tags, summary="fixture"):
    return {"operationId": op_id, "tags": list(tags), "summary": summary, "responses": {"200": {"description": "ok"}}}


def default_openapi_doc():
    item = {"type": "object", "properties": {"id": {"type": "string"}, "title": {"type": "string"},
                                              "token": {"type": "string", "example": SECRETS["jwt"]}}}
    paths = {"/items": {"parameters": [{"name": "X-Trace", "in": "header", "schema": {"type": "string"}}]},
             "/items/{itemId}": {"summary": "One item"}, "/session": {}, "/health": {}}
    for p, m, op_id, tags in _OP_LINES:
        paths[p][m] = _op(op_id, tags)
    return {
        "openapi": "3.0.3",
        "info": {"title": "Snag fixture API", "version": "1.0.0", "description": "Synthetic. Key " + SECRETS["aws-key"]},
        "servers": [{"url": "https://svc:" + URL_PASSWORD + "@api.example.test/v1"}],
        "paths": paths,
        "components": {
            "schemas": {"Item": item, "NewItem": {"type": "object"}, "Error": {"type": "object"}},
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"},
                                "apiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}},
        },
    }


_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")


def _endpoints(doc):
    out = {}
    for p, item in doc["paths"].items():
        for m, op in item.items():
            if m in _METHODS:
                out["%s %s" % (m.upper(), p)] = {"operationId": op.get("operationId"), "tags": list(op.get("tags", []))}
    return out


OPENAPI_KINDS = {
    "data.endpoint": {"%s %s" % (m.upper(), p): {"operationId": op_id, "tags": tags} for p, m, op_id, tags in _OP_LINES},
    "data.schema-type": {"Error": {"kind": "schema"}, "Item": {"kind": "schema"}, "NewItem": {"kind": "schema"}},
    "data.security": {"apiKeyAuth": {"type": "apiKey"}, "bearerAuth": {"type": "http"}},
}
OPENAPI_NEVER = [URL_PASSWORD, SECRETS["jwt"], SECRETS["aws-key"]]
OPENAPI_NOT_ENDPOINTS = ["/items parameters", "/items/{itemId} summary"]   # path-item keys that are not methods


def make_openapi_json(path, doc=None, swagger=False):
    """OpenAPI 3 JSON (or Swagger 2 with ``swagger=True``: `definitions` and `securityDefinitions`, no `servers`). All
    eight methods appear; path-level `parameters` and `summary` are not methods. The server URL carries a password and
    an example carries a fake JWT; the AWS key sits in `info.description`."""
    if doc is not None:
        p = _write(path, _dump(doc))
        return p, _text_facts(p, {"data.endpoint": _endpoints(doc)}, variant="custom")
    d = default_openapi_doc()
    if swagger:
        d = {"swagger": "2.0", "info": d["info"], "host": "api.example.test", "basePath": "/v1", "paths": d["paths"],
             "definitions": d["components"]["schemas"], "securityDefinitions": d["components"]["securitySchemes"]}
        p = _write(path, _dump(d))
        return p, _text_facts(p, OPENAPI_KINDS, never_stored=[SECRETS["jwt"], SECRETS["aws-key"]], variant="swagger")
    p = _write(path, _dump(d))
    return p, _text_facts(p, OPENAPI_KINDS, never_stored=OPENAPI_NEVER, not_endpoints=OPENAPI_NOT_ENDPOINTS, variant="normal")


YAML_ENDPOINT_KEYS = sorted(OPENAPI_KINDS["data.endpoint"])


def _yaml_text():
    out = ["openapi: 3.0.3", "info:", "  title: Snag fixture API", "  version: 1.0.0", "servers:",
           "  - url: https://svc:" + URL_PASSWORD + "@api.example.test/v1", "paths:"]
    for path in ("/items", "/items/{itemId}", "/session"):
        out.append("  %s:" % path)
        if path == "/items":
            out += ["    parameters:", "      - name: X-Trace", "        in: header"]
        if path == "/items/{itemId}":
            out.append("    summary: One item")
        for p, m, op_id, tags in _OP_LINES:
            if p == path:
                out += ["    %s:" % m, "      operationId: %s" % op_id]
                if tags:
                    out.append("      tags: [%s]" % ", ".join(tags))
    out += ['  "/quoted":', "    get:", "      operationId: quoted", "  /health:"]
    for p, m, op_id, tags in _OP_LINES:
        if p == "/health":
            out += ["    %s:" % m, "      operationId: %s" % op_id]
    out += ["components:", "  schemas:", "    get:", "      type: object", "    delete:", "      type: object",
            "  securitySchemes:", "    bearerAuth:", "      type: http", "x-secret-example: " + SECRETS["bearer"]]
    return _lines(*out)


YAML_NEVER = [URL_PASSWORD, TOKEN]


def make_openapi_yaml(path, variant="normal"):
    """OpenAPI YAML for the line scanner of 6.8; endpoint data is `{"operationId": null, "tags": []}`. Traps: path-level
    `parameters:` and `summary:` (not methods); schemas named `get` and `delete` under `components` (must not become
    methods of the last path); a quoted path key (`"/quoted":`) that the scanner does not match and that clears the
    current path (so its `get:` must not become `GET /session`). Variants: normal, crlf (same facts, CRLF line endings),
    empty (`paths: {}`)."""
    if variant == "empty":
        p = _write(path, "openapi: 3.0.3\ninfo:\n  title: Empty\n  version: 1.0.0\npaths: {}\n")
        return p, _text_facts(p, {}, variant="empty")
    text = _yaml_text()
    if variant == "crlf":
        text = text.replace("\n", "\r\n")
    p = _write(path, text)
    kinds = {"data.endpoint": {k: {"operationId": None, "tags": []} for k in YAML_ENDPOINT_KEYS}}
    return p, _text_facts(p, kinds, never_stored=YAML_NEVER, not_extracted=["GET /quoted"],
                          not_endpoints=["GET /session via the quoted path", "GET /items/{itemId} via components.schemas.get"],
                          limitations=["YAML read by a line scanner"], variant=variant)


# ----------------------------------------------------------------------------- GraphQL

GRAPHQL_SDL = _lines(
    "schema {", "  query: Query", "  mutation: Mutation", "  subscription: Subscription", "}",
    "", '"""Synthetic schema."""', "# type Commented { x: Int }", "scalar DateTime", "",
    "interface Node {", "  id: ID!", "}", "",
    "type Item implements Node {", "  id: ID!", "  title: String!", "  done: Boolean!", "  createdAt: DateTime", "}", "",
    "type User implements Node {", "  id: ID!", "  name: String!", "}", "",
    "input NewItem {", "  title: String!", "}", "",
    "enum Status {", "  OPEN", "  DONE", "}", "",
    "union SearchResult = Item | User", "",
    "type Query {", "  items(first: Int = 10, status: Status): [Item!]!", "  item(id: ID!): Item", "  me: User",
    "  search(q: String!): [SearchResult!]!", "}", "",
    "extend type Query {", "  itemsCount: Int", "}", "",
    "type Mutation {", "  createItem(input: NewItem!): Item", "  deleteItem(id: ID!): Boolean!", "}", "",
    "type Subscription {", "  itemChanged(id: ID!): Item", "}",
)
GRAPHQL_KINDS = {
    "data.schema-type": {"Item": {"kind": "type"}, "Mutation": {"kind": "type"}, "NewItem": {"kind": "input"},
                         "Node": {"kind": "interface"}, "Query": {"kind": "type"}, "SearchResult": {"kind": "union"},
                         "Status": {"kind": "enum"}, "Subscription": {"kind": "type"}, "User": {"kind": "type"},
                         "DateTime": {"kind": "scalar"}},
    "data.endpoint": {k: {} for k in ("mutation createItem", "mutation deleteItem", "query item", "query items",
                                      "query itemsCount", "query me", "query search", "subscription itemChanged")},
}
GRAPHQL_MUST_NOT = {"data.schema-type": ["Commented"]}


def _introspection():
    def t(kind, name, fields=None):
        d = {"kind": kind, "name": name}
        if fields is not None:
            d["fields"] = [{"name": f} for f in fields]
        return d
    types = [t("OBJECT", "__Schema", ["types"]), t("OBJECT", "__Type", ["kind", "name"])]
    types += [t("SCALAR", n) for n in ("String", "Int", "Float", "Boolean", "ID")]
    types += [t("INTERFACE", "Node", ["id"]), t("OBJECT", "Item", ["id", "title", "done", "createdAt"]),
              t("OBJECT", "User", ["id", "name"]), t("INPUT_OBJECT", "NewItem"), t("ENUM", "Status"),
              t("UNION", "SearchResult"), t("SCALAR", "DateTime"),
              t("OBJECT", "Query", ["items", "item", "me", "search", "itemsCount"]),
              t("OBJECT", "Mutation", ["createItem", "deleteItem"]), t("OBJECT", "Subscription", ["itemChanged"])]
    return {"data": {"__schema": {"queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"},
                                  "subscriptionType": {"name": "Subscription"}, "types": types}}}


def make_graphql(path, introspection=False):
    """GraphQL SDL (`.graphql`: a scalar, an `extend type Query`, a commented-out type) or introspection JSON
    (`__schema` under `data`, with the `__*` types and built-in scalars a lane must not emit); both give the same
    facts."""
    if introspection:
        p = _write(path, _dump(_introspection()))
        return p, _text_facts(p, GRAPHQL_KINDS, must_not={"data.schema-type": ["String", "Int", "Float", "Boolean", "ID", "__Schema", "__Type"]},
                              variant="introspection")
    p = _write(path, GRAPHQL_SDL)
    return p, _text_facts(p, GRAPHQL_KINDS, must_not=GRAPHQL_MUST_NOT, variant="normal")


# ----------------------------------------------------------------------------- proto

PROTO_TEXT = _lines(
    'syntax = "proto3";', "package example.snag.v1;", "// message Commented {}", "/* message InBlock {} */", "",
    "message Item {", "  string id = 1;", "  string title = 2;", "  message Meta {", "    string note = 1;", "  }", "}", "",
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


def _rpc(req, resp):
    return {"request": req, "response": resp}


PROTO_KINDS = {
    "data.schema-type": {"Item": {"kind": "message"}, "Item.Meta": {"kind": "message"}, "NewItem": {"kind": "message"},
                         "GetItemRequest": {"kind": "message"}, "ListItemsRequest": {"kind": "message"},
                         "Status": {"kind": "enum"}},
    "data.rpc": {"ItemService.GetItem": _rpc("GetItemRequest", "Item"),
                 "ItemService.ListItems": _rpc("ListItemsRequest", "Item"),
                 "ItemService.CreateItem": _rpc("NewItem", "Item"),
                 "ItemService.Watch": _rpc("GetItemRequest", "Item"),
                 "HealthService.Check": _rpc("GetItemRequest", "Item")},
}
PROTO_MUST_NOT = {"data.schema-type": ["Commented", "InBlock", "Meta"]}


def make_proto(path):
    """proto3 file: messages (one nested: `Item.Meta`), an enum, two services; one rpc streams the response, one both
    ways, one has an empty body; a `//` and a `/* */` comment each hold a message that must not count."""
    p = _write(path, PROTO_TEXT)
    return p, _text_facts(p, PROTO_KINDS, must_not=PROTO_MUST_NOT, variant="normal")


# ----------------------------------------------------------------------------- source repository

_LANG_BY_EXT = {".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript",
                ".cjs": "JavaScript", ".py": "Python", ".rs": "Rust", ".go": "Go", ".rb": "Ruby", ".sql": "SQL",
                ".prisma": "Prisma", ".java": "Java", ".kt": "Kotlin"}


def _pkg(name, **deps):
    return _dump({"name": name, "version": "0.1.0", "dependencies": deps}).decode("ascii")


def _repo_files():
    """relpath -> text for the normal repository: Express and Fastify (JavaScript), Next.js (TypeScript, app router and
    pages), FastAPI, Flask and Django (Python), Axum (Rust), Rails (Ruby) and a Go module, one manifest for every
    framework id of 6.4, tests, an import cycle, alias, index, `mod.rs` and Go package-folder imports, unresolved
    bare specifiers, env reads and embedded fake secrets (one a multi-line private key)."""
    f = {}
    f["package.json"] = _dump({
        "name": "snag-fixture", "version": "0.1.0", "main": "src/server.js", "bin": {"snag": "bin/cli.js"},
        "scripts": {"start": "node src/start.js", "test": "node --test tests/"},
        "dependencies": {"express": "^4.18.0", "fastify": "^4.0.0", "next": "^14.0.0", "react": "^18.2.0",
                         "react-dom": "^18.2.0"},
        "devDependencies": {"typescript": "^5.0.0"}}).decode("ascii")
    f["apps/vue/package.json"] = _pkg("vue-app", vue="^3.4.0", nuxt="^3.9.0")
    f["apps/svelte/package.json"] = _pkg("svelte-app", svelte="^4.0.0", **{"@sveltejs/kit": "^2.0.0"})
    f["apps/ng/package.json"] = _pkg("ng-app", **{"@angular/core": "^17.0.0"})
    f["apps/nest/package.json"] = _pkg("nest-app", **{"@nestjs/core": "^10.0.0"})
    f["apps/desktop/package.json"] = _pkg("desktop-app", electron="^28.0.0")
    f["apps/mobile/package.json"] = _pkg("mobile-app", **{"react-native": "^0.73.0", "expo": "^50.0.0"})
    f["tsconfig.json"] = _dump({"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}).decode("ascii")
    f["README.md"] = "# snag-fixture\n\nA synthetic repository for study-lane tests.\n"
    f["bin/cli.js"] = _lines("#!/usr/bin/env node", 'require("../src/server");')
    f["src/start.js"] = 'require("./server");\n'
    f["src/server.js"] = _lines(
        'const express = require("express");',
        'const itemsRouter = require("./routes/items");',
        'const { connect } = require("./db");',
        'const events = require("./events");',
        'const config = require("./config");',
        'const lib = require("./lib");',
        'const loadLazy = () => import("./lazy");',
        "const app = express();",
        "app.use(express.json());",
        'app.get("/health", (req, res) => res.json({ ok: true }));',
        'app.all("/ping-any", (req, res) => res.end());',
        'app.use("/static/*", (req, res) => res.end());',
        "app.use(itemsRouter);",
        "connect(process.env.DATABASE_URL);",
        "app.listen(process.env.PORT || 3000);",
        "module.exports = { app, events, config, lib, loadLazy };")
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
        'const pool = new Pool({ connectionString: process.env.DATABASE_URL, max: Number(process.env["DB_POOL_SIZE"]) });',
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
    f["src/lib/index.js"] = "module.exports = { lib: true };\n"
    f["src/config.js"] = _lines(
        "module.exports = {",
        '  stripeKey: "%s",' % SECRETS["stripe-live"],
        '  awsKey: "%s",' % SECRETS["aws-key"],
        "  lowercase: process.env.lowercase_var,",
        "  pem: `" + SECRETS["private-key"] + "`,",
        "};")
    f["src/lib/validate.js"] = _lines(
        "function validateItem(req, res, next) {",
        "  if (process.env.STRICT_VALIDATION && !req.body.title) { return res.status(400).end(); }",
        "  next();",
        "}",
        "module.exports = { validateItem };")
    f["src/lib/util.ts"] = "export const slug = (s: string) => s.toLowerCase();\n"
    f["src/types.ts"] = "export interface Item { id: string; title: string }\n"
    f["src/polyfill.ts"] = "export {};\n"
    f["src/api-client.ts"] = _lines(
        'import { slug } from "@/lib/util";',
        'import type { Item } from "./types";',
        'import "./polyfill";',
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
                    ("app/shop/[[...parts]]/page.tsx", "Shop"), ("app/_private/page.tsx", "Hidden"),
                    ("app/_private/deep/page.tsx", "Deeper"), ("pages/legacy.tsx", "Legacy"),
                    ("pages/blog/[slug].tsx", "Post"), ("pages/_app.tsx", "App")):
        f[p] = "export default function %s() { return null; }\n" % name
    f["app/api/items/route.ts"] = _lines(
        "export async function GET() { return Response.json([]); }",
        "export async function POST(req: Request) { return Response.json({}, { status: 201 }); }")
    f["app/api/things/route.ts"] = _lines(
        "export function GET() { return Response.json([]); }",
        "export const PATCH = async () => Response.json({});")
    f["app/_private/route.ts"] = "export async function GET() { return Response.json({}); }\n"
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
    f["services/py/requirements.txt"] = "# index token %s\nfastapi==0.110.0\nflask>=3.0\nuvicorn\n" % SECRETS["stripe-live"]
    f["services/py/pyproject.toml"] = _lines("[project]", 'name = "snag-fixture-py"', 'dependencies = ["django>=4.2"]')
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
        '    return "", 204',
        "",
        "",
        '@app.route("/flask/files/<path>", methods=["GET"])',
        "def serve(path):",
        "    return path",
        "",
        "",
        '@app.route("/flask/hello")',
        "def hello():",
        '    return "hello"')
    f["services/py/polls/urls.py"] = _lines(
        "from django.urls import path, re_path",
        "from .views import detail, index, results",
        "",
        "urlpatterns = [",
        '    path("", index),',
        '    path("polls/", index),',
        '    path("polls/<int:poll_id>/", detail),',
        '    path("polls/<slug:name>/results/", results),',
        '    re_path(r"^legacy/$", index),',
        "]")
    f["services/py/polls/views.py"] = "def index(request): ...\n\n\ndef detail(request, poll_id): ...\n\n\ndef results(request, name): ...\n"
    f["services/py/settings.py"] = 'GITHUB_TOKEN = "%s"\n' % SECRETS["github-token"]
    f["services/py/test_widgets.py"] = "from .api.main import app\n\n\ndef test_app():\n    assert app\n"
    f["services/rust/Cargo.toml"] = _lines("[package]", 'name = "snag-fixture"', 'version = "0.1.0"', "", "[dependencies]",
                                           'axum = "0.7"', 'actix-web = "4"', 'tauri = "1"')
    f["services/rust/src/main.rs"] = _lines(
        "use axum::{routing::get, Router};",
        "mod handlers;",
        "mod nested;",
        "mod ghost;",
        "",
        "fn main() {",
        '    let port = std::env::var("AXUM_PORT").unwrap_or_default();',
        '    let tag = env!("BUILD_TAG");',
        "    let app = Router::new()",
        '        .route("/rust/health", get(handlers::health))',
        '        .route("/rust/users/:id", get(handlers::user))',
        '        .route("/rust/users", get(handlers::create));',
        "}")
    f["services/rust/src/handlers.rs"] = "pub async fn health() {}\npub async fn user() {}\npub async fn create() {}\n"
    f["services/rust/src/nested/mod.rs"] = "pub fn nested() {}\n"
    f["services/rails/Gemfile"] = '# deploy key %s\nsource "https://rubygems.org"\ngem "rails", "~> 7.1"\n' % SECRETS["aws-key"]
    f["services/rails/config/routes.rb"] = _lines(
        "Rails.application.routes.draw do",
        '  get "/rails/widgets", to: "widgets#index"',
        '  get "/rails/widgets/:id", to: "widgets#show"',
        "end")
    f["services/rails/config/application.rb"] = 'SECRET = ENV["RAILS_MASTER_FIXTURE"]\n'
    f["services/go/go.mod"] = _lines("// notify " + SECRETS["slack"], "module example.test/svc", "", "go 1.21", "", "require (",
                                     "\tgithub.com/gin-gonic/gin v1.9.1", "\tgithub.com/labstack/echo/v4 v4.11.0",
                                     "\tgithub.com/go-chi/chi/v5 v5.0.10", ")")
    f["services/go/main.go"] = _lines(
        "package main", "", "import (", '\t"fmt"', '\t"example.test/svc/internal/util"', '\t"github.com/gin-gonic/gin"', ")", "",
        "func main() {", "\t_ = gin.New()", '\tfmt.Println(util.Slug("x"))', "}")
    f["services/go/internal/util/util.go"] = "package util\n\nfunc Slug(s string) string { return s }\n"
    f["services/go/internal/util/util_test.go"] = "package util\n\nimport \"testing\"\n\nfunc TestSlug(t *testing.T) {}\n"
    f["mobile_flutter/pubspec.yaml"] = _lines("name: snag_flutter", "dependencies:", "  flutter:", "    sdk: flutter")
    f["android/app/build.gradle"] = _lines("plugins {", "    id 'com.android.application'", "}")
    f["android/app/src/main/AndroidManifest.xml"] = _lines(
        '<?xml version="1.0" encoding="utf-8"?>',
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="com.example.snag">',
        "    <application>",
        '        <activity android:name="com.example.snag.MainActivity" android:exported="true">',
        "            <intent-filter>",
        '                <action android:name="android.intent.action.MAIN"/>',
        '                <category android:name="android.intent.category.LAUNCHER"/>',
        "            </intent-filter>",
        "        </activity>",
        "    </application>",
        "</manifest>")
    return f


REPO_BINARY = {"assets/logo.png": b"\x89PNG\r\n\x1a\n" + _lcg_bytes(512, seed=99)}
_ANDROID_MANIFEST = "android/app/src/main/AndroidManifest.xml"

REPO_ROUTES_BY_FILE = {
    "src/server.js": ["GET /health", "ANY /ping-any", "ANY /static/{rest*}"],
    "src/routes/items.js": ["GET /items", "POST /items", "GET /items/{itemId}", "DELETE /items/{itemId}"],
    "src/events.js": ["POST /events", "GET /events/{eventId}"],
    "app/page.tsx": ["GET /"],
    "app/items/[id]/page.tsx": ["GET /items/{id}"],
    "app/(marketing)/about/page.tsx": ["GET /about"],
    "app/@modal/login/page.tsx": ["GET /login"],
    "app/docs/[...slug]/page.tsx": ["GET /docs/{slug*}"],
    "app/shop/[[...parts]]/page.tsx": ["GET /shop/{parts*}"],
    "app/api/items/route.ts": ["GET /api/items", "POST /api/items"],
    "app/api/things/route.ts": ["GET /api/things", "PATCH /api/things"],
    "app/api/items/[id]/route.ts": ["DELETE /api/items/{id}"],
    "pages/legacy.tsx": ["GET /legacy"],
    "pages/blog/[slug].tsx": ["GET /blog/{slug}"],
    "pages/api/ping.ts": ["ANY /api/ping"],
    "services/py/api/main.py": ["GET /widgets", "POST /widgets", "GET /widgets/{widget_id}"],
    "services/py/flask_app.py": ["GET /flask/ping", "POST /flask/ping", "DELETE /flask/items/{item_id}",
                                 "GET /flask/files/{path}", "GET /flask/hello"],
    "services/py/polls/urls.py": ["ANY /", "ANY /polls", "ANY /polls/{poll_id}", "ANY /polls/{name}/results"],
    "services/rust/src/main.rs": ["GET /rust/health", "GET /rust/users/{id}", "GET /rust/users"],
    "services/rails/config/routes.rb": ["GET /rails/widgets", "GET /rails/widgets/{id}"],
}
REPO_NO_ROUTE_FILES = ["app/_private/page.tsx", "app/_private/deep/page.tsx", "app/_private/route.ts", "pages/_app.tsx"]
REPO_IMPORTS = {
    "bin/cli.js": ["src/server.js"],
    "src/start.js": ["src/server.js"],
    "src/server.js": ["src/config.js", "src/db.js", "src/events.js", "src/lazy.js", "src/lib/index.js",
                      "src/routes/items.js"],
    "src/routes/items.js": ["src/db.js", "src/lib/validate.js"],
    "src/api-client.ts": ["src/lib/util.ts", "src/polyfill.ts", "src/types.ts"],
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
    "services/rust/src/main.rs": ["services/rust/src/handlers.rs", "services/rust/src/nested/mod.rs"],
    "services/go/main.go": ["services/go/internal/util/util.go"],
}
REPO_UNRESOLVED = {   # every specifier that resolves to no file of the target, as written (6.4)
    "src/server.js": ["express"], "src/routes/items.js": ["express"], "src/db.js": ["pg"], "src/events.js": ["fastify"],
    "src/api-client.ts": ["./missing-module"],
    "services/py/api/main.py": ["fastapi", "os", "uvicorn"], "services/py/flask_app.py": ["flask", "os"],
    "services/py/polls/urls.py": ["django.urls"], "services/rust/src/main.rs": ["mod ghost"],
    "services/go/main.go": ["fmt", "github.com/gin-gonic/gin"], "services/go/internal/util/util_test.go": ["testing"],
}
REPO_ENV_BY_FILE = {
    "src/server.js": ["DATABASE_URL", "PORT"], "src/db.js": ["DATABASE_URL", "DB_POOL_SIZE"],
    "src/lib/validate.js": ["STRICT_VALIDATION"], "src/api-client.ts": ["NEXT_PUBLIC_API_URL", "VITE_API_BASE"],
    "services/py/api/main.py": ["PY_DEBUG", "WIDGET_CACHE", "WIDGET_DB_URL"], "services/py/flask_app.py": ["FLASK_SECRET_KEY"],
    "services/rust/src/main.rs": ["AXUM_PORT", "BUILD_TAG"], "services/rails/config/application.rb": ["RAILS_MASTER_FIXTURE"],
}
# source.table records: schema files give access null; code references give read or write (one record per file)
REPO_TABLE_RECORDS = [
    {"key": "items", "path": "db/schema.sql", "access": None}, {"key": "users", "path": "db/schema.sql", "access": None},
    {"key": "widgets", "path": "db/schema.sql", "access": None}, {"key": "notes", "path": "prisma/schema.prisma", "access": None},
    {"key": "items", "path": "src/routes/items.js", "access": "write"}, {"key": "users", "path": "src/db.js", "access": "read"},
    {"key": "widgets", "path": "services/py/api/main.py", "access": "write"},
]
REPO_TESTS = ["__tests__/page.test.tsx", "services/go/internal/util/util_test.go", "services/py/test_widgets.py",
              "src/routes/items.spec.js", "tests/health.test.js", "tests/items.test.js"]
REPO_SECRETS = {"src/config.js": ["aws-key", "private-key", "stripe-live"], "src/api-client.ts": ["bearer"],
                "app/page.tsx": ["google-api-key"], "services/py/settings.py": ["github-token"],
                "config/settings.json": ["slack"], "services/rails/Gemfile": ["aws-key"],
                "services/py/requirements.txt": ["stripe-live"], "services/go/go.mod": ["slack"]}
REPO_FRAMEWORKS = {
    "express": ["package.json"], "fastify": ["package.json"], "next": ["package.json"], "react": ["package.json"],
    "vue": ["apps/vue/package.json"], "nuxt": ["apps/vue/package.json"], "svelte": ["apps/svelte/package.json"],
    "sveltekit": ["apps/svelte/package.json"], "angular": ["apps/ng/package.json"], "nestjs": ["apps/nest/package.json"],
    "electron": ["apps/desktop/package.json"], "react-native": ["apps/mobile/package.json"],
    "expo": ["apps/mobile/package.json"], "django": ["services/py/pyproject.toml"],
    "flask": ["services/py/requirements.txt"], "fastapi": ["services/py/requirements.txt"],
    "axum": ["services/rust/Cargo.toml"], "actix-web": ["services/rust/Cargo.toml"], "tauri": ["services/rust/Cargo.toml"],
    "gin": ["services/go/go.mod"], "echo": ["services/go/go.mod"], "chi": ["services/go/go.mod"],
    "rails": ["services/rails/Gemfile"], "flutter": ["mobile_flutter/pubspec.yaml"], "android": ["android/app/build.gradle"],
}
REPO_ENTRIES = {
    "src/server.js": "main", "bin/cli.js": "bin", "src/start.js": "scripts.start", "app/layout.tsx": "next-layout",
    "services/py/api/main.py": "python-main", "services/rust/src/main.rs": "rust-main", "services/go/main.go": "go-main",
    _ANDROID_MANIFEST + "#com.example.snag.MainActivity": "android-launcher",
}
REPO_TRACE = {
    "GET /items": {
        "seed_files": ["src/routes/items.js"],
        "files": [{"path": "src/routes/items.js", "role": "seed", "depth": 0},
                  {"path": "src/db.js", "role": "uses", "depth": 1},
                  {"path": "src/lib/validate.js", "role": "uses", "depth": 1},
                  {"path": "src/server.js", "role": "used-by", "depth": 1},
                  {"path": "bin/cli.js", "role": "used-by", "depth": 2},
                  {"path": "src/start.js", "role": "used-by", "depth": 2},
                  {"path": "src/routes/items.spec.js", "role": "test", "depth": 1},
                  {"path": "tests/health.test.js", "role": "test", "depth": 1},
                  {"path": "tests/items.test.js", "role": "test", "depth": 1}],
        "env": ["DATABASE_URL", "DB_POOL_SIZE", "PORT", "STRICT_VALIDATION"],
        "tables": ["items", "users"],
    },
}
REPO_SEEDS = {"route": "GET /items", "path": "src/routes/items.js", "symbol": "validateItem",
              "literal": "SELECT * FROM items WHERE id", "missing": "GET /does-not-exist"}
REPO_SEED_FILES = {"route": ["src/routes/items.js"], "path": ["src/routes/items.js"],
                   "symbol": ["src/lib/validate.js"], "literal": ["src/routes/items.js"], "missing": []}


def _repo_records(files):
    """``(other, per_path)``: records without a file (language, framework) and records per file."""
    langs = {}
    for p in sorted(files):
        lang = _LANG_BY_EXT.get(Path(p).suffix)
        if lang:
            d = langs.setdefault(lang, {"files": 0, "bytes": 0})
            d["files"] += 1
            d["bytes"] += len(files[p].encode("utf-8"))
    other = {"source.language": langs, "source.framework": {k: {"from": v} for k, v in REPO_FRAMEWORKS.items()}}
    per = {}

    def add(path, kind, key, data):
        per.setdefault(path, {}).setdefault(kind, {})[key] = data
    for rel, keys in REPO_ROUTES_BY_FILE.items():
        for k in keys:
            add(rel, "source.route", k, {})
    for rel, keys in REPO_ENV_BY_FILE.items():
        for k in keys:
            add(rel, "source.env", k, {})
    for r in REPO_TABLE_RECORDS:
        add(r["path"], "source.table", r["key"], {"access": r["access"]})
    for t in REPO_TESTS:
        add(t, "source.test", t, {"imports": REPO_IMPORTS.get(t, [])})
    for key, via in REPO_ENTRIES.items():
        add(key.split("#")[0], "source.entry", key, {"via": via})
    return other, per


_MANIFEST_NAMES = ("Gemfile", "go.mod", "pubspec.yaml")


def _scanned(rel):
    """Files the source lane reads as text (6.4): the extension list plus the manifests named by `source.framework`."""
    name = rel.rsplit("/", 1)[-1]
    return (Path(rel).suffix in _SCAN_EXT or name in _MANIFEST_NAMES or bool(re.match(r"requirements.*\.txt$", name))
            or name.startswith("build.gradle"))


def make_repo(path, variant="normal"):
    """A small multi-framework repository (see ``_repo_files``). Variants: ``normal``, ``empty`` (a README only) and
    ``malformed`` (truncated package.json and tsconfig, invalid UTF-8 and binary source, a one-line 1 MB script with a
    route at each end, a self-import).

    Facts of ``normal``: ``records_by_path`` (`{path: {kind: {key: data}}}`, every record that has a file),
    ``kinds`` (the records without a file, and only the keys that have a record in exactly one file: `source.env
    DATABASE_URL` and the `source.table` keys are in several, so they are only in ``records_by_path``;
    ``table_records`` lists the table records flat), ``graph`` (`imports`, `unresolved`, `routes`,
    `tests` as in graph.json; only entries with something to list), ``env_by_file``, ``trace`` (expected `study trace
    "GET /items"`), ``seeds`` and ``seed_files`` (one seed of each form, 6.4 step 1), ``no_route_files`` (files that look
    like Next.js routes and are not), ``files`` (every path), ``binary`` (counted, not read).
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
    never = [SECRETS[k] for k in ("aws-key", "stripe-live", "google-api-key", "github-token", "slack", "private-key")] + [TOKEN]
    other, per = _repo_records(files)
    texts = {k: v for k, v in files.items() if _scanned(k)}
    for key, data in _secret_kinds(texts).items():
        per.setdefault(key.split("@", 1)[1], {}).setdefault("secret.embedded", {})[key] = data
    facts = _facts(_split_records(per, other), records_by_path=per, graph=graph, env_by_file=REPO_ENV_BY_FILE,
                   table_records=REPO_TABLE_RECORDS, never_stored=never, trace=REPO_TRACE, seeds=REPO_SEEDS,
                   seed_files=REPO_SEED_FILES, no_route_files=REPO_NO_ROUTE_FILES,
                   files=sorted(list(files) + list(REPO_BINARY)), binary=sorted(REPO_BINARY),
                   must_not={"source.env": ["lowercase_var"]}, variant="normal")
    return root, _with_secrets(facts, texts)


_SCAN_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".rs", ".go", ".rb", ".php", ".java", ".kt", ".cs", ".sql",
             ".prisma", ".vue", ".svelte", ".swift", ".dart", ".ex", ".exs", ".scala", ".json", ".toml", ".yaml", ".yml",
             ".gradle", ".kts", ".xml", ".html", ".md"}


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
    kinds = {"source.route": {"GET /start-of-line": {}, "GET /end-of-line": {}, "GET /after-bad-bytes": {}}}
    return root, _facts(
        kinds,
        files=["go.mod", "package.json", "pyproject.toml", "src/bad_utf8.js", "src/binary.ts", "src/huge-minified.js",
               "src/self.js", "tsconfig.json"],
        skips={"package.json": "malformed", "tsconfig.json": "malformed", "src/binary.ts": "binary"},
        partial_files=["src/bad_utf8.js"], partial_limitation=UTF8_NOTE,
        graph={"imports": {"src/self.js": ["src/self.js"]}},
        expect={"skip": ["malformed", "binary"]},
        no_skip_files=["go.mod", "pyproject.toml"],     # read by line regexes: a broken file is never skipped (6.2 rule 2)
        variant="malformed")


# ----------------------------------------------------------------------------- compare pair


def _at_dot(facts):
    """Compare is per ``(kind, key, path)`` (6.9): single-file targets use the path `.`."""
    for field in ("added", "removed", "changed", "unchanged"):
        for item in facts[field]:
            item.setdefault("path", ".")
    return facts


def _folder_pair(root):
    """Two folders: v1 has `a.js` (moved fact) and `keep.js`; v2 moved the axios call to `b.js`. The moved fact is
    `removed` at `a.js` and `added` at `b.js`; the rest is unchanged."""
    call = 'axios.get("/api/moved");\n'
    keep = 'axios.get("/api/keep");\nt("keep.title");\n'
    for v, files in (("v1", {"a.js": call, "keep.js": keep}), ("v2", {"b.js": call, "keep.js": keep})):
        for rel, text in files.items():
            _write(root / v / rel, text)
    moved = {"kind": "web.api-call", "key": "GET /api/moved"}
    facts = _facts(added=[dict(moved, path="b.js")], removed=[dict(moved, path="a.js")], changed=[],
                   unchanged=[{"kind": "web.api-call", "key": "GET /api/keep", "path": "keep.js"},
                              {"kind": "web.i18n-key", "key": "keep.title", "path": "keep.js"}],
                   artifact_kind="folder", variant="pair")
    return root / "v1", root / "v2", facts


def make_version_pair(root, kind="openapi"):
    """Two versions of one artifact for the compare lane: returns ``(path_v1, path_v2, facts)``.

    The files have the same name in two folders (``root/v1/<name>``, ``root/v2/<name>``), so single-file keys such as
    `secret.embedded` (`<pattern>@.`) are equal on both sides. ``facts`` lists, by ``(kind, key)`` as in 6.9: ``added``
    (v2 only), ``removed`` (v1 only), ``changed`` (both, data differs; with ``before`` and ``after``) and ``unchanged``
    (a few facts present and equal in both, `secret.embedded` included). The two versions differ in nothing else that
    produces a fact, so a compare study has exactly these changes. Kinds: ``openapi`` (data lane), ``har`` (web) and ``folder`` (two script folders; returns the two folders; a fact
    that moved to another file is removed at the old path and added at the new one). Every listed item carries its
    ``path`` (`.` for a single file). Both versions' ``never_stored`` values are in ``facts["never_stored"]``.
    """
    root = Path(root)
    if kind == "openapi":
        d1, d2 = default_openapi_doc(), default_openapi_doc()
        del d2["paths"]["/items/{itemId}"]["delete"]
        d2["paths"]["/items/{itemId}/archive"] = {"post": _op("archiveItem", ["items"])}
        d2["paths"]["/items"]["get"]["operationId"] = "listAllItems"
        d2["paths"]["/items"]["get"]["tags"] = ["items", "v2"]
        d2["components"]["schemas"]["ArchiveRequest"] = {"type": "object"}
        del d2["components"]["schemas"]["Error"]
        a, fa = make_openapi_json(root / "v1" / "api.json", doc=d1)
        b, fb = make_openapi_json(root / "v2" / "api.json", doc=d2)
        assert fa["kinds"]["secret.embedded"] == fb["kinds"]["secret.embedded"]
        sec = [{"kind": "secret.embedded", "key": k} for k in sorted(fa["kinds"]["secret.embedded"])]
        facts = _facts(
            added=[{"kind": "data.endpoint", "key": "POST /items/{itemId}/archive"},
                   {"kind": "data.schema-type", "key": "ArchiveRequest"}],
            removed=[{"kind": "data.endpoint", "key": "DELETE /items/{itemId}"},
                     {"kind": "data.schema-type", "key": "Error"}],
            changed=[{"kind": "data.endpoint", "key": "GET /items",
                      "before": {"operationId": "listItems", "tags": ["items"]},
                      "after": {"operationId": "listAllItems", "tags": ["items", "v2"]}}],
            unchanged=[{"kind": "data.endpoint", "key": "POST /items"}, {"kind": "data.schema-type", "key": "Item"},
                       {"kind": "data.security", "key": "bearerAuth"}] + sec,
            artifact_kind="openapi", variant="pair", never_stored=OPENAPI_NEVER)
        return a, b, _at_dot(facts)
    if kind == "har":
        both = {"Cookie": "session=" + COOKIE, "Authorization": SECRETS["bearer"]}
        e1 = default_har_entries()
        e2 = [e for e in default_har_entries() if "/api/blobs/" not in e["request"]["url"]]
        e2.append(har_entry("GET", _ORIGIN + "/api/reports", 200, t=20, req_headers=both, resp_mime="text/csv", resp_body="a,b\n"))
        e2.append(har_entry("GET", _ORIGIN + "/api/items?page=2&q=fixture-query-value", 500, t=21))
        a, fa = make_har(root / "v1" / "flow.har", e1)
        b, fb = make_har(root / "v2" / "flow.har", e2)
        assert fa["kinds"]["secret.embedded"] == fb["kinds"]["secret.embedded"], "version pair must not change secrets"
        sec = [{"kind": "secret.embedded", "key": k} for k in sorted(fa["kinds"]["secret.embedded"])]
        before = dict(HAR_ENDPOINTS["GET " + _API + "/items"])
        after = dict(before, statuses=[200, 500], count=2)
        facts = _facts(
            added=[{"kind": "web.endpoint", "key": "GET " + _API + "/reports"}],
            removed=[{"kind": "web.endpoint", "key": "GET " + _API + "/blobs/{id}"}],
            changed=[{"kind": "web.endpoint", "key": "GET " + _API + "/items", "before": before, "after": after}],
            unchanged=[{"kind": "web.endpoint", "key": "POST " + _API + "/session/login"},
                       {"kind": "web.host", "key": "analytics.vendor.test"}] + sec,
            artifact_kind="har", variant="pair", never_stored=sorted(set(fa["never_stored"]) | set(fb["never_stored"])),
            note="the new reports response is CSV and the 500 has no body, so neither adds a payload shape; the "
                 "Authorization header moves from the removed blobs entry to the added reports entry, so secret counts stay equal")
        return a, b, _at_dot(facts)
    if kind == "folder":
        return _folder_pair(root)
    raise ValueError("unknown pair kind: %r" % kind)


# ----------------------------------------------------------------------------- tier B: capture

CAPTURE_ACTIONS = ("navigate", "click", "type", "select", "submit", "wait", "observe")
CAPTURE_TYPED = ["tester-one-fixture-value", PASSWORD, "Option-fixture-value"]


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


def _url_key(method, url):
    u = urlsplit(url)
    segs = ["{id}" if (s.isdigit() or re.fullmatch(r"[0-9a-fA-F-]{16,}", s) or re.fullmatch(r"[A-Za-z0-9_-]{20,}", s)) else s
            for s in u.path.split("/") if s]
    return "%s/%s" % (u.hostname, "/".join(segs)) if method is None else "%s %s/%s" % (method, u.hostname, "/".join(segs))


def _capture_kinds(doc):
    """Expected facts of a capture document: steps that are objects with `n` and a known `action` count, the others are
    `malformed` skips (6.10) and `next` points to the next kept step."""
    steps = [s for s in doc["steps"] if isinstance(s, dict) and "n" in s and s.get("action") in CAPTURE_ACTIONS]
    kinds = {"flow.step": {}, "flow.screen": {}, "web.endpoint": {}, "web.payload-shape": {}}
    replaced = {}
    for i, s in enumerate(steps):
        what = _url_key(None, s["url"]) if s["action"] == "navigate" else s.get("target")
        kinds["flow.step"]["%s %s %s" % (s["n"], s["action"], what)] = {
            "screen": s.get("screen"), "next": steps[i + 1]["n"] if i + 1 < len(steps) else None}
        if s.get("screen"):
            kinds["flow.screen"][s["screen"]] = {}
        if "value" in s:
            replaced[str(s["n"])] = "<typed:%d chars>" % len(s["value"])
        for r in s.get("requests", []):
            key = _url_key(r["method"], r["url"])
            ep = kinds["web.endpoint"].setdefault(key, {"statuses": [], "requestTypes": [], "responseTypes": [], "count": 0,
                                                       "queryParams": []})
            ep["statuses"] = sorted(set(ep["statuses"]) | {r["status"]})
            ep["count"] += 1
            ep["queryParams"] = sorted(set(ep["queryParams"]) | {k for k, _ in parse_qsl(urlsplit(r["url"]).query)})
            for d, field in (("request", "requestShape"), ("response", "responseShape")):
                if field in r:
                    kinds["web.payload-shape"]["%s %s" % (key, d)] = {"shape": r[field]}
    return kinds, replaced


def _capture_facts(doc, **extra):
    kinds, replaced = _capture_kinds(doc)
    return _facts(kinds, replaced=replaced, never_stored=CAPTURE_TYPED + ["fixture-query-value"], **extra)


def make_capture(path):
    """Tier B capture file (`app-blueprint-capture/1`, 6.10) with real-looking typed values (a user name and a
    password) that the importer must replace with `<typed:N chars>` (``replaced``), URLs with query values that must be
    dropped (query names stay in `queryParams`), and request and response shapes. `flow.step` data is
    `{"screen", "next"}`; a `navigate` key is `<n> navigate host/path-template`."""
    return _write(path, _dump(_capture_doc())), _capture_facts(_capture_doc(), variant="normal")


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
REA_DROPPED = "link ev-9999 not in this bundle; dropped"


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
_SRC = {"artifact": "Acme.apk", "digest": "sha256:" + REA_SHA}
REA_RECORDS = [   # expected mapping of 5.8, one entry per imported record; `links` are "kind:key" of the target record
    {"kind": "rea.inspect-artifact", "key": "inspect_artifact:AndroidManifest.xml", "basis": "observed",
     "source": dict(_SRC, path="AndroidManifest.xml", offset=None), "links": [],
     "limitations": ["fixture limitation", REA_NOTE], "coverage": "unknown", "by": "import:rea"},
    {"kind": "rea.search-strings", "key": "search_strings:4096", "basis": "inferred",
     "source": dict(_SRC, path=None, offset=4096), "links": ["rea.inspect-artifact:inspect_artifact:AndroidManifest.xml"],
     "limitations": ["fixture limitation", REA_NOTE], "coverage": "unknown", "by": "import:rea"},
    {"kind": "rea.procedure-info", "key": "procedure_info:0x401000", "basis": "derived",
     "source": dict(_SRC, path=None, offset=None), "data_address": "0x401000",
     "links": ["rea.search-strings:search_strings:4096"],
     "limitations": ["fixture limitation", REA_NOTE, REA_DROPPED], "coverage": "unknown", "by": "import:rea"},
]


def _rea_tolerant():
    ok = _rea_rec("ev-t1", "inspect_artifact", {"kind": "artifact-path", "path": "res/values/strings.xml"}, "observed",
                  {"strings": 3})
    ok["future_field"] = {"anything": [1, 2, 3]}
    no_subject = _rea_rec("ev-t2", "list_strings", {"kind": "artifact-path", "path": "x"}, "observed", {})
    del no_subject["subject"]
    no_operation = _rea_rec("ev-t3", "x", {"kind": "artifact-path", "path": "x"}, "observed", {})
    del no_operation["operation"]
    bare = {"evidence_id": "ev-t4", "operation": "list_strings", "subject": {"name": "Acme.apk"}, "confidence": "weird",
            "normalized_result": {"n": 1}}
    huge = _rea_rec("ev-t5", "search_bytes", {"kind": "artifact-path", "path": "classes.dex"}, "observed",
                    {"blob": ["x" * 100] * 60})
    huge["locations"] = []
    mixed = _rea_rec("ev-t6", "xrefs", {"kind": "address", "address": "0x10"}, "observed", {"n": 2}, ["ev-t1", {"x": 1}])
    return {"evidence_format": 3, "records": [ok, no_subject, no_operation, bare, huge, mixed, 42], "extra": True}


REA_TOLERANT_RECORDS = [
    {"kind": "rea.inspect-artifact", "key": "inspect_artifact:res/values/strings.xml", "basis": "observed",
     "limitations": ["fixture limitation", REA_NOTE]},
    {"kind": "rea.list-strings", "key": "list_strings:Acme.apk", "basis": "inferred", "limitations": [REA_NOTE],
     "source": {"artifact": "Acme.apk", "digest": None, "path": None, "offset": None}},
    {"kind": "rea.search-bytes", "key": "search_bytes:Acme.apk", "basis": "observed", "limitations_include": [REA_NOTE, "data trimmed"],
     "data_max_bytes": 4096, "claim_max_chars": 300},
    {"kind": "rea.xrefs", "key": "xrefs:0x10", "basis": "observed", "data_address": "0x10",
     "links": ["rea.inspect-artifact:inspect_artifact:res/values/strings.xml"],
     "limitations": ["fixture limitation", REA_NOTE, "link not understood; dropped"]},
]


def make_rea_bundle(path, variant="normal"):
    """rea evidence bundle (5.8; the record layout is partly unverified, so tolerant variants exist).

    Variants: ``normal`` (three records and one unknown; ``records`` holds the expected mapping per record, with the
    dropped-link limitation of 5.8), ``empty`` (no records), ``tolerant`` (`evidence_format` 3 and seven entries:
    unknown extra fields, a record without `subject`, one without `operation`, a bare one with a confidence not ours,
    one with a result far over 4 KB and no location, links as a string and an object nobody understands, and a bare
    number; four import, three are `malformed` skips at `records[1]`, `records[2]`, `records[6]`), ``not_rea`` (no
    `evidence_format`: `target-unsupported`). `kind` is `"rea." + slug(operation)`, where `core.slug` turns runs of
    characters outside `[a-z0-9]` into `-`.
    """
    if variant == "normal":
        p = _write(path, _dump(_rea_doc()))
        return p, _facts(REA_KINDS, records=REA_RECORDS, counts={"imported": 3, "malformed": 0, "unknowns": 1},
                         never_stored=[REA_ABS_PATH], secret_patterns=[], variant="normal")
    if variant == "empty":
        p = _write(path, _dump({"evidence_format": 2, "records": []}))
        return p, _facts(counts={"imported": 0, "malformed": 0, "unknowns": 0}, secret_patterns=[], variant="empty")
    if variant == "tolerant":
        kinds = {"rea.inspect-artifact": {"inspect_artifact:res/values/strings.xml": {}},
                 "rea.list-strings": {"list_strings:Acme.apk": {}}, "rea.search-bytes": {"search_bytes:Acme.apk": {}},
                 "rea.xrefs": {"xrefs:0x10": {}}}
        p = _write(path, _dump(_rea_tolerant()))
        return p, _facts(kinds, records=REA_TOLERANT_RECORDS, counts={"imported": 4, "malformed": 3, "unknowns": 0},
                         malformed_paths=["records[1]", "records[2]", "records[6]"],
                         note="unrecognised evidence_format 3", never_stored=[REA_ABS_PATH], secret_patterns=[],
                         variant="tolerant")
    if variant == "not_rea":
        doc = _rea_doc()
        del doc["evidence_format"]
        p = _write(path, _dump(doc))
        return p, _facts(expect={"error": "target-unsupported"}, variant="not_rea")
    raise KeyError(variant)


# ----------------------------------------------------------------------------- tier C: JADX and apktool folders

_ANDROID_NS = "http://schemas.android.com/apk/res/android"


def _manifest_xml(doctype=False):
    head = '<?xml version="1.0" encoding="utf-8"?>\n'
    name = "1.4.2"
    if doctype:
        head += '<!DOCTYPE manifest [<!ENTITY fixture "x">]>\n'
        name = "&fixture;"
    return head + _lines(
        '<manifest xmlns:android="%s" android:versionCode="12" android:versionName="%s" package="com.example.snag">' % (_ANDROID_NS, name),
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


def _svc(kind, exported):
    return {"type": kind, "exported": exported}


MANIFEST_KINDS = {
    "mobile.package": {"com.example.snag": {"versionName": "1.4.2", "versionCode": 12, "minSdk": 24, "targetSdk": 34}},
    "mobile.permission": {"android.permission.CAMERA": {}, "android.permission.INTERNET": {}},
    "mobile.screen": {"com.example.snag.DetailActivity": {"exported": False, "launcher": False},
                      "com.example.snag.LinkActivity": {"exported": True, "launcher": False},
                      "com.example.snag.MainActivity": {"exported": True, "launcher": True}},
    "mobile.service": {"com.example.snag.BootReceiver": _svc("receiver", True),
                       "com.example.snag.FileProvider": _svc("provider", False),
                       "com.example.snag.SyncService": _svc("service", False)},
    "mobile.deeplink": {"snag://items/open": {"activity": "com.example.snag.LinkActivity", "autoVerify": True}},
    "mobile.setting": {"usesCleartextTraffic": {"value": "true"}, "debuggable": {"value": "true"},
                       "allowBackup": {"value": "false"},
                       "networkSecurityConfig": {"value": "@xml/network_security_config"}},
}
ENDPOINT_LITERALS = ["https://api.example.test/v1/items", "/api/v2/ping", "https://example.test/docs/privacy",
                     "https://api.example.test/v1/session", "/v1/health"]
SDK_KINDS = {"com/google/firebase": {"classes": 1}, "io/sentry": {"classes": 1}}      # known SDK prefixes only


def _java_files(prefix):
    p = prefix
    return {
        p + "com/example/snag/MainActivity.java": _lines(
            "package com.example.snag;", "", "import com.google.firebase.FirebaseApp;", "",
            "public class MainActivity extends android.app.Activity {",
            '    static final String BASE = "https://api.example.test/v1/items";',
            '    static final String PING = "/api/v2/ping";',
            '    static final String DOCS = "https://example.test/docs/privacy";',
            '    static final String NOTE = "see https://api.example.test/v1/note for details";',   # not a whole literal
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


def _android_facts(files, kinds, sources, **extra):
    """Shared facts of JADX and apktool folders: manifest kinds, SDK histogram, endpoints with locations, secrets."""
    facts = _facts(kinds, **extra)
    if sources:
        facts["kinds"]["mobile.sdk"] = {k: dict(v) for k, v in SDK_KINDS.items()}
        facts["kinds"]["mobile.endpoint"] = {e: {"from": "source"} for e in extra.get("endpoints", ENDPOINT_LITERALS)}
        facts["locations"] = _locations(files, facts["kinds"]["mobile.endpoint"])
    texts = {k: _decode(v) if isinstance(v, bytes) else v for k, v in files.items()
             if isinstance(v, str) or (isinstance(v, bytes) and b"\x00" not in v[:8192])}
    return _with_secrets(facts, texts)


def make_jadx_dir(root, variant="normal"):
    """JADX output folder (5.8). Variants: ``normal`` (`resources/AndroidManifest.xml` text manifest, `sources/**`),
    ``gradle`` (export-gradle layout `app/src/main/...`, same facts), ``empty`` (empty `sources/` and `resources/`),
    ``no_manifest``, ``no_sources``, ``doctype`` (manifest holds `<!DOCTYPE`/`<!ENTITY` and uses the entity: refused as
    a `malformed` skip with the note `DOCTYPE or ENTITY refused`, not listed in `missing`; sources still read),
    ``bad_manifest`` (truncated XML: `malformed` skip), ``binary_manifest`` (NUL in the first 8 KiB: `binary` skip),
    ``unsupported_manifest`` (text that does not start with `<?xml` or `<manifest`: `unsupported` skip). `gradle`
    expects `app/src/main/AndroidManifest.xml` and `app/src/main/java/` (scanned like `sources/`) and nothing missing.
    Facts: the `mobile.*` kinds with their 6.7 data, ``locations`` of each endpoint literal (path and line), ``skips``
    (path -> reason), ``notes``, and ``missing`` (the expected parts of 5.8 not found).
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if variant == "empty":
        (root / "sources").mkdir(exist_ok=True)
        (root / "resources").mkdir(exist_ok=True)
        return root, _facts(missing=["resources/AndroidManifest.xml"], variant="empty", secret_patterns=[])
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
    elif variant == "unsupported_manifest":
        files[man_path] = "this is not a manifest\n"
    elif variant != "no_manifest":
        raise KeyError(variant)
    for rel in sorted(files):
        _write(root / rel, files[rel])
    good = variant in ("normal", "gradle", "no_sources")
    kinds = {k: dict(v) for k, v in MANIFEST_KINDS.items()} if good else {}
    missing = []
    if variant == "no_manifest":
        missing.append("resources/AndroidManifest.xml")
    if variant == "no_sources":
        missing.append("sources/")
    extra = {}
    if variant == "doctype":
        extra.update(skips={man_path: "malformed"}, notes=["DOCTYPE or ENTITY refused"])
    elif variant == "bad_manifest":
        extra.update(skips={man_path: "malformed"})
    elif variant == "binary_manifest":
        extra.update(skips={man_path: "binary"})
    elif variant == "unsupported_manifest":
        extra.update(skips={man_path: "unsupported"})
    facts = _android_facts(files, kinds, variant != "no_sources", missing=missing,
                           never_stored=[SECRETS["aws-key"]], variant=variant, **extra)
    return root, facts


APKTOOL_YML = _lines(
    "!!brut.androlib.meta.MetaInfo", "apkFileName: snag.apk", "compressionType: false", "doNotCompress:", "- resources.arsc",
    "isFrameworkApk: false", "packageInfo:", "  forcedPackageId: '127'", "sdkInfo:", "  minSdkVersion: '24'",
    "  targetSdkVersion: '34'", "sharedLibrary: false", "sparseResources: false", "usesFramework:", "  ids:", "  - 1",
    "version: 2.9.3", "versionInfo:", "  versionCode: '12'", "  versionName: 1.4.2")
APKTOOL_VALUES = {"minSdkVersion": 24, "targetSdkVersion": 34, "versionCode": 12, "versionName": "1.4.2"}
STRINGS_XML = _lines('<?xml version="1.0" encoding="utf-8"?>', "<resources>",
                     '    <string name="app_name">Snag-fixture-text</string>',
                     '    <string name="welcome">Welcome-fixture-text</string>',
                     '    <string name="empty_state">Nothing-here-fixture-text</string>', "</resources>")


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
    in two folders), ``empty`` (only an `apktool.yml` with no keys), ``no_manifest``, ``binary_manifest`` (`binary`
    skip), ``no_smali``, ``doctype`` (refused: `malformed` skip, note `DOCTYPE or ENTITY refused`, not in `missing`),
    ``bad_yml`` (garbage instead of YAML), ``unquoted`` (versions without quotes: same values),
    ``unsupported_manifest`` (`unsupported` skip). Facts: ``yml`` (the four values the line regexes find, integers
    where all digits; without a manifest they produce no record, because `mobile.package` needs the package name),
    ``strings_count`` (counts only, no UI text), `mobile.endpoint` with ``locations``, `mobile.sdk` from the `.class`
    lines, ``skips``, ``notes``, ``missing``."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if variant == "empty":
        _write(root / "apktool.yml", "!!brut.androlib.meta.MetaInfo\n")
        return root, _facts(yml={}, missing=["AndroidManifest.xml", "smali/", "res/values/strings.xml"], secret_patterns=[],
                            variant="empty")
    if variant not in ("normal", "no_manifest", "binary_manifest", "no_smali", "doctype", "bad_yml", "unquoted",
                       "unsupported_manifest"):
        raise KeyError(variant)
    files = {"apktool.yml": APKTOOL_YML, "res/values/strings.xml": STRINGS_XML}
    if variant == "unquoted":
        files["apktool.yml"] = APKTOOL_YML.replace("'", "")
    elif variant == "bad_yml":
        files["apktool.yml"] = "{{{ not: [yaml\n"
    if variant == "binary_manifest":
        files["AndroidManifest.xml"] = _AXML_STUB
    elif variant == "doctype":
        files["AndroidManifest.xml"] = _manifest_xml(doctype=True)
    elif variant == "unsupported_manifest":
        files["AndroidManifest.xml"] = "this is not a manifest\n"
    elif variant != "no_manifest":
        files["AndroidManifest.xml"] = _manifest_xml()
    if variant != "no_smali":
        files.update(_smali_files())
    for rel in sorted(files):
        _write(root / rel, files[rel])
    good = variant not in ("no_manifest", "binary_manifest", "doctype", "unsupported_manifest")
    kinds = {k: dict(v) for k, v in MANIFEST_KINDS.items()} if good else {}
    extra = {}
    if variant == "unsupported_manifest":
        extra.update(skips={"AndroidManifest.xml": "unsupported"})
    if variant == "doctype":
        extra.update(skips={"AndroidManifest.xml": "malformed"}, notes=["DOCTYPE or ENTITY refused"])
    if variant == "binary_manifest":
        extra.update(skips={"AndroidManifest.xml": "binary"})
    facts = _android_facts(files, kinds, variant != "no_smali", endpoints=ENDPOINT_LITERALS[:2],
                           yml={} if variant == "bad_yml" else APKTOOL_VALUES, strings_count=3,
                           missing=["smali/"] if variant == "no_smali" else ["AndroidManifest.xml"] if variant == "no_manifest" else [],
                           never_stored=[SECRETS["aws-key"], "Snag-fixture-text", "Welcome-fixture-text",
                                         "Nothing-here-fixture-text"], variant=variant, **extra)
    return root, facts


# ----------------------------------------------------------------------------- empty and malformed variants

_WHOLE_JSON = ("har", "sourcemap", "openapi_json")        # must parse as a whole: `malformed`, `depth`, `binary`
_IMPORTS = {"capture": "bad-capture", "rea_bundle": "target-unsupported"}   # one-file importers: an error, no study
_SCANNED = ("bundle", "openapi_yaml", "graphql", "proto")   # regex and line scanners: they read what they have
_COMMON = ["zero_bytes", "empty", "truncated", "invalid_utf8", "binary", "minified_1mb", "too_large"]
VARIANTS = {
    "har": _COMMON + ["invalid_utf8_structure", "deep", "deep_body", "huge_body", "bad_entries",
                                                  "bad_json_body", "bad_base64_body"],
    "sourcemap": _COMMON + ["invalid_utf8_structure", "deep", "outside_root", "bad_version"],
    "bundle": _COMMON + ["pathological_1mb"],
    "sqlite": ["zero_bytes", "empty", "truncated", "binary", "header_only", "oversized"],
    "plist": ["zero_bytes", "empty", "truncated", "truncated_binary", "invalid_utf8", "invalid_utf8_structure", "binary",
              "deep", "many_keys", "minified_1mb", "too_large"],
    "openapi_json": _COMMON + ["invalid_utf8_structure", "deep"],
    "openapi_yaml": _COMMON + ["crlf"],
    "graphql": list(_COMMON),
    "proto": list(_COMMON),
    "capture": _COMMON + ["deep", "wrong_format", "bad_action", "steps_not_list", "step_missing_n"],
    "rea_bundle": _COMMON + ["deep", "tolerant", "not_rea"],
}
CAPS = {"capture": TEXT_CAP, "rea_bundle": 64 * 1024 * 1024}      # 6.2: a capture over 5 MiB, a rea bundle over 64 MiB


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


def _bad(variant, skips=None, error=None, kinds=None, **extra):
    """Facts of a variant. ``skips``: acceptable skip reasons (``None`` = nothing emitted and no skip is also right);
    ``error``: the importer error. ``kinds`` is empty unless given."""
    expect = {"error": error} if error else {"skip": skips}
    return _facts(kinds, variant=variant, expect=expect, **extra)


def _refit(facts, text, **extra):
    """The good facts again for a changed text: same kinds, secrets recounted."""
    out = dict(facts, **extra)
    out["kinds"] = {k: v for k, v in facts["kinds"].items() if k != "secret.embedded"}
    return _with_secrets(out, {".": text})


def _partial(facts, text, variant):
    """Facts of a text with undecodable bytes away from every fact: the good facts, `coverage: partial` and the note."""
    out = _refit(facts, text, variant=variant, coverage="partial")
    out["limitations"] = list(facts.get("limitations", [])) + [UTF8_NOTE]
    out["expect"] = {"skip": [None]}
    return out


def _insert(data, anchor, extra, after=None):
    at = data.index(anchor) + (len(anchor) if after is None else after)
    return data[:at] + extra + data[at:]


_DAMAGE = {   # anchor, offset: a harmless place inside a string no fact depends on
    "har": (b"fixture-recorder", 8), "sourcemap": (b'"React"', 4), "openapi_json": (b"Snag fixture", 4),
    "plist": (b"<string>Snag</string>", 10), "bundle": (b"synthetic", 4), "openapi_yaml": (b"Snag fixture", 4),
    "graphql": (b"Synthetic", 4), "proto": (b"example.snag", 8),
}
_STRUCTURE = {"har": b'"version"', "sourcemap": b'"version"', "openapi_json": b'"openapi"', "plist": b"<key"}
_CUT = {   # scanned kinds: the cut ends after this line; facts are literal for that prefix
    "bundle": 'axios.delete("/api/items");\n', "openapi_yaml": "      operationId: updateItem\n",
    "graphql": "  items(first: Int = 10, status: Status): [Item!]!\n",
    "proto": "  rpc ListItems(ListItemsRequest) returns (stream Item);\n",
}
_CUT_KINDS = {
    "bundle": {"web.route": {k: v for k, v in BUNDLE_KINDS["web.route"].items() if k != "/blog/{slug}"},
               "web.api-call": {k: v for k, v in BUNDLE_KINDS["web.api-call"].items() if k.endswith("/api/items")}},
    "openapi_yaml": {"data.endpoint": {k: {"operationId": None, "tags": []} for k in
                                       ("GET /items", "POST /items", "GET /items/{itemId}", "PUT /items/{itemId}")}},
    "graphql": {"data.schema-type": {k: v for k, v in GRAPHQL_KINDS["data.schema-type"].items()
                                     if k not in ("Mutation", "Subscription")},
                "data.endpoint": {"query items": {}}},
    "proto": {"data.schema-type": PROTO_KINDS["data.schema-type"],
              "data.rpc": {k: v for k, v in PROTO_KINDS["data.rpc"].items() if k in ("ItemService.GetItem", "ItemService.ListItems")}},
}


def _empty_variant(kind, path):
    if kind == "har":
        p = make_har(path, [])[0]
        return p, _text_facts(p, {}, variant="empty", entry_count=0, expect={"skip": [None]})
    if kind == "sourcemap":
        p = make_sourcemap(path, [], [])[0]
        return p, _text_facts(p, {}, variant="empty", expect={"skip": [None]})
    if kind == "bundle":
        p = _write(path, "")
        return p, _text_facts(p, {}, variant="empty", expect={"skip": [None]})
    if kind == "sqlite":
        p = make_sqlite(path, ["CREATE TABLE gone (a)", "DROP TABLE gone"])[0]
        return p, _facts(variant="empty", secret_patterns=[], expect={"skip": [None]})
    if kind == "plist":
        p = make_plist(path, "xml", {})[0]
        return p, _text_facts(p, {}, variant="empty", expect={"skip": [None]})
    if kind == "openapi_json":
        p = make_openapi_json(path, {"openapi": "3.0.3", "info": {"title": "Empty", "version": "1"}, "paths": {}})[0]
        return p, _text_facts(p, {}, variant="empty", expect={"skip": [None]})
    if kind == "openapi_yaml":
        p, facts = make_openapi_yaml(path, "empty")
        return p, dict(facts, expect={"skip": [None]})
    if kind == "graphql":
        p = _write(path, "# nothing here\n")
        return p, _text_facts(p, {}, variant="empty", expect={"skip": [None]})
    if kind == "proto":
        p = _write(path, 'syntax = "proto3";\n')
        return p, _text_facts(p, {}, variant="empty", expect={"skip": [None]})
    if kind == "capture":
        p = _write(path, _dump(_capture_variant("empty")))
        return p, _capture_facts(_capture_variant("empty"), variant="empty", expect={"skip": [None]})
    if kind == "rea_bundle":
        p, facts = make_rea_bundle(path, "empty")
        return p, dict(facts, expect={"skip": [None]})
    raise KeyError(kind)


def _padded_good(kind, path, name):
    """Good fixture plus one 1 MB line of padding that carries no facts: the facts of the good fixture still hold."""
    pad = "a" * ONE_MB
    if kind in ("har", "sourcemap", "openapi_json", "capture", "rea_bundle"):
        _, facts = _good(kind, path)
        doc = json.loads(Path(path).read_bytes().decode("utf-8"))
        (doc["log"] if kind == "har" else doc)["x-padding"] = pad
        text = json.dumps(doc, separators=(",", ":")) + "\n"
        _write(path, text)
    elif kind == "openapi_yaml":
        _, facts = _good(kind, path)
        text = Path(path).read_text(encoding="utf-8") + "x-padding: " + pad + "\n"
        _write(path, text)
    elif kind in ("graphql", "proto"):
        _, facts = _good(kind, path)
        text = Path(path).read_text(encoding="utf-8") + ("# " if kind == "graphql" else "// ") + pad + "\n"
        _write(path, text)
    elif kind == "plist":
        _, facts = _good(kind, path)
        make_plist(path, "xml", dict(DEFAULT_PLIST, Padding=pad))
        text = _decode(Path(path).read_bytes())
        facts = _facts(dict(facts["kinds"], **{"data.plist-key": dict(PLIST_KINDS["data.plist-key"], Padding=_t("string"))}),
                       never_stored=PLIST_NEVER)
    elif kind == "bundle":
        js, facts = make_bundle()
        filler = "".join("var a%d=function(){return %d};" % (i, i) for i in range(1, 30000))
        text = js.rstrip("\n").replace("\n", " ") + filler + ' var z = { path: "/end-of-line" };'
        _write(path, text)
        facts = _facts(dict(facts["kinds"], **{"web.route": dict(BUNDLE_KINDS["web.route"], **{"/end-of-line": {}})}),
                       never_stored=facts["never_stored"])
    else:
        raise KeyError(kind)
    out = _refit(facts, text, variant=name, single_line_bytes=Path(path).stat().st_size, expect={"skip": [None]})
    return Path(path), out


def make_variant(path, kind, name):
    """The empty and malformed cases of ``kind`` (see ``VARIANTS``). Returns ``(path, facts)``. ``facts["expect"]``
    follows the 6.2 reading rules: ``{"skip": [...]}`` (``None`` = nothing emitted without a skip is also right) or
    ``{"error": ...}`` for the one-file importers; ``facts["kinds"]`` holds what a lane must still emit (the facts
    readable in a cut prefix or away from damaged bytes, or the full facts of a valid variant such as ``minified_1mb``,
    ``crlf``, ``deep_body``, ``huge_body``, ``many_keys``, ``outside_root``)."""
    if name not in VARIANTS[kind]:
        raise KeyError("%s has no variant %r" % (kind, name))
    path = Path(path)
    imp = _IMPORTS.get(kind)
    if name == "empty":
        return _empty_variant(kind, path)
    if name == "zero_bytes":
        if imp:
            return _write(path, b""), _bad(name, error=imp)
        skips = ["malformed"] if kind in _WHOLE_JSON else [None] if kind in ("bundle", "graphql", "proto") else ["unsupported"]
        return _write(path, b""), _bad(name, skips)
    if name == "binary":
        data = _lcg_bytes(4096)
        if imp:
            return _write(path, data), _bad(name, error=imp)
        skips = ["unsupported"] if kind in ("sqlite", "plist") else ["binary"]
        return _write(path, data), _bad(name, skips)
    if name == "minified_1mb":
        return _padded_good(kind, path, name)
    if name == "pathological_1mb":
        p, facts = make_pathological_line(path, ONE_MB)
        return p, dict(facts, variant=name)
    if name == "too_large":
        _good(kind, path)
        data = Path(path).read_bytes()
        cap = CAPS.get(kind, TEXT_CAP)
        _write(path, data + b" " * (cap + 1 - len(data)))
        if imp:
            return path, _bad(name, error=imp, size=cap + 1)
        return path, _bad(name, ["too-large"], size=cap + 1)
    if name == "oversized":       # SQLite has no read cap (6.2): a database over 5 MiB is read like any other
        ddl = list(DEFAULT_DDL) + ["CREATE TABLE padding (b BLOB)", "INSERT INTO padding VALUES (zeroblob(5500000))"]
        p, facts = make_sqlite(path, ddl)
        facts.update(variant=name, expect={"skip": [None]}, size=p.stat().st_size)
        return p, facts
    if name in ("truncated", "truncated_binary"):
        if kind == "plist":
            make_plist(path, "binary" if name == "truncated_binary" else "xml")
        else:
            _good(kind, path)
        data = Path(path).read_bytes()
        if imp:
            _write(path, data[:len(data) * 6 // 10])
            return path, _bad(name, error=imp)
        if kind in _CUT:
            text = data.decode("utf-8")
            cut = text[:text.index(_CUT[kind]) + len(_CUT[kind])]
            _write(path, cut)
            extra = {"limitations": [UNCLOSED_NOTE]} if kind in ("graphql", "proto") else {}
            if kind == "openapi_yaml":
                extra = {"limitations": ["YAML read by a line scanner"]}
            return path, _text_facts(path, _CUT_KINDS[kind], variant=name, expect={"skip": [None]}, **extra)
        _write(path, data[:2000 if kind == "sqlite" else len(data) * 6 // 10])
        return path, _bad(name, ["malformed"], **({"secret_patterns": []} if kind == "sqlite" else {}))
    if name == "invalid_utf8" or name == "invalid_utf8_structure":
        if kind == "plist":
            make_plist(path, "xml")
        else:
            _, good_facts = _good(kind, path)
        data = Path(path).read_bytes()
        if imp:        # one-file importers: the damage breaks the syntax, so the file cannot be detected
            key = b'"records"' if kind == "rea_bundle" else b'"steps"'
            _write(path, _insert(data, key, b"\xff"))
            return path, _bad(name, error=imp)
        if name == "invalid_utf8_structure":
            _write(path, _insert(data, _STRUCTURE[kind], b"\xff"))
            return path, _bad(name, ["malformed"])
        anchor, off = _DAMAGE[kind]
        data = _insert(data, anchor, b"\xff\xfe", after=off)
        _write(path, data)
        if kind == "plist":
            good_facts = _facts(PLIST_KINDS, never_stored=PLIST_NEVER)
        return path, _partial(good_facts, _decode(data), name)
    if name == "deep":
        if kind == "plist":
            d = {"leaf": "x"}
            for i in range(7, -1, -1):
                d = {"k%d" % i: d}
            make_plist(path, "xml", d)
            paths = {".".join("k%d" % j for j in range(n)): {"type": "dict"} for n in range(1, 7)}
            return path, _text_facts(path, {"data.plist-key": paths}, variant=name, max_depth=6, limitations=[DEPTH_NOTE],
                                     must_not={"data.plist-key": ["k0.k1.k2.k3.k4.k5.k6"]}, expect={"skip": [None]})
        _write(path, _deep_json(50000))
        if imp:
            return path, _bad(name, error=imp, nesting=50000)
        return path, _bad(name, ["depth"], nesting=50000)
    if name == "many_keys":
        make_plist(path, "xml", {"key%03d" % i: i for i in range(600)})
        kept = {"key%03d" % i: {"type": "integer"} for i in range(500)}
        return path, _text_facts(path, {"data.plist-key": kept}, variant=name, max_keys=500, total_keys=600,
                                 limitations=["more than 500 plist key paths; 100 not recorded"], expect={"skip": [None]})
    if name == "header_only":
        return _write(path, b"SQLite format 3\x00" + b"\x00" * 4080), _bad(name, ["malformed"], secret_patterns=[])
    if name == "crlf":
        p, facts = make_openapi_yaml(path, "crlf")
        return p, dict(facts, expect={"skip": [None]})
    if name == "outside_root":
        sources = ["../../../outside-root/secret.js", "/srv/outside/app.js", "C:\\outside\\app.js", "webpack:///./src/ok.js"]
        doc = {"version": 3, "file": "main.js", "sourceRoot": "../../..", "sources": sources,
               "sourcesContent": ["// %s\n" % MAP_MARKER] + [None] * 3, "names": [], "mappings": "AAAA"}
        p = _write(path, _dump(doc))
        return p, _text_facts(p, {"web.source-module": {"(outside)": {"sources": 3}, "src": {"sources": 1}}},
                              unsafe_sources=sources[:3], variant=name, never_stored=[MAP_MARKER] + sources[:3],
                              expect={"skip": [None], "note": "no file outside the root may be opened; no unsafe source "
                                                              "path appears in any stored key, claim or excerpt"})
    if name == "bad_version":
        p = _write(path, _dump({"version": 2, "sources": "not-a-list", "sourcesContent": 7, "mappings": 1}))
        return p, _bad(name, ["malformed", None])
    if name == "deep_body":
        e = har_entry("GET", _ORIGIN + "/api/deep", 200, t=0, resp_mime=_JSON, resp_body=_deep_json(50000))
        p = make_har(path, [e])[0]
        key = "GET " + _API + "/deep"
        lim = ["body nested deeper than the parser allows; not shaped"]
        return p, _text_facts(p, {"web.endpoint": {key: _ep([200], [], [_JSON])}}, variant=name, entry_count=1, nesting=50000,
                              limitations=lim, record_limitations={"web.endpoint " + key: lim}, expect={"skip": [None]},
                              never_stored=[])
    if name in ("bad_json_body", "bad_base64_body"):
        good = default_har_entries()[2]
        if name == "bad_json_body":
            bad = har_entry("GET", _ORIGIN + "/api/broken", 200, t=1, resp_mime=_JSON, resp_body="{not json")
            lim = ["body not parseable as JSON; not shaped"]
        else:
            bad = har_entry("GET", _ORIGIN + "/api/broken", 200, t=1, resp_mime=_JSON, resp_body="xx")
            bad["response"]["content"].update(text="@@@@", encoding="base64")
            lim = ["body not valid base64; not shaped"]
        p = make_har(path, [good, bad])[0]
        ik = "GET " + _API + "/items"
        bk = "GET " + _API + "/broken"
        kinds = {"web.endpoint": {ik: HAR_ENDPOINTS[ik], bk: _ep([200], [], [_JSON])},
                 "web.payload-shape": {ik + " response": {"shape": HAR_SHAPES[ik + " response"]}}}
        return p, _text_facts(p, kinds, variant=name, entry_count=2, limitations=lim,
                              record_limitations={"web.endpoint " + bk: lim}, never_stored=har_never_stored([good, bad]),
                              expect={"skip": [None]})
    if name == "huge_body":
        e = har_entry("GET", _ORIGIN + "/api/huge", 200, t=0, resp_mime=_JSON, resp_body='{"blob":"%s"}' % ("a" * 3_000_000))
        p = make_har(path, [e])[0]
        kinds = {"web.endpoint": {"GET " + _API + "/huge": _ep([200], [], [_JSON])},
                 "web.payload-shape": {"GET " + _API + "/huge response": {"shape": {"blob": "string"}}}}
        return p, _text_facts(p, kinds, variant=name, entry_count=1, body_bytes=3_000_000, expect={"skip": [None]})
    if name == "bad_entries":
        entries = default_har_entries()
        good = entries[2]
        doc = _har_doc([{"startedDateTime": _iso(0)}, {"request": "nope", "response": 7}, good,
                        {"request": {"method": "GET"}, "response": {}}, None, 42])
        p = _write(path, _dump(doc))
        key = "GET " + _API + "/items"
        kinds = {"web.endpoint": {key: HAR_ENDPOINTS[key]},
                 "web.payload-shape": {key + " response": {"shape": HAR_SHAPES[key + " response"]}}}
        return p, _text_facts(p, kinds, variant=name, entry_count=6, valid_entries=1, never_stored=har_never_stored([good]),
                              expect={"skip": [None, "malformed"]})
    if name in ("wrong_format", "steps_not_list"):
        p = _write(path, _dump(_capture_variant(name)))
        return p, _bad(name, error="bad-capture")
    if name in ("bad_action", "step_missing_n"):
        doc = _capture_variant(name)
        p = _write(path, _dump(doc))
        facts = _capture_facts(doc, variant=name, expect={"skip": ["malformed"]})
        facts["malformed_paths"] = ["steps[0]"] if name == "step_missing_n" else ["steps[1]"]
        return p, facts
    if name == "tolerant":
        return make_rea_bundle(path, "tolerant")
    if name == "not_rea":
        return make_rea_bundle(path, "not_rea")
    raise KeyError(name)


def make_pathological_line(path, size=TEXT_CAP):
    """One line of exactly ``size`` bytes (default 5 MiB, the largest text file a lane reads) made of fragments that
    punish a careless regex: opened-but-unterminated `fetch(` and `t(` calls, long whitespace runs, quote runs and
    `path:` with no value. It holds no fact. A lane must finish it within 10 s (contract 6.2); the lazy pattern
    ``fetch\\(\\s*["'`](.*?)["'`]\\s*\\)`` takes about 98 s on it, which is why 6.5 requires a bounded string class."""
    frag = ('fetch("' + "a" * 300 + ' fetch(' + " " * 500 + "t(" * 200 + '"' * 100 + " path:" + " " * 300 + "/" * 200
            + "'`" * 50 + " ")
    reps = size // len(frag) + 1
    data = (frag * reps)[:size].encode("ascii")
    p = _write(path, data)
    return p, _facts(variant="pathological", size=size, single_line=True, secret_patterns=[], expect={"skip": [None]})


def make_too_large_text(path, size=TEXT_CAP + 1):
    """A plain-text file one byte over the 5 MiB read cap (`too-large`)."""
    p = _write(path, b"a" * size)
    return p, _facts(variant="too_large", size=size, expect={"skip": ["too-large"]})
