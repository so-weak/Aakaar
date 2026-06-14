# 01 — Web scrape → extract → webhook notify

Scrapes a page with the server-side Playwright pool, shapes the page content
into structured JSON, and POSTs it to a webhook together with a timestamp.

```
stamp (time.now)  ─┐
                   ├─> notify (cap.webhook_send)
scrape (cap.web_scrape) ─┘
```

`stamp` and `scrape` have no edge between them, so they run in the same DAG
layer (in parallel); `notify` waits for both.

## What each node does

- **scrape** — `cap.web_scrape` opens a fresh browser session for `url`,
  scrapes it, and closes the session. The `extract` field is a
  natural-language description of what to pull out: with an LLM configured
  (`OPENAI_API_KEY`), `scrape.data` is the model's structured JSON for that
  request — this is the "transform" step, page text → shaped record. Without
  an LLM it degrades to the deterministic `{text, tables}` shape (still valid
  payload, just unshaped). For JS-heavy pages add `wait_selector`.
- **notify** — `cap.webhook_send` POSTs the JSON `payload` through the SSRF
  guard. Each payload value is either a literal or a complete `${...}` ref
  (embedding refs inside longer strings is unsupported). Private/loopback
  hosts are blocked; to notify a service on your own network add
  `"allow_hosts": ["my-internal-host"]` to the inputs. Auth tokens go in
  `headers` — never logged.

## Required grants (tenant admin, once)

```json
{"capability_ref": "cap.web_scrape",   "account_alias": "default", "secrets": {}, "input_defaults": {}}
{"capability_ref": "cap.webhook_send", "account_alias": "default", "secrets": {}, "input_defaults": {}}
```

## Before importing

- Replace `url` with the page you want scraped.
- Replace the webhook `url` (the placeholder is not routable). For a quick
  test, any HTTPS request-bin service works.

Import + run: see [../README.md](../README.md). On success, the run's
`notify` node outputs `{status, body}` from your endpoint, visible in the
run detail view (`GET /runs/{id}` or the web UI).
