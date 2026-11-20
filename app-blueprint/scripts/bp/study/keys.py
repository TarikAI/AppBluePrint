"""study.keys: canonical keys shared by every lane (contract 6.3): route and endpoint keys, path templates, URL hosts,
framework ids, language names and json_shape(). Lanes and importers use these, never each other's, so the same fact
gets the same key in every lane and compare can match them.

Imports the standard library only (contract 1.2: core only).
"""
import re
from urllib.parse import urlsplit

# Framework ids (6.3): the short id, never the package name.
FRAMEWORKS = ("react", "next", "vue", "nuxt", "svelte", "sveltekit", "angular", "express", "fastify", "nestjs",
              "electron", "react-native", "expo", "hermes", "flutter", "unity", "xamarin", "capacitor", "cordova",
              "django", "flask", "fastapi", "axum", "actix-web", "tauri", "gin", "echo", "chi", "rails", "android")

# Language names by extension (6.3).
LANGUAGES = {".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript",
             ".mjs": "JavaScript", ".cjs": "JavaScript", ".py": "Python", ".rs": "Rust", ".go": "Go", ".rb": "Ruby",
             ".php": "PHP", ".java": "Java", ".kt": "Kotlin", ".cs": "C#", ".swift": "Swift", ".dart": "Dart",
             ".ex": "Elixir", ".exs": "Elixir", ".scala": "Scala", ".sql": "SQL", ".prisma": "Prisma", ".vue": "Vue",
             ".svelte": "Svelte"}

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT")


def language_of(path):
    """The language name of a path by its extension (case-sensitive, as 6.3 lists them), or None."""
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    dot = name.rfind(".")
    return LANGUAGES.get(name[dot:]) if dot > 0 else None


# ------------------------------------------------------------------------------------------------ path templates

_CATCH_ALL = re.compile(r"\[\[?\.\.\.([^\[\]/]+)\]\]?")              # [...slug], [[...slug]]
_BRACKET = re.compile(r"\[([^\[\]/.][^\[\]/]*)\]")                  # [id]
_ANGLE = re.compile(r"<(?:[A-Za-z_][\w]*:)?([A-Za-z_][\w]*)>")       # <id>, <int:id>, <path:p>
_BRACE = re.compile(r"\{\*?([A-Za-z_][\w-]*)(\*)?(?::[^{}/]*)?\}")    # {id}, {id:int}, {slug*}, {*rest}
_COLON = re.compile(r"(?<![\w$]):([A-Za-z_$][\w$]*)(?:\([^)/]{0,200}\))?\??")   # :itemId, :id(\d+), :id?
_UUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
_HEX16 = re.compile(r"[0-9A-Fa-f]{16,}")
_B64URL20 = re.compile(r"[A-Za-z0-9_-]{20,}")
_DIGITS = re.compile(r"[0-9]+")


def _param_segment(seg):
    """One route segment with every parameter written as {name} (6.3)."""
    if seg in ("*", "**"):
        return "{rest*}"
    seg = _CATCH_ALL.sub(lambda m: "{" + m.group(1) + "*}", seg)
    seg = _BRACKET.sub(lambda m: "{" + m.group(1) + "}", seg)
    seg = _ANGLE.sub(lambda m: "{" + m.group(1) + "}", seg)
    seg = _BRACE.sub(lambda m: "{" + m.group(1) + ("*" if m.group(2) or m.group(0).startswith("{*") else "") + "}",
                     seg)
    return _COLON.sub(lambda m: "{" + m.group(1) + "}", seg)


def _concrete_segment(seg):
    """One segment of an observed URL path (HAR, capture): all digits, a UUID, 16+ hex or 20+ base64url characters
    become {id} (6.3)."""
    for rx in (_DIGITS, _UUID, _HEX16, _B64URL20):
        if rx.fullmatch(seg):
            return "{id}"
    return seg


