# Load & smoke testing

Two layers, sharing one seeded workload (the fully offline example
[`examples/03-archive-transform-store`](../examples/03-archive-transform-store/)
— no credentials, no LLM, no browser, no agents):

| Asset | What | Used by |
|-------|------|---------|
| `ci/smoke.py` | end-to-end correctness: login → tenant → grants → workflow → run → poll → artifact | CI `backend-integration` job; run it locally after touching auth/workflows/runs/objects |
| `ci/seed.py` | seeds a tenant + workflow and prints `export` lines for k6 | `loadtest.yml`, manual load tests |
| `k6/runs.js` | sustained load: each VU starts a run and polls it to terminal | manual + the `loadtest` workflow (workflow_dispatch) |

## Smoke (correctness, seconds)

```bash
# 1. boot an API with known superuser creds (SQLite, fake LLM, no browser):
cd aakaar
AAKAAR_JWT_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))') \
AAKAAR_SUPERUSER_EMAIL=smoke@example.com AAKAAR_SUPERUSER_PASSWORD=smoke-password-1 \
AAKAAR_BROWSER_POOL=none AAKAAR_SCHEDULER_ENABLED=false \
.venv/bin/uvicorn aakaar.api.main:app --port 8000 &

# 2. run the smoke (httpx is a backend dependency, so the venv has it):
AAKAAR_SUPERUSER_EMAIL=smoke@example.com AAKAAR_SUPERUSER_PASSWORD=smoke-password-1 \
.venv/bin/python ../loadtest/ci/smoke.py
```

Exit 0 + `SMOKE PASS` or a named failing step. Each invocation creates a
fresh `smoke-<rand>` tenant — throwaway DBs are recommended.

## Load (k6)

Install [k6](https://k6.io) (`brew install k6` / the grafana apt repo).

```bash
# seed a tenant + workflow on the target, exporting AAKAAR_EMAIL/PASSWORD/WORKFLOW_ID:
eval "$(AAKAAR_API=http://127.0.0.1:8000 \
        AAKAAR_SUPERUSER_EMAIL=... AAKAAR_SUPERUSER_PASSWORD=... \
        .venv/bin/python loadtest/ci/seed.py)"

k6 run loadtest/k6/runs.js \
  -e AAKAAR_API=http://127.0.0.1:8000 \
  -e AAKAAR_EMAIL=$AAKAAR_EMAIL -e AAKAAR_PASSWORD=$AAKAAR_PASSWORD \
  -e AAKAAR_WORKFLOW_ID=$AAKAAR_WORKFLOW_ID \
  -e VUS=5 -e DURATION=1m
```

Knobs (all `-e`): `VUS` (default 5), `DURATION` (default `1m`),
`POLL_TIMEOUT_S` (default 60 — per-run polling budget).

### Rate limiter caveat (read before interpreting results)

The API ships an in-process token-bucket limiter **keyed by client IP**
(default 240 req/min; `aakaar/aakaar/core/middleware/rate_limit.py`). All k6
VUs come from one IP, so at ~5 VUs polling every second you will hit 429s
that say nothing about capacity. On the **test target only**, either raise
`AAKAAR_RATE_LIMIT_PER_MIN` (e.g. `100000`) or set
`AAKAAR_RATE_LIMIT_ENABLED=false`. Never load-test a production instance.

### Thresholds (the test fails if breached)

Encoded in `k6/runs.js`:

| Threshold | Value | Rationale |
|-----------|-------|-----------|
| `http_req_failed` | rate < 1% | transport errors / 5xx are correctness, not load |
| `http_req_duration{endpoint:start_run}` | p95 < 2s | run creation commits a row + schedules in-process; it must stay interactive |
| `http_req_duration{endpoint:poll_run}` | p95 < 1s | the run console polls this |
| `run_succeeded` | rate > 99% | the seeded workflow is deterministic — failures are real |
| `run_duration` | p95 < 30s | start→terminal wall clock for the 4-node offline pipeline; sustained growth = executor saturation |

Baseline expectation on a dev laptop (SQLite, single process): 5 VUs /
1 minute completes ~50–100 runs with all thresholds green. The platform is a
single-process design — the sustainable rate is bounded by node work and
SQLite writes, not HTTP. If `run_duration` climbs while HTTP stays fast, the
executor is saturated: scale CPU/IO, don't add uvicorn workers (the
in-process orchestrator is deliberately single-worker).

## CI

- `backend-integration` (in [`ci.yml`](../.github/workflows/ci.yml)) boots
  the API on SQLite and runs `ci/smoke.py` on every PR/push.
- [`loadtest.yml`](../.github/workflows/loadtest.yml) is **manual**
  (workflow_dispatch): boots the API the same way, seeds via `ci/seed.py`,
  and runs the k6 scenario with dispatch-time `vus`/`duration` inputs.
  Runner-grade numbers are for trend comparison between runs, not absolute
  capacity claims.
