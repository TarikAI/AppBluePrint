"""data: moved from blueprint.py 1.3.0 (contract 1.5)."""
import json
import re
from pathlib import Path

from bp.core import norm, read_text_safe
from bp.globs import pattern_instance_rx
from bp.plan import owner_of


STORE_KINDS = ("postgres", "sqlite", "mysql", "supabase", "redis", "s3", "other")


RESOURCE_KINDS = ("table", "view", "env", "api", "queue", "bucket", "other")


ACCESS = ("read", "write")


SCAN_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".rs", ".go", ".rb", ".php", ".java", ".kt", ".cs",
            ".sql", ".prisma", ".vue", ".svelte", ".swift", ".dart", ".ex", ".exs", ".scala"}


_SQL_CREATE = re.compile(r"\bcreate\s+(?:or\s+replace\s+)?(?:(?:global|local)\s+)?(?:temp(?:orary)?\s+|unlogged\s+)?"
                         r"(table|view|materialized\s+view)\s+(?:if\s+not\s+exists\s+)?([\w.\"`\[\]]+)", re.I)


_SQL_DROP = re.compile(r"\bdrop\s+(table|view|materialized\s+view)\s+(?:if\s+exists\s+)?([\w.\"`\[\]]+)", re.I)


_SQL_RENAME = re.compile(r"\balter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?([\w.\"`\[\]]+)\s+rename\s+to\s+([\w.\"`\[\]]+)", re.I)


_PRISMA_MODEL = re.compile(r"^\s*(model|view)\s+(\w+)\s*\{(.*?)^\s*\}", re.M | re.S)


_PRISMA_MAP = re.compile(r"@@map\(\s*\"([^\"]+)\"\s*\)")


_ENV_KEY = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", re.M)


_ENV_USE = [re.compile(p) for p in (
    r"process\.env\.([A-Z][A-Z0-9_]*)", r"process\.env\[\s*['\"]([A-Z][A-Z0-9_]*)['\"]", r"import\.meta\.env\.([A-Z][A-Z0-9_]*)",
    r"os\.environ(?:\.get)?\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]", r"os\.environ\[\s*['\"]([A-Z][A-Z0-9_]*)['\"]",
    r"getenv\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]", r"env::var(?:_os)?\(\s*\"([A-Z][A-Z0-9_]*)\"", r"env!\(\s*\"([A-Z][A-Z0-9_]*)\"",
    r"Deno\.env\.get\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]", r"ENV\[\s*['\"]([A-Z][A-Z0-9_]*)['\"]")]


def bare_name(name):
    n = re.sub(r"[\"`\[\]]", "", name).split(".")[-1]
    return n.lower()


def resource_names(r):
    names = [r.get("id", "")] + list(r.get("aliases", []))
    return [n for n in dict.fromkeys(n for n in names if n)]


def resource_owner(plan, r):
    if r.get("owner"):
        return r["owner"]
    if r.get("definedIn"):
        return owner_of(plan, norm(r["definedIn"]))[0]
    return None


def validate_data(plan, E, W):
    stores = set()
    for s in plan.get("stores", []):
        sid = s.get("id")
        if not sid:
            E(f"store without id: {json.dumps(s)[:80]}")
            continue
        if sid in stores:
            E(f"store {sid}: duplicate id")
        stores.add(sid)
        if s.get("kind") not in STORE_KINDS:
            E(f"store {sid}: kind must be one of {', '.join(STORE_KINDS)}")
    roster = {r.get("id") for r in plan["roster"]}
    planned = {f["path"] for f in plan["files"]}
    pat_rx = [pattern_instance_rx(p.get("glob", "")) for p in plan["patterns"] if p.get("glob")]
    ids = set()
    for r in plan.get("resources", []):
        rid = r.get("id")
        if not rid:
            E(f"resource without id: {json.dumps(r)[:80]}")
            continue
        if rid in ids:
            E(f"resource {rid}: duplicate id")
        ids.add(rid)
        if r.get("kind") not in RESOURCE_KINDS:
            E(f"resource {rid}: kind must be one of {', '.join(RESOURCE_KINDS)}")
        if r.get("store") and r["store"] not in stores:
            E(f"resource {rid}: unknown store {r['store']!r}")
        if not r.get("purpose"):
            E(f"resource {rid}: empty purpose")
        if r.get("owner") and r["owner"] not in roster:
            E(f"resource {rid}: owner {r['owner']!r} is not in the roster")
        d = norm(r["definedIn"]) if r.get("definedIn") else None
        if d and d not in planned and not any(rx.match(d) for rx in pat_rx):
            E(f"resource {rid}: definedIn {d} is not a planned file or pattern instance")
        if r.get("kind") in ("table", "view"):
            if not r.get("store"):
                W(f"resource {rid}: a {r['kind']} should name its store")
            if not d:
                W(f"resource {rid}: no definedIn (the migration or schema file that creates it)")
    used = {}
    items = [(f, f["path"], f["path"]) for f in plan["files"]] + [(p, "pattern " + str(p.get("id")), p.get("glob", "")) for p in plan["patterns"]]
    for item, label, path in items:
        for u in item.get("uses", []) or []:
            if not isinstance(u, dict) or u.get("resource") not in ids:
                name = u.get("resource") if isinstance(u, dict) else json.dumps(u)
                E(f"{label}: uses unknown resource {name}")
                continue
            if u.get("access") not in ACCESS:
                E(f"{label}: access for {u['resource']} must be read or write")
                continue
            used.setdefault(u["resource"], []).append((label, path, u["access"]))
    for r in plan.get("resources", []):
        rid = r.get("id")
        if not rid:
            continue
        if rid not in used:
            W(f"resource {rid}: no planned file or pattern uses it")
        owner = resource_owner(plan, r)
        for label, path, access in used.get(rid, []):
            fo = owner_of(plan, path)[0]
            if access == "write" and owner and fo and fo != owner:
                W(f"{label}: writes {rid}, which {owner} owns; changes need {owner}'s review")


