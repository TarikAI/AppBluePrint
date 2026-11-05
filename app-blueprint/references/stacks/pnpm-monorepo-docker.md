# Preset: pnpm workspace monorepo (Next.js app + Node services) on Docker Compose

- **id:** pnpm-monorepo-docker
- **Use when:** One repo holds a web app plus several small Node services that talk over HTTP, installed as one pnpm workspace and run together with Docker Compose (self-hosted or a single VM), with optional services behind compose profiles.
- **Grounded in:** a production pnpm workspace monorepo (a Next.js web app plus several Node services run with Docker Compose) and the official docs listed below, checked 2026-11-05

## Stack (re-check these docs before use)
| Part | Choice | Version checked | Docs to re-check |
|---|---|---|---|
| Package manager | pnpm workspace, one root lockfile | npm latest 12.16.1 | https://pnpm.io/workspaces, https://pnpm.io/pnpm-workspace_yaml, https://pnpm.io/settings/build |
| Runtime | Node.js, one major for build images, runtime images and CI | Node 26 is the current LTS | https://nodejs.org/api/test.html |
| Web app | Next.js 16 App Router, `src/` layout, `output: 'standalone'` | npm latest 16.4.5 (Active LTS, 2026-11-03 security release) | https://nextjs.org/docs/app/getting-started/project-structure, https://nextjs.org/docs/app/api-reference/config/next-config-js/output, https://nextjs.org/docs/app/guides/upgrading/version-16 |
| UI | React | npm latest 19.3.1 | https://react.dev/versions |
| Web i18n | next-intl, `messages/<lang>.json` | verify | verify (not fetched) |
| Web styling | Tailwind CSS v4 | npm latest 4.3.4 | https://tailwindcss.com/docs/installation/framework-guides/nextjs |
| Web database | SQLite via better-sqlite3, migrations as a typed array in code | verify | verify (not fetched) |
| Service database | Postgres + pgvector container (`pgvector/pgvector:pg<major>`) | pgvector latest 0.8.7 with image tags up to pg18; Postgres 18.6 is the newest stable major | https://github.com/pgvector/pgvector, https://www.postgresql.org/support/versioning/ |
| Service DB driver | `pg`; PGlite when no DB URL is set | verify | verify (not fetched) |
| Services | Plain Node ESM (`.mjs`) on node:http; TypeScript services can run with Node type stripping | Node built-in | https://nodejs.org/api/test.html |
| Containers | Docker Compose: one root file, profiles for optional services, overlay files in ops/ | `compose.yaml` is the canonical file name; `docker-compose.yml` is still read | https://docs.docker.com/compose/how-tos/profiles/, https://docs.docker.com/compose/intro/compose-application-model/ |
| Web unit tests | Vitest + jsdom + Testing Library | npm latest 5.1.0 | https://vitest.dev/guide/ |
| E2E tests | Playwright | npm latest 1.64.1 | https://playwright.dev/docs/intro |
| Service tests | `node --test` | Node built-in | https://nodejs.org/api/test.html |
| Optional Rust service | Cargo crate outside the pnpm graph | verify | https://doc.rust-lang.org/cargo/reference/workspaces.html |

## Layout
```text
<repo>/
  package.json                 root scripts only (dev:all with concurrently, doctor); packageManager pin; no app deps
  pnpm-workspace.yaml          members (web, services/*), allowBuilds, overrides: the install settings
  pnpm-lock.yaml               the only lockfile; install from the root only
  docker-compose.yml           full stack: database, services, web; optional services behind profiles
  .env.example                 every variable compose and the services read
  ops/
    init/                      SQL the Postgres image runs once on an empty volume (extensions)
    <feature>/compose.<feature>.yml   overlay for an optional stack (-f docker-compose.yml -f ... --profile ...)
  web/                         the Next.js app
    src/app/                   pages and route handlers (src/app/api/<area>/**/route.ts)
    src/components/            UI; panels/ for dashboard panels
    src/lib/                   server logic: db, migrations, auth, service clients
    src/proxy.ts               Next 16 proxy: host allow-list, CSP nonce, auth gate
    src/test/                  Vitest setup (isolated data dir)
    messages/                  locale files; every locale needs every key
    tests/                     Playwright specs
    scripts/                   CI guards (route auth, API parity, no mocks)
    openapi.json               web API contract, checked against route handlers
    Dockerfile                 deps, build, runtime stages; runs the standalone server
  services/
    <service>/                 one deployable Node service each
      package.json             @<scope>/<service>, "type": "module", serve and test scripts
      src/                     server code
      test/                    node:test suites
      Dockerfile               image used by compose
      README.md                port, env, endpoints, auth header
  packages/                    optional shared TS code; needs root-context image builds
  scripts/                     repo-level dev helpers (cleanup, doctor, start-prod)
  docs/                        architecture and plans
  .github/workflows/           CI; GitHub reads workflows only from the repo root
```

