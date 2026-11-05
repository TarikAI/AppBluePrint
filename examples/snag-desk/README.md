# Example: Snag Desk

A small blueprinted project, partway through phase 1, so every view has something to show.

- `.blueprint/blueprint.json`: the plan (4 modules, 2 phases, 10 files, 1 pattern, 4 owners)
- `.blueprint/STRUCTURE.md` and `.blueprint/ROSTER.md`: the generated views
- `.blueprint/map.html`: the interactive progress map; open it in a browser
- `notes/todo.txt`: an unplanned file, so the Drift view has an entry

The source files are placeholders and the verify commands are no-ops, so you can run any command here:

```
python .blueprint/bin/blueprint.py status --next
python .blueprint/bin/blueprint.py owner crates/snag-core/src/lib.rs
python .blueprint/bin/blueprint.py export agenticteam
python .blueprint/bin/blueprint.py sync
```
