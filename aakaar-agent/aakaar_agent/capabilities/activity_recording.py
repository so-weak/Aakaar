"""cap.activity_recording — record desktop activity as a redacted event trace.

inputs:  {action: "start"|"status"|"stop"|"discard", recording_id?: str,
          max_events?: int (default 2000, capped at 5000)}
outputs: {recording_id: str, status: "recording"|"stopped"|"discarded"|"idle",
          event_count: int, truncated: bool, events?: [Event] (on "stop" only)}

Event: {t: int (ms since start), kind: "click"|"scroll"|"key"|"text"|"window",
        data: object} where data is
  click:  {x: int, y: int, button: "left"|"right"|"middle"}
  scroll: {dx: int, dy: int}
  key:    {combo: str}   — only combos in _KEY_ALLOWLIST, ever
  text:   {count: int}   — count of redacted keystrokes, never the characters
  window: {title: str (<=300), app: str (<=120)}

PRIVACY HARD RULE: raw keystrokes never leave this process. Only allowlisted
navigation/hotkey combos become "key" events; every other key press (printable
characters, unrecognised keys, any other chord) is aggregated into "text"
events carrying only a count. On macOS the cmd modifier is normalised to ctrl
so e.g. cmd+c records as the cross-platform "ctrl+c".

One recording per agent process: idle -> recording -> stopped. Hitting the
event cap auto-stops the listeners and sets ``truncated``; the buffered events
stay in memory until the caller collects them with "stop" or drops them with
"discard". Consecutive scroll notches within 250 ms coalesce into one event so
a flick of the wheel doesn't eat the buffer.

Recovery: the slot is never permanently wedged. A session self-expires after a
TTL, and a "start" reclaims the slot from a session that is no longer actively
capturing (stopped, discarded, auto-stopped at the cap, or past its TTL) by
discarding it first. So a server that crashes or forgets an in-flight recording
(its registry is memory-only) does not block all future recordings on this
agent. A session that is genuinely still recording, within its TTL, is never
clobbered by a new start — the caller must stop or discard it explicitly.
"""

from __future__ import annotations

import platform
import threading
import time
import uuid
from typing import Any

REF = "cap.activity_recording"
VERSION = "1"
GUI = True

_DEFAULT_MAX_EVENTS = 2000
_HARD_MAX_EVENTS = 5000
_POLL_INTERVAL_S = 0.4  # foreground-window poll + idle text flush cadence
_TEXT_IDLE_FLUSH_S = 1.0
_SCROLL_COALESCE_MS = 250
# A session self-expires after this long so a server that crashes or forgets a
# recording (its in-memory registry is wiped on restart) can never wedge the
# agent in a permanent 'recording'/'stopped' state. Kept longer than the
# server's own recording TTL so, in the normal path, the server's discard wins
# and this is only a backstop. An expired session is auto-discarded: its events
# are dropped (the server can no longer collect them) and the slot frees up.
_SESSION_TTL_S = 3 * 60 * 60

_MODIFIERS = frozenset({"ctrl", "alt", "shift", "cmd"})
_MOD_ORDER = ("ctrl", "alt", "shift", "cmd")
_KEY_ALLOWLIST = frozenset(
    {
        "enter",
        "tab",
        "esc",
        "ctrl+a",
        "ctrl+c",
        "ctrl+v",
        "ctrl+s",
        "ctrl+tab",
        "alt+tab",
        "shift+tab",
    }
)

# Single active recording per agent process.
_slot_lock = threading.Lock()
_active: _Recorder | None = None


def _foreground_window() -> tuple[str, str]:
    """(title, app) of the focused window via pygetwindow (same access as
    cap.window_manage). Best effort: pygetwindow has no app name on most
    platforms and limited macOS support, so failures degrade to ("", "")."""
    try:
        import pygetwindow as gw

        win = gw.getActiveWindow()
    except Exception:
        return "", ""
    if win is None:
        return "", ""
    title = win if isinstance(win, str) else getattr(win, "title", "") or ""
    return str(title), ""


