# 06 — High error rate

The API exposes Prometheus metrics at **`GET /metrics`** (no auth, local
scrape; disable with `AAKAAR_METRICS_ENABLED=false`). The metric names, as
defined in `aakaar/aakaar/core/middleware/metrics.py`:

| Metric | Labels | Meaning |
|--------|--------|---------|
| `aakaar_http_requests_total` | `method`, `path`, `status` | request counter (path is the route template, e.g. `/runs/{run_id}`) |
| `aakaar_http_request_duration_seconds` | `method`, `path` | latency histogram (`_bucket`/`_sum`/`_count`) |
| `aakaar_runs_total` | `status` | declared for workflow-run outcomes, but **currently never incremented** — `record_run_outcome` has no caller in the orchestrator yet, so this series never appears in scrapes. Track run failures via the runs API below until that is wired. |

## First five minutes

```bash
M=http://localhost:8000/metrics

# 1. Where are the errors? 5xx by route:
curl -s $M | grep '^aakaar_http_requests_total' | grep 'status="5'

# 2. Are runs failing too, or only HTTP? (the runs API, not metrics — see table above)
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/runs | \
  python3 -c 'import json,sys,collections; print(collections.Counter(r["status"] for r in json.load(sys.stdin)))'

# 3. Is it slow rather than broken? p95-ish eyeball from the histogram:
curl -s $M | grep '^aakaar_http_request_duration_seconds_bucket' | grep -v 'le="0.0'
```

With Prometheus scraping, the equivalent rate queries:

```promql
sum by (path, status) (rate(aakaar_http_requests_total{status=~"5.."}[5m]))
histogram_quantile(0.95, sum by (le, path) (rate(aakaar_http_request_duration_seconds_bucket[5m])))
```

Counters are process-local and reset on API restart — a sudden drop to zero
usually means a restart, not a recovery. Correlate with the startup log line
(`lifespan: startup`).

## Correlate a failing route with logs

Every response carries an `X-Request-ID` header; the same id is stamped on
every log line emitted during that request. With `AAKAAR_LOG_FORMAT=json`:

```bash
# reproduce once, capture the id:
RID=$(curl -sD- -o /dev/null http://localhost:8000/runs | awk 'tolower($1)=="x-request-id:" {print $2}' | tr -d '\r')
# then filter the API log:
grep "$RID" api.log | python3 -m json.tool --no-ensure-ascii 2>/dev/null || grep "$RID" api.log
```

## Common signatures

| Signature | Likely cause | Action |
|-----------|--------------|--------|
| 5xx concentrated on one route after a deploy | regression in that router | roll back; the route template in the `path` label names the file in `aakaar/aakaar/api/routers/` |
| failed-run share climbing in `GET /runs`, HTTP healthy | capability-level failures (credentials, target site, SSRF guard, missing optional extra) | open a failed run (`GET /runs/{id}`) and read its `error` + `node_failed` event payload; vault errors → [03](03-vault-key-rotation.md); placement errors → [04](04-agent-fleet-degradation.md) |
| 429s (`Too many requests. Retry in Ns.`) | rate limiter — default 240/min per client, 20/min on `/auth` | confirm it's a real client flood before raising `AAKAAR_RATE_LIMIT_PER_MIN` / `AAKAAR_RATE_LIMIT_AUTH_PER_MIN`; 429 storms on `/auth` smell like credential stuffing — check `GET /audit` and source IPs |
| latency up across all routes, no errors | SQLite write contention or disk | the engine sets `busy_timeout=5000` — sustained waits mean a long writer; check disk space/IO; WAL file growing unboundedly means checkpointing is starved |
| every request 500 with DB errors | DB file problem | [02-sqlite-corruption-recovery](02-sqlite-corruption-recovery.md) |
| 503 on `/recordings` | `AAKAAR_REMOTE_EXEC_ENABLED=false` | expected when remote exec is off |
| OpenAI/LLM errors in logs, planning broken but runs fine | upstream LLM endpoint | runs don't need the LLM; planning degrades. Check `OPENAI_API_KEY` / `AAKAAR_OPENAI_BASE_URL`; never "fix" TLS errors with `AAKAAR_OPENAI_TLS_VERIFY=false` against a public endpoint |

## Load-related?

If the error rate tracks request volume, reproduce in a non-prod environment
with the k6 scenario in [`loadtest/`](../loadtest/README.md) before tuning
limits. The platform is a single-process design (in-process executor,
SQLite): the sustainable run-start rate is bounded by node work, not HTTP —
scale by giving the process more CPU/IO before anything else.

## Escalate

Use [escalation.md](escalation.md) with: the failing route(s) + status codes,
two or three `X-Request-ID`-correlated log excerpts, the run-status counts
from `GET /runs`, and what changed last (deploy, schedule, tenant onboarding).
