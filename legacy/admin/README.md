# legacy/admin — moved out of the core build

This directory holds the previous Admin UI (FastAPI + Vite/Vue3) that gave
a web frontend for managing per-user agent_runners, browsing memory, writing
KG triples, and running fused recall debug queries.

**It is no longer part of the supported memory service.** The repo no
longer ships any startup scripts at all — the three console-scripts below
are the entire deployment surface, and this admin UI is not one of them:

- `eidolon-memory-supervisor` — per-user agent_runner orchestration
- `eidolon-memory-discovery` — `/api/discovery/agent-routing` for external agents
- `eidolon-memory-agent`     — single agent_runner (MCP + NATS subscriber)

All admin functionality is reachable through the MCP / NATS contracts those
expose (see top-level `README.md`).

## Running it manually (if you really want to)

```bash
# 1. extras (FastAPI + uvicorn for the admin API)
uv sync --extra admin

# 2. supervisor + at least one agent_runner must be up first (see top README)

# 3. admin API
PYTHONPATH=legacy/admin/server uv run uvicorn main:app \
    --app-dir legacy/admin/server --host 127.0.0.1 --port 8010

# 4. web dev server
cd legacy/admin/web && npm install && npm run dev -- --port 5280
```

The Vite proxy still points at `127.0.0.1:8010` so the web frontend will work.
Bearer auth via `EIDOLON_MEMORY_ADMIN_TOKEN` is still honored.

## Why it was moved out

- Mixing UI lifecycle with the core service blurred the "memory is a pure
  read/write contract" boundary. The product surface is MCP + NATS; the UI is
  a debugging aid, not a production dependency.
- Multi-process bootstrap (supervisor + agent + admin api + vite) was hard
  to reason about and reload reliably; cutting it down to supervisor +
  discovery made the dev loop much simpler.
- Anything the UI did, the MCP tools and NATS subjects do directly — making
  the UI optional, not central.

No code was deleted; everything continues to work under `legacy/admin/` if
needed. Future cleanup may pin its deps explicitly or remove it entirely,
but for now it is preserved as-is for reference and ad-hoc debugging.