class _Recorder:
    """Recording session. Event ingestion (``record_*``) is decoupled from the
    pynput/pygetwindow wiring (``attach``) so tests can inject synthetic events
    without any GUI library."""

    def __init__(
        self, recording_id: str, max_events: int, ttl_s: float = _SESSION_TTL_S
    ) -> None:
        self.recording_id = recording_id
        self.max_events = max_events
        self.state = "recording"
        self.truncated = False
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._deadline = self._t0 + ttl_s
        self._events: list[dict[str, Any]] = []
        self._mods: set[str] = set()
        self._pending_text = 0
        self._last_key_at = 0.0
        self._last_window: tuple[str, str] | None = None
        self._stop_evt = threading.Event()
        self._listeners: list[Any] = []
        self._poller: threading.Thread | None = None

    # -- event ingestion (thread-safe; callable with synthetic events) -------

    def record_click(self, x: int, y: int, button: str) -> None:
        if button not in ("left", "right", "middle"):
            button = "left"
        with self._lock:
            if self.state != "recording":
                return
            self._flush_text_locked()
            self._push_locked("click", {"x": int(x), "y": int(y), "button": button})

    def record_scroll(self, dx: int, dy: int) -> None:
        with self._lock:
            if self.state != "recording":
                return
            self._flush_text_locked()
            now_ms = self._now_ms()
            if self._events:
                last = self._events[-1]
                if last["kind"] == "scroll" and now_ms - last["t"] <= _SCROLL_COALESCE_MS:
                    last["data"]["dx"] += int(dx)
                    last["data"]["dy"] += int(dy)
                    return
            self._push_locked("scroll", {"dx": int(dx), "dy": int(dy)})

    def record_key_down(self, name: str | None) -> None:
        """``name`` is a normalised key name: a modifier ("ctrl"/"alt"/"shift"/
        "cmd"), a special key ("enter"/"tab"/"esc"/...), a single character, or
        None for keys we can't identify. Anything that doesn't form an
        allowlisted combo only bumps the redacted-text counter."""
        with self._lock:
            if self.state != "recording":
                return
            if name in _MODIFIERS:
                self._mods.add(name)
                return
            combo = self._combo_locked(name)
            if combo in _KEY_ALLOWLIST:
                self._flush_text_locked()
                self._push_locked("key", {"combo": combo})
            else:
                self._pending_text += 1
                self._last_key_at = time.monotonic()

    def record_key_up(self, name: str | None) -> None:
        with self._lock:
            if name in _MODIFIERS:
                self._mods.discard(name)

    def record_window(self, title: str, app: str) -> None:
        title, app = (title or "")[:300], (app or "")[:120]
        with self._lock:
            if self.state != "recording":
                return
            current = (title, app)
            if current == self._last_window or not (title or app):
                return
            self._last_window = current
            self._flush_text_locked()
            self._push_locked("window", {"title": title, "app": app})

    def flush_idle_text(self) -> None:
        with self._lock:
            if (
                self.state == "recording"
                and self._pending_text
                and time.monotonic() - self._last_key_at >= _TEXT_IDLE_FLUSH_S
            ):
                self._flush_text_locked()

    # -- session control ------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "recording_id": self.recording_id,
                "status": self.state,
                "event_count": len(self._events),
                "truncated": self.truncated,
            }

    def stop(self) -> list[dict[str, Any]]:
        with self._lock:
            if self.state == "recording":
                self._flush_text_locked()
                self.state = "stopped"
            self._stop_evt.set()
            events = list(self._events)
        self._teardown()
        return events

    def discard(self) -> None:
        with self._lock:
            self.state = "discarded"
            self._events.clear()
            self._pending_text = 0
            self._stop_evt.set()
        self._teardown()

    def is_expired(self) -> bool:
        return time.monotonic() >= self._deadline

    def expire_if_stale(self) -> bool:
        """Drop the session if it has outlived its TTL. Returns True when it
        expired on this call. Used as a backstop against a server that crashed
        or forgot the recording and so never sends stop/discard: without this
        the listeners would keep capturing — and the slot stay occupied —
        forever."""
        if not self.is_expired():
            return False
        self.discard()
        return True

    # -- internals (lock held where suffixed _locked) -------------------------

    def _now_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def _combo_locked(self, name: str | None) -> str | None:
        if not name:
            return None
        mods = [m for m in _MOD_ORDER if m in self._mods]
        return "+".join([*mods, name])

    def _push_locked(self, kind: str, data: dict[str, Any]) -> None:
        self._events.append({"t": self._now_ms(), "kind": kind, "data": data})
        if len(self._events) >= self.max_events:
            # Hard cap: auto-stop. The poller (if attached) notices the stop
            # event and tears the listeners down; callbacks no-op meanwhile.
            self.state = "stopped"
            self.truncated = True
            self._pending_text = 0
            self._stop_evt.set()

    def _flush_text_locked(self) -> None:
        if self._pending_text and self.state == "recording":
            count, self._pending_text = self._pending_text, 0
            self._push_locked("text", {"count": count})

    # -- live wiring (pynput + window poller); not used by tests --------------

    def attach(self) -> None:
        try:
            from pynput import keyboard, mouse
        except Exception as e:  # pragma: no cover - needs the record extra
            raise RuntimeError(
                "activity_recording requires the 'record' extra (pynput)"
            ) from e

        names: dict[Any, str] = {}
        for attr, name in (
            ("ctrl", "ctrl"), ("ctrl_l", "ctrl"), ("ctrl_r", "ctrl"),
            ("alt", "alt"), ("alt_l", "alt"), ("alt_r", "alt"), ("alt_gr", "alt"),
            ("shift", "shift"), ("shift_l", "shift"), ("shift_r", "shift"),
            ("cmd", "cmd"), ("cmd_l", "cmd"), ("cmd_r", "cmd"),
            ("enter", "enter"), ("tab", "tab"), ("esc", "esc"),
        ):
            key = getattr(keyboard.Key, attr, None)
            if key is not None:
                names[key] = name
        darwin = platform.system() == "Darwin"

        def key_name(key: Any) -> str | None:
            name = names.get(key)
            if name is not None:
                # cmd is the primary modifier on macOS; record it as ctrl so
                # cmd+c becomes the cross-platform allowlisted "ctrl+c".
                return "ctrl" if darwin and name == "cmd" else name
            char = getattr(key, "char", None)
            if char and len(char) == 1:
                code = ord(char)
                if 1 <= code <= 26:  # ctrl-modified letters arrive as \x01..\x1a
                    return chr(code + 96)
                return char.lower()
            return None  # unidentified key: counts as redacted activity

        def on_click(x: float, y: float, button: Any, pressed: bool) -> None:
            if pressed:
                self.record_click(int(x), int(y), getattr(button, "name", "left"))

        def on_scroll(_x: float, _y: float, dx: int, dy: int) -> None:
            self.record_scroll(dx, dy)

        self._listeners = [
            mouse.Listener(on_click=on_click, on_scroll=on_scroll),
            keyboard.Listener(
                on_press=lambda k: self.record_key_down(key_name(k)),
                on_release=lambda k: self.record_key_up(key_name(k)),
            ),
        ]
        for listener in self._listeners:
            listener.start()
        self._poller = threading.Thread(
            target=self._poll, name="aakaar-recording-poll", daemon=True
        )
        self._poller.start()

    def _poll(self) -> None:  # pragma: no cover - thread loop, exercised live
        while not self._stop_evt.wait(_POLL_INTERVAL_S):
            if self.expire_if_stale():
                # expire_if_stale -> discard sets the stop event; the next wait
                # returns immediately and we fall through to teardown.
                continue
            self.record_window(*_foreground_window())
            self.flush_idle_text()
        self._stop_listeners()

    def _stop_listeners(self) -> None:
        listeners, self._listeners = self._listeners, []
        for listener in listeners:
            try:
                listener.stop()
            except Exception:  # pragma: no cover - defensive teardown
                pass

    def _teardown(self) -> None:
        self._stop_listeners()
        poller, self._poller = self._poller, None
        if poller is not None and poller is not threading.current_thread():
            poller.join(timeout=2)


