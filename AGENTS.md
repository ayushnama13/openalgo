# AGENTS.md — read first

Guidance for any agent working in this repository (Claude Code, OpenCode, or
otherwise). This file is a pointer, not a duplicate — read in this order:

1. **This file** — what the repo is, where things are.
2. **[`CLAUDE.md`](CLAUDE.md)** — product context, runtime invariants
   (ZeroMQ bus, multi-session login, SQLite pooling), conventions. Written for
   Claude Code but not Claude-specific in content — read it regardless of tool.
3. **[`docs/INDEX.md`](docs/INDEX.md)** — full documentation map (REST API,
   Python SDK, indicators, BDD specs, PRDs).
4. **`MEMORY.md`** (repo root, gitignored, local only) — running state
   snapshot plus dated work log. Not present on a fresh clone.

## What this repo is

OpenAlgo — self-hosted algorithmic trading platform (Flask backend, React 19
frontend). Several products sharing one broker session: Unified Broker API,
Python Strategy Host, Flow no-code builder, Options/Portfolio tools,
Charting Terminal, Scalping Terminal. Full detail in `CLAUDE.md`.

## Where things are

| Path | What it is |
|---|---|
| `CLAUDE.md` | Invariants, architecture, conventions — the canonical reference |
| `.claude/rules/` | Path-scoped detail (backend, frontend) — load only when touching matching files |
| `.claude/skills/` | Procedures (fd-audit, version-bump, broker-integration, indicator-*) — load on demand, not up front |
| `docs/` | All product/API documentation, source of truth — start at `docs/INDEX.md` |
| `MEMORY.md` | Local, gitignored, per-machine state + work log — not authoritative, not shared |
| `DISCOVERY_MAP.md` | Point-in-time audit snapshot — not maintained, don't treat as current |

## House rules

- Don't restate `CLAUDE.md` or `docs/` content here or in `MEMORY.md` — link
  to the source so there is one place to update, not several to keep in sync.
- After finishing a task with a non-obvious decision or incident, append a
  dated line to `MEMORY.md`'s Work Log — don't rewrite the sections above it.
- Schema changes need a migration script (see `CLAUDE.md`), not just a
  startup hook.
