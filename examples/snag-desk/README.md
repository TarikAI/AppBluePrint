# Example: Snag Desk

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
