# Optional hooks: write guard and automatic sync

## Claude Code

Add to the project's `.claude/settings.json` (merge with existing hooks). Ask the user before changing settings.

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Write|Edit|MultiEdit|NotebookEdit",
        "hooks": [{ "type": "command", "command": "python .blueprint/bin/blueprint.py hook pre" }] }
    ],
    "PostToolUse": [
      { "matcher": "Write|Edit|MultiEdit|NotebookEdit",
        "hooks": [{ "type": "command", "command": "python .blueprint/bin/blueprint.py hook post" }] }
    ]
  }
}
```

- **pre** stops a write to a file that is not in the blueprint, and, when `BLUEPRINT_OWNER` is set, a write to a file owned by someone else. The agent sees why and should propose a blueprint change instead.
- **post** runs `sync` quietly, so `state.json`, `STRUCTURE.md`, `ROSTER.md` and `map.html` stay current after every write.

Environment variables:

| Variable | Values | Effect |
|---|---|---|
| `BLUEPRINT_GUARD` | `block` (default), `ask`, `off` | `ask` lets the person approve an unplanned write instead of refusing it |
| `BLUEPRINT_OWNER` | a roster id | also refuse writes to files this owner does not own; set it per worker session |

Files under `.blueprint/` and ignored paths are always allowed.

## Codex and other agents

Codex and most other agents do not run Claude Code hooks. Use:

- the rules in [agent-rules.md](agent-rules.md), in `AGENTS.md`;
- `python .blueprint/bin/blueprint.py guard <path> --by <id>` in any wrapper or pre-commit step (exit 2 = refused);
- `python .blueprint/bin/blueprint.py sync` at the end of each task, or in CI next to `render --check`.

## CI

```
python .blueprint/bin/blueprint.py validate
python .blueprint/bin/blueprint.py render --check
python .blueprint/bin/blueprint.py sync --no-render --strict
```

`render --check` fails when the committed views no longer match the committed plan and state (run it before `sync`, which records a new sync time). `sync --strict` fails when there are unplanned files or missing `modify` targets.
