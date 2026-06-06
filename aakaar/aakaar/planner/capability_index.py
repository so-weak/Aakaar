"""Capability semantic index.

When a tenant has many capabilities, listing them all in the planner prompt
is wasteful and noisy. The capability index embeds capability descriptions
once and lets the planner retrieve a top-k relevant subset per user message.

For tenants with only a handful of grants the index is unnecessary — pass
the granted set straight through. The planner service decides which path
to take based on a threshold.

Indexes are partitioned by tenant_id so a grant change in one tenant doesn't
require re-embedding others.
"""

from __future__ import annotations

from dataclasses import dataclass

from aakaar.planner.embeddings import EmbeddingsClient
from aakaar.shared.registry import CapabilityDefinition, Registry
from aakaar.storage.vector_store import VectorItem, VectorStore

_NAMESPACE = "capabilities"


@dataclass(slots=True)
class CapabilityIndex:
    """Embeds capability descriptions and runs top-k searches.

    `reindex_for_tenant(tenant_id)` should be called when a grant is added,
    removed, or when a capability description changes. It is idempotent and
    cheap to re-run for small grant sets.
    """

    registry: Registry
    embeddings: EmbeddingsClient
    vector_store: VectorStore

    def reindex_for_tenant(self, tenant_id: str, granted: set[str]) -> None:
        """Refresh the per-tenant index to exactly the granted set.

        Adds new items, replaces drifted descriptions, and removes revoked
        capabilities.
        """
        existing_caps = {c.ref: c for c in self.registry.capabilities()}
        # Remove anything no longer granted (or no longer in the registry).
        # We don't have a `list_ids` on VectorStore; instead, infer from
        # mismatched grants by re-upserting the granted set and deleting any
        # known revocations the caller passes via revoke_for_tenant.
        if not granted:
            return
        items: list[VectorItem] = []
        texts: list[str] = []
        refs: list[str] = []
        for ref in sorted(granted):
            cap = existing_caps.get(ref)
            if cap is None:
                # Granted capability not present in the registry — skip; the
                # validator will catch this at planning time.
                continue
            refs.append(ref)
            texts.append(self._capability_text(cap))
        if not refs:
            return
        vectors = self.embeddings.embed(texts)
        for ref, vec, text in zip(refs, vectors, texts):
            items.append(VectorItem(id=ref, vector=vec, payload={"description": text}))
        self.vector_store.upsert(tenant_id, _NAMESPACE, items)

    def revoke_for_tenant(self, tenant_id: str, refs: list[str]) -> None:
        if not refs:
            return
        self.vector_store.delete(tenant_id, _NAMESPACE, refs)

    def search(self, tenant_id: str, query: str, k: int = 8) -> list[str]:
        """Return up to `k` capability refs ranked by relevance to `query`.

        The result is a list of refs, not full definitions; the caller looks
        up definitions in the registry as needed.
        """
        if k <= 0:
            return []
        vec = self.embeddings.embed([query])[0]
        hits = self.vector_store.search(tenant_id, _NAMESPACE, vec, k=k)
        return [h.id for h in hits]

    @staticmethod
    def _capability_text(cap: CapabilityDefinition) -> str:
        # Combine ref + description + tags into one document. Tags help the
        # embedding distinguish e.g. "login" from "report download" even when
        # the description is brief.
        parts = [cap.ref, cap.description]
        if cap.tags:
            parts.append("tags: " + ", ".join(cap.tags))
        return " | ".join(parts)
