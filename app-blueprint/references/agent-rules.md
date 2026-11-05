# Rules for building agents

Paste this section into the repo's `AGENTS.md` (Codex, Cursor, Copilot, Gemini, OpenCode and AgenticTeam read it). In `CLAUDE.md`, add one line: `Follow the "App blueprint" section of AGENTS.md.`

---

## App blueprint

This repo is built from a plan in `.blueprint/`. Read `.blueprint/STRUCTURE.md` (what files exist, where, and why) and `.blueprint/ROSTER.md` (who owns what) before writing code.

1. **Write only planned files you own.** Check with `python .blueprint/bin/blueprint.py owner <path>`. Ownership is decided by the last matching rule in ROSTER.md.
2. **No unplanned files.** If you need a file that is not in the plan, stop and propose a blueprint entry (path, action, module, phase, one-sentence purpose, owner). Do not create it until the plan is updated.
3. **Claim, build, mark.** Before starting a file: `blueprint.py claim <path> --by <your id>`. When it is complete: `blueprint.py mark <path> built --by <your id>`. If you are stuck: `mark <path> blocked --by <your id> --note "<why>"`.
4. **Contracts are exact.** Use the names in `produces` and `consumes` exactly. Change a shared contract (★ in STRUCTURE.md) only through its owner.
5. **Verify honestly.** Run your files' verify commands; `blueprint.py verify --owner <your id> --run` records the result. Never claim a file works without that.
6. **Do not edit** `.blueprint/state.json`, `log.jsonl`, `STRUCTURE.md`, `ROSTER.md` or `map.html`. The script writes them.
7. **See what is next** with `blueprint.py status --next --owner <your id>`.

Your id is the roster id you were assigned (for AgenticTeam, your agent id). If `BLUEPRINT_OWNER` is set in your environment, use that.
