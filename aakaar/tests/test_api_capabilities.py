"""Capability listing endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient
from pydantic import BaseModel

from aakaar.api.deps import AppDependencies
from aakaar.shared.registry import CapabilityDefinition, SecretSpec
from tests._api_helpers import (
    auth_headers,
    login,
    seed_tenant_admin,
)


class _In(BaseModel):
    pass


class _Out(BaseModel):
    pass


def test_capabilities_list_only_granted(
    deps: AppDependencies, client: TestClient
) -> None:
    deps.registry.add(
        CapabilityDefinition(
            ref="cap.granted",
            description="granted",
            input_schema=_In,
            output_schema=_Out,
            secrets=(SecretSpec(name="username"),),
        )
    )
    deps.registry.add(
        CapabilityDefinition(
            ref="cap.not_granted",
            description="not granted",
            input_schema=_In,
            output_schema=_Out,
        )
    )

    seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    token = login(client, email="a@a.test", password="adminpass1")

    # Grant one of them.
    r = client.post(
        "/admin/grants",
        headers=auth_headers(token),
        json={
            "capability_ref": "cap.granted",
            "account_alias": "primary",
            "secrets": {"username": "u"},
        },
    )
    assert r.status_code == 201, r.text

    r = client.get("/capabilities", headers=auth_headers(token))
    assert r.status_code == 200
    refs = {item["ref"] for item in r.json()}
    assert "cap.granted" in refs
    assert "cap.not_granted" not in refs
    # Action and control primitives are listed alongside.
    assert "browser.navigate" in refs
    assert "control.wait" in refs
