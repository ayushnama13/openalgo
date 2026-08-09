---
paths:
  - "frontend/**"
---

# Frontend (React / TypeScript) rules

## Build

`frontend/dist/` is in `.gitignore` so contributors cannot commit half-built
artifacts — but on `main` it **is tracked**. The `commit-dist` job in
`.github/workflows/ci.yml` force-adds (`git add -f`) the freshly built dist
back to `main` after every successful push.

- **Production servers and backend-only contributors need no Node.js.** A
  plain `git pull` from `main` brings the latest UI. This is the canonical
  upgrade path.
- **You** run `cd frontend && npm install && npm run build` (or `npm run
  dev`) locally, since the local `.gitignore` will not track your output.
  Build only — tests run in CI.
- **Feature branches** CI has not built may carry stale or missing `dist/`.
  Build locally or rebase onto recent `main`.

Config lives in `.env` (copy from `.sample.env`, repo root); `VALID_BROKERS`
gates which broker plugins load, and plugins are discovered at startup only.

## Tools registry

`frontend/src/lib/tools.ts` is the single source of truth for the `/tools`
suite — the home page derives its tool count from it. Add a new tool there
and both the `/tools` page and the home page update; do not hardcode a count
anywhere else.

## Commands

- `npm run dev` — Vite dev server
- `npm run build` — `tsc -b && vite build`
- `npm run lint` / `npm run format` / `npm run check` — Biome
- `npm run test:run` — Vitest (unit)
- `npm run e2e` — Playwright (end-to-end)

## Style

Biome (`frontend/biome.json`), functional components with hooks, PascalCase
component files, TanStack Query for server state.
