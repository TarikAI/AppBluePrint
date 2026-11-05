# Preset: Rust agent engine (Cargo workspace + React web)

- **id:** rust-agent-engine
- **Use when:** An engine that runs agent work step by step with a Postgres journal, human approvals and receipts, exposed through an Axum HTTP API (OpenAPI-generated web client, SSE live updates) and an MCP server, with business packs loaded as data and a React + TypeScript web app.
- **Grounded in:** a design for an approval-gated agent engine and the official docs and crate pages listed below, checked 2026-11-05. Every choice in the Stack table is a suggestion, not a decision; crate names, top folders and the paths inside them are this preset's suggestion too

## Stack (re-check these docs before use)
| Part | Choice | Version checked | Docs to re-check |
|---|---|---|---|
| Engine language | Rust, one Cargo workspace | verify; pin the toolchain in rust-toolchain.toml (`resolver = "3"` needs Rust 1.84+) | https://doc.rust-lang.org/cargo/reference/workspaces.html |
| HTTP API | Axum (suggested) | crates.io 0.8.9 | https://docs.rs/axum/latest/axum/ |
| OpenAPI from Rust types | utoipa + utoipa-axum (suggested) | utoipa 6.0.0, utoipa-axum 0.3.0 (targets axum 0.8) | https://docs.rs/utoipa-axum/latest/utoipa_axum/ |
| Tool schemas for MCP | schemars (suggested) | 1.2.3; emits JSON Schema 2020-12 by default | https://docs.rs/schemars/latest/schemars/ |
| Live updates | Server-sent events via `axum::response::sse` (suggested) | part of axum | https://docs.rs/axum/latest/axum/response/sse/index.html |
| Database | Postgres 17 or 18 (suggested) | 18.6 is the newest stable major; 17.11 is the current 17.x minor; 19 is at RC1 | https://www.postgresql.org/support/versioning/ |
| Database host | Managed Postgres or Supabase (choose per deployment) | verify | verify (not fetched) |
| DB access | sqlx, compile-time checked queries, offline `.sqlx` (suggested) | crates.io 0.9.0 | https://docs.rs/sqlx/latest/sqlx/, https://github.com/launchbadge/sqlx/blob/main/sqlx-cli/README.md |
| Search | pgvector (suggested; search only, Postgres stays the system of record) | extension 0.8.7; crate 0.4.2 with `sqlx` feature | https://github.com/pgvector/pgvector, https://github.com/pgvector/pgvector-rust |
| Durable work | Own step journal in Postgres (leases, outbox, idempotency keys, timers); a short duroxide spike first is optional (suggested) | duroxide 0.1.32 "preview", duroxide-pg 0.1.38 | https://github.com/microsoft/duroxide |
| Background jobs | apalis for OCR, PDF, thumbnails (suggested) | crates.io 1.0.0 | https://docs.rs/apalis/latest/apalis/ |
| Models | Own `ModelProvider` trait over genai and async-openai (suggested) | genai 0.7.3, async-openai 0.43.0 | verify (not fetched) |
| MCP | rmcp, official Rust SDK, as client and server (suggested) | crates.io 3.9.0; implements spec 2026-07-28 | https://github.com/modelcontextprotocol/rust-sdk |
| Policy | cedar-policy plus simple validators (suggested) | crates.io 4.14.1; Cedar language guide v4.6 | https://docs.cedarpolicy.com/, https://docs.rs/cedar-policy/latest/cedar_policy/ |
| Files and evidence | S3-compatible object store or Supabase Storage (suggested) | verify | verify (not fetched) |
| Observability | tracing + OpenTelemetry (suggested) | tracing-opentelemetry 0.34.0, opentelemetry 0.33.1 | verify (not fetched) |
| Web | React + TypeScript with Vite (suggested) | npm: react 19.3.1, vite 8.3.6 | https://react.dev/versions, https://vite.dev/guide/ |
| Web API types | openapi-typescript from the engine's openapi.json, openapi-fetch as the client (suggested) | npm: 7.13.0 / 0.17.0 | https://openapi-ts.dev/introduction |
| Field capture | PWA first, Expo only if offline or camera needs grow (suggested) | npm vite-plugin-pwa 2.0.0; Expo SDK 58 | https://vite-pwa-org.netlify.app/guide/, https://docs.expo.dev/versions/latest/ |
| Tests | cargo test; proptest property tests for store, policy and runner; evals/ sets (suggested) | proptest 1.12.0 | verify (not fetched) |
| JS package manager | pnpm or npm (pick one) | verify | none |

