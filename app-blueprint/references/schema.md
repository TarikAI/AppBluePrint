# blueprint.json field reference (version 1)

`validate` enforces everything marked **required**. Paths are repo-relative with forward slashes.

## Top level

| Field | Type | Notes |
|---|---|---|
| `version` | `1` | **required** |
| `app` | object | `name` **required**; `summary`, `platforms[]`, `preset` |
| `stack[]` | objects | `part`, `choice`, `docs` (URL you checked), `verified` (date checked) **required**; `version` |
| `approach` | object | free keys, usually `layout` and `notes`; shown in STRUCTURE.md |
| `modules[]` | objects | see below |
| `phases[]` | objects | ordered; at least one. `id`, `name`, `goal`, `exit` |
| `files[]` | objects | every individually planned file |
| `patterns[]` | objects | repeated files described once |
| `owners[]` | objects | ordered owner rules, last match wins |
| `roster[]` | objects | everyone who can own files |
| `decisions[]` | objects | `id`, `decision`, `why`, `source` |
| `ignore[]` | globs | extra paths `sync` never reports as drift |
| `stores[]` | objects | databases and storage services, see below |
| `resources[]` | objects | tables, env vars, APIs and other things the code connects to, see below |

## modules[]

| Field | Notes |
|---|---|
| `id` | **required**, unique |
| `path` | folder of the module, `""` for repo-level files |
| `layer` | e.g. domain, interface, data, ui, build |
| `purpose` | **required** |
| `dependsOn[]` | module ids; cycles are errors |
| `verify` | default verify command for the module's files and patterns |

## files[]

| Field | Notes |
|---|---|
| `path` | **required**, unique, repo-relative |
| `action` | **required**: `create`, `modify` (exists and will change), `delete` |
| `module`, `phase` | **required**, must exist |
| `purpose` | **required**, one sentence |
| `verify` | **required** unless the module has one (not needed for `delete`) |
| `contract` | `true` for shared contracts several owners depend on |
| `produces[]` | exact names this file provides: `type Snag`, `POST /snags`, `listSnags()` |
| `consumes[]` | names it uses; a name nobody produces is a warning |
| `dependsOn[]` | planned paths or pattern ids; a dependency in a later phase is an error |
| `ac[]` | acceptance-criteria ids (AgenticTeam `AC-` ids) this file serves |

| `uses[]` | resources this file touches: `{ "resource": "<id>", "access": "read" \| "write" }` |

## patterns[]

| Field | Notes |
|---|---|
| `id` | **required**, unique |
| `glob` | **required**, anchored at the repo root, e.g. `apps/web/src/screens/*Screen.tsx` |
| `module`, `phase` | **required** |
| `purpose` | **required**: what the set of files is for |
| `instance` | **required**: what one instance contains, specific enough to build one |
| `examples[]` | expected instance names |
| `verify` | or inherited from the module |

## stores[] and resources[]

Store: `{ "id", "kind": "postgres"|"sqlite"|"mysql"|"supabase"|"redis"|"s3"|"other", "schema": ["<globs of migration or schema files>"], "purpose" }`.

Resource:

| Field | Notes |
|---|---|
| `id` | **required**, unique; for a table, its name |
| `kind` | **required**: `table`, `view`, `env`, `api`, `queue`, `bucket`, `other` |
| `purpose` | **required** |
| `store` | a store id; expected for tables and views |
| `definedIn` | the planned file that creates it: a migration, `schema.prisma`, `.env.example`; expected for tables and views |
| `owner` | defaults to the owner of `definedIn` |
| `aliases[]` | other names the code uses, e.g. the Prisma model name `Snag` for table `snags` |
| `host` | for `api`: the host name the code calls, so `sync` can find it |

What `sync` checks (statically, no database connection):

| Issue | Meaning |
|---|---|
| `undeclared-table` | a schema file creates a table no resource declares |
| `missing-table` | a declared table or view that no schema file creates |
| `undeclared-use` | a planned file touches a resource (or schema table) without declaring it in `uses` |
| `undeclared-env` | a planned file reads an env var no resource declares (checked once any env resource exists) |
| `access-mismatch` | a file writes a resource it declares as read |
| `declared-not-found` | a file declares a use its code does not show yet (informational) |

SQL is read for `CREATE TABLE/VIEW`, `DROP` and `ALTER TABLE … RENAME TO`, in file order; Prisma for `model` and `@@map`. Code is scanned for SQL (`FROM`, `JOIN`, `INSERT INTO`, `UPDATE`, `DELETE FROM`), Supabase-style `.from('t')`, Prisma `prisma.model.create/find…`, and env reads in JS, Python, Rust, Deno and Ruby. `db-check` compares a live schema with the migrations: a SQLite file opened read-only, or a schema-only SQL dump.

## owners[] and roster[]

Owner rule: `{ "pattern": "<glob>", "owner": "<roster id>", "override": true|false, "note": "..." }`.

- Rules are checked in order; the **last** matching rule decides the owner, as in GitHub CODEOWNERS.
- A later rule whose planned matches all fall inside an earlier rule's matches is a refinement and needs nothing (a `**` fallback first, specific rules after).
- A later rule that **partly** overlaps an earlier rule of a different owner (it shares some paths and also takes paths the earlier rule does not cover) makes ownership depend on rule order. `validate` reports it as an error unless the later rule has `"override": true`.
- Every planned file and every pattern glob must have an owner.
- A rule that matches nothing, or never wins, is a warning.

Roster entry: `{ "id", "kind": "agent"|"human", "harness": "agenticteam"|"claude-code"|"codex"|..., "role", "github": "@handle", "lane" }`. With `validate --team team.json`, every `harness: "agenticteam"` agent must exist in team.json, and owners of files must have `workspace-write` access.

## Glob rules

| Pattern | Matches |
|---|---|
| `*` | anything inside one folder level |
| `**` | any number of folders |
| `?` | one character |
| `docs/` or `docs` | the folder and everything inside it |
| `*.md` (no slash) | at any depth |
| `/README.md` or `apps/web/x` (has a slash) | anchored at the repo root |
| `package.json` (no slash) | every package.json at any depth; write `/package.json` for the root one |

## Status (state.json, written by the script)

| Status | Glyph | Set by |
|---|---|---|
| planned | ○ | default; file not on disk |
| claimed | ◔ | `claim` by the owner |
| on disk | ◑ | automatic when the file exists |
| built | ● | `mark built` by the owner (file must exist) |
| verified | ✔ | `verify --run` passed; the file's hash is stored, and any later edit drops it to on disk |
| blocked | ▲ | `mark blocked --note` |

For `action: "delete"`, the file counts as verified once it is gone.

Drift found by `sync`: files on disk that are not planned, not pattern instances, not ignored and not a module's `README.md`; and `modify` targets that are missing.
