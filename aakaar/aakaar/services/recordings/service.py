"""In-memory activity-recording registry + agent control calls.

A recording is server-side state about an in-flight capture on one tenant's
agent. The registry is deliberately ephemeral: entries are tiny (no events —
those only ever transit through a single stop call), tenant-scoped, bounded
(max concurrent per tenant), and expire after a TTL so a crashed agent or an
abandoned UI can't leak entries. A restart simply forgets in-flight
recordings; the agent's own buffer is likewise memory-only.

Agent calls go through `RemoteDispatcher.invoke` — the same placement + wire
path runs use — invoking the agent-side `cap.activity_recording` capability.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from aakaar.services.recordings.compiler import EventContractViolation, RecordedEvent, parse_events
from aakaar.workers.remote import AgentRegistry, NoAgentAvailable, RemoteDispatcher
from aakaar.workers.remote.dispatcher import RemoteExecError

logger = logging.getLogger(__name__)

RECORDING_CAPABILITY = "cap.activity_recording"

MAX_ACTIVE_PER_TENANT = 5
RECORDING_TTL_SECONDS = 2 * 60 * 60
SWEEP_INTERVAL_SECONDS = 60.0

# Per-action dispatch deadlines. Stop is generous: it ships the whole event
# buffer (up to 5000 events) back in one frame.
_START_TIMEOUT_S = 15.0
_STATUS_TIMEOUT_S = 10.0
_STOP_TIMEOUT_S = 60.0
_DISCARD_TIMEOUT_S = 15.0


class RecordingError(Exception):
    """Base class for recording-service errors."""


class RecordingNotFound(RecordingError):
    """Unknown recording id (or not visible to this tenant)."""


class RecordingLimitReached(RecordingError):
    """The tenant already has the maximum number of concurrent recordings."""


class RecordingUnavailable(RecordingError):
    """Remote execution is disabled; recordings need an agent path."""


class AgentUnavailable(RecordingError):
    """No online agent satisfies the recording placement."""


class AgentRecordingError(RecordingError):
    """The agent failed, timed out, or broke the capability contract."""


@dataclass
class RecordingEntry:
    recording_id: str
    """Server-assigned id; the REST resource id. Never agent-controlled, so a
    misbehaving agent can't collide ids across tenants."""
    agent_recording_id: str
    """The id the agent allocated at start; sent back on status/stop/discard."""
    tenant_id: uuid.UUID
    created_by: uuid.UUID
    name: str
    agent_alias: str
    max_events: int
    started_at: datetime
    expires_at: datetime
    deadline: float = field(repr=False)
    """Monotonic-clock expiry; immune to wall-clock jumps."""


