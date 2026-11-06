"""End-to-end tests for scripts/blueprint.py. Run: python -m unittest discover -s tests -v"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SCRIPT = SKILL / "scripts" / "blueprint.py"
EXAMPLE = SKILL / "references" / "example-blueprint.json"
PY = sys.executable
OK_CMD = f'"{PY}" -c "pass"'
FAIL_CMD = f'"{PY}" -c "import sys; sys.exit(3)"'


def run(args, cwd, stdin=None, env=None):
    e = dict(os.environ)
    e.update(env or {})
    e["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([PY, str(SCRIPT)] + args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          input=stdin, env=e)


class BlueprintTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bp-test-"))
        r = run(["init", "--name", "Snag Desk", "--preset", "rust-agent-engine"], self.dir)
        self.assertEqual(r.returncode, 0, r.stderr)
        plan = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        for m in plan["modules"]:
            m["verify"] = OK_CMD
        for f in plan["files"]:
            f.pop("verify", None)
        self.plan = plan
        self.save()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def save(self):
        (self.dir / ".blueprint" / "blueprint.json").write_text(json.dumps(self.plan, indent=2), encoding="utf-8")

    def touch(self, rel, text="x\n"):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def tool(self, *args, **kw):
        return run(list(args), self.dir, **kw)

    # ------------------------------------------------------------------ tests

    def test_init_copies_tool_and_refuses_overwrite(self):
        self.assertTrue((self.dir / ".blueprint" / "bin" / "blueprint.py").exists())
        self.assertTrue((self.dir / ".blueprint" / "bin" / "map.template.html").exists())
        r = run(["init"], self.dir)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already exists", r.stderr)

    def test_example_validates(self):
        r = self.tool("validate")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OK: 10 files, 1 patterns", r.stdout)

    def test_narrower_rules_need_no_override(self):
        for rule in self.plan["owners"]:
            rule.pop("override", None)
        self.save()
        r = self.tool("validate")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_partial_overlap_is_an_error_unless_override(self):
        self.plan["owners"].append({"pattern": "**/Cargo.toml", "owner": "cto-architect"})
        self.save()
        r = self.tool("validate")
        self.assertEqual(r.returncode, 1)
        self.assertIn("partly overlap", r.stdout)
        self.assertIn("rule 5", r.stdout)
        self.plan["owners"][-1]["override"] = True
        self.save()
        r = self.tool("validate")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_unowned_file_is_an_error(self):
        self.plan["owners"] = self.plan["owners"][1:]
        self.save()
        r = self.tool("validate")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Cargo.toml: no owner rule matches it", r.stdout)

    def test_dependency_on_later_phase_is_an_error(self):
        self.plan["files"][4]["dependsOn"].append("crates/snag-core/src/lifecycle.rs")
        self.save()
        r = self.tool("validate")
        self.assertIn("lands in a later phase", r.stdout)

    def test_missing_purpose_and_unknown_module(self):
        self.plan["files"][0]["purpose"] = ""
        self.plan["files"][1]["module"] = "nope"
        self.save()
        r = self.tool("validate")
        self.assertIn("empty purpose", r.stdout)
        self.assertIn("unknown module 'nope'", r.stdout)

    def test_team_access_check(self):
        team = {"agents": [{"id": "cto-architect", "access": "workspace-write"}, {"id": "backend-lead", "access": "workspace-write"},
                           {"id": "backend-engineer", "access": "workspace-write"}, {"id": "frontend-engineer", "access": "review"}]}
        tp = self.dir / "team.json"
        tp.write_text(json.dumps(team), encoding="utf-8")
        r = self.tool("validate", "--team", str(tp))
        self.assertEqual(r.returncode, 1)
        self.assertIn("frontend-engineer: owns files but its AgenticTeam access is 'review'", r.stdout)

    def test_owner_last_match_wins(self):
        r = self.tool("owner", "crates/snag-core/src/lib.rs")
        self.assertIn("backend-lead (rule 3", r.stdout)
        r = self.tool("owner", "crates/snag-core/src/lifecycle.rs")
        self.assertIn("backend-engineer (rule 2", r.stdout)
        r = self.tool("owner", "Cargo.toml")
        self.assertIn("cto-architect (rule 1", r.stdout)

    def test_sync_status_drift_and_patterns(self):
        self.touch("Cargo.toml")
        self.touch("crates/snag-core/src/lib.rs")
        self.touch("apps/web/src/screens/SnagDetailScreen.tsx")
        self.touch("scratch/notes.txt")
        self.touch("crates/snag-core/Cargo.lock")
        r = self.tool("sync")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Unplanned files (drift): 1", r.stdout)
        self.assertIn("scratch/notes.txt", r.stdout)
        self.assertIn("Planned to modify but missing: 1", r.stdout)
        state = json.loads((self.dir / ".blueprint" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["patterns"]["web-screen"]["instances"], ["apps/web/src/screens/SnagDetailScreen.tsx"])
        for name in ("STRUCTURE.md", "ROSTER.md", "map.html"):
            self.assertTrue((self.dir / ".blueprint" / name).exists(), name)
        r = self.tool("sync", "--strict", "--quiet")
        self.assertEqual(r.returncode, 1)
        r = self.tool("status", "--next")
        self.assertIn("verified 0/10", r.stdout)
        self.assertIn("Ready to build next", r.stdout)

    def test_claim_mark_and_ownership(self):
        r = self.tool("claim", "apps/web/src/Inbox.tsx", "--by", "backend-engineer")
        self.assertEqual(r.returncode, 3)
        self.assertIn("owned by frontend-engineer", r.stderr)
        r = self.tool("claim", "apps/web/src/Inbox.tsx", "--by", "frontend-engineer")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.tool("mark", "apps/web/src/Inbox.tsx", "built", "--by", "frontend-engineer")
        self.assertEqual(r.returncode, 3)
        self.assertIn("does not exist on disk", r.stderr)
        self.touch("apps/web/src/Inbox.tsx")
        r = self.tool("mark", "apps/web/src/Inbox.tsx", "built", "--by", "frontend-engineer")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.tool("mark", "apps/web/src/api.ts", "blocked", "--by", "frontend-engineer")
        self.assertEqual(r.returncode, 64)
        r = self.tool("claim", "apps/web/src/Unplanned.tsx", "--by", "frontend-engineer")
        self.assertEqual(r.returncode, 3)
        log = (self.dir / ".blueprint" / "log.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertTrue(any('"built"' in line for line in log))

    def test_verify_marks_verified_and_edit_invalidates(self):
        self.touch("crates/snag-core/src/lib.rs")
        r = self.tool("verify", "--phase", "P1")
        self.assertIn("add --run", r.stdout)
        r = self.tool("verify", "--phase", "P1", "--owner", "backend-lead", "--run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.tool("status", "--json")
        rows = {x["path"]: x for x in json.loads(r.stdout)["rows"]}
        self.assertEqual(rows["crates/snag-core/src/lib.rs"]["status"], "verified")
        self.touch("crates/snag-core/src/lib.rs", "changed\n")
        r = self.tool("status", "--json")
        rows = {x["path"]: x for x in json.loads(r.stdout)["rows"]}
        self.assertEqual(rows["crates/snag-core/src/lib.rs"]["status"], "ondisk")

    def test_failed_verify_exits_nonzero(self):
        self.plan["modules"][0]["verify"] = FAIL_CMD
        self.save()
        self.touch("crates/snag-core/src/lib.rs")
        r = self.tool("verify", "--owner", "backend-lead", "--run")
        self.assertEqual(r.returncode, 1)

    def test_guard_and_hook(self):
        r = self.tool("guard", "scratch/new.py")
        self.assertEqual(r.returncode, 2)
        r = self.tool("guard", "apps/web/src/Inbox.tsx", "--by", "backend-engineer")
        self.assertEqual(r.returncode, 2)
        r = self.tool("guard", "apps/web/src/screens/NewScreen.tsx", "--by", "frontend-engineer")
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.dumps({"cwd": str(self.dir), "tool_input": {"file_path": str(self.dir / "scratch" / "x.py")}})
        r = self.tool("hook", "pre", stdin=payload)
        self.assertEqual(r.returncode, 2)
        self.assertIn("not in the blueprint", r.stderr)
        r = self.tool("hook", "pre", stdin=payload, env={"BLUEPRINT_GUARD": "ask"})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")
        ok = json.dumps({"cwd": str(self.dir), "tool_input": {"file_path": str(self.dir / "Cargo.toml")}})
        self.touch("Cargo.toml")
        r = self.tool("hook", "post", stdin=ok)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.dir / ".blueprint" / "map.html").exists())

    def test_render_check_and_contracts(self):
        r = self.tool("render")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.tool("render", "--check")
        self.assertEqual(r.returncode, 0, r.stdout)
        structure = (self.dir / ".blueprint" / "STRUCTURE.md").read_text(encoding="utf-8")
        self.assertIn("```mermaid", structure)
        self.assertIn("snag-core/", structure)
        self.assertIn("⟨pattern web-screen", structure)
        html = (self.dir / ".blueprint" / "map.html").read_text(encoding="utf-8")
        self.assertNotIn("__BLUEPRINT_DATA__", html)
        self.assertNotIn("/*__MINDMAP_JS__*/", html)
        self.assertIn("function mindMap(host, root, opts)", html)
        self.assertIn('id="v-mindmap"', html)
        self.assertTrue((self.dir / ".blueprint" / "bin" / "mindmap.js").exists())
        self.assertIn("Snag Desk", html)
        self.plan["files"][0]["purpose"] = "Workspace manifest, changed"
        self.save()
        r = self.tool("render", "--check")
        self.assertEqual(r.returncode, 1)
        r = self.tool("contracts", "--write")
        self.assertTrue((self.dir / "crates" / "snag-core" / "README.md").exists())
        text = (self.dir / "crates" / "snag-core" / "README.md").read_text(encoding="utf-8")
        self.assertIn("type Snag", text)
        self.touch("crates/snag-core/src/lib.rs")
        r = self.tool("sync")
        self.assertNotIn("Unplanned files", r.stdout)
        self.assertNotIn("snag-core/README.md", r.stdout)

    def test_exports(self):
        r = self.tool("export", "agenticteam")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("add-task --project . --id bp-p1-backend-engineer", r.stdout)
        self.assertIn('--path "crates/snag-api/src/main.rs"', r.stdout)
        self.assertIn("--verifies bp-p1-backend-engineer", r.stdout)
        r = self.tool("export", "agenticteam", "--json")
        tasks = json.loads(r.stdout)
        fe = next(t for t in tasks if t["id"] == "bp-p1-frontend-engineer")
        self.assertIn("bp-p1-backend-engineer", fe["dependsOn"])
        p2 = next(t for t in tasks if t["id"] == "bp-p2-backend-engineer")
        self.assertIn("bp-p1-verify", p2["dependsOn"])
        self.plan["roster"][0]["github"] = "@tarikai"
        self.save()
        r = self.tool("export", "codeowners")
        self.assertIn("* @tarikai", r.stdout)
        self.assertIn("# /crates/ backend-engineer", r.stdout)
        r = self.tool("export", "csv")
        self.assertIn("pattern,apps/web/src/screens/*Screen.tsx", r.stdout)

    def test_copied_tool_runs_from_project(self):
        r = subprocess.run([PY, ".blueprint/bin/blueprint.py", "validate"], cwd=self.dir, capture_output=True, text=True, encoding="utf-8",
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
