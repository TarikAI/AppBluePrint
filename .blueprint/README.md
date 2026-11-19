# .blueprint

- `blueprint.json`: the plan. Agents propose changes; a person approves them.
- `state.json`, `log.jsonl`: progress. Written only by `bin/blueprint.py`.
- `STRUCTURE.md`, `ROSTER.md`, `map.html`: generated views. Do not edit.

Common commands (run from the repo root):

```
python .blueprint/bin/blueprint.py validate
python .blueprint/bin/blueprint.py sync
python .blueprint/bin/blueprint.py status --next
python .blueprint/bin/blueprint.py owner <path>
```
