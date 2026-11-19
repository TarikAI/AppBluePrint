"""Fuzz a version 2 plan: no wrongly shaped or missing value anywhere in blueprint.json may crash a command's core path,
with the registry loaded (contract 3.3, 10.1; every W1 and W3 workstream's acceptance includes this test).

The plan is contract 2.10's example (test_validate.V2_EXAMPLE), which uses all four new sections. For every key of the
top level, of app and approach, of every entry of every list section, and of the nested 2.0 values (criteria[] and
their fields, experiment, source), each value in VALUES is substituted in turn, and each key is deleted in turn. The
only acceptable outcomes are a normal return or SystemExit with code 1.

"With the registry loaded": every DOMAIN and VIEWS module the build lacks is stood in for by a stub that defines every
hook and reads the plan the way a wave 1 module would (iterating every new section, reading every field with the type
V2_FIELDS promises). A real module, once it exists, is used instead of its stub, so each later workstream runs this
test with its own code.
"""
import argparse
import contextlib
import copy
import io
import json
import shutil
import tempfile
import time
import traceback
import unittest
from pathlib import Path
from unittest import mock

import support as S  # noqa: F401  (puts scripts/ on sys.path)

from bp import plan as bplan, registry, status, validate
from bp.commands import plan as cmd_plan, progress
from bp.views.context import build_context
from bp.views.render import render_all, render_map

from test_registry_hooks import extension
from test_validate import V2_EXAMPLE

VALUES = [None, 5, True, "x", [], {}, ["a"], [5], [{}], [None]]
NESTED = {"criteria", "experiment", "source", "uses"}

STUB = '''
def _s(v):
    return v if isinstance(v, str) else ""

def _items(plan, key):
    return plan.get(key) or []

def _touch(plan):
    """Read every 2.0 field with the type V2_FIELDS promises (a wrong type would raise here)."""
    out = []
    for m in _items(plan, "milestones"):
        out.append(_s(m.get("id")) + _s(m.get("name")).lower() + _s(m.get("due"))[:4])
    for f in _items(plan, "features"):
        out.append(_s(f.get("title")).strip() + _s(f.get("kind")).upper() + str(f.get("draft") is True))
        out.extend(d.lower() for d in f.get("dependsOn", []))
        for c in f.get("criteria", []):
            out.append(" ".join(_s(c.get(k)) for k in ("id", "scenario", "action", "expected", "level", "verify")))
        src = f.get("source", {})
        out.extend(b.upper() for b in src.get("behaviours", []))
    for t in _items(plan, "tasks"):
        out.append(_s(t.get("title")) + str(t.get("ownerAction") is True) + ",".join(t.get("dependsOn", [])))
    for u in _items(plan, "unknowns"):
        exp = u.get("experiment", {})
        out.append(_s(u.get("severity")) + ",".join(u.get("blocks", [])) + _s(exp.get("timebox")).lower())
        out.extend(e.lower() for e in u.get("evidenceFor", []) + u.get("evidenceAgainst", []))
    for f in _items(plan, "files") + _items(plan, "patterns"):
        out.extend(x.lower() for x in f.get("features", []))
        out.append(str(f.get("infrastructure") is True))
    for d in _items(plan, "decisions"):
        out.extend(a.lower() for a in d.get("alternatives", []))
    for p in _items(plan, "phases"):
        out.append(_s(p.get("rollback")).lower())
    return out

CODES = []

def validate(plan, E, W, vctx):
    for line in _touch(plan):
        if line.startswith("never"):
            E(line)

def context(root, plan, state, ctx):
    ctx.setdefault("stub_seen", []).append(len(_touch(plan)))

def file_tags(ctx):
    return {f["path"]: list(f.get("features", [])) for f in ctx["plan"]["files"] if f.get("features")}

def roster_sections(ctx):
    return [("Stub " + __name__, "\\n".join(_touch(ctx["plan"]))[:200])]

def map_payload(ctx):
    return {"stub_" + __name__.replace(".", "_"): len(_touch(ctx["plan"]))}

def status_lines(ctx, args):
    return _touch(ctx["plan"])[:3]

def status_json(ctx):
    return {"stub_" + __name__.replace(".", "_"): _touch(ctx["plan"])[:3]}

def refuse(action, root, plan, path, by):
    _touch(plan)
    return None
'''
SINGLE = {"features": "\ndef status_card(ctx, fid):\n    return [fid] + _touch(ctx['plan'])[:2]\n",
          "records": "\ndef verify_feature(args, ctx):\n    return [], 0\n"}