## Layout
```text
<repo>/
  Cargo.toml                 virtual workspace: members crates/*, resolver "3", [workspace.dependencies], [workspace.lints]
  Cargo.lock                 shared lockfile (committed)
  rust-toolchain.toml        pinned toolchain
  .sqlx/                     sqlx offline query data from `cargo sqlx prepare --workspace` (committed)
  compose.yaml               dev and test Postgres only
  DESIGN.md                  design tokens for the web apps (colors, type, spacing)
  crates/
    engine-core/             shared types: Task, Step, Proposal, Approval, Receipt, Cost (serde, schemars, utoipa derives)
    engine-store/            Postgres pool, row-level security, journal, outbox, timers, leases
      migrations/            sqlx migrations
    engine-runner/           task claiming, agent loop, stored waits, retries
    engine-models/           ModelProvider trait, provider adapters, cost reservation and settlement
    engine-tools/            tool registry, effect types, MCP client, connectors
    engine-policy/           Cedar policies, validators, Do it / Ask me / Never
      policies/              .cedar files and the Cedar schema
    engine-evidence/         file storage, fingerprints, receipts
    engine-api/              HTTP API, SSE events, OpenAPI document (openapi.json committed)
    engine-mcp-server/       MCP server for coding agents and other MCP clients; no approve tool
    engine-server/           the process binary wiring store, runner and api; dev CLI engine-dev
  packs/
    example/                 one example pack: objects (item.toml), lifecycles, playbooks, policies, tests
  apps/
    web/                     inbox, task and proposal review, receipts, admin console (React + TypeScript)
    field/                   phone capture (PWA or Expo)
  evals/                     fixed example sets with known answers, per workflow and model
  .github/workflows/         CI: fmt, clippy, sqlx check, tests, restart test, contract drift
```

## Modules and layers
| Module id | Path | Layer | Depends on |
|---|---|---|---|
| core | `crates/engine-core/` | domain types | none |
| store | `crates/engine-store/` | persistence | core |
| models | `crates/engine-models/` | adapter | core |
| policy | `crates/engine-policy/` | rules | core |
| evidence | `crates/engine-evidence/` | adapter | core, store |
| tools | `crates/engine-tools/` | adapter | core, store (outbox), evidence |
| runner | `crates/engine-runner/` | orchestration | core, store, models, tools, policy, evidence |
| api | `crates/engine-api/` | interface | core, store, policy |
| mcp-server | `crates/engine-mcp-server/` | interface | core, store, policy |
| server | `crates/engine-server/` | process wiring | store, runner, api |
| pack-example | `packs/example/` | domain pack (data and tests) | core, tools, policy |
| web | `apps/web/` | ui | api (generated types, SSE) |
| field | `apps/field/` | ui (mobile) | api |
| evals | `evals/` | test data | runner, models, pack-example |
| repo-tooling | `Cargo.toml`, `rust-toolchain.toml`, `.sqlx/`, `compose.yaml`, `.github/` | tooling | none |

## Phase 1 walking skeleton (every file)
Phase 1 pass test: one fake task goes intake → runner (fake model) → proposal → approval → receipt, and survives a process restart. Approval here is a dev-only command line; real approvals come only with the signed-in app. tools, policy, evidence, mcp-server, packs, apps and evals start in later phases. If you try duroxide, run that spike in a throwaway folder that is not a workspace member.

