"""globs: moved from blueprint.py 1.3.0 (contract 1.5)."""
import re

from bp.core import norm


_RX_CACHE = {}


def glob_rx(pattern):
    """CODEOWNERS / gitignore-style glob.

    `*` stays inside one folder, `**` crosses folders, `?` is one character.
    A pattern with a slash (other than a trailing one) is anchored to the repo root;
    one without, like `*.md`, matches at any depth. A pattern that names a folder
    also matches everything inside it.
    """
    if pattern in _RX_CACHE:
        return _RX_CACHE[pattern]
    p = pattern.strip().replace("\\", "/")
    anchored = p.startswith("/") or "/" in p.rstrip("/")
    p = p.lstrip("/")
    if p.endswith("/"):
        p = p + "**"
    out, i = "", 0
    while i < len(p):
        c = p[i]
        if c == "*":
            if p[i:i + 3] == "**/":
                out += "(?:.*/)?"
                i += 3
                continue
            if p[i:i + 2] == "**":
                out += ".*"
                i += 2
                continue
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        else:
            out += re.escape(c)
        i += 1
    rx = re.compile(("^" if anchored else "^(?:.*/)?") + out + "(?:/.*)?$")
    _RX_CACHE[pattern] = rx
    return rx


def glob_match(pattern, path):
    return bool(glob_rx(pattern).match(norm(path)))


def pattern_instance_rx(glob):
    """A repeated-file pattern's glob, anchored, used to find its instances on disk."""
    p = norm(glob).lstrip("/")
    out, i = "", 0
    while i < len(p):
        c = p[i]
        if c == "*":
            if p[i:i + 3] == "**/":
                out += "(?:.*/)?"
                i += 3
                continue
            if p[i:i + 2] == "**":
                out += ".*"
                i += 2
                continue
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        else:
            out += re.escape(c)
        i += 1
    return re.compile("^" + out + "$")
