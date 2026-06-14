"""cap.activity_recording — state machine + redaction, with synthetic events.

No pynput here: tests drive ``_Recorder.record_*`` directly and stub
``attach()`` when exercising the run() state machine, so the suite passes on a
headless box without the 'record' extra.
"""

from __future__ import annotations

import json

import pytest

from aakaar_agent.capabilities import activity_recording as rec


@pytest.fixture(autouse=True)
def _clean_slot():
    rec._active = None
    yield
    if rec._active is not None:
        rec._active.discard()
        rec._active = None


@pytest.fixture
def no_listeners(monkeypatch):
    monkeypatch.setattr(rec._Recorder, "attach", lambda self: None)


def _recorder(max_events: int = 100) -> rec._Recorder:
    return rec._Recorder("rid", max_events)


# -- redaction -----------------------------------------------------------------


def test_printable_keys_aggregate_into_text_counts() -> None:
    r = _recorder()
    for ch in "hunter2!":
        r.record_key_down(ch)
        r.record_key_up(ch)
    r.record_click(10, 20, "left")
    events = r.stop()
    assert [e["kind"] for e in events] == ["text", "click"]
    assert events[0]["data"] == {"count": 8}
    # the characters themselves must not appear anywhere in the trace
    assert "hunter" not in json.dumps(events)


def test_allowlisted_combos_become_key_events() -> None:
    r = _recorder()
    r.record_key_down("ctrl")
    r.record_key_down("c")
    r.record_key_up("ctrl")
    r.record_key_down("enter")
    r.record_key_down("alt")
    r.record_key_down("tab")
    r.record_key_up("alt")
    events = r.stop()
    assert [e["data"]["combo"] for e in events] == ["ctrl+c", "enter", "alt+tab"]


def test_non_allowlisted_combos_are_redacted_not_leaked() -> None:
    r = _recorder()
    r.record_key_down("ctrl")
    r.record_key_down("x")  # ctrl+x: not allowlisted
    r.record_key_up("ctrl")
    r.record_key_down("shift")
    r.record_key_down("a")  # shift+a: a capital letter, must never leak
    r.record_key_up("shift")
    r.record_key_down(None)  # unidentifiable key
    events = r.stop()
    assert [e["kind"] for e in events] == ["text"]
    assert events[0]["data"] == {"count": 3}
    dumped = json.dumps(events)
    assert "ctrl+x" not in dumped and "shift" not in dumped


def test_pending_text_flushes_before_interleaved_events() -> None:
    r = _recorder()
    r.record_key_down("a")
    r.record_key_down("b")
    r.record_key_down("ctrl")
    r.record_key_down("s")
    r.record_key_up("ctrl")
    r.record_key_down("c")
    events = r.stop()
    assert [e["kind"] for e in events] == ["text", "key", "text"]
    assert events[0]["data"]["count"] == 2
    assert events[1]["data"]["combo"] == "ctrl+s"
    assert events[2]["data"]["count"] == 1


def test_window_events_dedupe_and_truncate() -> None:
    r = _recorder()
    r.record_window("T" * 500, "A" * 200)
    r.record_window("T" * 500, "A" * 200)  # same window again: no event
    r.record_window("", "")  # empty focus: no event
    r.record_window("other", "app")
    events = r.stop()
    assert [e["kind"] for e in events] == ["window", "window"]
    assert len(events[0]["data"]["title"]) == 300
    assert len(events[0]["data"]["app"]) == 120


def test_scroll_coalesces_quick_notches() -> None:
    r = _recorder()
    r.record_scroll(0, -1)
    r.record_scroll(0, -2)
    r.record_scroll(1, 0)
    events = r.stop()
    assert len(events) == 1
    assert events[0]["data"] == {"dx": 1, "dy": -3}


def test_event_shape_matches_contract() -> None:
    r = _recorder()
    r.record_click(5, 6, "right")
    r.record_scroll(0, 2)
    events = r.stop()
    for event in events:
        assert set(event) == {"t", "kind", "data"}
        assert isinstance(event["t"], int) and event["t"] >= 0
    assert events[0]["data"] == {"x": 5, "y": 6, "button": "right"}


# -- cap / auto-stop -----------------------------------------------------------


def test_cap_auto_stops_with_truncated_flag() -> None:
    r = _recorder(max_events=3)
    for i in range(10):
        r.record_click(i, i, "left")
    assert r.state == "stopped"
    assert r.truncated is True
    assert r.status()["event_count"] == 3
    r.record_key_down("enter")  # ignored after auto-stop
    assert len(r.stop()) == 3


# -- run() state machine -------------------------------------------------------


async def test_start_status_stop_cycle(no_listeners) -> None:
    out = await rec.run({"action": "start"}, {})
    rid = out["recording_id"]
    assert out["status"] == "recording" and out["event_count"] == 0
    rec._active.record_key_down("enter")
    status = await rec.run({"action": "status", "recording_id": rid}, {})
    assert status == {
        "recording_id": rid,
        "status": "recording",
        "event_count": 1,
        "truncated": False,
    }
    stopped = await rec.run({"action": "stop"}, {})
    assert stopped["status"] == "stopped"
    assert stopped["event_count"] == 1
    assert stopped["events"][0]["data"] == {"combo": "enter"}
    idle = await rec.run({"action": "status"}, {})
    assert idle["status"] == "idle" and idle["event_count"] == 0


