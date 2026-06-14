# Example workflows

Four worked, importable workflows, each in its own directory:

| Example | Flow | Needs |
|---------|------|-------|
| [01-web-scrape-notify](01-web-scrape-notify/) | scrape a page → extract structured data → POST it to a webhook | nothing but network (LLM optional) |
| [02-sftp-pdf-email](02-sftp-pdf-email/) | fetch a PDF over SFTP → extract its text → email text + attachment | SFTP + SMTP credentials |
| [03-archive-transform-store](03-archive-transform-store/) | seed a CSV → pandas transform → pack a zip → publish to a stable object-store key | nothing (fully offline) |
| [04-remote-desktop](04-remote-desktop/) | focus a window → click → type, on a remote workstation | one enrolled, online agent |

Each `workflow.json` is a complete request body for `POST /workflows`
(`{name, description, dag, rationale}`) and validates against the real DAG
schema (`aakaar/aakaar/shared/dag/types.py`) and the registered capability
schemas. `03` runs end-to-end on a bare dev install — it is also the basis of
the CI smoke test (`loadtest/ci/smoke.py`).

## Importing an example

The API validates three layers at save time: DAG structure, registry schemas,
and **tenant grants** — every `cap.*` node must be granted to your tenant
first, or the POST returns `422 ... not granted`. Each example's README lists
the exact grants it needs.

```bash
API=http://localhost:8000

# 1. Log in (any tenant user can create workflows; grants need a tenant ADMIN).
TOKEN=$(curl -s -X POST $API/auth/login -H 'Content-Type: application/json' \
  -d '{"email": "admin@your-tenant.example", "password": "..."}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

# 2. Create the grants the example's README lists, e.g. for 01:
curl -s -X POST $API/admin/grants -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"capability_ref": "cap.web_scrape", "account_alias": "default", "secrets": {}, "input_defaults": {}}'
curl -s -X POST $API/admin/grants -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"capability_ref": "cap.webhook_send", "account_alias": "default", "secrets": {}, "input_defaults": {}}'

# 3. Import. Some examples embed object-store URIs of the form
#    aakaar://t/TENANT_ID/... — substitute your tenant id (it is in any
#    workflow/run response, or ask your superuser):
TENANT_ID=...   # your tenant's uuid
python3 - "$TENANT_ID" 01-web-scrape-notify/workflow.json <<'EOF' > /tmp/wf.json
import json, sys
tenant, path = sys.argv[1], sys.argv[2]
print(json.dumps(json.loads(open(path).read().replace("TENANT_ID", tenant))))
EOF
WF_ID=$(curl -s -X POST $API/workflows -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d @/tmp/wf.json \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

# 4. Run it and poll:
RUN_ID=$(curl -s -X POST $API/workflows/$WF_ID/runs -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"inputs": {}}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -s $API/runs/$RUN_ID -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Or skip the curl entirely: the web UI's workflow editor accepts the same DAG
JSON, and runs show live in the run console.

## Reading the DAG format

- `nodes[].ref` — `cap.*` (granted capability), dotted action
  (`file.write_csv`, `time.now`), or control (`human.prompt`,
  `control.wait`). `kind` must match what the registry declares for the ref.
- `${node_id.field}` — wires an upstream output into an input. A ref must be
  the **entire** string: `"body": "${pdf.text}"` works,
  `"body": "Text: ${pdf.text}"` does **not** (embedding is unsupported by
  design; compose literal text in its own input or a payload object key).
- `edges` — `{"from": ..., "to": ...}`. Every ref needs an edge path from
  producer to consumer; the examples declare all edges explicitly.
- `nodes[].target` — where the node runs: omitted/`"server"` = API host;
  an agent alias / `pool:<name>` / `os:<name>` = remote agent (example 04).
- `dag.id`/`dag.version` stay `""`/`0` in files — the server assigns them on
  save.

## Inputs vs. literals

`POST /workflows/{id}/runs` accepts an `inputs` object; it is recorded on the
run (and reused by `/runs/{id}/rerun`) but is **not** injected into node
input resolution — `${...}` refs only see upstream node outputs. So these
examples parameterize by literal values in the DAG: edit the placeholder
values (webhook URL, remote path, recipient, agent alias) before importing,
or save an edited copy as a new workflow version later.
