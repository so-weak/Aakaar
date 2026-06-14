#!/usr/bin/env python3
"""Seed a live Aakaar API for the k6 load scenario.

Creates a fresh tenant + admin, grants the secret-less capabilities the
offline example workflow needs, imports the workflow, and prints the env
lines the k6 script consumes:

    eval "$(AAKAAR_SUPERUSER_EMAIL=... AAKAAR_SUPERUSER_PASSWORD=... python loadtest/ci/seed.py)"
    k6 run loadtest/k6/runs.js

Same environment contract as smoke.py (AAKAAR_API, AAKAAR_SUPERUSER_EMAIL,
AAKAAR_SUPERUSER_PASSWORD); only the export lines go to stdout, progress goes
to stderr.
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import httpx

# Reuse the smoke helpers (same directory; Python puts the script dir on sys.path).
from smoke import API, EXAMPLE, GRANT_REFS, expect, fail, login


def main() -> None:
    su_email = os.environ.get("AAKAAR_SUPERUSER_EMAIL")
    su_password = os.environ.get("AAKAAR_SUPERUSER_PASSWORD")
    if not su_email or not su_password:
        fail("env", "AAKAAR_SUPERUSER_EMAIL / AAKAAR_SUPERUSER_PASSWORD must be set")

    client = httpx.Client(timeout=30.0)
    su_token = login(client, su_email, su_password, "superuser-login")

    suffix = uuid.uuid4().hex[:8]
    admin_email = f"loadtest-{suffix}@smoke.example"
    admin_password = "loadtest-admin-pw-1"
    tenant = expect(
        client.post(
            f"{API}/superuser/tenants",
            headers={"Authorization": f"Bearer {su_token}"},
            json={
                "slug": f"loadtest-{suffix}",
                "name": f"Loadtest {suffix}",
                "admin_email": admin_email,
                "admin_password": admin_password,
            },
        ),
        201,
        "create-tenant",
    )
    tenant_id = tenant["id"]

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

    body = json.loads(EXAMPLE.read_text().replace("TENANT_ID", tenant_id))
    workflow = expect(
        client.post(f"{API}/workflows", headers=auth, json=body), 201, "create-workflow"
    )

    print(f"seeded tenant={tenant['slug']} workflow={workflow['id']}", file=sys.stderr)
    print(f"export AAKAAR_EMAIL={admin_email}")
    print(f"export AAKAAR_PASSWORD={admin_password}")
    print(f"export AAKAAR_WORKFLOW_ID={workflow['id']}")


if __name__ == "__main__":
    main()
