"""registry: the module lists and hook collection that let workstreams add behaviour without editing shared files
(contract 1.4). Imports nothing of bp statically; every module is reached through importlib, so a module that a later
wave adds is picked up by its file alone, and one that does not exist yet is skipped.

Hooks are module-level names (all optional; a module defines only what it needs). Signatures, owners and merge rules
are those of contract 1.4: CODES, validate, context, file_tags, roster_sections, views, map_payload, status_lines,
status_json, status_card, verify_feature, refuse, register.
"""
import importlib
import importlib.util
import re

DOMAIN = ("rules", "data", "unknowns", "records", "features", "roadmap", "evidence")
VIEWS = ("views.roadmap_md", "views.features_md")
COMMAND_TABLE = {   # every subcommand of 2.0 -> its module under bp; 1.3.0's --help order, then the 2.0 commands
    "init": "commands.plan", "validate": "commands.plan", "render": "commands.plan", "sync": "commands.progress",
    "status": "commands.progress", "owner": "commands.progress", "claim": "commands.progress",
    "mark": "commands.progress", "verify": "commands.progress", "guard": "commands.progress",
    "hook": "commands.progress", "contracts": "commands.plan", "export": "commands.plan", "db-check": "commands.data",
    "feedback": "commands.learn", "report": "commands.learn", "doctor": "commands.learn",
    "migrate": "commands.migrate", "task": "commands.roadmap", "accept": "commands.records",
    "evidence": "commands.evidence", "study": "commands.study"}

# Where a 2.0 subcommand or flag arrives, for the not-built message (contract 11).
ARRIVES = {"migrate": "W0-B", "task": "W1-R", "accept": "W3-V", "evidence": "W2-E", "study": "W2-E",
           "unknowns": "W1-U", "records": "W3-V", "features": "W1-F", "roadmap": "W1-R", "signals": "W3-L"}


def _not_built_here(name):
    """None when bp.<name> may be absent ("not built": the SKILL this tool came from lacks it). In a project copy whose
    MANIFEST lists it, a missing module is a damaged copy, never "not built": ImportError."""
    if importlib.import_module("bp.install").listed_missing(name):
        raise ImportError(f"bp/{name.replace('.', '/')}.py is listed in this copy's MANIFEST but missing: the copy is "
                          "incomplete. Run: python <skill>/scripts/blueprint.py init --refresh-tool")


def load(name):
    """bp.<name>, or None when that module is not built. Any other import error propagates (a bug), and so does a
    module this copy's MANIFEST lists (an incomplete copy)."""
    full = "bp." + name
    try:
        return importlib.import_module(full)
    except ModuleNotFoundError as e:
        if e.name == full:
            _not_built_here(name)
            return None
        raise


def modules(kind):
    """The modules of DOMAIN or VIEWS that exist, in list order."""
    names = {"DOMAIN": DOMAIN, "VIEWS": VIEWS}[kind.upper()]
    return [m for m in (load(n) for n in names) if m is not None]


def _short(mod):
    return mod.__name__[3:] if mod.__name__.startswith("bp.") else mod.__name__


def _hooked(name):
    return [m for m in modules("DOMAIN") + modules("VIEWS") if callable(getattr(m, name, None))]


def hook(name):
    """The hook function `name` of the single module that defines it (status_card, verify_feature), or None. Two
    modules defining it is a bug."""
    found = _hooked(name)
    if len(found) > 1:
        raise RuntimeError(f"hook {name} is defined by {', '.join(_short(m) for m in found)}; it must have one owner")
    return getattr(found[0], name) if found else None


def collect(hook, *args):
    """[(module name, result)] of every DOMAIN then VIEWS module defining the hook, called with args, in order."""
    return [(_short(m), getattr(m, hook)(*args)) for m in _hooked(hook)]


def codes():
    """[(regex, code)]: CODES of the new DOMAIN modules in DOMAIN order, then data.CODES, then rules.CODES (the 1.x
    list, whose broad regexes must come last, contract 7.4)."""
    mods = {_short(m): m for m in modules("DOMAIN")}
    order = [n for n in DOMAIN if n not in ("rules", "data")] + ["data", "rules"]
    owned = [(n, rx, code) for n in order if n in mods for rx, code in getattr(mods[n], "CODES", None) or []]
    _check_codes(owned, mods)
    return [(rx, code) for _, rx, code in owned]


def _check_codes(owned, mods):
    """A code belongs to one module (the 1.x codes to data and rules), and every example message a module gives in
    CODE_EXAMPLES ({code: message}) must classify as its own code with the full list: else RuntimeError naming both
    modules (review N7: a broad regex earlier in DOMAIN order would silently steal another module's messages)."""
    owner = {}
    for n, _, code in owned:
        if code in owner and owner[code] != n:
            raise RuntimeError(f"validation code {code!r} is given by {owner[code]} and {n}; a code has one owner")
        owner.setdefault(code, n)
    for n in [x for x in DOMAIN if x in mods]:
        for code, example in (getattr(mods[n], "CODE_EXAMPLES", None) or {}).items():
            hit = next(((m, c) for m, rx, c in owned if re.search(rx, example)), (None, "other"))
            if hit[1] != code:
                raise RuntimeError(f"validation code overlap: the example {example!r} is claimed by "
                                   f"{hit[0] or 'no regex'} ({hit[1]}) before {n} ({code}), which gives it; "
                                   "narrow one regex")


def refusal(action, root, plan, path, by):
    """The first non-empty result of a DOMAIN module's refuse hook (action "write" or "claim"), in DOMAIN order: a
    message string, or a tuple (message, error, next); None when no module refuses."""
    for m in _hooked("refuse"):
        msg = m.refuse(action, root, plan, path, by)
        if msg:
            return msg
    return None


def signal(root, signal, code):
    """Record an automatic loop signal through bp.signals when it exists (contract 9.4). Capture never breaks a
    command: a missing module is a no-op and any error inside it is ignored."""
    try:
        mod = load("signals")
        if mod is not None:
            mod.record(root, signal, code)
    except Exception:  # noqa: BLE001 - capture is best effort, like learn.store_append
        pass


def exists(name):
    """True when the module bp.<name> is present in this build (it is not imported)."""
    try:
        found = importlib.util.find_spec("bp." + name) is not None
    except ModuleNotFoundError:  # a missing parent package
        found = False
    if not found:
        _not_built_here(name)
    return found


def command_built(command):
    """True when the module that implements this subcommand is present (contract 1.4: else it answers not-built)."""
    return command in COMMAND_TABLE and exists(COMMAND_TABLE[command])
