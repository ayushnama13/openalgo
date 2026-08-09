---
paths:
  - "*.py"
  - "app.py"
  - "blueprints/**"
  - "services/**"
  - "database/**"
  - "broker/**"
  - "websocket_proxy/**"
  - "restx_api/**"
  - "sandbox/**"
  - "upgrade/**"
---

# Backend (Python / Flask) rules

## Eventlet + Gunicorn (production)

Production (Ubuntu direct and Docker) runs `gunicorn --worker-class eventlet -w 1`:

- **No `asyncio`.** Eventlet monkey-patches the stdlib and is incompatible with `asyncio.run()`, `async`/`await`, and `asyncio.get_event_loop()`. Async work must use eventlet green threads or run on a separate real OS thread — see `telegram_bot_service.py:_render_plotly_png` for the pattern.
- **Single worker (`-w 1`) is mandatory.** Flask-SocketIO state is in-process and cannot be shared across workers.
- **`threading.local()` maps to green threads**, which is why `scoped_session` works correctly under eventlet.

`uv run app.py` (dev server) uses standard threading, not eventlet. Code must
work in both. `asyncio` works fine on the dev server and **breaks in
production** — this is the single most common way a change passes locally and
fails on deploy. SQLite locking is also stricter on Windows.

## Logging

`logger = get_logger(__name__)` from `utils/logging.py` in every module. Error
logging is always `logger.exception()` — it captures the traceback and routes
it to the JSON handler. Never `import traceback` / `traceback.print_exc()` /
`traceback.format_exc()`; those bypass centralized logging. Never `print()`.

**When debugging, read `log/errors.jsonl` first.** One JSON object per line:
timestamp, logger, module, `file:line`, message, full traceback, and Flask
request context (method, path, IP) when available. Truncated to the last 1000
entries at startup.

## FD hygiene

Every DB engine/session, file, socket, WebSocket, ZMQ socket, subprocess pipe,
thread, and executor is a file descriptor, and production is a single
Gunicorn worker that never restarts — a leak accumulates until "too many open
files". Preventing one at creation is far cheaper than hunting it later:

- SQLite engines via `database.engine_factory.create_db_engine()` (`NullPool` — never `StaticPool`, see root `CLAUDE.md`)
- Every `scoped_session` registered in the `app.py` teardown, or used as `with db_session() as session:`
- HTTP via the shared `utils/httpx_client.get_httpx_client()`, always with an explicit timeout
- WebSocket adapters close before reconnect
- Subprocesses write to a log file (not `PIPE`) and are `.wait()`-reaped
- Threads and executors are shared module-level singletons, never per-call

FD leak prevention rests on five session-cleanup layers: `app.py`
`teardown_appcontext`; `traffic_logger.py` explicit `logs_session.remove()` in
a `finally`; `security_middleware.py` for the banned-IP WSGI path; and
teardown handlers in `blueprints/traffic.py` and `blueprints/security.py`.

After a change touching any of these, run the **`fd-audit`** skill before
calling it done.

## Database access

Goes through the SQLAlchemy ORM, not raw SQL.

## Schema migrations

Users upgrade with `cd upgrade && uv run migrate_all.py`, so every schema
change ships as a script in `upgrade/` registered in that file's `MIGRATIONS`
list. Applying the change from `init_db()` alone is *not* enough: seeding
functions typically only run against an empty table, so an existing
installation keeps the old schema forever and the change silently never
reaches the ~290k live deployments.

- **Idempotent, and safe to re-run.** Check whether the change is already
  present (`PRAGMA table_info`) and return quietly if so.
- **Support `--status`** to report what would change without changing it.
- **Never clobber a value the user may have customised.** Guard the update on
  the old value, so an admin who has already set their own is left alone.
- **Backfill from the data, not from a default.** A new column defaulted
  uniformly is usually wrong for existing rows; derive each row's value from
  what the row already says.
- **SQLite limits shape the approach.** It cannot alter a `CHECK` constraint
  or add a `UNIQUE` column in place: rebuild the table (see
  `migrate_sandbox_trigger_pending.py`) or add a partial unique index instead.

Test it against a *copy of a real database forced back to the old schema*,
not only a fresh one. A migration that works on an empty database and fails
on a populated one is the common failure.

## Style

Ruff (`uv run ruff check . --fix`, `uv run ruff format .`), config in
`pyproject.toml`; 4 spaces, Google-style docstrings.
