# Working with AgenticTeam, agent platforms, other skills, and EGM

## AgenticTeam

[AgenticTeam](https://github.com/TarikAI/AgenticTeam) runs a build through fixed stages with a team of role agents. The blueprint is the source tree and ownership plan those stages need. `.agentic-team/team.json` holds the agent ids and their access levels.

| Stage | What the blueprint does there |
|---|---|
| 00_intake, 01_product | Read `BRIEF.md` and `PRD.md` for intake. Put the PRD's `AC-` ids on the files that serve them (`files[].ac`). |
| 02_solution | cto-architect (or fullstack-engineer) drafts the blueprint with this skill. `ARCHITECTURE.md` links `.blueprint/STRUCTURE.md` as its source tree. Significant choices go into `decisions[]` with the ADR id as `source`. |
| 03_readiness | The Goldfish readiness review ([readiness-review.md](readiness-review.md)) runs as a verification task for code-reviewer. `status --json` lists files per `ac` id for `READINESS.md`. |
| 04_build | `export agenticteam --team .agentic-team/team.json` prints one `add-task` per owner and phase: `--path` for each owned file and pattern, `--depends-on` for earlier phases and cross-owner dependencies, `--input` for STRUCTURE.md and ROSTER.md, `--check` for the verify commands, and one `--verifies` task per phase for qa-lead. The orchestrator reviews and runs them. |
| 05_verify | qa-lead runs `verify --run` and `sync --strict`; the results go into `VERIFICATION.md`. |
| 07_learn | `log.jsonl` and `decisions[]` feed the retrospective. |

Worker rules:
- Each worker session sets `BLUEPRINT_OWNER=<agent id>` so the guard refuses writes outside its files.
- Workers `claim` before writing and `mark built` when done.
- Their AgenticTeam evidence is `file:<path>` for each file and `cmd:<exec-id>` for the verify run.

Owners must be agents with `workspace-write` access. Review roles (code-reviewer, qa-lead, security-engineer, performance-engineer, accessibility-specialist, privacy-engineer) and read-only roles never own files. `validate --team` enforces this.

## Agent platforms and worker pools

For a platform that runs coding workers itself (for example Codex app-server or Claude Agent SDK workers, one worktree per task):
- `export json` gives the plan and progress in one document.
- `export agenticteam --json` gives task objects with owned paths, dependencies and checks, ready to become worker tasks.
- Each worker runs with `BLUEPRINT_OWNER` set to its roster id, so the guard keeps it inside its own files.
- If the platform has its own build dashboard, put its milestone ids in `decisions[].source` or the phase notes, so the plan and the dashboard point at each other.
- Treat anything the platform's plan marks as only proposed as a decision to confirm before it becomes a planned file.

## Related skills

- **[adminwright](https://github.com/TarikAI/adminwright):** when the app has an admin console, plan one module for it (for example `apps/admin`). Give it one owner rule to the adminwright lane and a decision pointing at `.admin-console/manifest.json`. Do not duplicate adminwright's screens and capabilities in the blueprint; plan only the files adminwright will not generate, and let `sync` pick up what it builds through a pattern.
- **[DesignArchitect](https://github.com/TarikAI/DesignArchitect) / stitch-architect:** their page and state graph is the list of screens. Turn each page family into a pattern (route file plus component) or individual files for phase 1, and keep the page ids in each `instance` or `examples` so design and code can be traced.
- **[skill-creator](https://github.com/anthropics/skills):** for evaluating or changing this skill itself.

## Elephant-Goldfish Model (optional)

[EGM](https://github.com/google/elephant-goldfish-model) is not required; this skill's readiness review borrows its ideas. If it is installed:
- `egm-draft` and `egm-review` suit per-feature design docs; their "Target Files" lists can be added to the blueprint as new entries.
- `egm-drift-report` checks that behaviour matches the design docs, which complements `sync` (which only checks structure).
- `egm-bootstrap` can document an existing repo before you blueprint it.

## Harnesses

| Harness | Install the skill | Notes |
|---|---|---|
| Claude Code | `~/.claude/skills/app-blueprint/` (or a project's `.claude/skills/`) | Optional hooks in [hooks.md](hooks.md). Goldfish review through the Agent tool. |
| Codex | `~/.codex/skills/app-blueprint/` | Reads `AGENTS.md`. Use `agents/openai.yaml` for the skill picker. Run `guard` and `sync` from tasks. |
| Others (Cursor, Copilot, Gemini, OpenCode, Antigravity) | their skills folder, or just `AGENTS.md` plus the copied `.blueprint/bin/blueprint.py` | The tool is plain Python, so any agent that can run a command can use it. |
