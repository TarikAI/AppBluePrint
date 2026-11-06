# App Blueprint

An agent skill that plans an app's complete file structure **before any code is written**, then keeps a roster of who owns which files and a progress map that stays in sync with the repo.

Coding agents left alone guess where files go, invent folders, and collide when several work in parallel. App Blueprint gives them one plan to build from:

- **Structure:** every planned file with its purpose, module, phase, contracts and the command that checks it.
- **Visualization:** an annotated tree, a module graph and an interactive map, all generated from the plan.
- **Roster:** path-glob owner rules (last match wins, like CODEOWNERS), so no two agents write the same file.
- **Progress map:** each file moves planned → claimed → on disk → built → verified. Progress is counted from those states and re-synced from the real files, and anything built outside the plan shows up as drift.
- **Data connections:** tables, env vars and services are declared with the files that read and write them, then checked against migrations and code, so a schema change never surprises another owner.
- **A learning loop:** the tool records its own crashes and your corrections locally; maintainer agents turn them into tested fixes that you approve.

It works in **Claude Code**, **Codex** and any agent that can run a Python command. The tool is a single standard-library Python script with no dependencies.

## Install

```bash
git clone https://github.com/TarikAI/AppBluePrint.git
cd AppBluePrint
python install.py                 # Claude Code and Codex
python install.py --target claude # ~/.claude/skills/app-blueprint
python install.py --target codex  # ~/.codex/skills/app-blueprint
python install.py --dest <dir>    # any other agent's skills folder
python install.py --agents        # maintainers: also the learning-loop agents, into ~/.claude/agents
```

For other agents, copy `app-blueprint/` into their skills folder, or just point them at `app-blueprint/SKILL.md`. Re-running `install.py` moves the previous copy to a `skill-backups` folder first.

## Use

Ask your agent to "plan the file structure for …", "make an app blueprint", or "split this build across agents". The skill:

1. Asks for the idea, the stack and how the app will be built (alone, with an agent team, or with people).
2. Starts from a stack preset and re-checks the preset's docs links before trusting it.
3. Drafts the plan one section at a time, with your approval after each: layout and modules, phases, every phase-1 file and shared contract, later files (repeated ones as patterns), then config, tests and CI.
4. Assigns owners, renders the views, and asks a fresh zero-context subagent whether a newcomer could build each file without making a design choice.
5. Hands off: rules for `AGENTS.md`/`CLAUDE.md`, optional write-guard hooks, and task exports for agent teams.

Everything lands in `.blueprint/` in your repo:

| File | Written by | What it is |
|---|---|---|
| `blueprint.json` | the agent, with your approval | stack, modules, phases, files, patterns, owner rules, roster, decisions |
| `STRUCTURE.md` | the script | annotated tree, Mermaid module graph, contracts, drift |
| `ROSTER.md` | the script | owners, rules with win counts, write lists per phase |
| `map.html` | the script | interactive map: overview, mind map, tree, roster, phases, modules, data, drift |
| `state.json`, `log.jsonl` | the script only | progress and an append-only event log |
| `bin/blueprint.py` | `init` | the tool every agent runs from the repo |

### The command line

```bash
python .blueprint/bin/blueprint.py validate                 # fields, phases, dependencies, owner overlaps
python .blueprint/bin/blueprint.py render                   # STRUCTURE.md, ROSTER.md, map.html
python .blueprint/bin/blueprint.py sync                     # compare with files on disk, list drift
python .blueprint/bin/blueprint.py status --next            # progress, and what is ready to build
python .blueprint/bin/blueprint.py owner src/api/users.ts   # who owns a path, and which rule says so
python .blueprint/bin/blueprint.py claim <path> --by <id>   # before writing
python .blueprint/bin/blueprint.py mark <path> built --by <id>
python .blueprint/bin/blueprint.py verify --phase P1 --run  # run verify commands; passing files become verified
python .blueprint/bin/blueprint.py export agenticteam       # or codeowners, csv, json
python .blueprint/bin/blueprint.py db-check --sqlite app.db # compare a live schema with the migrations, read-only
python .blueprint/bin/blueprint.py doctor                   # is this repo's copy of the tool out of date?
python .blueprint/bin/blueprint.py feedback --kind guidance --note "..."   # tell the maintainers what went wrong
```

Exit codes: 0 ok, 1 error, 2 refused by the write guard, 3 not allowed (wrong owner or unplanned path), 64 bad usage, 70 unexpected error (recorded as an incident).

## Data connections

The file tree says who writes which file. The database is the contract most likely to collide: one table change ripples through files owned by several people, and a live database can drift from its migrations. So the plan can also declare:

```json
"stores":    [{ "id": "main-db", "kind": "postgres", "schema": ["supabase/migrations/*.sql"], "purpose": "System of record" }],
"resources": [{ "id": "orders", "kind": "table", "store": "main-db", "definedIn": "supabase/migrations/0001_init.sql", "purpose": "One row per order" },
              { "id": "STRIPE_SECRET_KEY", "kind": "env", "definedIn": ".env.example", "purpose": "Server-side Stripe key" }],
"files":     [{ "path": "src/api/orders.ts", "uses": [{ "resource": "orders", "access": "write" }], ... }]
```

