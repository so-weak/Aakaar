# Capability Authoring Guide

How to add a new **capability** to Aakaar safely. A capability is the
highest-leverage thing the LLM planner can reach for — it encapsulates a unit of
real-world automation (send an email, POST a webhook, open an SFTP session)
behind a stable, schema-validated interface and, where needed, owns the
credentials it uses. Because the planner composes capabilities from untrusted
natural language (see the [security whitepaper](security-whitepaper.md) §7),
**every capability is a piece of the security boundary.** Get the declarations
right and the platform protects you; get them wrong and you can hand a model a
way to move money or leak a secret.

This guide is normative for code under `aakaar/aakaar/capabilities/`. Read a
neighbouring capability and match it — `cap.sftp_login`
(`capabilities/sftp_login/`) for the credentialed pattern, `cap.webhook_send`
(`capabilities/integration/webhook_send/`) for the SSRF-guarded HTTP pattern,
`cap.data_validate` (`capabilities/data/data_validate/`) for a read-only one.

---

## 1. Anatomy of a capability

One package per capability under `aakaar/aakaar/capabilities/<area>/<name>/`,
exposing two module-level names (`capabilities/_base.py` discovers them
automatically at startup; modules whose names start with `_` are skipped):

- **`definition: CapabilityDefinition`** — the contract the planner and validator
  see.
- **`handler: CapabilityHandler`** — `async def handler(ctx: ActivityContext,
  inputs: dict[str, Any]) -> dict[str, Any]`. (A *remote-only* capability that runs
  exclusively on the agent sets `remote_only = True` and may omit the server
  handler.)

```python
from pydantic import BaseModel, ConfigDict, Field
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import CapabilityDefinition

CAP_REF = "cap.my_thing"

class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")          # REQUIRED — see §3
    target: str = Field(description="…")

class _Outputs(BaseModel):
    status: int = Field(description="…")

definition = CapabilityDefinition(
    ref=CAP_REF,
    description="One precise sentence the planner reads to decide when to use this.",
    input_schema=_Inputs,
    output_schema=_Outputs,
    side_effecting=False,        # DECLARE THIS — see §4
    secrets=(),                  # names only — see §5
    tags=("area", "verb"),
)

async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    ...
    return {"status": 200}
```

The package registers itself by being importable; add a `tests/test_cap_<name>.py`
suite (see §8).

---

## 2. Describe vs declare — the planner contract

The planner never sees your handler. It sees the **definition** — the `ref`, the
`description`, and the JSON Schema derived from `input_schema`/`output_schema`.
Two rules follow:

- **The `description` is load-bearing.** It is how the model decides whether to
  reach for your capability and how to fill its inputs. Write it as a precise,
  one-purpose sentence; describe *when* to use it, not how it's implemented.
- **The schemas are the validator.** Planner output is shape-checked against
  `input_schema` before a DAG can be saved or run. A field the model must not
  invent should not be in the input schema; a field that constrains behaviour
  (allowlists, timeouts) belongs there with `Field(description=...)` so the model
  fills it correctly.

The MCP projection (`aakaar-mcp/`) exposes the *same* definitions as MCP tools —
one registry, two channels — so your `description` and schemas also drive any MCP
client. Keep them clean.

---

## 3. Strict input schemas (`extra="forbid"`)

Every input model **must** set `model_config = ConfigDict(extra="forbid")`. This
is not stylistic: it means a planner (or a caller) cannot smuggle an
undeclared field past the validator into your handler. A capability that accepts
extra keys is a capability whose real input surface is unbounded — exactly what
you don't want when the input comes from an LLM.

- Constrain with validators where it matters: `Field(gt=0, le=300)` for a
  timeout, a regex `pattern` for an identifier, an enum for a mode.
- Outputs are also a declared model so downstream nodes (and the validator) know
  the shape your capability produces.

---

## 4. The `side_effecting` flag — the dry-run boundary

`side_effecting` governs the **dry-run** simulation path (the engine can run a
DAG in `RunMode.DRY_RUN`, walking the full topology but short-circuiting
side-effecting nodes to a simulated success). It is tri-state (`types.py`):

