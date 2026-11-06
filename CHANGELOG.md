# Changelog

All notable changes to the app-blueprint skill. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version is `TOOL_VERSION` in `app-blueprint/scripts/blueprint.py`.

## [Unreleased]

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