| Path | Action | Module | Purpose | Verify |
|---|---|---|---|---|
| `Cargo.toml` | create | repo-tooling | Virtual workspace: members, `resolver = "3"`, `[workspace.package]`, `[workspace.dependencies]`, `[workspace.lints]` | `cargo check --workspace` |
| `Cargo.lock` | create | repo-tooling | Generated by the first build; committed | `cargo build --workspace --locked` |
| `rust-toolchain.toml` | create | repo-tooling | Pinned toolchain and components (rustfmt, clippy) | `cargo --version` |
| `compose.yaml` | create | repo-tooling | Postgres for dev and tests with a healthcheck; no app containers | `docker compose up -d db` |
| `.env.example` | create | repo-tooling | `DATABASE_URL` (non-owner app role), `DATABASE_ADMIN_URL` (migrations), `RUST_LOG`, `ENGINE_DEV_APPROVALS`; placeholders only | copy to `.env`, then `cargo run -p engine-server` starts |
| `.sqlx/` (generated) | create | repo-tooling | Offline query data for every `query!` macro | `cargo sqlx prepare --workspace --check` |
| `.gitignore` | create | repo-tooling | target/, env files; `.sqlx/` stays tracked | `git status` after a build |
| `.github/workflows/ci.yml` | create | repo-tooling | fmt, clippy `-D warnings`, `sqlx prepare --check`, tests with a Postgres service, restart test, openapi drift | CI run green |
| `crates/engine-core/Cargo.toml` | create | core | serde, schemars, utoipa from workspace deps | `cargo test -p engine-core` |
| `crates/engine-core/src/lib.rs` | create | core | Task, Step, Proposal (with fingerprint), Approval, Receipt, Cost; ids; status enums | `cargo test -p engine-core` |
| `crates/engine-store/Cargo.toml` | create | store | sqlx (postgres, tokio runtime, TLS feature), core | `cargo check -p engine-store` |
| `crates/engine-store/src/lib.rs` | create | store | Pool; transaction helper that runs `SET LOCAL` tenant id; append step plus record changes plus cost plus outbox in one transaction; lease-based task claim; stored waits | `cargo test -p engine-store` |
| `crates/engine-store/migrations/0001_engine.sql` | create | store | tenants, tasks, steps (journal), proposals, approvals, receipts, cost entries, outbox, timers; `FORCE ROW LEVEL SECURITY` and policies; app role grants | `sqlx migrate run --source crates/engine-store/migrations` |
| `crates/engine-store/tests/journal.rs` | create | store | A failed step leaves no journal, record or cost row; tenant A cannot read tenant B's tasks | `cargo test -p engine-store` |
| `crates/engine-models/Cargo.toml` | create | models | core | `cargo check -p engine-models` |
| `crates/engine-models/src/lib.rs` | create | models | `ModelProvider` trait; deterministic `FakeProvider`; reserve-then-settle cost, unknown price stays reserved | `cargo test -p engine-models` |
| `crates/engine-runner/Cargo.toml` | create | runner | core, store, models | `cargo check -p engine-runner` |
| `crates/engine-runner/src/lib.rs` | create | runner | Claim task, run steps with a save after each, emit proposal, wait (stored, no running process), resume on approval, execute once with an idempotency key, write receipt | `cargo test -p engine-runner` |
| `crates/engine-api/Cargo.toml` | create | api | axum, utoipa, utoipa-axum, core, store | `cargo check -p engine-api` |
| `crates/engine-api/src/lib.rs` | create | api | `OpenApiRouter`: GET /health, POST /v1/tasks (intake), GET /v1/tasks/{id} (status and receipt). No approve route | `cargo test -p engine-api` |
| `crates/engine-api/src/bin/export-openapi.rs` | create | api | Writes the OpenAPI document to crates/engine-api/openapi.json | `cargo run -p engine-api --bin export-openapi && git diff --exit-code crates/engine-api/openapi.json` |
| `crates/engine-api/openapi.json` | create | api | Generated API contract; committed | `git diff --exit-code` after export |
| `crates/engine-server/Cargo.toml` | create | server | store, runner, api, tokio, tracing | `cargo build -p engine-server` |
| `crates/engine-server/src/main.rs` | create | server | Binary `engine-server`: run migrations as admin role, start API and runner workers, graceful shutdown | `cargo run -p engine-server`, then `curl -fsS localhost:8080/health` |
| `crates/engine-server/src/bin/engine-dev.rs` | create | server | Dev-only CLI: `approve <proposal-id>`, `show <task-id>`; refuses unless `ENGINE_DEV_APPROVALS=1` and a local database | `cargo run -p engine-server --bin engine-dev -- show <id>` |
| `crates/engine-server/tests/p1_restart.rs` | create | server | Pass test: POST a fake task, wait for awaiting-approval, kill the process, restart, approve with engine-dev, wait for done; exactly one execution and one receipt | `cargo test -p engine-server --test p1_restart` |

## Shared contracts (every file)
| Path | Purpose | Consumed by |
|---|---|---|
| `crates/engine-core/src/lib.rs` | Domain types; the single source for OpenAPI schemas and MCP tool schemas | every crate, apps/web (through openapi.json), mcp-server |
| `crates/engine-store/migrations/0001_engine.sql` | Journal, approvals, receipts, cost and RLS schema | store, runner, api, evals, later migrations |
| `crates/engine-api/openapi.json` | Generated HTTP contract | apps/web, apps/field, tests, external clients |
| `apps/web/src/api/schema.d.ts` | Generated by openapi-typescript from openapi.json (phase 2). Regenerate, never hand-edit | apps/web, apps/field |
| `crates/engine-policy/policies/schema.cedarschema` | Cedar entity, action and context shapes (phase 2) | policy, runner, api, packs |
| `packs/example/pack.toml` | Pack manifest: objects, lifecycles, playbooks, roles, locked actions (later phase) | runner, policy, tools, apps/web, evals |
| `.env.example` | Variable names and the two database roles | every owner, devops-engineer |
| `compose.yaml` | Dev and test database | store tests, server tests, CI |
| `DESIGN.md` | Design tokens (colors, type, spacing) shared by the web apps | apps/web, apps/field, ux-ui-designer |