def path_template(path, concrete=False):
    """The canonical path of a route or endpoint (6.3): a query or fragment dropped, repeated slashes collapsed, a
    leading '/' added and a trailing one removed ('/' stays).

    concrete=False (routes as written in code, bundles, manifests, API specs): every parameter becomes {name}
    (':itemId', '<int:id>', '<id>', '[id]', '{id}' -> '{itemId}'/'{id}'; '[...slug]', '[[...slug]]' -> '{slug*}';
    a bare '*' -> '{rest*}'), and Next.js route groups '(group)' and parallel slots '@slot' are dropped.
    concrete=True (paths observed in traffic: HAR entries, capture requests): nothing is read as a parameter or
    dropped; a segment that is all digits, a UUID, 16+ hex or 20+ base64url characters becomes '{id}'."""
    p = str(path).split("#", 1)[0].split("?", 1)[0]
    out = []
    for seg in p.split("/"):
        if not seg:
            continue
        if concrete:
            out.append(_concrete_segment(seg))
            continue
        if len(seg) > 2 and seg.startswith("(") and seg.endswith(")"):
            continue
        if len(seg) > 1 and seg.startswith("@"):
            continue
        out.append(_param_segment(seg))
    return "/" + "/".join(out)


def route_key(method, path, concrete=False):
    """'METHOD /path' (6.3): the method upper-cased, or ANY when the source gives none (also for all, use and
    anything that is not a word); the path through path_template."""
    m = method.upper() if isinstance(method, str) and re.fullmatch(r"[A-Za-z]+", method) else "ANY"
    if m in ("ALL", "USE"):
        m = "ANY"
    return f"{m} {path_template(path, concrete)}"


_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def url_host(url):
    """The host of a URL as 6.5 keys it: lower-cased, userinfo dropped, ':port' kept unless it is the scheme's
    default; '' when there is none."""
    try:
        parts = urlsplit(str(url))
        host = parts.hostname or ""
    except ValueError:
        return ""
    try:
        port = parts.port
    except ValueError:  # a port that is not a number from 0 to 65535: the host alone
        port = None
    if ":" in host:
        host = f"[{host}]"
    if port is not None and _DEFAULT_PORTS.get(parts.scheme.lower()) != port:
        host += f":{port}"
    return host


def url_path(url):
    """The path of a URL ('/' when it has none)."""
    try:
        return urlsplit(str(url)).path or "/"
    except ValueError:
        return "/"


def endpoint_key(method, url):
    """'METHOD host/path-template' of an observed request (web.endpoint, 6.5): the URL's host by url_host and its
    path with concrete templating."""
    m = route_key(method, "/").split(" ", 1)[0]
    return f"{m} {url_host(url)}{path_template(url_path(url), concrete=True)}"


# ------------------------------------------------------------------------------------------------ json_shape

def _type(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    return "string"


def json_shape_info(value, depth=4, max_keys=50):
    """(json_shape(value, depth, max_keys), cut): cut is True when a value below `depth` levels was replaced by
    '...' (the lane then adds deeper_note(depth), reading rule 3)."""
    cut = [False]

    def shape(v, level):
        if level > depth:
            cut[0] = True
            return "..."
        if isinstance(v, dict):
            keys = sorted(v, key=str)[:max_keys]
            return {str(k): shape(v[k], level + 1) for k in keys}
        if isinstance(v, (list, tuple)):
            return [shape(v[0], level + 1)] if v else []
        return _type(v)

    return shape(value, 1), cut[0]


def json_shape(value, depth=4, max_keys=50):
    """The shape of parsed JSON (6.3): key names and the types string, number, boolean, null; an object becomes an
    object of shapes with at most max_keys keys in sorted order; an array becomes [shape of its first element] ([]
    when empty). The value given is level 1; a value at level depth + 1 is '...':
    {"a":{"b":{"c":{"d":1}}}} -> {"a":{"b":{"c":{"d":"..."}}}} and [[[[1]]]] -> [[[["..."]]]] with depth 4."""
    return json_shape_info(value, depth, max_keys)[0]