def _is_reclaimable(recorder: _Recorder) -> bool:
    """True when ``start`` may take over ``recorder``'s slot without losing an
    in-flight capture: it is no longer actively recording (already stopped or
    discarded — including the cap-driven auto-stop the server never collected),
    or it has outlived its TTL. An active, un-expired session is never
    reclaimed."""
    return recorder.state != "recording" or recorder.is_expired()


def _parse_max_events(value: Any) -> int:
    if value is None:
        return _DEFAULT_MAX_EVENTS
    try:
        max_events = int(value)
    except (TypeError, ValueError):
        raise ValueError("max_events must be an integer") from None
    if max_events < 1:
        raise ValueError("max_events must be >= 1")
    return min(max_events, _HARD_MAX_EVENTS)


async def run(inputs: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
    global _active
    action = inputs.get("action")
    if action not in ("start", "status", "stop", "discard"):
        raise ValueError("action must be start, status, stop, or discard")
    requested_id = inputs.get("recording_id")

    if action == "start":
        max_events = _parse_max_events(inputs.get("max_events"))
        with _slot_lock:
            stale = _active
            if stale is not None and _is_reclaimable(stale):
                # The previous session is no longer actively capturing (it was
                # stopped/discarded, or auto-stopped at the event cap, or has
                # outlived its TTL) but the server never collected it — e.g. the
                # server restarted and forgot the recording. Reclaim the slot so
                # the agent isn't wedged until a process restart. A session that
                # is genuinely still 'recording' (and not past TTL) is *not*
                # clobbered; the caller must stop/discard it first.
                stale.discard()  # idempotent; drops any uncollected buffer
                _active = stale = None
            if stale is not None:
                raise RuntimeError(
                    f"recording {stale.recording_id} is {stale.state}; "
                    "stop or discard it before starting another"
                )
            started = _Recorder(uuid.uuid4().hex, max_events)
            try:
                started.attach()  # raises cleanly if pynput is missing
            except BaseException:
                started.discard()  # don't leak partially-started listeners
                raise
            _active = started
        return {
            "recording_id": started.recording_id,
            "status": "recording",
            "event_count": 0,
            "truncated": False,
        }

    with _slot_lock:
        recorder = _active
        if recorder is not None and recorder.expire_if_stale():
            # A session that outlived its TTL acts as if it were already gone:
            # status reports idle, stop/discard report no active recording.
            _active = recorder = None
    if recorder is None:
        if action == "status":
            return {
                "recording_id": str(requested_id or ""),
                "status": "idle",
                "event_count": 0,
                "truncated": False,
            }
        raise RuntimeError("no active recording")
    if requested_id and requested_id != recorder.recording_id:
        raise ValueError(f"recording_id mismatch: active is {recorder.recording_id}")

    if action == "status":
        return recorder.status()
    if action == "stop":
        events = recorder.stop()
        with _slot_lock:
            if _active is recorder:
                _active = None
        return {
            "recording_id": recorder.recording_id,
            "status": "stopped",
            "event_count": len(events),
            "truncated": recorder.truncated,
            "events": events,
        }
    recorder.discard()
    with _slot_lock:
        if _active is recorder:
            _active = None
    return {
        "recording_id": recorder.recording_id,
        "status": "discarded",
        "event_count": 0,
        "truncated": recorder.truncated,
    }
