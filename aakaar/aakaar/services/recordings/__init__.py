"""Activity recording: capture desktop events on a tenant agent and compile
them into a reviewable draft workflow.

  - service:  in-memory, tenant-scoped, bounded + TTL'd recording registry;
              drives the agent-side `cap.activity_recording` capability over
              the existing remote dispatch path
  - compiler: privacy-contract enforcement for the returned event stream and
              event → draft-DAG compilation
"""

from aakaar.services.recordings.compiler import (
    ALLOWED_KEY_COMBOS,
    MAX_COMPILED_NODES,
    CompiledRecording,
    EmptyRecording,
    EventContractViolation,
    RecordedEvent,
    compile_recording,
    parse_events,
)
from aakaar.services.recordings.service import (
    MAX_ACTIVE_PER_TENANT,
    RECORDING_CAPABILITY,
    RECORDING_TTL_SECONDS,
    AgentRecordingError,
    AgentUnavailable,
    RecordingEntry,
    RecordingError,
    RecordingLimitReached,
    RecordingNotFound,
    RecordingService,
    RecordingUnavailable,
)

__all__ = [
    "ALLOWED_KEY_COMBOS",
    "MAX_ACTIVE_PER_TENANT",
    "MAX_COMPILED_NODES",
    "RECORDING_CAPABILITY",
    "RECORDING_TTL_SECONDS",
    "AgentRecordingError",
    "AgentUnavailable",
    "CompiledRecording",
    "EmptyRecording",
    "EventContractViolation",
    "RecordedEvent",
    "RecordingEntry",
    "RecordingError",
    "RecordingLimitReached",
    "RecordingNotFound",
    "RecordingService",
    "RecordingUnavailable",
    "compile_recording",
    "parse_events",
]
