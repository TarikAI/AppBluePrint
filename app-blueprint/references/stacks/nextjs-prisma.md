# Preset: Next.js App Router + Prisma + Tailwind + Vitest on Vercel

- **id:** nextjs-prisma
- **Use when:** A server-rendered web app with pages, an admin area, server actions and a few route handlers in one Next.js project, using Prisma for the database and deployed to Vercel.
- **Grounded in:** a production Next.js App Router + Prisma app on Vercel and the official docs listed below, checked 2026-11-05

## Stack (re-check these docs before use)
| Part | Choice | Version checked | Docs to re-check |
|---|---|---|---|
| Runtime | Node.js | Vercel default is 26.x; 24.x and 22.x also offered. Next 16 needs Node 20.9+ | https://vercel.com/docs/functions/runtimes/node-js/node-js-versions |
| Framework | Next.js App Router, Server Actions | npm latest 16.4.5 (Active LTS); 15.5.29 is the patched 15.x (Maintenance LTS) from the 2026-11-03 security release | https://nextjs.org/docs/app/getting-started/project-structure, https://nextjs.org/docs/app/guides/upgrading/version-16, https://nextjs.org/blog/upcoming-nextjs-security-release-november-2026 |
| UI | React + react-dom | npm latest 19.3.1 | https://react.dev/versions |
| Language | TypeScript, strict | npm latest 7.0.2 | verify (https://www.typescriptlang.org/docs/, not fetched) |
| ORM | Prisma (`prisma` CLI + `@prisma/client`) | npm: `@prisma/client` latest 7.10.1; `prisma` CLI `latest` tag is 8.0.0-rc.30, `prev` 7.10.1. Prisma 6 security patches end 2026-11-19 | https://www.prisma.io/docs/orm/more/upgrade-guides/upgrading-versions/upgrading-to-prisma-7 |
| Database | PostgreSQL in production; a SQLite file is an option for local dev only | verify | https://www.prisma.io/docs/orm/more/upgrade-guides/upgrading-versions/upgrading-to-prisma-7 |
| Validation | zod | verify | verify (https://zod.dev, not fetched) |
| Styling | Tailwind CSS v4 via `@tailwindcss/postcss` | docs v4.3; npm latest 4.3.4 | https://tailwindcss.com/docs/installation/framework-guides/nextjs |
| Unit tests | Vitest, node environment, temp database | npm latest 5.1.0 (needs Node 22.12+, Vite 6.4+) | https://vitest.dev/guide/, https://nextjs.org/docs/app/guides/testing/vitest |
| Lint | ESLint flat config + eslint-config-next | npm latest eslint 10.14.0; keep eslint-config-next on the same version as Next. `next lint` is removed in Next 16 | https://nextjs.org/docs/app/guides/upgrading/version-16 |
| Hosting | Vercel; scheduled jobs via vercel.json `crons` (or a scheduled GitHub Actions workflow) | platform feature; plan limits apply | https://vercel.com/docs/cron-jobs |
| Server-only guard | `server-only` package | verify | verify (not fetched) |

## Layout
```text
<app>/
  app/                        routes only: pages, layouts, route handlers, server actions
    layout.tsx                root layout (html/body, fonts, metadata base)
    page.tsx                  home
    globals.css               @import "tailwindcss" and @theme tokens
    error.tsx                 route error boundary ("use client")
    global-error.tsx          root error boundary
    not-found.tsx
    <section>/[slug]/page.tsx public dynamic pages (params is a Promise)
    admin/                    staff area: layout.tsx guard, login/, one folder per screen, actions.ts
    api/<name>/route.ts       route handlers: health, cron, webhooks, agent API
    sitemap.ts, robots.ts, manifest.ts   metadata routes
  components/                 shared React components; "use client" only where state or effects are needed
  lib/                        server logic and helpers
    db.ts                     the one PrismaClient
    env.ts                    zod-validated env, import "server-only"
    auth.ts                   sessions and role checks
    generated/prisma/         Prisma 7 client output (example path; Prisma 7 requires an explicit output)
  prisma/
    schema.prisma             data model
    migrations/               migration history (`prisma migrate dev`)
    seed.ts                   dev seed
  prisma.config.ts            Prisma 7+: schema path, migrations, datasource url, seed
  proxy.ts                    Next 16 request proxy (middleware.ts on Next 15)
  instrumentation.ts          runs once at server start; loads Node-only checks behind NEXT_RUNTIME
  instrumentation-node.ts     imports lib/env so bad config fails at boot
  public/                     static assets
  scripts/                    CLI tasks run with tsx (admin user, data export/import)
  tests/                      Vitest suites
    setup/                    temp database, next/* mocks
    stubs/                    server-only stub
    helpers/
  next.config.ts              headers and CSP, images, serverActions limits
  vitest.config.ts
  eslint.config.mjs
  postcss.config.mjs
  vercel.json                 crons
  .github/workflows/          CI: typecheck, lint, test, build; optional cron workflow
```

## Modules and layers
| Module id | Path | Layer | Depends on |
|---|---|---|---|
| routes-public | `app/**` except `app/admin/**` and `app/api/**` | ui route | components, domain |
| routes-admin | `app/admin/**` | ui route and server actions | auth, domain, db, components |
| route-handlers | `app/api/**/route.ts` | server entry | auth, domain, db |
| components | `components/**`, `app/globals.css` | ui | client-safe helpers in lib only |
| auth | `lib/auth.ts`, `proxy.ts` | server | db, env |
| domain | `lib/**` except db, env, auth | server | db, env |
| db | `lib/db.ts`, `prisma/**`, `prisma.config.ts` | data | env |
| env | `lib/env.ts`, `instrumentation.ts`, `instrumentation-node.ts`, `.env.example` | config | none |
| tests | `tests/**`, `vitest.config.ts` | test | all modules |
| tooling | `package.json`, `tsconfig.json`, `next.config.ts`, `eslint.config.mjs`, `postcss.config.mjs`, `scripts/**`, `.github/**`, `vercel.json` | tooling | none |

## Phase 1 walking skeleton (every file)
| Path | Action | Module | Purpose | Verify |
|---|---|---|---|---|
| `package.json` | create | tooling | Scripts dev, build, start, typecheck, lint, test, db:*; postinstall `prisma generate` | `npm ci` |
| `tsconfig.json` | create | tooling | strict, bundler resolution, `@/*` alias | `npm run typecheck` |
| `next.config.ts` | create | tooling | Security headers and CSP, `poweredByHeader: false`, `images.remotePatterns: []` | `npm run build` |
| `postcss.config.mjs` | create | tooling | `@tailwindcss/postcss` plugin | `npm run build` |
| `eslint.config.mjs` | create | tooling | Flat config extending eslint-config-next | `npm run lint` |
| `app/globals.css` | create | components | `@import "tailwindcss";` plus `@theme` tokens | `npm run build` |
| `app/layout.tsx` | create | routes-public | Root layout, fonts, metadata | `npm run build` |
| `app/page.tsx` | create | routes-public | Server component listing items from lib/queries | `npm start`, open / |
| `app/admin/layout.tsx` | create | routes-admin | Server-side guard: getAdminUser() or redirect to /admin/login | `curl -sI http://localhost:3000/admin` redirects to /admin/login |
| `app/admin/login/page.tsx` | create | routes-admin | Sign-in form posting to a server action | `npm run build` |
| `app/admin/page.tsx` | create | routes-admin | Create-item form and item list | `npm run build` |
| `app/admin/actions.ts` | create | routes-admin | `"use server"`: signIn, createItem; re-checks role, zod-parses FormData, revalidatePath | `npm test` |
| `app/api/health/route.ts` | create | route-handlers | GET: ok plus a `SELECT 1` | `curl -fsS http://localhost:3000/api/health` |
| `proxy.ts` | create | auth | Cheap cookie check for /admin/* except login; the real check stays in layout and actions (middleware.ts on Next 15) | `curl -sI http://localhost:3000/admin` without a cookie redirects; `/admin/login` does not |
| `instrumentation.ts` | create | env | `register()` imports instrumentation-node only when `NEXT_RUNTIME === "nodejs"` | `npm run build && npm start` |
| `instrumentation-node.ts` | create | env | Imports lib/env so a missing secret fails at boot | `npm start` with AUTH_SECRET unset: requests fail and the log names the variable |
| `lib/env.ts` | create | env | zod env schema, `import "server-only"`, strict in production | `npm test` |
| `lib/db.ts` | create | db | Single PrismaClient (global in dev); driver adapter on Prisma 7 | `npm test` |
| `lib/auth.ts` | create | auth | Password check, signed session cookie, getAdminUser() | `npm test` |
| `lib/queries.ts` | create | domain | Read functions used by pages | `npm test` |
| `prisma/schema.prisma` | create | db | generator, datasource (production provider), User, Session, Item | `npx prisma validate` |
| `prisma.config.ts` | create | db | Prisma 7+: schema path, migrations path, datasource url, seed command | `npx prisma validate` |
| `prisma/migrations/<timestamp>_init/migration.sql` | create | db | First migration from `prisma migrate dev --name init`, generated for the production provider | `npx prisma migrate deploy` on an empty database |
| `prisma/seed.ts` | create | db | Idempotent dev seed (upserts) | `npx prisma db seed` |
| `.env.example` | create | env | DATABASE_URL, AUTH_SECRET, NEXT_PUBLIC_SITE_URL, CRON_SECRET; no real values | `npm run build` |
| `vitest.config.ts` | create | tests | Sets a temp DATABASE_URL in the main process before workers start; globalSetup, setupFiles, `server-only` alias, `fileParallelism: false` | `npm test` |
| `tests/setup/global-db.ts` | create | tests | Creates the schema in the throwaway database and removes it after; refuses anything that is not a test database | `npm test` |
| `tests/setup/next-mocks.ts` | create | tests | Stand-ins for next/headers, next/cache, next/navigation (redirect throws) | `npm test` |
| `tests/stubs/server-only.ts` | create | tests | Empty module behind the `server-only` alias | `npm test` |
| `tests/admin-actions.test.ts` | create | tests | createItem rejects without a session and persists with an admin session | `npm test` |
| `.github/workflows/ci.yml` | create | tooling | npm ci, typecheck, lint, test, `prisma migrate deploy` on a throwaway database, build | CI run green |
| `.gitignore` | create | tooling | .next*, env files, local databases, generated client | `git status` after a build |

## Shared contracts (every file)
| Path | Purpose | Consumed by |
|---|---|---|
| `prisma/schema.prisma` | The data model | db, domain, auth, routes-admin, route-handlers, tests, seed |
| `prisma/migrations/<timestamp>_init/migration.sql` | Start of the production schema history | deploy, CI, tests/setup/global-db.ts |
| `prisma.config.ts` | Where the CLI finds schema, migrations, URL and seed (Prisma 7+) | db, CI, tests |
| `lib/generated/prisma/` | Generated client and model types (Prisma 7). Regenerate, never hand-edit | all server code |
| `lib/db.ts` | The only database client | domain, auth, route-handlers, routes-admin, tests |
| `lib/env.ts` | Validated environment | all server code, instrumentation-node.ts |
| `lib/auth.ts` | Session and role checks | proxy.ts, app/admin/layout.tsx, every server action, route handlers |
| `.env.example` | Variable names; only `NEXT_PUBLIC_*` reach the browser | every owner, devops-engineer |
| `app/globals.css` | Tailwind import and design tokens | components, all routes |

## Repeated-file patterns
| Pattern id | Glob | One instance contains | Example instances |
|---|---|---|---|
| public-page | `app/<section>/**/page.tsx` | Server component: `await params`, query via lib, generateMetadata, notFound() | `app/items/[slug]/page.tsx`, `app/blog/[slug]/page.tsx` |
| admin-screen | `app/admin/<entity>/page.tsx` | Server-rendered list and forms posting to server actions; guarded by the admin layout | `app/admin/items/page.tsx`, `app/admin/posts/page.tsx` |
| server-actions | `app/**/actions.ts`, `app/*-actions.ts` | `"use server"`; auth and role re-check, zod parse, write, audit, revalidate or redirect | `app/admin/actions.ts`, `app/contact/actions.ts`, `app/auth-actions.ts` |
| route-handler | `app/api/<name>/route.ts` | Exported GET/POST; auth (session, cron secret or agent key), zod, rate limit, JSON response | `app/api/cron/route.ts`, `app/api/items/route.ts` |
| segment-ui | `app/**/error.tsx`, `app/**/loading.tsx`, `app/**/not-found.tsx` | Boundary UI for one segment | `app/admin/error.tsx` |
| metadata-route | `app/sitemap.ts`, `app/robots.ts`, `app/manifest.ts`, `app/*.xml/route.ts` | Generated SEO file | `app/feed.xml/route.ts` |
| component | `components/*.tsx` | One component; server by default | `item-card.tsx`, `contact-form.tsx` |
| lib-module | `lib/*.ts` | Server logic for one domain; `import "server-only"` when it touches db or secrets | `lib/items.ts`, `lib/billing.ts` |
| agent-task | `lib/agents/*.ts` | One agent task or provider: prompt, call, budget check, drafts queued for review | `lib/agents/summarize.ts`, `lib/agents/draft-reply.ts` |
| migration | `prisma/migrations/*/migration.sql` | One generated migration | `<timestamp>_add_items/migration.sql` |
| test | `tests/*.test.ts` | Vitest suite for one module or route; DB tests assert the temp database first | `tests/admin-actions.test.ts`, `tests/api-items-route.test.ts` |
| script | `scripts/*.ts` | One tsx CLI task | `scripts/create-admin.ts`, `scripts/export-data.ts` |

## Ignore
```text
node_modules/
.next/
.next-*/
out/
build/
coverage/
output/
.vercel/
next-env.d.ts
*.tsbuildinfo
lib/generated/prisma/
prisma/*.db
prisma/*.db-journal
.env
.env*.local
.env.production
*.log
```

## Verify commands
| Command | Proves |
|---|---|
| `npm ci` | Install works and postinstall generates the Prisma client |
| `npx prisma validate` | Schema (and prisma.config.ts on Prisma 7) parse |
| `npx prisma migrate deploy` (throwaway database) | Migration history builds the schema from empty |
| `npm run typecheck` | Strict TypeScript passes |
| `npm run lint` | ESLint passes with zero warnings |
| `npm test` | Vitest suites pass against the temp database with next/* mocks |
| `npm run build` | Production build passes; server/client boundary errors surface here |
| `npm start` then `curl -fsS http://localhost:3000/api/health` | Boot-time env check passes and the server reaches the database |
| `npx prisma db seed` | Seed runs and can run twice (Prisma 7 no longer seeds from `migrate dev`) |

## Suggested owner rules (last match wins)
Rules are checked in order and the last match wins. A leading `/` anchors a rule to the repo root (`/package.json` is only the root manifest; `package.json` would match every one). A rule nested inside an earlier rule needs nothing; mark a rule `override` only where it partly overlaps an earlier rule of another owner, which `validate` otherwise reports.

| Order | Pattern | Owner | Note |
|---|---|---|---|
| 1 | `**` | cto-architect | Fallback |
| 2 | `app/**` | frontend-engineer | |
| 3 | `components/**` | frontend-engineer | |
| 4 | `app/api/**` | backend-engineer | |
| 5 | `app/**/actions.ts` | backend-engineer | Server actions are server code |
| 6 | `app/*-actions.ts` | backend-engineer | |
| 7 | `app/admin/**` | platform-admin-engineer | override: the admin area, including its actions.ts |
| 8 | `lib/**` | backend-engineer | |
| 9 | `lib/agents/**` | ai-ml-engineer | |
| 10 | `prisma/**` | database-engineer | Migrations are generated from the committed schema |
| 11 | `scripts/**` | backend-engineer | |
| 12 | `tests/**` | test-engineer | |
| 13 | `/vitest.config.ts` | test-engineer | |
| 14 | `.github/**` | devops-engineer | |
| 15 | `/vercel.json` | devops-engineer | |
| 16 | `/package.json` | build-integrator | |
| 17 | `/next.config.ts` | build-integrator | |
| 18 | `/tsconfig.json` | build-integrator | |
| 19 | `/eslint.config.mjs` | build-integrator | |
| 20 | `/postcss.config.mjs` | build-integrator | |
| 21 | `prisma/schema.prisma` | backend-lead | override: data contract |
| 22 | `/prisma.config.ts` | backend-lead | override |
| 23 | `lib/generated/prisma/**` | backend-lead | override: generated, never hand-edited |
| 24 | `lib/db.ts` | backend-lead | override |
| 25 | `lib/env.ts` | backend-lead | override |
| 26 | `lib/auth.ts` | backend-lead | override: auth contract |
| 27 | `/proxy.ts` | backend-lead | override |
| 28 | `app/globals.css` | frontend-lead | override: design tokens |
| 29 | `.env.example` | cto-architect | override |

## Pitfalls
- Anything a `"use client"` file imports ships to the browser. Put `import "server-only"` in db, env, auth and secret-handling modules; only `NEXT_PUBLIC_*` variables reach the client, and they are inlined at build time.
- Server actions are public POST endpoints. Re-check the session and role inside every action; the proxy and the admin layout are not enough. Validate FormData with zod.
- Next 16 renames `middleware.ts` to `proxy.ts`; proxy runs on the Node.js runtime only. Keep edge middleware as middleware if you need edge. Edge middleware cannot import DB code, so it can only run a cheap edge-safe check such as verifying a signed cookie.
- Next 16 removes sync access to `params`, `searchParams`, `cookies()` and `headers()`; `revalidateTag` needs a second argument; `next lint` and the `eslint` key in next.config are gone (remove `eslint.ignoreDuringBuilds` when upgrading); Turbopack is the default and a custom webpack config fails the build unless you pass `--webpack`; parallel route slots need `default.js`.
- Stay on a patched release line. The November 2026 security release fixed 15.x in 15.5.29 and 16.x in 16.4.5; check the Next.js security notices before pinning a version.
- Use one PrismaClient (global singleton in dev); hot reload otherwise opens new connections. On serverless Postgres use the pooled URL at runtime.
- Prisma 7: the `prisma-client` generator needs an `output` path, the datasource URL moves to prisma.config.ts, a driver adapter is required, `.env` is not loaded automatically, `migrate dev` no longer runs generate or seed, and the package is ESM. npm `prisma@latest` currently installs an 8.0 release candidate: pin `prisma` and `@prisma/client` to the same major.
- `prisma db push` keeps no migration history, so changes cannot be reviewed or replayed. Use `prisma migrate` for anything with production data.
- Migrations are provider-specific: migrations generated against SQLite will not run on Postgres. Generate and test against the production provider, or stay on `db push` with a provider-neutral schema.
- SQLite files do not persist on Vercel; production there needs Postgres.
- Tests must set DATABASE_URL before anything imports lib/db, and must refuse to touch prisma/dev.db or production (for example, assert the database path is inside the OS temp dir). Run DB test files one at a time when they share a database.
- Vitest does not support async Server Components; unit-test lib and actions, and cover async pages with E2E tests.
- A wildcard in `images.remotePatterns` turns `/_next/image` into an open fetch proxy.
- Vercel cron calls are GET requests in UTC; check `Authorization: Bearer $CRON_SECRET`. Cron Jobs are now a legacy feature moving to Schedules, and plan limits apply (a scheduled GitHub Actions workflow that calls the cron route is an alternative).
- After a write, call revalidatePath or revalidateTag, or pages keep serving stale data.
