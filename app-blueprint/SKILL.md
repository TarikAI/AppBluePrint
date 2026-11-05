---
name: app-blueprint
description: Plan an app's complete file structure before any code is written, from its tech stack and build approach, then keep a roster of who owns which files and a progress map that stays in sync with the repo. Produces .blueprint/blueprint.json plus generated STRUCTURE.md (annotated tree and module graph), ROSTER.md (owner rules and write lists) and map.html (interactive progress map). Use when starting a new app or platform, planning a large feature's files, splitting build work across agents (AgenticTeam, Claude Code, Codex) or developers without file collisions, mapping an existing repo's structure, or checking build progress and drift against the plan. Triggers: "plan the file structure", "app blueprint", "what files should the app have", "folder structure for this stack", "who builds which files", "build roster", "progress map", "is the code drifting from the plan". Not for a single-file change or a bug fix.
---

# App Blueprint

The plan is one file, `.blueprint/blueprint.json`. A script turns it into views for people and agents and keeps progress in sync with the real repo. Agents build from the plan; nobody guesses where a file goes or who writes it.

| File in `.blueprint/` | Written by | What it is |
|---|---|---|
| `blueprint.json` | you, with the user's approval | stack, modules, phases, every planned file, patterns, owner rules, roster, decisions |
| `state.json`, `log.jsonl` | the script only | progress per file, append-only event log |
| `STRUCTURE.md`, `ROSTER.md`, `map.html` | the script only (`render`, `sync`) | annotated tree and module graph; owners and write lists; interactive map |
| `bin/blueprint.py` | `init` copies it | the tool every agent and harness runs from the repo |

Field reference: [references/schema.md](references/schema.md). Worked example: [references/example-blueprint.json](references/example-blueprint.json).

## The tool

Before `init`, run the script beside this file: `python <this skill folder>/scripts/blueprint.py`. After `init`, everyone runs the copy in the repo, which works the same in Claude Code, Codex and any other agent:

```
python .blueprint/bin/blueprint.py <command>      # on Windows, `py -3` also works
```

Commands: `init`, `validate [--team team.json]`, `render [--check]`, `sync`, `status [--owner X] [--phase P] [--next]`, `owner <path>`, `claim <path> --by X`, `mark <path> built|blocked|planned --by X`, `verify [--phase P] [--run]`, `guard <path> --by X`, `contracts [--write]`, `export codeowners|csv|json|agenticteam`. Each has `--help`. Exit codes: 0 ok, 1 error, 2 guard refused, 3 not allowed (wrong owner, unplanned path), 64 bad usage.

## Choose the mode

- **New app or platform** → steps 1–8.
- **Existing repo** → "Existing code" below, then steps 5–8.
- **New feature in a blueprinted repo** → step 4 for the new files only, then steps 5–6 and 8.
- **Follow progress** → "Following progress".

## 1. Intake

Ask only what you cannot find. Read existing material first: `PRODUCT.md`, `DESIGN.md`, `README.md`, an AgenticTeam run (`BRIEF.md`, `PRD.md`, `ARCHITECTURE.md` under `.agentic-team/runs/<run>/`), planning docs, a DesignArchitect page graph or an adminwright manifest. You need:

- what the app does, for whom, on which platforms;
- the stack, or the constraints that decide it;
- the build approach: monorepo or not, who builds (AgenticTeam agents, Claude Code, Codex, people), and roughly in what order.

## 2. Stack and preset

Pick the closest preset in [references/stacks/](references/stacks/) and read it:

| Preset | Use for |
|---|---|
| `vite-react-supabase` | React SPA on Supabase, with optional Capacitor mobile builds and Vercel functions |
| `nextjs-prisma` | Next.js App Router with Prisma and Vitest |
| `pnpm-monorepo-docker` | pnpm workspace with a web app and services under Docker Compose |
| `rust-agent-engine` | Rust Cargo workspace engine (Axum, Postgres journal, approvals, MCP) with a React web app and packs |

Presets you keep for yourself go in `references/stacks/local/`, which is git-ignored. When one matches, prefer it.

A preset is a starting point. **Re-check every docs URL in it with your web tools before you rely on it**: folder conventions change between framework versions. Record each stack part in `stack[]` with `docs` and today's date in `verified`; `validate` rejects stack entries without them. If no preset fits, research the official docs the same way and say which conventions you took from where.

## 3. Initialise

```
python <skill>/scripts/blueprint.py init --name "App name" --preset <preset-id>
```

Refuses if a blueprint already exists. `init --refresh-tool` updates only the copied script.

## 4. Draft the structure, one section at a time

Write `blueprint.json` in sections. After each section, run `validate` (and `render --force` to show a draft tree), show the user the relevant part of `STRUCTURE.md`, and ask: approve, revise, or rewrite. Do not move on without approval.

1. **Layout and modules**: top-level folders, `modules[]` with path, layer, purpose, `dependsOn`, and a module `verify` command.
2. **Phases**: P1 is always a walking skeleton: the thinnest version that builds, runs and is tested end to end. Each later phase must leave the app buildable. Give each phase an `exit` check.
3. **Phase 1 files and shared contracts, every file**: one entry per file with `action` (create, modify, delete), `module`, `phase`, a one-sentence `purpose`, and `verify` if it differs from the module's. Mark shared contracts (types, API schema, DB schema, env example, design tokens) `"contract": true`. Give them `produces`, and give their users `consumes`, using the same exact names (`type Snag`, `POST /snags`) so parallel builders agree before either writes code. Add `dependsOn` paths; a file may not depend on a later phase.
4. **Later phases**: list files that are unique; describe repeated files once as a **pattern** (`glob`, `instance` = what one instance contains, `examples`), such as one row for all CRUD screens. `sync` finds the instances on disk.
5. **Config, tests, CI**: the files that make the verify commands work.

