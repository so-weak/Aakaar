# Contributing to Aakaar

This repo is a monorepo: a Python backend, a TypeScript SPA, a Python remote
agent, a shared capability SDK, and an MCP server. Most contributions touch
exactly one of them.

## Repository map

| Path | What | Stack |
|------|------|-------|
| `aakaar/` | API, planner, interpreter/orchestrator, capability registry, scheduler, vault, DB + Alembic migrations | Python 3.12, FastAPI, SQLAlchemy |
| `aakaar-web/` | Operator console SPA | React + TypeScript + Vite + react-query |
| `aakaar-agent/` | Workstation agent (GUI/desktop capabilities, dials out over WebSocket) | Python |
| `aakaar-broker/` | Optional stateless WebSocket rendezvous relay for agents/API with no stable address | Python |
| `aakaar-capabilities/` | Host-neutral capability SDK (`aakaar_caps`) shared by server and agent | Python |
| `aakaar-mcp/` | Registry projected as MCP tools | Python |
| `runbooks/`, `examples/`, `loadtest/` | Ops runbooks, importable example workflows, k6 load tests | — |

## Inner loop

```bash
./dev.sh        # from the repo root (macOS: opens API + web in new Terminal tabs)
```

First run bootstraps everything: creates `aakaar/.venv`, installs the backend
plus `aakaar-capabilities` editable, installs Playwright Chromium, runs
`alembic upgrade head`, and `npm install`s the frontend. Subsequent runs are
fast no-ops. API on http://localhost:8000, web on http://localhost:5173; stop
with `Ctrl+C` per tab or `./dev-stop.sh`.

Backend env lives in `aakaar/.env` (`AAKAAR_JWT_SECRET`, optional
`OPENAI_API_KEY`, `AAKAAR_SUPERUSER_EMAIL`/`_PASSWORD` for the first login).
Without an OpenAI key the app boots with fake LLM/embedding clients — fine for
everything except real planning.

Not on macOS? Run the pieces by hand — the exact commands are in the
[root README](README.md#starting-each-service-separately).

## Quality gates

CI (`.github/workflows/ci.yml`) enforces exactly these; run them locally
before pushing.

### Backend (`aakaar/`)

```bash
cd aakaar
.venv/bin/ruff check aakaar tests   # lint — ruff, line-length 100, E/F/I/B/UP/SIM
.venv/bin/mypy aakaar               # strict mode + pydantic plugin (tests not type-checked)
.venv/bin/python -m pytest -q       # full suite; warnings are errors
```

Notes that bite newcomers:

- `pytest` runs with `filterwarnings = error` — a new `DeprecationWarning`
  from *your* code fails the suite. Third-party noise is already ignored in
  `pyproject.toml`; extend that list only for warnings you genuinely cannot
  fix.
- `asyncio_mode = "auto"`: async test functions need no decorator.
- Tests must not require external services. SQLite, the local vault, the
  object store, and fake LLM clients cover everything; tests that need a
  server use FastAPI's TestClient in-process.
- Heavy/optional libraries are **lazy-imported** behind optional-dependency
  extras (see `[project.optional-dependencies]` in `aakaar/pyproject.toml`
  and the `gui` extra in `aakaar-agent/pyproject.toml`). A capability module
  must import cleanly with the extra absent and raise a clear `RuntimeError`
  naming the extra when its handler runs.

### Frontend (`aakaar-web/`)

```bash
cd aakaar-web
npm run typecheck   # tsc -b --noEmit
npm run build       # vite build — CI runs both; there is no lint script
```

### Agent (`aakaar-agent/`)

Not in CI yet (no GUI on runners), but keep `ruff check` and its `pytest`
suite green locally — reconnect tests run against an in-process WebSocket
server and need no display.

### Integration job

CI also boots the real API against SQLite (uvicorn, fake LLM, no browser
pool) and runs `loadtest/ci/smoke.py`: login → tenant → workflow → run →
poll to success → artifact fetch. If you change auth, workflows, runs, or
the object store, run it locally:

```bash
cd aakaar && .venv/bin/uvicorn aakaar.api.main:app --port 8000 &   # with .env set
python ../loadtest/ci/smoke.py
```

## Conventions

- **Read the neighboring module first and match it.** Style consistency
  beats personal preference everywhere in this repo.
- Comments explain non-obvious constraints, not what the code does.
- New capabilities: one package under `aakaar/aakaar/capabilities/<area>/<name>/`
  exposing `definition` (a `CapabilityDefinition` with strict pydantic
  schemas, `extra="forbid"`) and an async `handler`. The package walker
  registers it automatically; add a `tests/test_cap_<name>.py` suite.
- Schema/DB changes go through Alembic (`aakaar/aakaar/db/migrations/`);
  never edit an applied migration.
- Secrets discipline: never log secret values, never store them in DB
  columns — names in grants, values in the vault.

## Pull requests

- Keep PRs scoped to one concern; mechanical refactors separate from
  behavior changes.
- Every behavior change ships with tests that fail without it.
- All four CI jobs (backend, frontend, dep-scan, secret-scan) plus the
  integration job must pass; gitleaks runs over full history, so never
  commit even a throwaway credential.
- Security-sensitive findings: follow [SECURITY.md](SECURITY.md) instead of
  opening a PR/issue.