## Modules and layers
| Module id | Path | Layer | Depends on |
|---|---|---|---|
| root-tooling | `package.json`, `pnpm-workspace.yaml`, `pnpm-lock.yaml`, `scripts/**`, `.gitignore` | tooling | none |
| ops | `docker-compose.yml`, `ops/**`, `.env.example`, `*/Dockerfile` | deploy | every image |
| web-routes | `web/src/app/**` | ui route and route handlers | web-lib, web-components |
| web-components | `web/src/components/**` | ui | client-safe parts of web-lib |
| web-lib | `web/src/lib/**` | server | svc-data over HTTP, web database |
| web-proxy | `web/src/proxy.ts` | request edge | web-lib (auth) |
| web-i18n | `web/messages/**` | content | none |
| web-tests | `web/tests/**`, `web/src/**/*.test.ts`, `web/vitest.config.ts`, `web/playwright.config.ts` | test | web modules |
| svc-data | `services/data-service/src/**` | service | its Postgres database |
| svc-tests | `services/*/test/**` | test | each service |
| ci | `.github/workflows/**` | tooling | all |

## Phase 1 walking skeleton (every file)
| Path | Action | Module | Purpose | Verify |
|---|---|---|---|---|
| `package.json` | create | root-tooling | Root scripts: dev:all (concurrently), test (`pnpm -r test`); packageManager pin | `pnpm install` |
| `pnpm-workspace.yaml` | create | root-tooling | `packages: [web, services/*]`; allowBuilds for native deps; security overrides | `pnpm install --frozen-lockfile` |
| `pnpm-lock.yaml` | create | root-tooling | The one lockfile, written by the root install | `pnpm install --frozen-lockfile` |
| `.gitignore` | create | root-tooling | node_modules, .next, .data, env files, local databases | `git status` after a build |
| `.env.example` | create | ops | DB password, service API key, ports, allowed hosts; placeholders only | `docker compose config` |
| `docker-compose.yml` | create | ops | db with healthcheck; data-service internal only, waits for a healthy db; web publishes the only port; named volumes; builds use the repo root as context | `docker compose config` |
| `ops/init/01-init.sql` | create | ops | `CREATE EXTENSION` lines only; runs on the first start of an empty volume | `docker compose up -d db` |
| `.github/workflows/ci.yml` | create | ci | pnpm/action-setup, frozen install at root, `pnpm -r test`, web typecheck and build, `docker compose config` | CI run green |
| `web/package.json` | create | web-routes | next, react; scripts dev, build, start, typecheck, test, test:e2e | `pnpm --filter web build` |
| `web/tsconfig.json` | create | web-routes | strict; `@/*` maps to `src/*` | `pnpm --filter web typecheck` |
| `web/next.config.ts` | create | web-routes | `output: 'standalone'`; `outputFileTracingRoot` at the workspace root; exclude .data and .git from tracing | `pnpm --filter web build` |
| `web/src/app/layout.tsx` | create | web-routes | Root layout | `pnpm --filter web build` |
| `web/src/app/page.tsx` | create | web-routes | Server component: calls data-service, shows status and one list | `pnpm --filter web test:e2e` |
| `web/src/app/api/health/route.ts` | create | web-routes | GET: web ok and data-service reachable | `curl -fsS http://localhost:3000/api/health` |
| `web/src/lib/data-service.ts` | create | web-lib | Server-only client: base URL and API key from env, timeout, typed responses | `pnpm --filter web test` |
| `web/src/lib/data-service.test.ts` | create | web-tests | Mocked fetch: sends the key, maps errors | `pnpm --filter web test` |
| `web/src/proxy.ts` | create | web-proxy | Host allow-list and auth gate | `curl -s -o /dev/null -w "%{http_code}" -H "Host: other.example" http://localhost:3000/` is refused |
| `web/vitest.config.ts` | create | web-tests | Environment, path aliases, isolated data dir | `pnpm --filter web test` |
| `web/playwright.config.ts` | create | web-tests | webServer starts the built app; baseURL | `pnpm --filter web test:e2e` |
| `web/tests/smoke.spec.ts` | create | web-tests | Home page renders data that came from data-service | `pnpm --filter web test:e2e` |
| `web/Dockerfile` | create | ops | deps, build, runtime stages; corepack pnpm at the packageManager version; copies standalone, .next/static, public; non-root user | `docker compose build web` |
| `services/data-service/package.json` | create | svc-data | `"type": "module"`; serve and test scripts; pg dependency | `pnpm --filter @app/data-service test` |
| `services/data-service/src/server.mjs` | create | svc-data | node:http server: /health and one endpoint; API key required unless bound to loopback | `pnpm --filter @app/data-service test` |
| `services/data-service/src/db.mjs` | create | svc-data | pg Pool from `DATA_DB_URL`; applies migrations at start | `pnpm --filter @app/data-service test` |
| `services/data-service/src/migrations/001_init.sql` | create | svc-data | Service tables | `pnpm --filter @app/data-service test` |
| `services/data-service/test/server.test.mjs` | create | svc-tests | Starts on port 0: 200 on /health, 401 without key, round trip against a throwaway database | `pnpm --filter @app/data-service test` |
| `services/data-service/Dockerfile` | create | ops | node slim, prod deps only, non-root user, HEALTHCHECK on /health | `docker compose build data-service` |

