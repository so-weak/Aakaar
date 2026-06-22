"""cap.web_scrape moved to aakaar_caps.caps.web_scrape (shared, runs on server
or agent). Thin server adapter: re-exports the shared SPEC/run as
`definition`/`handler` plus the internals the tests reference.
"""

from aakaar.capabilities._shared import definition_from_spec, server_handler_for
from aakaar_caps.caps.web_scrape import (  # noqa: F401
    CAP_REF,
    SPEC,
    _coerce_tables,
    _Inputs,
    _parse_llm_json,
    run,
)

definition = definition_from_spec(SPEC)
handler = server_handler_for(SPEC, run)

__all__ = ["CAP_REF", "definition", "handler", "_Inputs", "_coerce_tables", "_parse_llm_json"]
