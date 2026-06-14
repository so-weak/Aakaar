# 03 — Archive ingest → data transform → object store

A fully offline pipeline: writes a CSV into the tenant object store,
filters/sorts it with the declarative pandas pipeline, packs the source and
the result into a zip, and copies the zip to a **stable** object-store key
that downstream consumers (or a schedule's next run) can rely on.

```
seed (file.write_csv) → transform (cap.data_transform) → pack (cap.archive_manage) → publish (cap.file_manage)
```

It needs no credentials, no LLM, no browser, and no agent — it is the
workflow to import first when validating a fresh install, and it is what the
CI smoke test (`loadtest/ci/smoke.py`) runs.

## What each node does

- **seed** — `file.write_csv` is an *action* (platform primitive, no grant
  needed). It writes literal rows to an explicit URI. **Replace `TENANT_ID`
  in `file_uri` with your tenant's uuid before importing** (actions take full
  `aakaar://t/<tenant>/<key>` URIs). In a real deployment this node is
  replaced by whatever produced the batch: `cap.sftp_read`,
  `cap.email_fetch`, `cap.file_download`, or a previous run's output.
- **transform** — `cap.data_transform` applies the `ops` pipeline (here:
  keep `status == "FAILED"`, sort by `amount` descending) and writes the
  result back to the object store; sources beyond 64 MiB are refused before
  pandas materializes them. Ops grammar (filter/sort/groupby/pivot/derive/…)
  is documented in the capability's docstring
  (`aakaar/aakaar/capabilities/data/data_transform/__init__.py`).
- **pack** — `cap.archive_manage` with `op: "create"` bundles both URIs into
  a zip under the run's prefix and returns `archive_uri`. (Extraction in the
  reverse direction is bomb-guarded: max 1000 members, 256 MiB decompressed
  budget enforced on the real stream.)
- **publish** — `cap.file_manage` copies the run-scoped archive to the
  stable key `examples/archive-demo/latest-report.zip` (bare keys resolve
  against the run's tenant). Each run overwrites it — "latest" semantics.

## Required grants (tenant admin, once — all secret-less)

```json
{"capability_ref": "cap.data_transform", "account_alias": "default", "secrets": {}, "input_defaults": {}}
{"capability_ref": "cap.archive_manage", "account_alias": "default", "secrets": {}, "input_defaults": {}}
{"capability_ref": "cap.file_manage",    "account_alias": "default", "secrets": {}, "input_defaults": {}}
```

## Verifying the output

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "$API/objects?uri=aakaar://t/$TENANT_ID/examples/archive-demo/latest-report.zip" \
  -o /tmp/latest-report.zip && unzip -l /tmp/latest-report.zip
```

Expect two members: `transactions.csv` and the transformed result csv.

## Why no extract step?

Two runtime constraints shape this example: `${...}` refs cannot index into
lists (so `${unpack.extracted_uris.0}` does not resolve), and
`op: "extract"` lands members under a per-run random prefix (so their URIs
are not statically addressable). Pipelines that *consume* archives should
therefore either iterate extracted URIs in application code via the run's
outputs, or keep the archive as the unit of exchange, as here.
