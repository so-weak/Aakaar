"""cap.file_download moved to aakaar_caps.caps.file_download (shared, runs on
server or agent; its discovery helper moved alongside).

This module re-exports the shared SPEC/run as the server's `definition`/`handler`
(so the capability loader registers it normally and existing imports keep
working) plus the internals the tests reference. The logic lives once in the
shared module; this is a thin server adapter.
"""

from aakaar.capabilities._shared import definition_from_spec, server_handler_for
from aakaar_caps.caps.file_download import (  # noqa: F401
    CAP_REF,
    SPEC,
    _Inputs,
    run,
)

definition = definition_from_spec(SPEC)
handler = server_handler_for(SPEC, run)

__all__ = ["CAP_REF", "definition", "handler", "_Inputs"]
