"""cap.web_form_fill moved to aakaar_caps.caps.web_form_fill (shared, runs on
server or agent). Thin server adapter: re-exports the shared SPEC/run as
`definition`/`handler` plus the internals the tests reference.
"""

from aakaar.capabilities._shared import definition_from_spec, server_handler_for
from aakaar_caps.caps.web_form_fill import (  # noqa: F401
    CAP_REF,
    SPEC,
    _Inputs,
    run,
)

definition = definition_from_spec(SPEC)
handler = server_handler_for(SPEC, run)

__all__ = ["CAP_REF", "definition", "handler", "_Inputs"]
