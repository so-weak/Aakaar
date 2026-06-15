# ADR 0005: First-class airgap posture

- **Status:** Accepted
- **Date:** 2026-06-15
- **Deciders:** Platform engineering

## Context

The target deployment frequently has **no outbound network**. The platform must
boot, plan, execute server-side capabilities, and serve the UI with the host
disconnected from the internet. Two things normally reach out: the embedding
model download (Hugging Face hub) and the LLM API. Heavy ML/browser libraries
must also not be a hard import cost on a minimal install.

## Decision

Make airgap a supported, first-class mode rather than an afterthought:

- **Offline embeddings.** `AAKAAR_HF_OFFLINE=true` makes the BGE embedder load
  *only* from the local Hugging Face cache and never reach the hub (it sets
  `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE` and `local_files_only=True`). The model
  is pre-staged into `<data_dir>/hf_cache`. On a networked machine, leave it
  `false` so the first run populates the cache.
- **Lazy heavy dependencies.** ML/browser libraries are imported lazily behind
  optional-dependency extras (`aakaar/pyproject.toml`; the `gui` extra in
  `aakaar-agent/pyproject.toml`). A capability module must import cleanly with
  the extra absent and raise a clear `RuntimeError` naming the extra when its
  handler runs. So a minimal install neither pulls nor imports them.
- **No-LLM boot.** With no `OPENAI_API_KEY`, the app boots with
  `FakeLLMClient`/`FakeEmbeddingsClient` — everything works except real planning.
  A self-hosted LLM gateway on the LAN is reachable via `AAKAAR_OPENAI_BASE_URL`
  (with `AAKAAR_OPENAI_TLS_VERIFY=false` *only* for a self-signed local gateway).
- **Local-only audit and metrics.** The audit file sink writes
  `<data_dir>/audit/audit.jsonl` on the host (no external log shipper); `/metrics`
  is scraped locally with no external egress.
- **Airgap compose target.** `docker-compose.airgap.yml` brings up API + web on
  SQLite with no external services.

## Consequences

**Positive**

- The platform runs fully disconnected; the only thing that degrades without
  network is LLM planning, which a LAN gateway restores.
- A minimal install stays small — no forced ML/Chromium import on hosts that
  don't use those capabilities.

**Negative / accepted trade-offs**

- **Models and wheels must be pre-staged.** Offline hosts need the BGE cache and
  any optional extras vendored ahead of time; there is no on-demand fetch.
- `AAKAAR_OPENAI_TLS_VERIFY=false` is a foot-gun if misused — it is only for a
  self-signed *local* gateway and logs loudly; it must never be set against a
  public endpoint.
- Time-sensitive features that assume connectivity (e.g. an external OIDC IdP)
  are simply unavailable air-gapped; auth then relies on local password + MFA.

## Alternatives considered

- **Assume connectivity and fetch models on first run.** Rejected: fails on the
  air-gapped target.
- **Bundle every heavy dependency unconditionally.** Rejected: bloats minimal
  installs and slows boot for hosts that don't need browser/ML caps.