async def test_single_session_per_process(no_listeners) -> None:
    await rec.run({"action": "start"}, {})
    with pytest.raises(RuntimeError, match="stop or discard"):
        await rec.run({"action": "start"}, {})
    await rec.run({"action": "discard"}, {})
    out = await rec.run({"action": "start"}, {})  # free again after discard
    assert out["status"] == "recording"


async def test_start_reclaims_orphaned_auto_stopped_session(no_listeners) -> None:
    """A session the server forgot (here: auto-stopped at the event cap, never
    collected) must not wedge the agent. A new start reclaims the slot instead
    of 502-ing forever."""
    await rec.run({"action": "start", "max_events": 1}, {})
    rec._active.record_click(1, 1, "left")  # hits the cap -> auto-stop
    assert rec._active.state == "stopped"
    orphan_id = rec._active.recording_id

    out = await rec.run({"action": "start"}, {})  # reclaims rather than raising
    assert out["status"] == "recording"
    assert rec._active.recording_id != orphan_id


async def test_start_does_not_clobber_an_active_session(no_listeners) -> None:
    """A genuinely-active, un-expired recording is never silently taken over —
    the caller must stop/discard it first. Guards against a buggy/racing server
    killing an in-flight capture."""
    await rec.run({"action": "start"}, {})
    rec._active.record_click(1, 1, "left")
    active_id = rec._active.recording_id
    with pytest.raises(RuntimeError, match="stop or discard"):
        await rec.run({"action": "start"}, {})
    assert rec._active.recording_id == active_id  # untouched


async def test_start_reclaims_session_past_ttl(no_listeners, monkeypatch) -> None:
    """Even a session still nominally 'recording' is reclaimable once it has
    outlived its TTL — the backstop for a server that crashed before stopping."""
    await rec.run({"action": "start"}, {})
    assert rec._active.state == "recording"
    # Force the existing session past its deadline.
    rec._active._deadline = rec.time.monotonic() - 1
    assert rec._active.is_expired()

    out = await rec.run({"action": "start"}, {})
    assert out["status"] == "recording"
    assert not rec._active.is_expired()


async def test_status_on_expired_session_reports_idle(no_listeners) -> None:
    """A session past its TTL behaves as if gone: status -> idle, stop/discard
    -> no active recording. The slot is freed on the next call."""
    await rec.run({"action": "start"}, {})
    rec._active._deadline = rec.time.monotonic() - 1
    idle = await rec.run({"action": "status"}, {})
    assert idle["status"] == "idle" and idle["event_count"] == 0
    assert rec._active is None
    with pytest.raises(RuntimeError, match="no active recording"):
        await rec.run({"action": "stop"}, {})


def test_expire_if_stale_discards_and_frees() -> None:
    r = rec._Recorder("rid", 100, ttl_s=0.0)
    r.record_click(1, 1, "left")
    assert r.expire_if_stale() is True
    assert r.state == "discarded"
    assert r.stop() == []  # buffer dropped
    # A fresh, long-TTL session is not stale.
    r2 = rec._Recorder("rid2", 100, ttl_s=3600)
    assert r2.expire_if_stale() is False
    assert r2.state == "recording"


async def test_discard_drops_events_and_frees_slot(no_listeners) -> None:
    await rec.run({"action": "start"}, {})
    rec._active.record_click(1, 1, "left")
    out = await rec.run({"action": "discard"}, {})
    assert out["status"] == "discarded" and out["event_count"] == 0
    with pytest.raises(RuntimeError, match="no active recording"):
        await rec.run({"action": "stop"}, {})


async def test_recording_id_mismatch_rejected(no_listeners) -> None:
    await rec.run({"action": "start"}, {})
    with pytest.raises(ValueError, match="mismatch"):
        await rec.run({"action": "stop", "recording_id": "bogus"}, {})


async def test_stop_without_session_raises(no_listeners) -> None:
    with pytest.raises(RuntimeError, match="no active recording"):
        await rec.run({"action": "stop"}, {})


async def test_bad_action_and_max_events_rejected(no_listeners) -> None:
    with pytest.raises(ValueError, match="action"):
        await rec.run({"action": "pause"}, {})
    with pytest.raises(ValueError, match="max_events"):
        await rec.run({"action": "start", "max_events": 0}, {})
    with pytest.raises(ValueError, match="max_events"):
        await rec.run({"action": "start", "max_events": "lots"}, {})


async def test_max_events_clamped_to_hard_cap(no_listeners) -> None:
    await rec.run({"action": "start", "max_events": 999999}, {})
    assert rec._active.max_events == rec._HARD_MAX_EVENTS


async def test_start_requires_record_extra() -> None:
    try:
        import pynput  # noqa: F401

        pytest.skip("pynput present; cannot exercise the graceful refusal")
    except Exception:
        pass
    with pytest.raises(RuntimeError, match="record"):
        await rec.run({"action": "start"}, {})