def parse_sql_tables(text, source):
    """Tables and views a SQL file leaves behind, applying creates, drops and renames in order."""
    events = [(m.start(), "create", m.group(1).lower(), bare_name(m.group(2))) for m in _SQL_CREATE.finditer(text)]
    events += [(m.start(), "drop", m.group(1).lower(), bare_name(m.group(2))) for m in _SQL_DROP.finditer(text)]
    events += [(m.start(), "rename", bare_name(m.group(1)), bare_name(m.group(2))) for m in _SQL_RENAME.finditer(text)]
    found = {}
    for _, ev, a, b in sorted(events):
        if ev == "create":
            found[b] = {"name": b, "kind": "view" if "view" in a else "table", "source": source}
        elif ev == "drop":
            found.pop(b, None)
        elif ev == "rename" and a in found:
            found[b] = dict(found.pop(a), name=b)
    return found


def parse_prisma_tables(text, source):
    found = {}
    for m in _PRISMA_MODEL.finditer(text):
        mp = _PRISMA_MAP.search(m.group(3))
        name = (mp.group(1) if mp else m.group(2)).lower()
        found[name] = {"name": name, "kind": "view" if m.group(1) == "view" else "table", "source": source, "model": m.group(2)}
    return found


def schema_sources(root, plan, disk):
    globs = [g for s in plan["stores"] for g in s.get("schema", [])]
    globs += [r["definedIn"] for r in plan["resources"] if r.get("definedIn") and r.get("kind") in ("table", "view")]
    rxs = [pattern_instance_rx(g) for g in globs]
    return [p for p in disk if any(rx.match(p) for rx in rxs)]


def code_refs(text, names, prisma_models):
    """Return 'read', 'write' or None for how text uses a table known by any of `names`."""
    access = None
    for n in names:
        q = re.escape(n)
        if re.search(r"\b(?:insert\s+into|update|delete\s+from|truncate(?:\s+table)?|merge\s+into|upsert\s+into)\s+[\"`\[]?(?:\w+\.)?" + q + r"\b", text, re.I) \
                or re.search(r"\.from\(\s*['\"]" + q + r"['\"]\s*\)\s*\.\s*(?:insert|update|upsert|delete)\b", text):
            return "write"
        if re.search(r"\b(?:from|join)\s+[\"`\[]?(?:\w+\.)?" + q + r"\b", text, re.I) or re.search(r"\.from\(\s*['\"]" + q + r"['\"]", text):
            access = "read"
    for model in prisma_models:
        acc = model[:1].lower() + model[1:]
        if re.search(r"\." + re.escape(acc) + r"\.(?:create|createMany|update|updateMany|upsert|delete|deleteMany)\b", text):
            return "write"
        if re.search(r"\." + re.escape(acc) + r"\.(?:find\w*|count|aggregate|groupBy)\b", text):
            access = "read"
    return access