VIEW = '''
def views(ctx):
    plan = ctx["plan"]
    lines = []
    for f in plan["features"]:
        lines.append("## " + (f.get("title") or "") + " " + " ".join(c.get("id", "") for c in f.get("criteria", [])))
    return {"%s": "\\n".join(lines) + "\\n"} if lines else {}
'''


def stubs():
    """Stub modules for every DOMAIN and VIEWS module this build lacks."""
    files = {}
    for name in registry.DOMAIN:
        if not registry.exists(name):
            files[name.replace(".", "/") + ".py"] = STUB + SINGLE.get(name, "")
    for name, view in zip(registry.VIEWS, registry.OPTIONAL_VIEWS):
        if not registry.exists(name):
            files[name.replace(".", "/") + ".py"] = VIEW % view
    return files


def mutation_paths(plan):
    """Every location to mutate, as a tuple of keys and indexes from the plan root, nested 2.0 values included."""
    out = []

    def walk(node, path, depth):
        if isinstance(node, dict):
            for k, v in node.items():
                out.append(path + (k,))
                if depth == 0 and isinstance(v, (dict, list)):
                    walk(v, path + (k,), 1)
                elif k in NESTED and isinstance(v, (dict, list)):
                    walk(v, path + (k,), depth + 1)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                out.append(path + (i,))
                if isinstance(item, dict):
                    walk(item, path + (i,), depth + 1)

    walk(plan, (), 0)
    return out


def apply(plan, path, value, delete=False):
    node = plan
    for step in path[:-1]:
        node = node[step]
    if delete:
        if isinstance(node, dict):
            del node[path[-1]]
        else:
            node.pop(path[-1])
    else:
        node[path[-1]] = value


