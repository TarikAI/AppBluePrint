"""Fuzz the plan loader: no wrongly shaped or missing value in blueprint.json may crash any command's core path.

For every key of the top level, of app and approach, and of every entry in every list section (including
files[].uses[] entries), each value in VALUES is substituted in turn, and each key is deleted in turn. The only
acceptable outcomes are a normal return or SystemExit with code 1. Anything else fails and prints the mutation.
"""
import argparse
import contextlib
import copy
import importlib.util
import io
import json
import shutil
import tempfile
import time
import traceback
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
EXAMPLE = SKILL / "references" / "example-blueprint.json"
spec = importlib.util.spec_from_file_location("blueprint_fuzz", SKILL / "scripts" / "blueprint.py")
bp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bp)

VALUES = [None, 5, True, "x", [], {}, ["a"], [5], [{}], [None]]
MIGRATION = "CREATE TABLE snags (id uuid primary key, status text);\n"
MAIN_RS = 'fn db() { let url = std::env::var("DATABASE_URL").unwrap(); }\nconst Q: &str = "SELECT * FROM snags";\n'
SAMPLE = "crates/snag-core/src/lib.rs"


def mutation_paths(plan):
    """Every location to mutate, as a tuple of keys and indexes from the plan root."""
    out = []
    for key, val in plan.items():
        out.append((key,))
        if isinstance(val, dict):
            out.extend((key, k) for k in val)
        elif isinstance(val, list):
            for i, item in enumerate(val):
                out.append((key, i))
                if isinstance(item, dict):
                    for k, v in item.items():
                        out.append((key, i, k))
                        if k == "uses" and isinstance(v, list):
                            for j, u in enumerate(v):
                                if isinstance(u, dict):
                                    out.extend((key, i, k, j, uk) for uk in u)
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


class PlanFuzzTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        cls.root = Path(tempfile.mkdtemp(prefix="bp-fuzz-"))
        (cls.root / ".blueprint").mkdir()
        for f in cls.base["files"]:
            p = cls.root / f["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(MIGRATION if f["path"].endswith(".sql") else MAIN_RS, encoding="utf-8")
        (cls.root / "extra").mkdir()
        (cls.root / "extra" / "loose.txt").write_text("x\n", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def run_paths(self, plan_json):
        root = self.root
        (root / ".blueprint" / "blueprint.json").write_text(json.dumps(plan_json), encoding="utf-8")
        plan = bp.load_plan(root)
        bp.validate_plan(plan)
        state = bp.load_state(root)
        disk = bp.repo_files(root, plan)
        if plan["stores"] or plan["resources"]:
            state["data"] = bp.scan_data(root, plan, state, disk)
        comp = bp.compute(root, plan, state)
        bp.render_structure(plan, state, comp)
        bp.render_roster(plan, state, comp)
        bp.render_map(plan, state, comp)
        bp.data_view(plan, state)
        bp.agenticteam_tasks(plan, comp)
        bp.owner_of(plan, SAMPLE)
        bp.guard_decision(root, plan, SAMPLE, "backend-lead")
        for fmt in ("csv", "codeowners", "json", "agenticteam"):
            bp.cmd_export(argparse.Namespace(root=str(root), format=fmt, team=None, json=False))
        bp.cmd_contracts(argparse.Namespace(root=str(root), write=False, module=None))
        bp.cmd_status(argparse.Namespace(root=str(root), owner=None, phase=None, next=True, json=False))

    def test_no_mutation_crashes(self):
        started = time.time()
        paths = mutation_paths(self.base)
        crashes, count = {}, 0
        for path in paths:
            cases = [(v, False) for v in VALUES] + [(None, True)]
            for value, delete in cases:
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
        print(f"\nfuzz: {count} mutations in {elapsed:.1f}s")
        report = "\n".join(f"{len(v)} x {k}; e.g. {v[0]}" + ("; " + "; ".join(v[1:3]) if len(v) > 1 else "")
                           for k, v in sorted(crashes.items(), key=lambda kv: -len(kv[1])))
        self.assertEqual(crashes, {}, f"{sum(map(len, crashes.values()))} crashing mutations of {count}:\n{report}")
        self.assertGreater(count, 1500)


if __name__ == "__main__":
    unittest.main()