def scan_data(root, plan, state, disk):
    """Static data scan: tables from schema files, env vars, and what each planned file touches."""
    tables = {}
    for p in schema_sources(root, plan, disk):
        text = read_text_safe(root / p) or ""
        found = parse_prisma_tables(text, p) if p.endswith(".prisma") else parse_sql_tables(text, p)
        tables.update(found)
    env_example = sorted({k for name in (".env.example", ".env.sample", ".env.template") if (root / name).is_file()
                          for k in _ENV_KEY.findall(read_text_safe(root / name) or "")})
    declared = {r["id"]: r for r in plan["resources"] if r.get("id")}
    by_name = {}
    for r in declared.values():
        for n in resource_names(r):
            by_name[n.lower()] = r["id"]
    issues = []
    for t in sorted(tables):
        if t not in by_name:
            issues.append({"type": "undeclared-table", "resource": t, "path": tables[t]["source"],
                           "detail": f"{tables[t]['kind']} {t} is created in {tables[t]['source']} but not declared in resources"})
    have_schema = bool(schema_sources(root, plan, disk))
    for r in declared.values():
        if r.get("kind") in ("table", "view") and have_schema and not any(n.lower() in tables for n in resource_names(r)):
            issues.append({"type": "missing-table", "resource": r["id"], "detail": f"{r['id']} is declared but no schema file creates it"})
    # what each planned file (and pattern instance) touches
    targets = [f["path"] for f in plan["files"] if f.get("action") != "delete"]
    for inst in (state.get("patterns") or {}).values():
        targets += inst.get("instances", [])
    declared_uses = {}
    for f in plan["files"]:
        declared_uses[f["path"]] = {u["resource"]: u.get("access") for u in f.get("uses", []) if isinstance(u, dict)}
    pat_uses = [(pattern_instance_rx(p.get("glob", "")), {u["resource"]: u.get("access") for u in p.get("uses", []) if isinstance(u, dict)})
                for p in plan["patterns"] if p.get("glob")]
    env_declared = {n: r["id"] for r in declared.values() if r.get("kind") == "env" for n in resource_names(r)}
    api_hosts = {r.get("host"): r["id"] for r in declared.values() if r.get("kind") == "api" and r.get("host")}
    table_keys = {}
    for t, info in tables.items():
        table_keys[by_name.get(t, t)] = ([t] + [n for n, rid in by_name.items() if rid == by_name.get(t)], [info["model"]] if info.get("model") else [])
    for r in declared.values():
        if r.get("kind") in ("table", "view") and r["id"] not in table_keys:
            table_keys[r["id"]] = (resource_names(r), [a for a in r.get("aliases", []) if a[:1].isupper()])
    observed = {}
    schema_files = set(schema_sources(root, plan, disk))
    for path in sorted(set(targets)):
        if path in schema_files or Path(path).suffix.lower() not in SCAN_EXT or not (root / path).is_file():
            continue
        text = read_text_safe(root / path)
        if not text:
            continue
        seen = {}
        for key, (names, models) in table_keys.items():
            acc = code_refs(text, names, models)
            if acc:
                seen[key] = acc
        for rx in _ENV_USE:
            for name in rx.findall(text):
                seen[env_declared.get(name, "env:" + name)] = "read"
        for host, rid in api_hosts.items():
            if host in text:
                seen[rid] = "read"
        if not seen:
            continue
        observed[path] = seen
        decl = dict(declared_uses.get(path, {}))
        for rx, uses in pat_uses:
            if rx.match(path):
                decl.update(uses)
        for res, acc in seen.items():
            if res.startswith("env:"):
                if env_declared:
                    issues.append({"type": "undeclared-env", "path": path, "resource": res[4:], "detail": f"{path} reads env var {res[4:]}, which is not a declared resource"})
                continue
            if res not in declared:
                issues.append({"type": "undeclared-use", "path": path, "resource": res, "detail": f"{path} {acc}s {res}, which is not a declared resource"})
            elif res not in decl:
                issues.append({"type": "undeclared-use", "path": path, "resource": res, "detail": f"{path} {acc}s {res} but its blueprint entry does not list it in uses"})
            elif acc == "write" and decl[res] == "read":
                issues.append({"type": "access-mismatch", "path": path, "resource": res, "detail": f"{path} writes {res} but is declared read-only"})
        for res in decl:
            if res in declared and declared[res].get("kind") in ("table", "view", "env") and res not in seen:
                issues.append({"type": "declared-not-found", "path": path, "resource": res, "detail": f"{path} declares {res} but the code does not reference it yet"})
    return {"tables": sorted(tables.values(), key=lambda t: t["name"]), "envExample": env_example,
            "observed": observed, "issues": issues, "live": (state.get("data") or {}).get("live")}


def data_view(plan, state):
    """Resources with owners, readers and writers, for the views."""
    data = state.get("data") or {}
    res = []
    for r in plan["resources"]:
        rid = r.get("id")
        readers, writers = [], []
        for f in plan["files"]:
            for u in f.get("uses", []):
                if isinstance(u, dict) and u.get("resource") == rid:
                    (writers if u.get("access") == "write" else readers).append(f["path"])
        for p in plan["patterns"]:
            for u in p.get("uses", []):
                if isinstance(u, dict) and u.get("resource") == rid:
                    (writers if u.get("access") == "write" else readers).append(p.get("glob"))
        issues = [i for i in data.get("issues", []) if i.get("resource") == rid]
        res.append({"id": rid, "kind": r.get("kind"), "store": r.get("store"), "definedIn": r.get("definedIn"),
                    "owner": resource_owner(plan, r), "purpose": r.get("purpose", ""), "readers": readers, "writers": writers,
                    "issues": len(issues)})
    return {"stores": plan["stores"], "resources": res, "tables": data.get("tables", []), "issues": data.get("issues", []),
            "observed": data.get("observed", {}), "live": data.get("live")}
