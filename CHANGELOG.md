# Changelog

All notable changes to the app-blueprint skill. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version is `TOOL_VERSION` in `app-blueprint/scripts/blueprint.py`.

## [Unreleased]

## [1.3.0] - 2026-11-18

### Changed

- `contracts --module ID` on a module without a path says so and exits 0, and the hint to add `--write` appears only when something is left to write. `contracts` takes `--module ID` (repeatable) to list or write the README for chosen modules only; an unknown id exits 64. After writing, it reminds you to run the project's own tests, because a new README can break tooling that treats every entry in a folder as an item. SKILL.md step 8 says contracts are optional: list first, write per module when in doubt, then run the project's checks (c-55831b76).
- `validate` warns when an owner rule that is a bare folder name (`agents/`) matches that folder at the root and in a nested place. SKILL.md and `references/schema.md` now say that `docs/` matches any folder named docs and `/docs/` only the root one, and the example blueprint anchors its folder rules (c-5a1a611f).
- The minimum Python version is now 3.10 (3.9 reached end of life and is no longer on GitHub's Ubuntu runners). CI tests 3.10 and 3.12.

### Fixed

- A malformed `blueprint.json` (for example `"files": ["oops"]`) no longer crashes with an unexpected error. Every command exits 1 and names each bad entry (wrong-typed text, list and `uses` fields included), such as `blueprint.json: files[0] must be an object (got string)`, then says to fix it and run `validate` (c-62072a1b).
- Tracked files in nested folders named `.claude`, `.codex` or `.agentic-team` (for example `tests/golden/codex/.codex/config.toml`) were invisible to `sync`. Those three default ignores now match the repo root only. An ignore rule never drops a file that is individually planned. It also never drops a path that matches a pattern, but only when the rule is a plain folder rule (`.claude/`, `src/legacy/`, no wildcard) and the pattern's glob names every one of its folders (`**/.claude/*.md` names `.claude`; `src/**/*.py` does not name `legacy`, so your own `ignore: ["src/legacy/"]` still wins). Wildcard rules such as `*.lock` yield only to individually planned files. This holds with and without git. `sync` says when default rules dropped tracked files (never counting `.blueprint/`) and records up to 20 of them as `ignoredTracked` in state (c-cf8599d6).
- Because `.claude/`, `.codex/` and `.agentic-team/` are now ignored at the repo root only, `guard` and the pre-write hook refuse unplanned writes into nested folders of those names, as they do for any other unplanned path (c-cf8599d6).
- A feedback test failed on Windows runners, where temp folders can appear in 8.3 short form; the path was still removed.

## [1.2.0] - 2026-11-06

### Added

- Data connections: `stores[]` and `resources[]` (tables, views, env vars, APIs, queues, buckets) and per-file `uses` with read or write access. `validate` checks them and flags writes to a resource owned by someone else.
- Static data scan in `sync`: reads SQL migrations (create, drop, rename) and Prisma schemas, and scans planned files for SQL, Supabase-style `.from()`, Prisma calls, env reads in five languages and API hosts. It reports undeclared tables and uses, missing tables, access mismatches and declared uses not yet in code. `sync --strict` fails on them.
- `db-check`: compares a live schema with the migrations, read-only and only on request. It reads a SQLite file directly, or Postgres from a `pg_dump --schema-only` file. It never stores credentials.
- Data views: a Data tab and a Data branch in the mind map in `map.html`, a Data section in `STRUCTURE.md` and data ownership in `ROSTER.md`.
- Learning loop capture: unexpected errors are recorded as incidents in `~/.app-blueprint` with paths removed (exit code 70). Also added `feedback`, `report` (prints a cleaned GitHub issue body) and `doctor` (finds out-of-date tool copies), and friction counts from `validate`. `APP_BLUEPRINT_NO_CAPTURE=1` turns all of it off.
- Maintainer tooling: `maintainers/learn.py` (harvest, promote, decide, scan-private, export, store sync, presets), the contract `maintainers/SPEC.md`, and four agents (`blueprint-harvester`, `blueprint-fixer`, `blueprint-reviewer`, `preset-scout`).
- GitHub issue forms, a pull request template, CI on Linux and Windows with Python 3.9 and 3.12, `CONTRIBUTING.md` and `learning/LESSONS.md`.
- `install.py --agents` installs the maintainer agents for Claude Code.
- `maintainers/make_example.py` regenerates the example project.

### Changed

- Project ids for the learning loop come from the git origin and the folder inside the repo, so one project on two machines counts once.

## [1.1.0] - 2026-11-06

### Added

- Mind map view in `map.html`: the app at the centre, branches by module, owner or phase, files coloured by status.
- Shared renderer `assets/mindmap.js`, inlined by `render` and copied into `.blueprint/bin` by `init`.

### Fixed

- `init --refresh-tool` now works when run from the copied tool in `.blueprint/bin`.

## [1.0.0] - 2026-11-06

### Added

- Initial release.
- `blueprint.py` CLI, standard library only: `init`, `validate`, `render`, `sync`, `status`, `owner`, `claim`, `mark`, `verify`, `guard`, `hook`, `contracts`, `export`.
- Generated views: `STRUCTURE.md` (annotated tree and module graph), `ROSTER.md` (owners and write lists) and `map.html` (interactive progress map).
- CODEOWNERS-style owner rules, last match wins, with a check for partial overlaps.
- Four stack presets: `vite-react-supabase`, `nextjs-prisma`, `pnpm-monorepo-docker`, `rust-agent-engine`.
- AgenticTeam export (`export agenticteam`) and team check (`validate --team`).
- Claude Code hooks: a pre-write guard and a post-write sync.
- Example project `examples/snag-desk`.
- Installer `install.py` for Claude Code, Codex and other agents.
