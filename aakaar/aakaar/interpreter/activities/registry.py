"""Activity registry.

Maps DAG node refs to concrete async handlers. Built-in non-browser
handlers live in sibling modules (http, file, storage); browser handlers
join in PR 5; capabilities are loaded by their own loader.

Control nodes (`control.wait`, `human.prompt`) are NOT in this registry —
they're interpreted by the executor directly because they have non-activity
semantics (timers, signals).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aakaar.interpreter.activities.types import ActivityContext

logger = logging.getLogger(__name__)
ActivityHandler = Callable[[ActivityContext, dict[str, Any]], Awaitable[dict[str, Any]]]


class ActivityRegistry:
    """Ref-keyed map of activity handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, ActivityHandler] = {}

    def register(self, ref: str, handler: ActivityHandler) -> None:
        if ref in self._handlers:
            raise ValueError(f"activity handler already registered for {ref!r}")
        self._handlers[ref] = handler
        logger.debug("activity registered ref=%s", ref)

    def get(self, ref: str) -> ActivityHandler | None:
        return self._handlers.get(ref)

    def __contains__(self, ref: object) -> bool:
        return isinstance(ref, str) and ref in self._handlers

    def refs(self) -> list[str]:
        return sorted(self._handlers)


def build_default_activities() -> ActivityRegistry:
    """Returns an ActivityRegistry preloaded with all built-in primitives.

    Browser primitives are registered too, but they raise at runtime if the
    deployment does not configure an `ActivityContext.browser_pool`.
    """
    from aakaar.interpreter.activities import browser as _browser
    from aakaar.interpreter.activities import document as _document
    from aakaar.interpreter.activities import file as _file
    from aakaar.interpreter.activities import http as _http
    from aakaar.interpreter.activities import http_extended as _http_extended
    from aakaar.interpreter.activities import storage as _storage
    from aakaar.interpreter.activities import time_ as _time

    reg = ActivityRegistry()
    _http.register_into(reg)
    _http_extended.register_into(reg)
    _file.register_into(reg)
    _document.register_into(reg)
    _storage.register_into(reg)
    _browser.register_into(reg)
    _time.register_into(reg)
    logger.info("default activities registered (count=%d)", len(reg.refs()))
    return reg