class RecordingService:
    def __init__(
        self,
        *,
        dispatcher: RemoteDispatcher | None,
        agents: AgentRegistry,
        max_per_tenant: int = MAX_ACTIVE_PER_TENANT,
        ttl_seconds: float = RECORDING_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._dispatcher = dispatcher
        self._agents = agents
        self._max_per_tenant = max_per_tenant
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[str, RecordingEntry] = {}
        # Guards _entries and _pending_discard. Mutations never span an await, so
        # the lock is only held for dict/list operations.
        self._lock = threading.Lock()
        # Entries purged off any code path (TTL expiry seen by _get/list_active/
        # begin_recording, not just the sweep) queue here so their agent-side
        # capture still gets a discard. Bounded by the number of recordings that
        # ever existed; drained by the sweep and opportunistically by callers
        # that have a running loop. Keyed nowhere — order doesn't matter.
        self._pending_discard: list[RecordingEntry] = []
        # Strong refs to in-flight opportunistic drains, so they aren't GC'd
        # before they finish (asyncio only keeps weak refs to bare tasks).
        self._drain_tasks: set[asyncio.Task[int]] = set()
        self._sweep_task: asyncio.Task[None] | None = None

    # ---- lifecycle (lifespan-wired) ----------------------------------------

    async def start(self) -> None:
        if self._sweep_task is None:
            self._sweep_task = asyncio.create_task(self._sweep_loop())
            logger.debug("recordings: sweep task started")

    async def stop(self) -> None:
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweep_task
            self._sweep_task = None
        # On shutdown, best-effort tell every agent to discard its live capture
        # so a restart doesn't leave recorders running until they self-expire.
        with self._lock:
            leftover = list(self._entries.values()) + self._pending_discard
            self._entries.clear()
            self._pending_discard = []
        for entry in leftover:
            with contextlib.suppress(Exception):
                await self._discard_on_agent(entry)

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            try:
                await self.sweep_expired()
            except Exception:  # pragma: no cover - sweep must never die
                logger.exception("recordings: sweep failed")

    async def sweep_expired(self) -> int:
        """Purge newly-expired entries, then discard every queued entry on its
        agent — including ones purged on request paths since the last sweep —
        so an abandoned recording doesn't keep capturing on the agent. Returns
        the number of agent discards this call issued."""
        with self._lock:
            self._purge_expired_locked()
        return await self._drain_pending_discards()

    # ---- recording operations ----------------------------------------------

    async def begin_recording(
        self,
        *,
        tenant_id: uuid.UUID,
        created_by: uuid.UUID,
        name: str,
        agent_alias: str,
        max_events: int,
    ) -> RecordingEntry:
        dispatcher = self._require_dispatcher()
        # Resolve placement up-front for a precise, user-fixable error, and pin
        # the recording to the exact agent chosen here. The requested target may
        # be a pool label or share a prefix with sibling aliases; re-resolving on
        # every later status/stop/discard could land on a *different* agent than
        # the one actually recording. We therefore reject pool-style targets and
        # store the resolved alias, so all subsequent calls hit the same agent.
        try:
            conn = self._agents.resolve(tenant_id, agent_alias, ref=RECORDING_CAPABILITY)
        except NoAgentAvailable as e:
            raise AgentUnavailable(str(e)) from e
        resolved_alias = conn.info.alias
        if resolved_alias != agent_alias:
            raise AgentUnavailable(
                f"target {agent_alias!r} resolved to agent {resolved_alias!r} via "
                "pool matching; recording requires an exact agent alias"
            )

        now_wall = datetime.now(UTC)
        entry = RecordingEntry(
            recording_id=uuid.uuid4().hex,
            agent_recording_id="",
            tenant_id=tenant_id,
            created_by=created_by,
            name=name,
            agent_alias=resolved_alias,
            max_events=max_events,
            started_at=now_wall,
            expires_at=now_wall + timedelta(seconds=self._ttl),
            deadline=self._clock() + self._ttl,
        )
        # Reserve the slot before the (await-ing) agent call so two concurrent
        # starts can't both pass the per-tenant bound.
        with self._lock:
            self._purge_expired_locked()
            active = sum(1 for e in self._entries.values() if e.tenant_id == tenant_id)
            over_limit = active >= self._max_per_tenant
            if not over_limit:
                self._entries[entry.recording_id] = entry
        self._kick_drain()  # discard anything the purge above expired
        if over_limit:
            raise RecordingLimitReached(
                f"tenant already has {active} active recording(s); "
                f"the limit is {self._max_per_tenant}"
            )

        outputs: dict[str, Any] | None = None
        try:
            outputs = await self._invoke(
                dispatcher,
                entry,
                {"action": "start", "max_events": max_events},
                timeout_s=_START_TIMEOUT_S,
            )
            agent_recording_id = outputs.get("recording_id")
            if not isinstance(agent_recording_id, str) or not agent_recording_id:
                raise AgentRecordingError("agent did not return a recording_id on start")
            if str(outputs.get("status")) != "recording":
                raise AgentRecordingError(
                    f"agent reported status {outputs.get('status')!r} on start"
                )
        except RecordingError:
            with self._lock:
                self._entries.pop(entry.recording_id, None)
            # The dispatch may have succeeded (ok=True) yet broken the contract —
            # e.g. an unexpected status, or a recording_id present but unusable.
            # If the agent began capturing before sending that malformed reply,
            # its slot stays wedged until its own TTL backstop unless we discard
            # it. Salvage any usable recording_id from the reply and best-effort
            # discard; a pure dispatch failure leaves `outputs` None, so we only
            # attempt this when the agent actually replied with one.
            if outputs is not None:
                salvaged = outputs.get("recording_id")
                if isinstance(salvaged, str) and salvaged:
                    entry.agent_recording_id = salvaged
                    with contextlib.suppress(Exception):
                        await self._discard_on_agent(entry)
            raise
        entry.agent_recording_id = agent_recording_id
        logger.info(
            "recording started id=%s tenant=%s agent=%s max_events=%d",
            entry.recording_id,
            tenant_id,
            agent_alias,
            max_events,
        )
        return entry

    async def recording_status(
        self, *, tenant_id: uuid.UUID, recording_id: str
    ) -> tuple[RecordingEntry, dict[str, Any]]:
        """Return the entry plus the agent's live view ({status, event_count})."""
        entry = self._get(tenant_id, recording_id)
        outputs = await self._invoke(
            self._require_dispatcher(),
            entry,
            {"action": "status", "recording_id": entry.agent_recording_id},
            timeout_s=_STATUS_TIMEOUT_S,
        )
        return entry, outputs

    async def stop_recording(
        self, *, tenant_id: uuid.UUID, recording_id: str
    ) -> tuple[RecordingEntry, list[RecordedEvent], bool]:
        """Stop capture and return the validated event stream plus the agent's
        ``truncated`` flag.

        The entry is removed once the agent acknowledges the stop — even if
        the events then fail privacy validation, there is nothing left to
        manage server-side. A failed dispatch keeps the entry so the caller
        can retry or discard.

        ``truncated`` is True when the agent auto-stopped at its event cap, so
        the tail of the user's actions was never recorded; the caller surfaces
        this to the operator since the compiled draft is then incomplete.
        """
        entry = self._get(tenant_id, recording_id)
        outputs = await self._invoke(
            self._require_dispatcher(),
            entry,
            {"action": "stop", "recording_id": entry.agent_recording_id},
            timeout_s=_STOP_TIMEOUT_S,
        )
        with self._lock:
            self._entries.pop(entry.recording_id, None)
        try:
            events = parse_events(outputs.get("events"))
        except EventContractViolation:
            # Do not log or persist anything from the payload — it may carry
            # exactly the raw input the contract forbids.
            logger.error(
                "recording id=%s agent=%s returned events violating the privacy "
                "contract; rejected",
                entry.recording_id,
                entry.agent_alias,
            )
            raise
        truncated = bool(outputs.get("truncated"))
        logger.info(
            "recording stopped id=%s tenant=%s agent=%s events=%d truncated=%s",
            entry.recording_id,
            tenant_id,
            entry.agent_alias,
            len(events),
            truncated,
        )
        return entry, events, truncated

    async def discard_recording(
        self, *, tenant_id: uuid.UUID, recording_id: str
    ) -> RecordingEntry:
        """Drop the recording. The server entry always goes away; telling the
        agent is best-effort so a dead agent can't make a recording
        undeletable."""
        entry = self._get(tenant_id, recording_id)
        with self._lock:
            self._entries.pop(entry.recording_id, None)
        await self._discard_on_agent(entry)
        logger.info(
            "recording discarded id=%s tenant=%s agent=%s",
            entry.recording_id,
            tenant_id,
            entry.agent_alias,
        )
        return entry

    def list_active(self, tenant_id: uuid.UUID) -> list[RecordingEntry]:
        with self._lock:
            self._purge_expired_locked()
            active = sorted(
                (e for e in self._entries.values() if e.tenant_id == tenant_id),
                key=lambda e: e.started_at,
            )
        self._kick_drain()  # discard anything this purge expired
        return active

    # ---- internals -----------------------------------------------------------

    def _require_dispatcher(self) -> RemoteDispatcher:
        if self._dispatcher is None:
            raise RecordingUnavailable(
                "remote execution is disabled (AAKAAR_REMOTE_EXEC_ENABLED=false); "
                "activity recording requires an agent connection"
            )
        return self._dispatcher

    def _get(self, tenant_id: uuid.UUID, recording_id: str) -> RecordingEntry:
        with self._lock:
            self._purge_expired_locked()
            entry = self._entries.get(recording_id)
            # Cross-tenant ids are indistinguishable from unknown ids (404).
            found = entry is not None and entry.tenant_id == tenant_id
        self._kick_drain()  # discard anything this purge expired
        if not found:
            raise RecordingNotFound(recording_id)
        assert entry is not None  # narrowed by `found`
        return entry

    async def _invoke(
        self,
        dispatcher: RemoteDispatcher,
        entry: RecordingEntry,
        inputs: dict[str, Any],
        *,
        timeout_s: float,
    ) -> dict[str, Any]:
        try:
            return await dispatcher.invoke(
                tenant_id=entry.tenant_id,
                target=entry.agent_alias,
                ref=RECORDING_CAPABILITY,
                inputs=inputs,
                timeout_s=timeout_s,
            )
        except RemoteExecError as e:
            raise AgentRecordingError(str(e)) from e

    async def _discard_on_agent(self, entry: RecordingEntry) -> None:
        if self._dispatcher is None or not entry.agent_recording_id:
            return
        try:
            await self._dispatcher.invoke(
                tenant_id=entry.tenant_id,
                target=entry.agent_alias,
                ref=RECORDING_CAPABILITY,
                inputs={"action": "discard", "recording_id": entry.agent_recording_id},
                timeout_s=_DISCARD_TIMEOUT_S,
            )
        except RemoteExecError as e:
            logger.warning(
                "recording id=%s: agent-side discard failed (%s); the agent "
                "self-expires the session after its TTL and a later start "
                "reclaims the slot, so this cannot wedge the agent permanently",
                entry.recording_id,
                e,
            )

    def _purge_expired_locked(self) -> list[RecordingEntry]:
        """Drop expired entries and queue them for an agent-side discard.

        Called under ``_lock`` from every read/write path. Whoever purges an
        entry owns telling the agent to stop capturing; since most callers can't
        await here (they hold the lock, and some are sync), the entries land in
        ``_pending_discard`` instead. The sweep drains that queue, and async
        callers also kick an opportunistic drain so a hung sweep can't strand a
        capture for a full interval.
        """
        now = self._clock()
        expired = [e for e in self._entries.values() if e.deadline <= now]
        for e in expired:
            self._entries.pop(e.recording_id, None)
        self._pending_discard.extend(expired)
        return expired

    def _take_pending_discards(self) -> list[RecordingEntry]:
        with self._lock:
            if not self._pending_discard:
                return []
            pending = self._pending_discard
            self._pending_discard = []
            return pending

    async def _drain_pending_discards(self) -> int:
        """Best-effort discard every queued expired entry on its agent. Returns
        the number of entries it processed."""
        pending = self._take_pending_discards()
        for entry in pending:
            logger.warning(
                "recording expired id=%s tenant=%s agent=%s",
                entry.recording_id,
                entry.tenant_id,
                entry.agent_alias,
            )
            await self._discard_on_agent(entry)
        return len(pending)

    def _kick_drain(self) -> None:
        """Schedule a drain if a loop is running (sync/async request paths run
        inside one); otherwise the next sweep picks the queue up."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._drain_pending_discards())
        # Hold a strong ref so the task isn't GC'd mid-flight.
        self._drain_tasks.add(task)
        task.add_done_callback(self._on_drain_done)

    def _on_drain_done(self, task: asyncio.Task[int]) -> None:
        self._drain_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            # Per-entry discard failures are already logged in _discard_on_agent;
            # anything reaching here is an unexpected drain-loop fault. Log it so
            # the task's exception is retrieved (no warning) and stays visible.
            logger.warning(
                "recordings: opportunistic discard drain failed",
                exc_info=task.exception(),
            )


__all__ = [
    "MAX_ACTIVE_PER_TENANT",
    "RECORDING_CAPABILITY",
    "RECORDING_TTL_SECONDS",
    "AgentRecordingError",
    "AgentUnavailable",
    "RecordingEntry",
    "RecordingError",
    "RecordingLimitReached",
    "RecordingNotFound",
    "RecordingService",
    "RecordingUnavailable",
]
