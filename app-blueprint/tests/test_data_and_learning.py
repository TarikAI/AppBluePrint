"""Tests for data connections (resources, static scan, db-check) and learning capture
(incidents, feedback, report, doctor). Run: python -m unittest discover -s tests -v"""
import importlib.util
import json
import os
import shutil
import sqlite3
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

spec = importlib.util.spec_from_file_location("blueprint_mod", SCRIPT)
bp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bp)

MIGRATION = """-- 0001
CREATE TABLE IF NOT EXISTS public.snags (id uuid primary key, status text not null);
CREATE TABLE snag_history (id bigserial primary key, snag_id uuid references snags(id));
CREATE TABLE scratch (x int);
DROP TABLE scratch;
"""
MAIN_RS = """use std::env;
fn db() { let url = env::var("DATABASE_URL").unwrap(); let key = env::var("SECRET_KEY").ok(); }
const INSERT: &str = "INSERT INTO snags (id, status) VALUES ($1, $2)";
const HISTORY: &str = "SELECT * FROM snag_history WHERE snag_id = $1";
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bp-data-"))
        self.store = Path(tempfile.mkdtemp(prefix="bp-store-"))
        r = self.run_bp("init", "--name", "Snag Desk")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.plan = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        for m in self.plan["modules"]:
            m["verify"] = OK_CMD
        for f in self.plan["files"]:
            f.pop("verify", None)
        self.save()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.store, ignore_errors=True)

    def run_bp(self, *args, env=None, cwd=None):
        e = dict(os.environ, PYTHONIOENCODING="utf-8", APP_BLUEPRINT_HOME=str(self.store))
        e.pop("APP_BLUEPRINT_NO_CAPTURE", None)
        e.update(env or {})
        return subprocess.run([PY, str(SCRIPT)] + list(args), cwd=cwd or self.dir, capture_output=True, text=True,
                              encoding="utf-8", env=e)

    def save(self):
        (self.dir / ".blueprint" / "blueprint.json").write_text(json.dumps(self.plan, indent=2), encoding="utf-8")

    def touch(self, rel, text="x\n"):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def store_records(self, name):
        p = self.store / name
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


class DataTest(Base):
    def test_example_with_data_validates(self):
        r = self.run_bp("validate")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("2 data resources", r.stdout)
        self.assertIn("writes snags, which backend-lead owns", r.stdout)

    def test_data_validation_errors_and_friction(self):
        self.plan["resources"][0]["store"] = "nope"
        self.plan["resources"].append({"id": "orders", "kind": "table", "purpose": "x", "definedIn": "db/orders.sql"})
        self.plan["files"][4]["uses"].append({"resource": "ghost", "access": "read"})
        self.plan["files"][4]["uses"].append({"resource": "orders", "access": "delete"})
        self.save()
        r = self.run_bp("validate")
        self.assertEqual(r.returncode, 1)
        self.assertIn("resource snags: unknown store 'nope'", r.stdout)
        self.assertIn("definedIn db/orders.sql is not a planned file", r.stdout)
        self.assertIn("uses unknown resource ghost", r.stdout)
        self.assertIn("access for orders must be read or write", r.stdout)
        friction = self.store_records("friction.jsonl")
        self.assertEqual(len(friction), 1)
        self.assertEqual(friction[0]["codes"].get("resource-invalid"), 2)
        self.assertEqual(friction[0]["codes"].get("uses-invalid"), 2)
        self.assertEqual(len(friction[0]["projectId"]), 12)

    def test_scan_finds_tables_env_and_drift(self):
        self.touch("crates/snag-api/migrations/0001_snags.sql", MIGRATION)
        self.touch("crates/snag-api/src/main.rs", MAIN_RS)
        self.touch(".env.example", "DATABASE_URL=postgres://user:pass@localhost/db\n")
        r = self.run_bp("sync")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Data: 2 table(s) in schema files", r.stdout)
        state = json.loads((self.dir / ".blueprint" / "state.json").read_text(encoding="utf-8"))
        data = state["data"]
        self.assertEqual([t["name"] for t in data["tables"]], ["snag_history", "snags"])
        self.assertEqual(data["envExample"], ["DATABASE_URL"])
        seen = data["observed"]["crates/snag-api/src/main.rs"]
        self.assertEqual(seen["snags"], "write")
        self.assertEqual(seen["snag_history"], "read")
        self.assertEqual(seen["DATABASE_URL"], "read")
        types = {(i["type"], i["resource"]) for i in data["issues"]}
        self.assertIn(("undeclared-table", "snag_history"), types)
        self.assertIn(("undeclared-use", "snag_history"), types)
        self.assertIn(("undeclared-env", "SECRET_KEY"), types)
        self.assertNotIn(("undeclared-use", "snags"), types)
        structure = (self.dir / ".blueprint" / "STRUCTURE.md").read_text(encoding="utf-8")
        self.assertIn("## Data", structure)
        self.assertIn('r_snags[("snags")]', structure)
        self.assertIn("[undeclared-table]", structure)
        roster = (self.dir / ".blueprint" / "ROSTER.md").read_text(encoding="utf-8")
        self.assertIn("## Data ownership", roster)
        html = (self.dir / ".blueprint" / "map.html").read_text(encoding="utf-8")
        self.assertIn('"resources"', html)
        r = self.run_bp("sync", "--strict", "--quiet")
        self.assertEqual(r.returncode, 1)

    def test_access_mismatch_and_declared_not_found(self):
        self.plan["files"][4]["uses"] = [{"resource": "snags", "access": "read"}, {"resource": "DATABASE_URL", "access": "read"}]
        self.save()
        self.touch("crates/snag-api/migrations/0001_snags.sql", MIGRATION)
        self.touch("crates/snag-api/src/main.rs", 'const Q: &str = "UPDATE snags SET status = $1";\n')
        self.run_bp("sync")
        data = json.loads((self.dir / ".blueprint" / "state.json").read_text(encoding="utf-8"))["data"]
        types = {(i["type"], i["resource"]) for i in data["issues"]}
        self.assertIn(("access-mismatch", "snags"), types)
        self.assertIn(("declared-not-found", "DATABASE_URL"), types)

    def test_db_check_sqlite_and_dump(self):
        self.touch("crates/snag-api/migrations/0001_snags.sql", MIGRATION)
        self.run_bp("sync")
        db = self.dir / "live.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE snags (id text)")
        con.execute("CREATE TABLE legacy_audit (id int)")
        con.commit()
        con.close()
        r = self.run_bp("db-check", "--sqlite", str(db))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("In the live database but in no migration: legacy_audit", r.stdout)
        self.assertIn("In migrations but not in the live database: snag_history", r.stdout)
        live = json.loads((self.dir / ".blueprint" / "state.json").read_text(encoding="utf-8"))["data"]["live"]
        self.assertEqual(live["source"], "sqlite:live.db")
        self.assertNotIn(str(self.dir), json.dumps(live))
        r = self.run_bp("db-check", "--sqlite", str(db), "--strict", "--ignore", "legacy_audit")
        self.assertEqual(r.returncode, 1)
        dump = self.dir / "schema.sql"
        dump.write_text("CREATE TABLE public.snags (id uuid);\nCREATE TABLE public.snag_history (id bigint);\n", encoding="utf-8")
        r = self.run_bp("db-check", "--schema-dump", str(dump), "--strict")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("matches the migrations", r.stdout)

    def test_sql_and_prisma_parsing_and_code_refs(self):
        sql = "create table a (x int); alter table a rename to b; create view v as select 1; drop view v;"
        self.assertEqual(sorted(bp.parse_sql_tables(sql, "m.sql")), ["b"])
        prisma = 'model Snag {\n  id String @id\n  @@map("snags")\n}\nmodel User {\n  id Int @id\n}\n'
        found = bp.parse_prisma_tables(prisma, "schema.prisma")
        self.assertEqual(sorted(found), ["snags", "user"])
        self.assertEqual(found["snags"]["model"], "Snag")
        self.assertEqual(bp.code_refs("await prisma.snag.create({ data })", ["snags"], ["Snag"]), "write")
        self.assertEqual(bp.code_refs("await prisma.snag.findMany()", ["snags"], ["Snag"]), "read")
        self.assertEqual(bp.code_refs("supabase.from('snags').select('*')", ["snags"], []), "read")
        self.assertEqual(bp.code_refs("supabase.from('snags').insert(row)", ["snags"], []), "write")
        self.assertIsNone(bp.code_refs("const snagsCount = 3", ["snags"], []))


class LearningTest(Base):
    def test_crash_records_incident(self):
        self.plan["files"] = ["oops"]
        self.save()
        r = self.run_bp("validate")
        self.assertEqual(r.returncode, 70, r.stdout + r.stderr)
        self.assertIn("Recorded as incident", r.stderr)
        inc = self.store_records("incidents.jsonl")
        self.assertEqual(len(inc), 1)
        rec = inc[0]
        self.assertEqual(rec["command"], "validate")
        self.assertEqual(rec["error"], "AttributeError")
        self.assertTrue(all(f.startswith("blueprint.py:") for f in rec["frames"]))
        self.assertNotIn(str(self.dir), json.dumps(rec))
        self.assertEqual(rec["project"], self.dir.name)
        r = self.run_bp("report")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Title: [bug] AttributeError", r.stdout)
        self.assertIn("Nothing has been sent", r.stderr)
        self.assertNotIn(str(self.dir), r.stdout)
        self.assertNotIn(self.dir.name, r.stdout)

    def test_capture_can_be_turned_off(self):
        self.plan["files"] = ["oops"]
        self.save()
        r = self.run_bp("validate", env={"APP_BLUEPRINT_NO_CAPTURE": "1"})
        self.assertEqual(r.returncode, 70)
        self.assertIn("Not recorded", r.stderr)
        r = self.run_bp("feedback", "--kind", "bug", "--note", "x", env={"APP_BLUEPRINT_NO_CAPTURE": "1"})
        self.assertIn("kept in this project's log only", r.stdout)
        self.assertEqual(list(self.store.iterdir()), [], "capture off must not write anything to the store")

    def test_feedback_goes_to_store_and_project_log(self):
        r = self.run_bp("feedback", "--kind", "guidance", "--part", "SKILL.md step 4",
                        "--note", f"P1 had no test file; see {self.dir}", "--by", "claude-code")
        self.assertEqual(r.returncode, 0, r.stderr)
        fb = self.store_records("feedback.jsonl")
        self.assertEqual(fb[0]["kind"], "guidance")
        self.assertIn("<project>", fb[0]["note"])
        self.assertNotIn(str(self.dir), fb[0]["note"])
        log = (self.dir / ".blueprint" / "log.jsonl").read_text(encoding="utf-8")
        self.assertIn('"event": "feedback"', log)
        r = self.run_bp("feedback", "--kind", "nope", "--note", "x")
        self.assertEqual(r.returncode, 2)

    def test_report_without_incidents(self):
        r = self.run_bp("report")
        self.assertEqual(r.returncode, 1)
        self.assertIn("No incidents recorded", r.stderr)

    def test_doctor(self):
        r = self.run_bp("doctor", "--strict")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("learning store", r.stdout)
        self.assertIn("this project's tool copy: " + bp.TOOL_VERSION, r.stdout)
        (self.dir / ".blueprint" / "bin" / "VERSION").write_text("0.9.0\n", encoding="utf-8")
        r = self.run_bp("doctor", "--strict")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Out of date", r.stdout)

    def test_project_identity_is_machine_independent(self):
        if shutil.which("git") is None:
            self.skipTest("git not available")
        ids = []
        for url in ("git@github.com:Owner/Repo.git", "https://user:secret@github.com/Owner/Repo"):  # scan-private: allow
            repo = Path(tempfile.mkdtemp(prefix="bp-id-"))
            try:
                subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
                subprocess.run(["git", "remote", "add", "origin", url], cwd=repo, check=True)
                (repo / "apps" / "shop").mkdir(parents=True)
                ident = bp.project_identity(repo / "apps" / "shop")
                self.assertEqual(ident, "github.com/owner/repo#apps/shop/")
                self.assertNotIn("secret", ident)
                ids.append(ident)
            finally:
                shutil.rmtree(repo, ignore_errors=True)
        self.assertEqual(ids[0], ids[1])

    def test_redact(self):
        s = bp.redact(r"failed at C:\Users\someone\proj\x.py and /home/someone/x for a@b.com via https://example.com/a?b=1 token abcdef1234567890abcdef1234")  # scan-private: allow
        self.assertNotIn("someone", s)
        self.assertNotIn("a@b.com", s)  # scan-private: allow
        self.assertIn("<url:example.com>", s)
        self.assertIn("<token>", s)


if __name__ == "__main__":
    unittest.main()