| Value | Meaning | Dry-run behaviour |
|-------|---------|-------------------|
| `True` | Declared side-effecting — writes/sends/uploads/transfers that escape the run sandbox | **Simulated** (never performed) |
| `False` | Declared read-only — scrapes, parses, GET-style reads, `time.now` | **Executed for real** (safe) |
| `None` (omitted) | **UNDECLARED** | Treated **conservatively as side-effecting** — simulated, so a forgetful new capability can never move money in a simulation |

**Declare it explicitly.** `None` is the *safe fallback*, not a recommendation:

- If your capability performs **any** external, irreversible effect (sends an
  email, POSTs a webhook, writes a file to SFTP, clicks "Submit" on a portal),
  set **`side_effecting=True`**. A dry-run must never fire it.
- If it is genuinely read-only and side-effect-free, set **`side_effecting=False`**
  so a dry-run exercises it for real (the dry-run is more useful when reads run).

> Note on the current tree: read-only capabilities here declare
> `side_effecting=False` (e.g. `cap.data_validate`, `cap.ocr_extract`); some
> side-effecting ones (e.g. `cap.webhook_send`, `cap.email_send`) omit the flag
> and rely on the conservative `None` default. Correct behaviour either way, but
> **new side-effecting capabilities should declare `True` explicitly** so the
> intent is visible at the definition and not dependent on the fallback.

The flag is deliberately kept off the planner/validator surface — it governs
execution mode only, not what the model may compose.

---

## 5. The credential envelope — secrets, never values

A capability declares the credentials it needs by **name** and fetches the values
from the vault at execution time. Names never leave the definition; values never
enter the DAG env.

**Declare** (from `cap.sftp_login`):

```python
from aakaar.shared.registry import CapabilityDefinition, SecretSpec

definition = CapabilityDefinition(
    ref=CAP_REF,
    ...
    secrets=(
        SecretSpec(name="username", description="SSH username."),
        SecretSpec(name="password", description="SSH password. Optional when a key is supplied."),
    ),
)
```

**Fetch** (in the handler):

```python
from aakaar.interpreter.credentials import fetch_credentials

async def handler(ctx, inputs):
    alias = inputs["account_alias"]
    creds = fetch_credentials(ctx, capability_ref=CAP_REF, account_alias=alias)
    username = (creds.get("username") or "").strip()
    password = creds.get("password") or None
    ...
```

`fetch_credentials` enforces defense-in-depth: the requested `account_alias` must
exist in the **tenant's grant for this exact capability** (the planner already
gated visibility, but the fetch re-checks), it reads fresh from the vault per
call, and the returned dict is keyed by your `SecretSpec.name`s. Rules:

- **Never return a secret in your `_Outputs`** and never put it in the dict your
  handler returns — outputs are persisted (redacted, but don't rely on that as a
  primary control) and visible to downstream nodes.
- **Never log a secret value.** Log the alias, the host, counts — not the
  credential. (See `cap.webhook_send`, which logs `host`/`payload_keys` and
  states it never logs header values or the body.)
- A capability that needs no credentials sets `secrets=()` and must not fetch.
  Generic primitives that need auth are a sign the work belongs in a capability,
  not an action (`ActionDefinition` carries no credentials by design).

---

## 6. SSRF guard for outbound HTTP

Any capability that makes an outbound HTTP request from a **model-influenced URL**
must route it through the SSRF guard (`aakaar/aakaar/core/net/ssrf.py`), or a
poisoned prompt could aim it at loopback, link-local, or cloud-metadata
endpoints. The pattern (from `cap.webhook_send`):

```python
from aakaar.core.net.ssrf import SsrfBlocked, assert_host_allowed, build_async_client

async def handler(ctx, inputs):
    url = inputs["url"]
    allow_hosts = tuple(inputs.get("allow_hosts") or ())

    parsed = httpx.URL(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{CAP_REF}: url must be http or https")
    # Early, precise rejection; the transport re-checks at connect time too.
    assert_host_allowed(parsed.host, allow_hosts=allow_hosts)

    async with build_async_client(allow_hosts=allow_hosts, timeout=timeout) as client:
        try:
            resp = await client.post(url, ...)
        except SsrfBlocked:
            raise   # surface unchanged so the orchestrator can classify it
```

