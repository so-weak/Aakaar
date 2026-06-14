#!/usr/bin/env python3
"""End-to-end smoke test against a live Aakaar API.

Exercises the critical tenant path with zero external services:

    superuser login -> create tenant -> tenant-admin login -> create grants
    -> import the offline example workflow (examples/03-archive-transform-store)
    -> start a run -> poll to SUCCEEDED -> fetch the produced artifact.

Run it against a freshly booted API (SQLite, fake LLM, no browser needed):

    AAKAAR_SUPERUSER_EMAIL=smoke@example.com \
    AAKAAR_SUPERUSER_PASSWORD=smoke-password-1 \
    python loadtest/ci/smoke.py

Environment:
    AAKAAR_API                  base URL (default http://127.0.0.1:8000)
    AAKAAR_SUPERUSER_EMAIL      superuser the API was bootstrapped with (required)
    AAKAAR_SUPERUSER_PASSWORD   its password (required)
    SMOKE_RUN_TIMEOUT_S         run-completion deadline (default 120)

Exits 0 on success; prints the failing step and exits 1 otherwise.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

API = os.environ.get("AAKAAR_API", "http://127.0.0.1:8000").rstrip("/")
RUN_TIMEOUT_S = float(os.environ.get("SMOKE_RUN_TIMEOUT_S", "120"))
EXAMPLE = (
    Path(__file__).resolve().parents[2]
    / "examples"
    / "03-archive-transform-store"
    / "workflow.json"
)
GRANT_REFS = ("cap.data_transform", "cap.archive_manage", "cap.file_manage")


def fail(step: str, detail: object) -> None:
    print(f"SMOKE FAIL [{step}]: {detail}", file=sys.stderr)
    sys.exit(1)


def expect(resp: httpx.Response, status: int, step: str) -> dict:
    if resp.status_code != status:
        fail(step, f"expected {status}, got {resp.status_code}: {resp.text[:500]}")
    return resp.json() if resp.content else {}


def login(client: httpx.Client, email: str, password: str, step: str) -> str:
    body = expect(
        client.post(f"{API}/auth/login", json={"email": email, "password": password}),
        200,
        step,
    )
    token = body.get("access_token")
    if not token:
        fail(step, f"no access_token in login response: {body}")
    return token


def main() -> None:
    su_email = os.environ.get("AAKAAR_SUPERUSER_EMAIL")
    su_password = os.environ.get("AAKAAR_SUPERUSER_PASSWORD")
    if not su_email or not su_password:
        fail("env", "AAKAAR_SUPERUSER_EMAIL / AAKAAR_SUPERUSER_PASSWORD must be set")

    client = httpx.Client(timeout=30.0)

    # 0. Readiness — tolerate a still-booting API for up to 60s.
    deadline = time.monotonic() + 60
    while True:
        try:
            if client.get(f"{API}/healthz").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            fail("healthz", f"API at {API} not ready within 60s")
        time.sleep(1)
    print(f"ok: healthz ({API})")

    # 1. Superuser login + fresh tenant (unique slug so reruns don't collide).
    su_token = login(client, su_email, su_password, "superuser-login")
    suffix = uuid.uuid4().hex[:8]
    admin_email = f"admin-{suffix}@smoke.example"
    admin_password = "smoke-admin-pw-1"
    tenant = expect(
        client.post(
            f"{API}/superuser/tenants",
            headers={"Authorization": f"Bearer {su_token}"},
            json={
                "slug": f"smoke-{suffix}",
                "name": f"Smoke {suffix}",
                "admin_email": admin_email,
                "admin_password": admin_password,
            },
        ),
        201,
        "create-tenant",
    )
    tenant_id = tenant["id"]
    print(f"ok: tenant {tenant['slug']} ({tenant_id})")

    # 2. Tenant-admin login + the example's secret-less grants.
    token = login(client, admin_email, admin_password, "admin-login")
    auth = {"Authorization": f"Bearer {token}"}
    for ref in GRANT_REFS:
        expect(
            client.post(
                f"{API}/admin/grants",
                headers=auth,
                json={
                    "capability_ref": ref,
                    "account_alias": "default",
                    "secrets": {},
                    "input_defaults": {},
                },
            ),
            201,
            f"grant-{ref}",
        )
    print(f"ok: {len(GRANT_REFS)} grants")

    # 3. Import the offline example workflow (validates DAG + grants server-side).
    body = json.loads(EXAMPLE.read_text().replace("TENANT_ID", tenant_id))
    workflow = expect(
        client.post(f"{API}/workflows", headers=auth, json=body), 201, "create-workflow"
    )
    workflow_id = workflow["id"]
    print(f"ok: workflow {workflow_id} v{workflow['latest_version']}")

    # 4. Run it and poll to a terminal status.
    run = expect(
        client.post(f"{API}/workflows/{workflow_id}/runs", headers=auth, json={"inputs": {}}),
        201,
        "start-run",
    )
    run_id = run["id"]
    deadline = time.monotonic() + RUN_TIMEOUT_S
    status = run["status"]
    detail = {"run": dict(run, error=None), "events": []}
    while status not in ("succeeded", "failed", "cancelled"):
        if time.monotonic() > deadline:
            fail("poll-run", f"run {run_id} still {status} after {RUN_TIMEOUT_S}s")
        time.sleep(1)
        detail = expect(client.get(f"{API}/runs/{run_id}", headers=auth), 200, "poll-run")
        status = detail["run"]["status"]
    if status != "succeeded":
        events = [
            {"kind": e["kind"], "node_id": e["node_id"], "payload": e["payload"]}
            for e in detail["events"][-5:]
        ]
        fail("run-status", f"run ended {status}; error={detail['run']['error']}; tail={events}")
    print(f"ok: run {run_id} succeeded")

    # 5. The workflow published an artifact at a stable key — fetch it.
    artifact_uri = f"aakaar://t/{tenant_id}/examples/archive-demo/latest-report.zip"
    resp = client.get(f"{API}/objects", headers=auth, params={"uri": artifact_uri})
    if resp.status_code != 200:
        fail("artifact", f"GET /objects -> {resp.status_code}: {resp.text[:300]}")
    if not resp.content.startswith(b"PK"):
        fail("artifact", f"expected a zip (PK magic), got {resp.content[:16]!r}")
    print(f"ok: artifact {artifact_uri} ({len(resp.content)} bytes)")

    print("SMOKE PASS")


if __name__ == "__main__":
    main()