Rules while drafting:
- Zero-decision rule: a fresh agent must be able to build each file from its entry and the contracts it names, without choosing a schema, an API path, a library or an edge-case rule. If it would have to choose, decide now and record it in `decisions[]`.
- Keep it lean: no speculative folders, no custom code where the framework has a convention, reuse what exists.
- Never invent versions. Write what you verified, or ask.

## 5. Roster and owner rules

- `roster[]`: everyone who writes files. Use AgenticTeam agent ids when the team builds (`backend-engineer`, `frontend-engineer`, …; `harness: "agenticteam"`), harness names for single agents (`claude-code`, `codex`), and people with a `github` handle.
- `owners[]`: ordered path globs, **last match wins** as in CODEOWNERS. Put a fallback first (`**`), then specific rules. A later rule nested inside an earlier one is a normal refinement. A later rule that only **partly** overlaps an earlier rule of another owner (for example `crates/*/tests/**` after `crates/store/**`) makes ownership depend on rule order: `validate` reports it as an error unless that rule says `"override": true`. It also reports every planned file or pattern without an owner.
- Anchor root-only files with a leading slash: `/package.json` is the root manifest, while `package.json` matches every package.json in the repo.
- Shared contracts belong to a lead, not to a builder.
- With AgenticTeam, run `validate --team .agentic-team/team.json` (or the AgenticTeam repo's `team.json`). It checks that every owner is a real agent with `workspace-write` access; review roles such as qa-lead or code-reviewer can never own files.

Globs: `*` stays in one folder, `**` crosses folders, a trailing `/` means everything inside, a pattern without a slash (`*.md`) matches at any depth. Check any path with `owner <path>`.

## 6. Render and readiness review

Run `validate`, then `render`. Then get a **Goldfish review**: a fresh subagent with no conversation history that reads only `blueprint.json`, `STRUCTURE.md` and `ROSTER.md`, using the brief in [references/readiness-review.md](references/readiness-review.md).
- Claude Code: the Agent tool. Codex and others: their subagent tool.
- With AgenticTeam: a verification task for `code-reviewer`.
- If the harness cannot start a fresh context, say so and ask the user to run the review in a new session. A review in the same context is not a Goldfish review.

Go through the findings with the user and fix what they accept.

## 7. The map

`render` and `sync` always write `.blueprint/map.html`, a self-contained page with Overview, Tree, Roster, Phases, Modules and Drift views. Tell the user where it is. If this host can publish pages (for example a claude.ai artifact tool), **ask** whether they also want a shareable copy; never publish without a yes, and republish only when asked again.

## 8. Hand-off to builders

1. Add the rules in [references/agent-rules.md](references/agent-rules.md) to the repo's `AGENTS.md` (read by Codex, Cursor, Copilot, Gemini and others) and point `CLAUDE.md` at it.
2. Optional: install the write guard and auto-sync hooks from [references/hooks.md](references/hooks.md). In Claude Code they block unplanned or foreign writes and refresh the map after each write.
3. AgenticTeam builds: `export agenticteam --team <team.json>` prints one `add-task` per owner and phase, with owned `--path`s, dependencies, checks and a verification task per phase. Show the commands to the user; the orchestrator runs them in the 04_build stage. See [references/integrations.md](references/integrations.md).
4. People on GitHub: `export codeowners > .github/CODEOWNERS` (only roster entries with a `github` handle produce active lines).
5. Optional: `contracts --write` creates a README contract in each module folder (overview, interface from `produces`, invariants, side effects, verification) and never overwrites an existing README.

## Existing code

1. `init`, then `sync`. Every file shows as unplanned.
2. Read the tree. Group folders into `modules[]`, list shared contracts and key files individually, and describe repeated files as patterns. Put generated or vendor folders in `ignore`.
3. Use `action: "modify"` for files that exist and will change, `create` for new ones.
4. Repeat `sync` until the unplanned list is empty or every remaining file is deliberately ignored. A huge unplanned list means the patterns are too narrow.

## Following progress

- `sync` compares the plan with the files on disk, updates state, refreshes the views, and lists drift: files that exist but were never planned, and planned "modify" targets that are missing.
- Status ladder, per file: `planned` ○ → `claimed` ◔ → `on disk` ◑ (auto) → `built` ● (owner marks it) → `verified` ✔ (its verify command passed with `verify --run`). `blocked` ▲ needs a note. Editing a verified file drops it back to on disk until it is verified again.
- `status --next` lists what is ready to build: open files in the earliest unfinished phase whose dependencies are built.
- Progress is only ever counted from these states. Never estimate a percentage.
- Verify commands are shell commands from the plan. Read them (`verify` without `--run` lists them) before running them, and never add one that does more than build or test.

## Changing the plan

Agents propose; the user approves. Edit `blueprint.json`, run `validate` and `render`, and record why in `decisions[]`. Never edit `state.json`, `log.jsonl` or the generated views by hand. Never mark a file you do not own. Never create a file that is not planned: propose the entry first.

## Other references

- [references/integrations.md](references/integrations.md): AgenticTeam stages and export, agent platforms with worker pools, adminwright, DesignArchitect, and EGM (optional).
- [references/schema.md](references/schema.md): every field, statuses and glob rules.