## Shared contracts (every file)
| Path | Purpose | Consumed by |
|---|---|---|
| `pnpm-workspace.yaml` | Workspace members, allowBuilds, overrides | every package, every Dockerfile, CI |
| `.env.example` | Variable names for compose and all services | ops, web-lib, every service |
| `docker-compose.yml` | Service names, internal URLs (`http://data-service:<port>`), published ports, volumes, profiles | web-lib (service URL), services, ops |
| `services/data-service/README.md` | The service's HTTP contract: endpoints, auth header, env, port | web-lib, other services |
| `services/data-service/src/migrations/001_init.sql` | Service schema | svc-data, svc-tests |
| `web/src/lib/data-service.ts` | Typed client side of the service contract | web-routes |
| `web/openapi.json` | Web API contract, checked against route handlers in CI (for example an `api:parity` script) | external clients, CLIs, tests |
| `web/src/app/globals.css` | Design tokens | web-components, web-routes |
| `web/messages/en.json` | Source i18n keys; all locales must match | web-components, web-routes, localization |

## Repeated-file patterns
| Pattern id | Glob | One instance contains | Example instances |
|---|---|---|---|
| service | `services/*/` | package.json, src/, test/, Dockerfile, README.md, plus a compose entry | `data-service`, `auth-service`, `worker` |
| route-handler | `web/src/app/api/**/route.ts` | Role check whose result is read, zod input, JSON response, openapi.json entry, a test for the auth gate and happy path | `api/tasks/route.ts`, `api/tasks/[id]/route.ts` |
| panel | `web/src/components/panels/*-panel.tsx` | One dashboard panel fed by its route; no mock data | `activity-feed-panel.tsx`, `audit-trail-panel.tsx` |
| service-migration | `services/*/src/migrations/*.sql` | One schema change | `001_init.sql` |
| service-test | `services/*/test/*.test.mjs` | node:test suite for one module or route | `auth.test.mjs`, `server.test.mjs` |
| unit-test | `web/src/**/__tests__/*.test.ts` | Vitest suite for one lib module or route | `src/lib/__tests__/auth.test.ts` |
| e2e-spec | `web/tests/*.spec.ts` | Playwright spec for one feature | `items-crud.spec.ts`, `auth-guards.spec.ts` |
| locale | `web/messages/<lang>.json` | Full key set for one language | `ar.json`, `ja.json` |
| compose-overlay | `ops/*/compose.*.yml` | Optional stack, profile-gated, loopback-only ports | `ops/monitoring/compose.monitoring.yml` |
| ci-guard | `web/scripts/check-*.mjs` | One repo rule enforced in CI against a committed baseline | `check-route-auth.mjs`, `check-no-mocks.mjs` |

## Ignore
```text
node_modules/
**/node_modules/
.pnpm-store/
**/.next/
**/dist/
**/build/
**/out/
**/coverage/
**/test-results/
**/playwright-report/
**/.data/
**/*.db
**/*.db-shm
**/*.db-wal
**/*.sqlite
**/*.tsbuildinfo
services/*/target/
.env
**/.env
**/.env.*
!**/.env.example
*.log
```

## Verify commands
| Command | Proves |
|---|---|
| `pnpm install --frozen-lockfile` (repo root) | Lockfile matches every package.json and all build scripts are approved |
| `pnpm -r test` | Every package's tests pass |
| `pnpm --filter web typecheck && pnpm --filter web build` | The web app compiles and emits .next/standalone |
| `pnpm --filter web test:e2e` | Playwright smoke passes through the real server |
| `docker compose config` | Compose files, overlays and env interpolation are valid |
| `docker compose up -d --build && docker compose ps` | Images build and every healthcheck turns healthy |
| `curl -fsS http://localhost:3000/api/health` | The web container reaches data-service over the compose network |
| `docker compose down && docker compose up -d`, then re-read the test row | Data survives a restart in the named volume |