- **Default-deny private addresses.** Private/loopback/link-local/reserved targets
  are blocked. Reaching an *internal* host is an explicit opt-in: the DAG/grant
  must list that exact hostname in `allow_hosts` (expose `allow_hosts` as an
  input so it's auditable, never a hard-coded bypass).
- Use `build_async_client(...)` (or `build_sync_client(...)`) rather than a raw
  `httpx.AsyncClient` — the guard transport re-checks at connect time, covering
  redirects.
- Let `SsrfBlocked` propagate; don't wrap it into a generic error.

---

## 7. Path guards for filesystem access

- **Arbitrary local reads are off by default.** `file.read_local` (reading worker
  filesystem paths into the object store) is gated behind
  `AAKAAR_ALLOW_LOCAL_PATHS=true` and denies otherwise — do not add a capability
  that re-opens that surface without the same gate.
- When a capability does touch paths (e.g. archive extraction), **resolve and
  contain**: expand `~`, `resolve()` the path, and verify it stays within the
  intended root before reading/writing, so a `../../etc/...` input can't escape.
  See `capabilities/files/archive_manage/` and
  `interpreter/activities/file.py` for the in-tree pattern.
- Prefer the **object store** (`ctx`-provided) over raw filesystem paths for run
  artifacts — it's the canonical, tenant-scoped location.

---

## 8. Lazy-import heavy dependencies

Heavy or optional libraries (SSH, OCR, browser, ML) must be **lazy-imported
inside the handler**, never at module top level, and gated behind an
optional-dependency extra (`aakaar/pyproject.toml`). A capability module must
import cleanly with the extra absent and raise a clear `RuntimeError` naming the
extra when its handler actually runs. This keeps a minimal/air-gapped install
small (ADR [0005](adr/0005-airgap-posture.md)).

```python
async def handler(ctx, inputs):
    import asyncssh        # lazy: not imported unless this capability runs
    ...
```

---

## 9. Tests and the quality gate

Every capability ships a `tests/test_cap_<name>.py` suite, and every behaviour
change ships a test that fails without it. Keep handlers testable by factoring
pure helpers (URL/body assembly, parsing) out of the I/O path — see
`build_request_kwargs` in `cap.webhook_send`, unit-tested without a live server.

Tests must not require external services: SQLite, the local vault, the object
store, and fake LLM clients cover everything; HTTP is exercised with a fake
transport or `httpx`'s test utilities, not a real network.

Run the gate before you push ([CONTRIBUTING.md](../CONTRIBUTING.md)):

```bash
cd aakaar
.venv/bin/ruff check aakaar tests
.venv/bin/mypy aakaar
.venv/bin/python -m pytest -q
```

`pytest` runs with `filterwarnings = error`: a new `DeprecationWarning` from your
code fails the suite.

---

## 10. Author's pre-merge checklist

- [ ] Package under `capabilities/<area>/<name>/` exposing `definition` + `handler`.
- [ ] `_Inputs` and `_Outputs` are Pydantic models; **`_Inputs` sets
      `extra="forbid"`** and constrains fields.
- [ ] `description` is one precise sentence aimed at the planner.
- [ ] **`side_effecting` is declared explicitly** — `True` for any external
      irreversible effect, `False` only if genuinely read-only.
- [ ] Credentials declared as `SecretSpec` names; fetched via `fetch_credentials`;
      **no secret returned in outputs, none logged**.
- [ ] Outbound HTTP from a model-influenced URL goes through `assert_host_allowed`
      + `build_async_client`; `allow_hosts` is an explicit input, not a hard-coded
      bypass.
- [ ] Filesystem paths resolved and contained; no new unguarded local-read surface.
- [ ] Heavy deps lazy-imported in the handler behind an extra, with a clear error
      when absent.
- [ ] `tests/test_cap_<name>.py` added; `ruff` + `mypy --strict` + `pytest` green.