`sync` then reads the migrations or Prisma schema and scans the planned files for table names, env vars and API hosts. It reports:
- tables no resource declares, and declared tables that no migration creates;
- files that touch a resource without declaring it, or that write something declared read-only.

A table belongs to the owner of the file that defines it, so a write from anyone else is flagged for that owner's review. The live database is checked only when you run `db-check`: SQLite files are opened read-only, and Postgres is compared through a `pg_dump --schema-only` file you make. The tool never asks for credentials.

## A skill that learns

There is no telemetry. The loop only uses what stays on your machine or what someone chooses to file:

- **Capture:** an unexpected error in `blueprint.py` is recorded as an incident in `~/.app-blueprint`, with paths removed. Agents record your corrections with `feedback`, and `report` turns an incident into an issue body you can file. Validation failures are counted to show which rules confuse people. Set `APP_BLUEPRINT_NO_CAPTURE=1` to turn it all off.
- **Harvest and promote:** a maintainer runs `maintainers/learn.py`. A lesson qualifies when it is seen in two or more projects, is a crash with a traceback, or is an issue labelled bug.
- **Fix and review:** maintainer agents write a failing test first, then fix, and a fresh agent with no history reviews the pull request. The owner merges.

The maintainer agents are `blueprint-harvester`, `blueprint-fixer`, `blueprint-reviewer` and a monthly `preset-scout`. See [maintainers/README.md](maintainers/README.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

## Try the example

`examples/snag-desk/` is a small project partway through phase 1. Open `examples/snag-desk/.blueprint/map.html` in a browser, or run:

```bash
cd examples/snag-desk
python .blueprint/bin/blueprint.py status --next
```

## Stack presets

| Preset | For |
|---|---|
| `vite-react-supabase` | React SPA on Supabase, optional Capacitor mobile builds, Vercel functions |
| `nextjs-prisma` | Next.js App Router with Prisma and Vitest |
| `pnpm-monorepo-docker` | pnpm workspace with a web app and services under Docker Compose |
| `rust-agent-engine` | Rust Cargo workspace engine (Axum, Postgres journal, approvals, MCP) with a React web app |

Each preset has a layout, modules, an every-file phase-1 walking skeleton, shared contracts, repeated-file patterns, verify commands, suggested owner rules and pitfalls. They were checked against official docs on 2026-11-05. Versions move, so the skill re-checks the links each time. Put presets of your own in `app-blueprint/references/stacks/local/` (git-ignored).

## Works with

- **[AgenticTeam](https://github.com/TarikAI/AgenticTeam):** `export agenticteam` emits one `add-task` per owner and phase, with owned `--path`s, dependencies, checks and a verification task per phase. `validate --team` checks that owners are real agents with write access.
- **[adminwright](https://github.com/TarikAI/adminwright)** and **[DesignArchitect](https://github.com/TarikAI/DesignArchitect):** admin consoles and page graphs plug in as modules and patterns.
- **GitHub CODEOWNERS:** `export codeowners` turns roster entries with a GitHub handle into review owners.
- **Claude Code hooks:** an optional pre-write guard blocks unplanned or foreign writes, and a post-write hook re-syncs the map. See `app-blueprint/references/hooks.md`.

## Tests

```bash
python -m unittest discover -s app-blueprint/tests -v
python -m unittest discover -s maintainers/tests -v
python maintainers/learn.py scan-private --all
```

The tests cover the CLI end to end: data connections, learning capture, every preset through the validator, and the maintainer script. CI runs them on Linux and Windows with Python 3.9 and 3.12.

## Related work and credits

- **[Elephant-Goldfish Model](https://github.com/google/elephant-goldfish-model)** (Google, Apache-2.0): zero-context reviewers, the zero-decision readiness standard, explicit file boundaries and folder README contracts. The readiness review and folder contracts here follow these ideas; EGM itself is not required.
- **[cc-sdd](https://github.com/gotalab/cc-sdd)** and **[superpowers](https://github.com/obra/superpowers)**: a file-structure plan with one responsibility per file, and consumes/produces contracts per unit.
- **[GitHub Spec Kit](https://github.com/github/spec-kit)**, **[Kiro](https://kiro.dev/docs/specs)** and **[BMAD Method](https://github.com/bmad-code-org/BMAD-METHOD)**: task lines with exact paths, dependency waves, and a fixed status vocabulary. BMAD's warning that full trees go stale is why `sync` exists.
- **[GitHub CODEOWNERS](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners)**: last-match-wins ownership.

What App Blueprint adds is the file as the unit for the whole app, planned before code exists, with owners per path and progress per file.

## License

[MIT](LICENSE)