## Suggested owner rules (last match wins)
Rules are checked in order and the last match wins. A leading `/` anchors a rule to the repo root (`/package.json` is only the root manifest; `package.json` would match every one). A rule nested inside an earlier rule needs nothing; mark a rule `override` only where it partly overlaps an earlier rule of another owner, which `validate` otherwise reports.

| Order | Pattern | Owner | Note |
|---|---|---|---|
| 1 | `**` | cto-architect | Fallback |
| 2 | `web/**` | frontend-engineer | |
| 3 | `web/src/app/api/**` | backend-engineer | |
| 4 | `web/src/lib/**` | backend-engineer | |
| 5 | `web/messages/**` | localization-specialist | |
| 6 | `web/tests/**` | test-engineer | |
| 7 | `web/**/*.test.ts` | test-engineer | override: colocated unit tests |
| 8 | `services/**` | backend-engineer | |
| 9 | `services/*/src/migrations/**` | database-engineer | |
| 10 | `services/*/test/**` | test-engineer | |
| 11 | `/docker-compose.yml` | devops-engineer | |
| 12 | `ops/**` | devops-engineer | |
| 13 | `**/Dockerfile` | devops-engineer | override: every Dockerfile |
| 14 | `.github/**` | devops-engineer | |
| 15 | `/package.json` | build-integrator | Root manifest only |
| 16 | `/pnpm-lock.yaml` | build-integrator | |
| 17 | `web/package.json` | build-integrator | |
| 18 | `web/next.config.ts` | build-integrator | |
| 19 | `/pnpm-workspace.yaml` | cto-architect | override: install settings for every package |
| 20 | `/.env.example` | cto-architect | override |
| 21 | `services/data-service/README.md` | backend-lead | override: service contract |
| 22 | `services/data-service/src/migrations/001_init.sql` | backend-lead | override: P1 schema contract |
| 23 | `web/src/lib/data-service.ts` | backend-lead | override: client side of the contract |
| 24 | `web/openapi.json` | backend-lead | override |
| 25 | `web/src/proxy.ts` | backend-lead | override: auth gate |
| 26 | `web/src/app/globals.css` | frontend-lead | override: design tokens |
| 27 | `web/messages/en.json` | frontend-lead | override: source keys |

## Pitfalls
- Install only from the repo root. A `pnpm install` inside a package creates a second lockfile that drifts from the root one, and a Dockerfile that copies it builds something CI never tested. Build images from the repo root context with the root lockfile.
- pnpm 11+ blocks dependency build scripts by default (`strictDepBuilds: true`, install fails with `ERR_PNPM_IGNORED_BUILDS`). List native packages (better-sqlite3, node-pty, sharp, esbuild) in `allowBuilds`. `onlyBuiltDependencies` was removed in v11.
- Any image that installs outside the workspace needs the same allowBuilds and overrides. If you keep a separate copy for image builds, add a test that fails when it drifts from the root file.
- Pin one pnpm version everywhere: `packageManager`, `corepack prepare` in every Dockerfile, and CI.
- Next standalone output in a monorepo: set `outputFileTracingRoot` to the workspace root or files outside the app are left out; copy `public/` and `.next/static` into the standalone folder; exclude `.data` and `.git` from tracing.
- `NEXT_PUBLIC_*` values are inlined at build time. Pass them as Docker build args, not runtime env.
- Next 16: `proxy.ts` replaces `middleware.ts`, route `params` is a Promise (`await params`), and Turbopack is the default (pass `--webpack` if you still depend on a custom webpack config).
- Check the returned result of every auth helper in every route handler. Awaiting a role check without reading what it returns is an auth bypass.
- Native modules (better-sqlite3, node-pty) are compiled for one Node ABI. Keep the build and runtime image on the same Node version, install build tools in the deps stage, and rebuild after a Node change.
- A service that listens on 0.0.0.0 inside the compose network must require an API key. Publish only the web port; bind optional admin ports to 127.0.0.1. Use `${VAR:?message}` so a missing secret stops compose.
- `docker-entrypoint-initdb.d` scripts run only when the volume is empty. Schema changes belong in the service's migrations, not in ops/init.
- Services without `profiles` always start. Put optional services behind a profile and check merged overlays with `docker compose config`.
- GitHub reads workflows only from the root `.github/workflows/`; a copy under a package never runs.
- Tests never touch the live data directory or database. Give each Vitest worker its own data dir, and make the database getter refuse the checkout's real database when it runs under Vitest.
- If the web app keeps its SQLite migrations in one typed array (for example `src/lib/migrations.ts`), append with the next free id, never reuse one, and guard `ALTER TABLE` with `PRAGMA table_info`.
- Every UI key must exist in every locale file, or next-intl throws `MISSING_MESSAGE` at runtime.
- On a Windows checkout with CRLF, a shebang in an `.mjs` file that Vitest imports breaks the import; keep shebangs out of imported scripts and set `.gitattributes`.
