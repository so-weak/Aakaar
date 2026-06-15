# ADR 0004: A `KeyProvider` seam for an external KMS without a KMS dependency

- **Status:** Accepted
- **Date:** 2026-06-15
- **Deciders:** Platform engineering, Security

## Context

Tenant credentials are stored in the filesystem vault, Fernet-encrypted at rest
(`AAKAAR_VAULT_KEY`). A bank's security team will usually require that the root
of trust for that encryption live in **their** key manager — an HSM, an on-prem
secrets daemon, or a cloud KMS — not in an environment variable on the app host.
But we cannot take a hard dependency on any specific KMS SDK: that would violate
the "plain PyPI, no third-party infra" constraint and couple the platform to one
vendor.

## Decision

Introduce a `KeyProvider` **Protocol** (`aakaar/aakaar/vault/key_provider.py`)
as the single source of Fernet key material for the vault. `LocalVault` no longer
reads keys directly; it asks a provider for `get_active_key()` (encrypts new
writes) and `decryption_keys()` (every key still accepted, active first —
MultiFernet order).

Two providers ship:

- **`LocalKeyProvider`** (default) — reads the same comma-separated
  `AAKAAR_VAULT_KEY` Fernet keys as before. Byte-for-byte identical behaviour, so
  existing vault files and tests are unaffected.
- **`EnvelopeKeyProvider`** (scaffold, **not wired by default**) — demonstrates
  external-KMS **envelope encryption**: a data key (itself a Fernet key) does the
  at-rest encryption; the data key is stored only *wrapped* by a master key the
  KMS holds and never releases. The KMS call is injected as `unwrap_fn`
  (`wrapped_bytes -> data_key_str`), so this class has **zero** third-party
  imports and is unit-tested with a fake unwrap. A real deployment supplies a
  small adapter that calls its KMS's Decrypt/unwrap API.

Unwrap happens once at construction and is cached (the vault is long-lived; we
must not phone the KMS per secret read). Failure to unwrap the *active* key is
fatal; a *previous* key that no longer unwraps (rotated out) is logged and
skipped.

## Consequences

**Positive**

- A bank can root vault encryption in their own KMS/HSM **without us shipping or
  importing any KMS SDK** — the seam stays inside the no-infra constraint.
- Rotation maps cleanly onto the existing MultiFernet window: pass the current
  wrapped data key plus previous ones.
- The default path is unchanged, so nothing regresses for sites that keep keys in
  the environment.

**Negative / accepted trade-offs**

- `EnvelopeKeyProvider` is a **scaffold**: the integrator writes the adapter and
  wires it; we do not provide a turnkey cloud integration (by design).
- The unwrapped data key is held **in process memory** for the vault's lifetime —
  envelope encryption protects the data key at rest, not against a process-memory
  compromise. That residual is documented in the security whitepaper.
- Provider wiring for the envelope path is not exposed through `AAKAAR_*` env
  today; selecting it is a code-level integration in the deployment's app
  assembly.

## Alternatives considered

- **Depend on a specific KMS SDK (boto3/google-cloud-kms/…).** Rejected:
  third-party infra + vendor lock-in.
- **Only support a raw key in the environment.** Rejected: many banks forbid the
  root of trust living as plaintext config on the app host.