## Repeated-file patterns
| Pattern id | Glob | One instance contains | Example instances |
|---|---|---|---|
| migration | `crates/engine-store/migrations/*.sql` | One schema change with RLS enabled and forced, policies and app-role grants | `0002_cost_ledger.sql` |
| crate | `crates/engine-*/` | Cargo.toml using workspace deps and lints, src/lib.rs, tests/ | `engine-tools`, `engine-evidence` |
| api-route | `crates/engine-api/src/routes/*.rs` | Handlers with `#[utoipa::path]`, types from engine-core, registered with `routes!()` | `routes/tasks.rs`, `routes/events.rs` |
| tool | `crates/engine-tools/src/tools/*.rs` | One tool: name, effect type (read, draft, change internal, send outside, irreversible), schemars input, execute with idempotency key, re-read check | `item_create.rs`, `email_send.rs` |
| model-adapter | `crates/engine-models/src/providers/*.rs` | One provider: capabilities, price table, cost settlement | `anthropic.rs`, `openai.rs`, `openrouter.rs` |
| cedar-policy | `crates/engine-policy/policies/*.cedar` | Rules for one action family, with tests | `email.cedar`, `item.cedar` |
| failure-scenario | `crates/engine-server/tests/scenario_*.rs` | One failure scenario from the test plan as an automated test | `scenario_restart_before_review.rs`, `scenario_unknown_cost.rs`, `scenario_rejected_no_mutation.rs` |
| pack-object | `packs/*/objects/*.toml` | Object definition and lifecycle | `item.toml`, `review.toml` |
| playbook | `packs/*/playbooks/*/` | Versioned recipe: instructions, allowed tools, output schema, checks, worked examples | `draft-items-from-email/` |
| eval-set | `evals/<workflow>/<set>/` | Fixed cases with known answers and a scorer config | `evals/items/email-v1/` |
| web-screen | `apps/web/src/screens/*.tsx` | One screen fed by engine events through the generated client | `Inbox.tsx`, `ProposalReview.tsx` |
| locale | `apps/web/src/locales/<lang>.json` | Full key set for one language (check RTL layout on every screen if you ship an RTL language) | `en.json`, `ar.json` |

## Ignore
```text
target/
**/node_modules/
apps/*/dist/
apps/field/.expo/
coverage/
*.profraw
.env
.env.*
!.env.example
evals/**/runs/
```

## Verify commands
| Command | Proves |
|---|---|
| `cargo fmt --all --check` | Formatting |
| `cargo clippy --workspace --all-targets -- -D warnings` | Lints pass with no warnings |
| `docker compose up -d db && sqlx migrate run --source crates/engine-store/migrations` | Migrations apply to an empty Postgres |
| `cargo sqlx prepare --workspace --check` | `.sqlx` matches the queries and schema |
| `SQLX_OFFLINE=true cargo build --workspace --locked` | The workspace builds without a database |
| `cargo test --workspace` | Unit, journal and RLS tests pass |
| `cargo test -p engine-server --test p1_restart` | Phase 1 pass test: intake → fake model → proposal → dev approval → receipt across a process kill |
| `cargo run -p engine-api --bin export-openapi && git diff --exit-code crates/engine-api/openapi.json` | The API contract matches the Rust types |
| `npx openapi-typescript crates/engine-api/openapi.json -o apps/web/src/api/schema.d.ts && git diff --exit-code` (phase 2) | Web client types match the API |

## Suggested owner rules (last match wins)
Rules are checked in order and the last match wins. A leading `/` anchors a rule to the repo root (`/package.json` is only the root manifest; `package.json` would match every one). A rule nested inside an earlier rule needs nothing; mark a rule `override` only where it partly overlaps an earlier rule of another owner, which `validate` otherwise reports.

