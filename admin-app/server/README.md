# admin-app server

Tiny FastAPI service backing `admin-app`'s recon-upload page. Independent
of aakar and nbbl — its own venv, its own port (8001), its own storage
directory.

## One-time setup

```bash
cd admin-app/server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8001 --reload
```

The repo's `start.sh` does this automatically alongside the other dev
processes.

## Endpoints

- `GET  /healthz` — liveness check
- `POST /api/recon/uploads` — multipart form upload. Fields:
  - `file`        — the .csv or .zip
  - `switch_type` — `Issuer` or `Acquirer`
  - `cycle`       — e.g. `C02`
  - `date`        — yyyy-mm-dd
  - `skip_recon`  — `Yes` / `No` (optional, default `No`)

  Files are written to
  `uploads/{date}/{switch_type}/{cycle}_{utc-ts}_{filename}`.

- `GET  /api/recon/uploads` — newest-first history (in-memory; clears on
  restart).

The page (`admin-app/src/pages/ReconUpload.jsx`) calls these via Vite's
`/api` proxy, so the frontend doesn't hard-code `localhost:8001`.
