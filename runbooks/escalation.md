# Incident escalation template

Copy this into the incident channel / ticket when handing an Aakaar incident
to another engineer or to the maintainers. Security-relevant incidents
(suspected tenant-isolation breach, secret exposure, compromised agent key)
follow [SECURITY.md](../SECURITY.md) **as well** — email, don't file publicly.

```text
INCIDENT: <one line — what is broken, from the user's point of view>

Severity:    SEV1 (platform down / data loss) | SEV2 (degraded, workaround exists) | SEV3 (cosmetic / single tenant inconvenience)
Started:     <UTC timestamp of first impact, and how you know>
Detected by: <monitoring | tenant report | deploy verification>
Status:      investigating | mitigated | resolved

IMPACT
- Tenants affected:   <all | list of tenant slugs | unknown>
- Surface affected:   <API routes / runs / planning / agents / schedules / web UI>
- Data at risk:       <none | runs lost | artifacts lost | secrets possibly exposed>

ENVIRONMENT
- Deployment:  <dev.sh | docker compose | docker-compose.airgap.yml | other>
- Commit/tag:  <git SHA the API is running>
- DB:          <sqlite path | postgres DSN host only>
- Broker:      <none | url>
- Last change: <deploy / config change / migration, with time>

EVIDENCE (attach, don't paraphrase)
- /healthz output and timestamp
- /metrics excerpts: aakaar_http_requests_total 5xx lines for affected routes
- 2-3 log excerpts correlated by X-Request-ID (AAKAAR_LOG_FORMAT=json if possible)
- For run incidents: GET /runs/{id} JSON (status, error, last 10 events)
- For agent incidents: GET /agents output + agent-side log tail with close codes
- Audit trail slice if actions are in question: GET /audit (or data/audit/audit.jsonl tail)

ACTIONS TAKEN (chronological, with timestamps)
- <what you ran / changed, and what it did — include runbook numbers, e.g. "followed 02 step 2a">

CURRENT HYPOTHESIS
- <best theory, and what evidence would confirm/refute it>

ASKS
- <what you need from the next person: decision, access, code fix, review>

BACKUP STATE
- Last verified backup: <timestamp, location>   (runbook 01)
- Restore drill last performed: <date>
```

Rules of thumb:

- **Never paste secret values, vault file contents, enrollment keys, or JWT
  secrets** into an incident thread — names and timestamps only.
- Mitigate before root-causing for SEV1: restore from backup (runbook 01)
  beats live-debugging a corrupted DB.
- One owner at a time. Hand-off is explicit: the new owner replies
  "taking it" and updates `Status`.
- After resolution, file the post-mortem within 3 business days: timeline,
  root cause, the runbook that was missing or wrong (fix it in the same PR).
