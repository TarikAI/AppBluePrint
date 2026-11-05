# Preset: Vite + React + Supabase + Capacitor on Vercel

- **id:** vite-react-supabase
- **Use when:** A browser app (SPA, optionally pre-rendered) with Supabase for Postgres, auth and storage, a few server endpoints as Vercel functions, and the same web bundle wrapped as an Android/iOS app with Capacitor.
- **Grounded in:** a production React + Vite + Supabase + Capacitor app and the official docs listed below, checked 2026-11-05

## Stack (re-check these docs before use)
| Part | Choice | Version checked | Docs to re-check |
|---|---|---|---|
| Runtime | Node.js | Vercel default is now 26.x; 24.x and 22.x also offered; Node 20 deprecated 2026-10-01 | https://vercel.com/docs/functions/runtimes/node-js/node-js-versions |
| Build tool | Vite | npm latest 8.3.6 (builds with Rolldown, needs Node 20.19+ or 22.12+) | https://vite.dev/guide/ |
| UI | React + react-dom | npm latest 19.3.1 | https://react.dev/versions |
| Routing | react-router-dom v6 (stay on v6 if you pre-render with vite-react-ssg) | npm latest react-router-dom 7.18.5 | verify (https://reactrouter.com, not fetched) |
| Pre-rendering (optional) | vite-react-ssg | npm latest 0.9.2; supports React Router v6 only | https://github.com/Daydreamer-riri/vite-react-ssg |
| Styling | Tailwind CSS v4 via `@tailwindcss/vite`, no tailwind.config.js | docs v4.3; npm latest 4.3.4 | https://tailwindcss.com/docs/installation/using-vite |
| Data + auth | Supabase (Postgres, Auth, Storage), `@supabase/supabase-js` | npm latest 2.119.0 | https://supabase.com/docs/guides/api/api-keys |
| Schema and migrations | SQL files in `supabase/migrations/`, Supabase CLI for the local stack | npm `supabase` CLI latest 3.0.0 (breaking, 2026-11-02) | https://supabase.com/docs/guides/local-development/overview, https://supabase.com/changelog/supabase-cli-v3 |
| DB types | `supabase gen types typescript` | part of the Supabase CLI | https://supabase.com/docs/guides/api/rest/generating-types |
| DB tests | pgTAP via `supabase test db`, or PGlite (`@electric-sql/pglite`) in node:test | verify | https://supabase.com/docs/guides/database/testing |
| Server endpoints | Vercel Node functions in `api/`, `@vercel/node` types | `@vercel/node` npm latest 29.0.1 | https://vercel.com/docs/functions/runtimes/node-js |
| Scheduled jobs | `crons` in vercel.json (now a legacy feature, moving to Schedules; UTC only) | platform feature; Hobby plan allows daily schedules only | https://vercel.com/docs/cron-jobs |
| Native shell | Capacitor (Android and iOS) | npm latest 8.6.0; 9.0.0-beta.0 on `next` | https://capacitorjs.com/docs/getting-started, https://capacitorjs.com/docs/getting-started/environment-setup |
| Tests | `node --test` on `tests/*.test.mjs` | Node built-in | https://nodejs.org/api/test.html |
| Language | TypeScript, two configs (src bundler, api NodeNext) | npm latest 7.0.2 | verify (https://www.typescriptlang.org/docs/, not fetched) |
| i18n (optional) | i18next + react-i18next | verify | verify (https://www.i18next.com, not fetched) |

## Layout
```text
<app>/
  index.html                 web entry HTML (Vite serves it from the root)
  index.native.html          native-shell entry HTML: SPA mount, no manifest, no service worker
  vite.config.ts             React + Tailwind plugins, dev /api bridge, `--mode native` build to dist-app/
  capacitor.config.ts        appId, appName, webDir (the native build output), androidScheme
  vercel.json                build/output dirs, function runtime, headers and CSP, SPA rewrite, crons
  tsconfig.json              client TS config for src/ (excludes api/)
  .env.example               every variable; VITE_* = public, everything else = server only
  .env.native                public API origin for the native build (committed, no secrets)
  src/                       browser code; everything here ships to users
    main.tsx                 web entry (createRoot, or ViteReactSSG when pre-rendering)
    main.native.tsx          native entry: Capacitor plugins, deep-link auth return
    App.tsx                  route table and app shell
    index.css                @import "tailwindcss" and @theme tokens
    components/              feature screens and UI, one folder per feature when it grows
    lib/                     client helpers: Supabase client, API caller, native bridge, i18n
    locales/                 one translation file per language
    admin/                   staff console (screens/, ui/, lib/) when the app has one
  api/                       Vercel functions; server only
    <area>/[...path].ts      one deployed function per area, dispatches by sub-path
    _<area>/                 handlers for one area (underscore: not deployed on its own)
    _lib/                    shared server code: auth, CORS, secret-key client, audit
    cron/                    scheduled endpoints (check CRON_SECRET)
    tsconfig.json            server TS config (NodeNext)
  supabase/
    migrations/              ordered SQL: tables, RLS policies, grants, functions
    seed.sql                 dev seed
    config.toml              local stack config from `supabase init`
    tests/database/          pgTAP tests, if you use `supabase test db`
  dev/                       dev-only tooling (Vite plugin that serves api/ during `vite dev`)
  scripts/                   build and repo checks (sitemap, locale parity, native build finish)
  tests/                     node:test suites; DB tests load migrations into PGlite
    fixtures/
  public/                    static files copied as-is (icons, manifest, sw.js, robots.txt)
  android/                   Capacitor Android project (from `npx cap add android`, then edited)
  ios/                       Capacitor iOS project (`npx cap add ios`; SPM by default in Capacitor 8)
  dist/                      web build output (generated)
  dist-app/                  native web bundle that `npx cap sync` copies (generated)
```

## Modules and layers
| Module id | Path | Layer | Depends on |
|---|---|---|---|
| web-shell | `index.html`, `src/main.tsx`, `src/App.tsx`, `tsconfig.json` | ui-entry | web-ui, client-lib |
| web-ui | `src/components/**`, `src/index.css` | ui | client-lib, locales |
| admin-ui | `src/admin/**` | ui | client-lib, api-routes (over HTTP) |
| client-lib | `src/lib/**` | client service | db-schema (through RLS), api-routes (over HTTP) |
| locales | `src/locales/**` | content | none |
| native-shell | `capacitor.config.ts`, `index.native.html`, `src/main.native.tsx`, `.env.native`, `android/**`, `ios/**` | platform | web-shell, client-lib |
| api-routes | `api/*/[...path].ts`, `api/*.ts`, `api/cron/*.ts` | server entry | api-handlers, server-lib |
| api-handlers | `api/_*/**` except `api/_lib/**` | server | server-lib |
| server-lib | `api/_lib/**`, `api/tsconfig.json` | server infra | db-schema (secret key) |
| db-schema | `supabase/**` | data | none |
| dev-tools | `dev/**`, `scripts/**`, `vite.config.ts`, `package.json` | tooling | api-routes |
| tests | `tests/**` | test | db-schema, server-lib, api-handlers, client-lib |
| deploy-config | `vercel.json`, `.env.example`, `.gitignore` | config | api-routes, web-shell |

## Phase 1 walking skeleton (every file)
| Path | Action | Module | Purpose | Verify |
|---|---|---|---|---|
| `package.json` | create | dev-tools | Scripts dev, build, build:app, test, lint; engines.node; dependencies | `npm install` |
| `tsconfig.json` | create | web-shell | Client TS config (bundler resolution, react-jsx), excludes api/ | `npx tsc --noEmit` |
| `vite.config.ts` | create | dev-tools | React and Tailwind plugins, dev api bridge, `--mode native` builds index.native.html into dist-app/ | `npm run build` |
| `index.html` | create | web-shell | Web entry HTML loading src/main.tsx | `npm run build` |
| `src/main.tsx` | create | web-shell | Web entry; plain createRoot in P1 (add vite-react-ssg only if pages need pre-rendering) | `npm run build` |
| `src/App.tsx` | create | web-shell | Routes: home, sign-in, items | `npm run build` |
| `src/index.css` | create | web-ui | `@import "tailwindcss";` plus `@theme` tokens | `npm run build` |
| `src/lib/supabase.ts` | create | client-lib | The one browser client (public key only), session and sign-in helpers | `npx tsc --noEmit` |
| `src/lib/api.ts` | create | client-lib | fetch wrapper: Bearer token, `VITE_API_BASE_URL` inside the native shell, one error shape | `npx tsc --noEmit` |
| `src/components/Items.tsx` | create | web-ui | Lists the user's items (read through RLS), adds one via POST /api/items/create | `npm run dev`, sign in, add an item |
| `api/tsconfig.json` | create | server-lib | Server TS config (NodeNext, node types) | `npx tsc --noEmit -p api/tsconfig.json` |
| `api/_lib/http.ts` | create | server-lib | CORS allow-list (web origins plus Capacitor origins), OPTIONS, JSON errors | `npm test` |
| `api/_lib/auth.ts` | create | server-lib | Reads the Bearer JWT, resolves the user with Supabase Auth, 401 otherwise | `npm test` |
| `api/_lib/supabaseAdmin.ts` | create | server-lib | Secret-key (service role) client; imported only from api/ | `npx tsc --noEmit -p api/tsconfig.json` |
| `api/health.ts` | create | api-routes | GET: status and build id, no secrets | `curl -fsS http://localhost:3000/api/health` |
| `api/items/[...path].ts` | create | api-routes | One function for /api/items/*; dispatches to api/_items handlers | `npm test` |
| `api/_items/create.ts` | create | api-handlers | Validates input, inserts a row owned by the caller, returns it | `npm test` |
| `dev/vite-plugin-api.ts` | create | dev-tools | Serves api/ handlers during `vite dev` with Vercel-like req/res shims; skips `_` paths | `npm run dev`, then curl /api/health |
| `supabase/config.toml` | create | db-schema | Local stack config from `supabase init` | `supabase start` |
| `supabase/migrations/0001_init.sql` | create | db-schema | profiles and items tables, RLS enabled, own-row policies, grants | `npm test` |
| `tests/schema.test.mjs` | create | tests | Loads migrations into PGlite with stub roles and `auth.uid()`; user A cannot read user B's items | `npm test` |
| `tests/api-items.test.mjs` | create | tests | Calls the handler with mock req/res: 401 without token, 201 with a stubbed user | `npm test` |
| `.env.example` | create | deploy-config | All variable names, public (VITE_*) and server-only split | `npm run build` with the values copied to .env.local |
| `vercel.json` | create | deploy-config | framework, build and output dirs, function runtime, SPA rewrite that excludes /api, security headers | `npx vercel build` |
| `.gitignore` | create | deploy-config | Ignores build output and env files; keeps .env.example and .env.native | `git status` after a build |
| `capacitor.config.ts` | create | native-shell | appId, appName, `webDir: 'dist-app'`, `androidScheme: 'https'` | `npx cap sync android` |
| `index.native.html` | create | native-shell | Native entry HTML: no manifest, no service worker | `npm run build:app` |
| `src/main.native.tsx` | create | native-shell | Plain SPA mount, Capacitor plugin wiring, no service worker | `npm run build:app` |
| `scripts/finish-native-build.mjs` | create | dev-tools | Renames dist-app/index.native.html to index.html for Capacitor | `npm run build:app` |
| `.env.native` | create | native-shell | `VITE_API_BASE_URL`, the public API origin (no secrets; committed) | `npm run build:app` |
| `android/` (generated tree) | create | native-shell | Output of `npx cap add android`; commit it and hand-edit only planned files | `cd android && ./gradlew assembleDebug` |

## Shared contracts (every file)
| Path | Purpose | Consumed by |
|---|---|---|
| `supabase/migrations/0001_init.sql` | Tables, RLS policies, grants: what the public key may read and write | client-lib, api-handlers, tests, later migrations |
| `src/lib/database.types.ts` | Generated by `supabase gen types typescript` (recommended). Regenerate, never hand-edit | client-lib, api-handlers |
| `api/_lib/auth.ts` | How every handler gets the caller from the Bearer token | all api-handlers, api-routes |
| `api/_lib/http.ts` | CORS allow-list and JSON error shape; must list the Capacitor origins | all api-routes, native-shell |
| `src/lib/supabase.ts` | The single browser Supabase client and auth helpers | web-ui, admin-ui, native-shell |
| `src/lib/api.ts` | Client side of the API: base URL, auth header, error shape | web-ui, admin-ui |
| `.env.example` | Variable names and which side may read them | every owner, devops-engineer |
| `.env.native` | Public API origin baked into the native bundle | native-shell, server-lib (CORS) |
| `src/index.css` | Tailwind import and `@theme` design tokens | web-ui, admin-ui |
| `src/locales/en.ts` | Source translation keys; other locales must match | web-ui, admin-ui, localization |

## Repeated-file patterns
| Pattern id | Glob | One instance contains | Example instances |
|---|---|---|---|
| migration | `supabase/migrations/[0-9][0-9][0-9][0-9]_*.sql` | One schema change: DDL, RLS enable and policies, grants, in one file | `0002_payments.sql`, `0003_admin_audit.sql` |
| api-dispatcher | `api/*/[...path].ts` | Map of sub-path to handler, CORS, 404 for unknown paths | `api/ai/[...path].ts`, `api/admin/[...path].ts` |
| api-handler | `api/_*/*.ts` (not `api/_lib`) | One endpoint: auth check, input validation, secret-key query, JSON response | `api/_ai/chat.ts`, `api/_payments/checkout.ts` |
| cron-endpoint | `api/cron/*.ts` | `Authorization: Bearer $CRON_SECRET` check, one sweep, a log line | `api/cron/purge-expired.ts` |
| webhook-endpoint | `api/payments/*webhook.ts` | Raw-body read, signature check, idempotent upsert | `api/payments/provider-webhook.ts` |
| feature-screen | `src/components/*.tsx` | One routed screen or feature component | `Settings.tsx`, `Pricing.tsx` |
| admin-screen | `src/admin/screens/screen-*.tsx` | One staff screen: reads through RLS or an endpoint, writes only through a server command | `screen-users.tsx`, `screen-plans.tsx` |
| client-module | `src/lib/*.ts` | Data access or helpers for one feature | `items.ts`, `billing.ts` |
| locale | `src/locales/<lang>.ts` | Full key set for one language | `ar.ts`, `fr.ts` |
| test | `tests/*.test.mjs` | node:test suite for one policy, route or migration | `schema.test.mjs`, `api-items.test.mjs` |
| repo-check | `scripts/check-*.mjs` | One guard run by `npm run lint` | `check-locale-parity.mjs` |

## Ignore
```text
node_modules/
dist/
dist-app/
build/
coverage/
output/
.vercel/
.env
.env.local
.env.*.local
*.log
android/.gradle/
android/build/
android/app/build/
android/local.properties
android/capacitor-cordova-android-plugins/
android/app/src/main/assets/public/
android/app/src/main/assets/capacitor.config.json
android/app/src/main/assets/capacitor.plugins.json
ios/App/build/
ios/App/App/public/
ios/App/Pods/
```

## Verify commands
| Command | Proves |
|---|---|
| `npm ci` | The lockfile installs cleanly |
| `npx tsc --noEmit && npx tsc --noEmit -p api/tsconfig.json` | Client and server code both type-check under their own configs |
| `npm test` | Migrations (in PGlite), RLS policies and handlers behave |
| `npm run build` | The web build succeeds into dist/ |
| `npm run dev` then `curl -fsS http://localhost:3000/api/health` | The dev server serves the SPA and api/ handlers together |
| `npm run build:app && npx cap sync android` | The native bundle builds and is copied into android/ |
| `cd android && ./gradlew assembleDebug` | The Android project compiles (Capacitor 8 needs Android Studio 2025.2.1+, API 24+) |
| `supabase db reset` | Migrations and seed apply from empty on the local stack (needs Docker; CLI v3 is local by default) |
| `npx vercel build` | Vercel can build the output and functions with this vercel.json |

## Suggested owner rules (last match wins)
Rules are checked in order and the last match wins. A leading `/` anchors a rule to the repo root (`/package.json` is only the root manifest; `package.json` would match every one). A rule nested inside an earlier rule needs nothing; mark a rule `override` only where it partly overlaps an earlier rule of another owner, which `validate` otherwise reports.

| Order | Pattern | Owner | Note |
|---|---|---|---|
| 1 | `**` | cto-architect | Fallback for anything unmatched |
| 2 | `src/**` | frontend-engineer | |
| 3 | `src/admin/**` | platform-admin-engineer | |
| 4 | `src/locales/**` | localization-specialist | |
| 5 | `api/**` | backend-engineer | |
| 6 | `api/_ai/**` | ai-ml-engineer | |
| 7 | `api/_payments/**` | integration-engineer | |
| 8 | `api/payments/**` | integration-engineer | Webhook entry points |
| 9 | `api/_admin/**` | platform-admin-engineer | |
| 10 | `supabase/**` | database-engineer | Later migrations stay here |
| 11 | `tests/**` | test-engineer | |
| 12 | `android/**` | mobile-engineer | |
| 13 | `ios/**` | mobile-engineer | |
| 14 | `/capacitor.config.ts` | mobile-engineer | |
| 15 | `/index.native.html` | mobile-engineer | |
| 16 | `src/main.native.tsx` | mobile-engineer | override of rule 2 |
| 17 | `/.env.native` | mobile-engineer | |
| 18 | `/vercel.json` | devops-engineer | |
| 19 | `.github/**` | devops-engineer | |
| 20 | `/package.json` | build-integrator | |
| 21 | `/vite.config.ts` | build-integrator | |
| 22 | `dev/**` | build-integrator | |
| 23 | `scripts/**` | build-integrator | |
| 24 | `api/_lib/**` | backend-lead | override: shared server contract |
| 25 | `supabase/migrations/0001_init.sql` | backend-lead | override: P1 schema contract |
| 26 | `src/lib/database.types.ts` | backend-lead | override: generated from the schema |
| 27 | `src/lib/supabase.ts` | frontend-lead | override: client contract |
| 28 | `src/lib/api.ts` | frontend-lead | override: client contract |
| 29 | `src/index.css` | frontend-lead | override: design tokens |
| 30 | `src/locales/en.ts` | frontend-lead | override: source keys |
| 31 | `.env.example` | cto-architect | override |

## Pitfalls
- Everything under `src/` ships to browsers and the app bundle. Only `VITE_*` variables reach the client; never give the Supabase secret/service-role key or a provider API key a `VITE_` name, and never import `api/_lib` from `src/`.
- The secret key (`sb_secret_...`, legacy `service_role`) bypasses RLS. Use it only in `api/`. Supabase is deprecating the legacy `anon`/`service_role` keys by the end of 2026; check which key type the project uses.
- Every new table needs RLS enabled and its policies and grants in the same migration. A table without RLS is open to anyone holding the public key.
- Writing a migration file does not apply it. If migrations are applied by hand instead of through the CLI, check the live database before relying on a table.
- Numbered migrations (`0001_...`) and the timestamps `supabase migration new` writes do not mix well. Pick one scheme per repo.
- Supabase CLI v3 (2026-11-02) is local by default, asks before hosted changes (`--yes` needed in CI), and replaces `--linked`/`--project-ref`/`--db-url` with `--remote`. Pin the CLI version in CI.
- Every non-underscore file in `api/` becomes its own Vercel function. Keep one `[...path].ts` dispatcher per area and real handlers in `api/_<area>/`.
- `api/tsconfig.json` uses NodeNext, so relative imports in `api/` need `.js` extensions (`../_lib/http.js`). Vercel functions do not support TS path mappings.
- Webhooks that verify signatures must read the raw request stream, not the parsed `req.body` helper; keep a small raw-body reader in `api/_lib`.
- `webDir` must point at the SPA build (`dist-app`), not the pre-rendered `dist/`, or the app opens to a white screen. No service worker inside the native shell. Run `npx cap sync` after every web build.
- Inside the shell the page origin is `https://localhost` (Android, androidScheme https) or `capacitor://localhost` (iOS). Relative `/api` calls hit the shell; use `VITE_API_BASE_URL` and allow-list those origins in CORS. Never reflect an arbitrary Origin.
- Google OAuth rejects in-app WebViews. Open the system browser and return through a custom-scheme deep link (AndroidManifest intent filter and `custom_url_scheme` in strings.xml).
- The CSP in vercel.json must list the Supabase `https` and `wss` hosts and any AI or payment hosts; a tighter CSP silently breaks realtime, media and analytics.
- vite-react-ssg supports React Router v6 only. Upgrading the router means replacing the pre-render step.
- Vite 8 bundles with Rolldown. When upgrading from an older major, check `build.rollupOptions`/`manualChunks` in the Vite migration notes first.
- Capacitor 8 needs Node 22+, Xcode 26+ and Android Studio 2025.2.1+.
- On the Hobby plan, crons must be daily and `engines.node` must be a major Vercel offers; either mistake fails every deploy.
- Tests never use the live Supabase project. Use PGlite with stub roles and `auth.uid()`, or the local stack.