| Order | Pattern | Owner | Note |
|---|---|---|---|
| 1 | `**` | cto-architect | Fallback |
| 2 | `crates/**` | backend-engineer | |
| 3 | `crates/engine-store/**` | database-engineer | Journal, RLS, later migrations |
| 4 | `crates/engine-models/**` | ai-ml-engineer | |
| 5 | `crates/engine-tools/**` | integration-engineer | |
| 6 | `crates/engine-mcp-server/**` | integration-engineer | |
| 7 | `crates/engine-runner/**` | backend-lead | Core dependency; needs human Rust review |
| 8 | `crates/engine-policy/**` | backend-lead | Core dependency; needs human Rust review |
| 9 | `crates/*/tests/**` | test-engineer | override: includes p1_restart.rs and failure scenarios |
| 10 | `packs/**` | backend-engineer | |
| 11 | `packs/*/playbooks/**` | ai-ml-engineer | |
| 12 | `packs/*/policies/**` | backend-lead | Locked actions |
| 13 | `apps/web/**` | frontend-engineer | |
| 14 | `apps/field/**` | mobile-engineer | |
| 15 | `apps/*/src/locales/**` | localization-specialist | |
| 16 | `evals/**` | eval-engineer | |
| 17 | `/compose.yaml` | devops-engineer | |
| 18 | `.github/**` | devops-engineer | |
| 19 | `/Cargo.toml` | build-integrator | Root manifest |
| 20 | `/rust-toolchain.toml` | build-integrator | |
| 21 | `.sqlx/**` | build-integrator | Generated; refresh with `cargo sqlx prepare --workspace` |
| 22 | `crates/engine-core/**` | backend-lead | override: shared types |
| 23 | `crates/engine-store/migrations/0001_engine.sql` | backend-lead | override: P1 schema contract |
| 24 | `crates/engine-api/openapi.json` | backend-lead | override: generated contract |
| 25 | `crates/engine-policy/policies/schema.cedarschema` | backend-lead | override |
| 26 | `packs/*/pack.toml` | backend-lead | override: pack contract |
| 27 | `apps/web/src/api/schema.d.ts` | frontend-lead | override: generated client types |
| 28 | `/DESIGN.md` | frontend-lead | override: design tokens |
| 29 | `/.env.example` | cto-architect | override |

## Pitfalls
- The stack choices here are suggestions. Record them as proposed in a blueprint until the owner confirms each one, and leave the database host and field app open until phase 1 needs them.
- No approve path for agents: no approve route in the API, no approve tool in the MCP server, no API-key approval. The phase 1 `engine-dev approve` command must refuse to run unless `ENGINE_DEV_APPROVALS=1` is set against a local database, and is removed or disabled once the signed-in app exists.
- An approval covers the proposal's fingerprint and the record version it was based on. Recompute both before executing; a mismatch voids the approval.
- A step's journal entry, the records it changed, its cost entry and any outbox row commit in one transaction or not at all. Never call an outside system inside that transaction; write the outbox row and send after commit.
- Use `FORCE ROW LEVEL SECURITY` on every tenant table, connect as a non-owner role, and set the tenant id with `SET LOCAL` inside each transaction; a session-level `SET` leaks between pooled connections. Run migrations with a separate owner role.
- Task claiming uses leases that expire, so a crashed worker's task is picked up again from its last saved step. Re-check the target before retrying any action without an idempotency key, and ask a person when the outcome is unknown.
- Reserve a cost ceiling before every model call and settle after. An unknown price stays reserved and raises an alert; cost is never recorded as zero.
- The restart test must kill the real process, not drop an object in the same process, and must assert exactly one execution and one receipt.
- sqlx `query!` macros need a live database or `.sqlx` at compile time. Run `cargo sqlx prepare --workspace` after any query or schema change, commit `.sqlx`, and check it in CI with `--check`.
- openapi.json and schema.d.ts are generated. Never hand-edit them; CI regenerates both and fails on a diff. Derive both `utoipa::ToSchema` and `schemars::JsonSchema` on engine-core types so the API and MCP schemas cannot drift.
- Emails, PDFs and web pages are evidence, never instructions. Every tool declares its effect type, and anything that leaves the organization or creates an official record is locked to "Ask me".
- Engine crates must not import pack code. A second business domain is a second pack, and adding it should need no change to the engine crates.
- Prefer one database. Workflow engines that bring their own store add a second database to run and back up; reach for one only when the Postgres journal cannot meet a need. Check the license and maintenance status of any self-hosted object store before choosing it.
- Postgres 18 is the newest stable major and 19 is at release candidate. Pick the major together with the host, and check that pgvector images exist for it (they exist for pg17 and pg18).
- rmcp follows MCP spec 2026-07-28 and gates transports behind cargo features; check feature names before wiring the server.
- engine-store, engine-policy and engine-runner get property tests with thousands of random cases and line-by-line human review. Plan extra time if no one on the team has deep Rust experience.
- Keep scripts and tests cross-platform: no hardcoded POSIX paths or `df`/`ps` calls.