class PlanFuzzV2Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = json.loads(V2_EXAMPLE)
        cls.root = Path(tempfile.mkdtemp(prefix="bp-fuzz-v2-"))
        (cls.root / ".blueprint").mkdir()
        for f in cls.base["files"]:
            p = cls.root / f["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("// " + f["path"] + "\r\n", encoding="utf-8")
        (cls.root / "extra.txt").write_text("x\n", encoding="utf-8")
        (cls.root / ".blueprint" / "blueprint.json").write_text(V2_EXAMPLE, encoding="utf-8")
        loaded = bplan.load_plan(cls.root)
        proofs = {}
        for p in ("crates/snag-api/src/main.rs", "crates/snag-api/tests/snags.rs", "Cargo.toml"):
            proofs[p] = {"state": "built", "verified": {"ok": True, "cmd": "c", "at": "t", "sha": "x",
                                                        "inputs": status.proof_inputs(cls.root, loaded, [p])}}
        state = {"stateVersion": 2, "files": proofs, "patterns": {}, "unplanned": ["extra.txt"], "syncedAt": None,
                 "head": None, "tasks": {"T-domain": {"status": "doing", "by": "owner"}}}
        (cls.root / ".blueprint" / "state.json").write_text(json.dumps(state), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def run_paths(self, plan_json):
        root = self.root
        (root / ".blueprint" / "blueprint.json").write_text(json.dumps(plan_json), encoding="utf-8")
        plan = bplan.load_plan(root)
        validate.validate_findings(plan, root=root)
        state = bplan.load_state(root)
        ctx = build_context(root, plan, state)
        render_all(root, plan, state, quiet=True, check=True)
        render_map(plan, state, ctx["comp"], ctx)
        status.next_ready(plan, ctx["comp"]["rows"], None, blocked_phases={"P2"})
        for path in ("crates/snag-api/src/main.rs", ".blueprint/state.json"):
            status.guard_decision(root, plan, path, "backend-lead")
            registry.refusal("write", root, plan, path, "backend-lead")
        registry.refusal("claim", root, plan, "apps/web/src/offline.ts", "frontend-engineer")
        ns = dict(root=str(root), owner=None, phase=None, next=True, json=False, features=True, feature=None,
                  milestone=None, tasks=True, unknowns=True)
        progress.cmd_status(argparse.Namespace(**ns))
        progress.cmd_status(argparse.Namespace(**dict(ns, json=True)))
        progress.cmd_status(argparse.Namespace(**dict(ns, feature="F-log-snag")))
        cmd_plan.cmd_export(argparse.Namespace(root=str(root), format="json", team=None, json=False))

    def test_no_mutation_crashes(self):
        started = time.time()
        paths = mutation_paths(self.base)
        crashes, count = {}, 0
        with extension(stubs()), mock.patch("subprocess.run", side_effect=OSError("no git in the fuzz test")):
            self.assertTrue(all(registry.exists(n) for n in registry.DOMAIN + registry.VIEWS))
            for path in paths:
                for value, delete in [(v, False) for v in VALUES] + [(None, True)]:
                    plan = copy.deepcopy(self.base)
                    try:
                        apply(plan, path, value, delete)
                    except (KeyError, IndexError):
                        continue
                    count += 1
                    label = f"{'/'.join(map(str, path))} = " + ("<deleted>" if delete else json.dumps(value))
                    try:
                        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                            self.run_paths(plan)
                    except SystemExit as e:
                        if e.code != 1:
                            crashes.setdefault(f"SystemExit({e.code})", []).append(label)
                    except Exception as e:  # any other exception is a crash
                        tb = traceback.extract_tb(e.__traceback__)[-1]
                        crashes.setdefault(f"{type(e).__name__}: {e} at {Path(tb.filename).name}:{tb.lineno}", []).append(label)
        elapsed = time.time() - started
        print(f"\nfuzz v2: {count} mutations of {len(paths)} paths in {elapsed:.1f}s")
        report = "\n".join(f"{len(v)} x {k}; e.g. {v[0]}" + ("; " + "; ".join(v[1:3]) if len(v) > 1 else "")
                           for k, v in sorted(crashes.items(), key=lambda kv: -len(kv[1])))
        self.assertEqual(crashes, {}, f"{sum(map(len, crashes.values()))} crashing mutations of {count}:\n{report}")
        self.assertGreater(count, 2000)

    def test_stubs_cover_every_hook_and_the_cli_builds(self):
        from bp import cli
        with extension(stubs()):
            self.assertTrue(all(registry.exists(n) for n in registry.DOMAIN + registry.VIEWS))
            mods = registry.modules("DOMAIN") + registry.modules("VIEWS")
            for hook in ("validate", "context", "file_tags", "roster_sections", "map_payload", "status_lines",
                         "status_json", "refuse", "views"):
                self.assertTrue(any(hasattr(m, hook) for m in mods), hook)
            self.assertIsNotNone(registry.hook("status_card"))
            self.assertIsNotNone(registry.hook("verify_feature"))
            cli.build_parser()

    def test_mutation_paths_reach_the_nested_values(self):
        paths = set(mutation_paths(self.base))
        for p in (("features", 0, "criteria", 1, "level"), ("unknowns", 0, "experiment", "timebox"),
                  ("tasks", 0, "ownerAction"), ("milestones", 0, "due"), ("files", 0, "infrastructure"),
                  ("phases", 0, "rollback"), ("app", "name"), ("features",), ("features", 1, "criteria", 0)):
            self.assertIn(p, paths)


if __name__ == "__main__":
    unittest.main()
