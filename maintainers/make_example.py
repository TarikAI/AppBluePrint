"""Regenerate examples/snag-desk from app-blueprint/references/example-blueprint.json.

    python maintainers/make_example.py

The example is a small project partway through phase 1. Its files are placeholders, its verify
commands are no-ops, and its API code deliberately touches one undeclared table and one undeclared
env var, so every view (including Data and Drift) has something to show.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "app-blueprint"
EX = ROOT / "examples" / "snag-desk"
TOOL = [sys.executable, str(SKILL / "scripts" / "blueprint.py")]

FILES = {
    "Cargo.toml": '[workspace]\nmembers = ["crates/snag-core", "crates/snag-api"]\nresolver = "3"\n',
    "crates/snag-core/Cargo.toml": '[package]\nname = "snag-core"\nversion = "0.1.0"\nedition = "2024"\n',
    "crates/snag-core/src/lib.rs": "// Snag, SnagId and SnagStatus (placeholder)\npub struct Snag;\n",
    "crates/snag-api/Cargo.toml": '[package]\nname = "snag-api"\nversion = "0.1.0"\nedition = "2024"\n',
    "crates/snag-api/migrations/0001_snags.sql": (
        "CREATE TABLE snags (id uuid PRIMARY KEY, status text NOT NULL);\n"
        "CREATE TABLE snag_history (id bigserial PRIMARY KEY, snag_id uuid REFERENCES snags(id));\n"),
    "crates/snag-api/src/main.rs": (
        "// Axum server (placeholder)\nuse std::env;\n"
        'fn db_url() -> String { env::var("DATABASE_URL").unwrap() }\n'
        'fn secret() -> Option<String> { env::var("SECRET_KEY").ok() }\n'
        'const INSERT: &str = "INSERT INTO snags (id, status) VALUES ($1, $2)";\n'
        'const HISTORY: &str = "SELECT * FROM snag_history WHERE snag_id = $1";\n'),
    ".env.example": "DATABASE_URL=postgres://app:change-me@localhost:5432/snags\n",
    "apps/web/package.json": '{ "name": "web", "private": true }\n',
    "apps/web/src/screens/SnagDetailScreen.tsx": "// placeholder screen\nexport default function SnagDetailScreen() { return null; }\n",
    "notes/todo.txt": "An unplanned file, so the Drift view has an entry.\n",
}

README = """# Example: Snag Desk

A small blueprinted project, partway through phase 1, so every view has something to show.

- `.blueprint/blueprint.json`: the plan (4 modules, 2 phases, 12 files, 1 pattern, 4 owners, a Postgres store with a `snags` table and a `DATABASE_URL` env var)
- `.blueprint/STRUCTURE.md` and `.blueprint/ROSTER.md`: the generated views
- `.blueprint/map.html`: the interactive map, with a mind map and a Data view; open it in a browser
- `notes/todo.txt`: an unplanned file, so the Drift view has an entry
- `crates/snag-api/src/main.rs` reads a `snag_history` table and a `SECRET_KEY` env var that the plan does not declare, so the data scan reports them

The source files are placeholders and the verify commands are no-ops, so you can run any command here:

```
python .blueprint/bin/blueprint.py status --next
python .blueprint/bin/blueprint.py owner crates/snag-core/src/lib.rs
python .blueprint/bin/blueprint.py sync
python .blueprint/bin/blueprint.py export agenticteam
```
"""


def bp(*args, check=True):
    r = subprocess.run(TOOL[:1] + [str(EX / ".blueprint" / "bin" / "blueprint.py")] + list(args), cwd=EX,
                       capture_output=True, text=True, encoding="utf-8",
                       env={**__import__("os").environ, "APP_BLUEPRINT_NO_CAPTURE": "1", "PYTHONIOENCODING": "utf-8"})
    if check and r.returncode != 0:
        raise SystemExit(f"{' '.join(args)} failed:\n{r.stdout}\n{r.stderr}")
    return r


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if EX.exists():
        shutil.rmtree(EX)
    EX.mkdir(parents=True)
    subprocess.run(TOOL + ["init", "--name", "Snag Desk", "--preset", "rust-agent-engine"], cwd=EX, check=True, capture_output=True)
    plan = json.loads((SKILL / "references" / "example-blueprint.json").read_text(encoding="utf-8"))
    for m in plan["modules"]:
        m["verify"] = 'python -c "pass"'
    for f in plan["files"]:
        f.pop("verify", None)
    plan["approach"]["notes"] += " Example only: files are placeholders and every verify command is a no-op, so all commands run anywhere."
    (EX / ".blueprint" / "blueprint.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    for rel, text in FILES.items():
        p = EX / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (EX / "README.md").write_text(README, encoding="utf-8")
    bp("validate")
    bp("claim", "apps/web/src/api.ts", "--by", "frontend-engineer")
    bp("mark", "crates/snag-api/src/main.rs", "built", "--by", "backend-engineer")
    bp("verify", "--owner", "backend-lead", "--run")
    bp("verify", "--owner", "cto-architect", "--run")
    bp("mark", "apps/web/package.json", "blocked", "--by", "frontend-engineer", "--note", "waiting on the generated API client")
    print(bp("sync").stdout)


if __name__ == "__main__":
    main()
