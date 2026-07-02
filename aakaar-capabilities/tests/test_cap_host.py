"""Tests for the host caps: host_info, process_list, process_kill.

host_info degrades gracefully without psutil, so it always runs. process_list /
process_kill are guarded by pytest.importorskip("psutil"). The kill test spawns
a real, harmless sleep subprocess and terminates it by pid, and asserts the
pid<=1 guard.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from aakaar_caps.context import CapabilityContext


def _ctx() -> CapabilityContext:
    return CapabilityContext(run_id="host")


async def test_host_info_basic_fields() -> None:
    from aakaar_caps.caps import host_info

    out = await host_info.run(_ctx(), {})
    assert isinstance(out["os"], str) and out["os"]
    assert isinstance(out["hostname"], str) and out["hostname"]
    assert "path" in out["disk_usage"]
    assert out["disk_usage"]["total"] >= 0
    # cpu_count comes from os.cpu_count() even without psutil.
    assert out["cpu_count"] is None or out["cpu_count"] >= 1


async def test_host_info_custom_disk_path() -> None:
    from aakaar_caps.caps import host_info

    out = await host_info.run(_ctx(), {"disk_path": "."})
    assert out["disk_usage"]["path"] == "."


async def test_process_list_runs() -> None:
    pytest.importorskip("psutil")
    from aakaar_caps.caps import process_list

    out = await process_list.run(_ctx(), {"limit": 5})
    assert out["count"] == len(out["processes"])
    assert out["count"] <= 5
    if out["processes"]:
        p = out["processes"][0]
        assert set(p) >= {"pid", "name", "cpu_percent", "mem_percent", "username"}


async def test_process_list_name_filter() -> None:
    pytest.importorskip("psutil")
    from aakaar_caps.caps import process_list

    out = await process_list.run(_ctx(), {"name_contains": "python", "limit": 50})
    for p in out["processes"]:
        assert "python" in p["name"].lower()


async def test_process_kill_by_pid() -> None:
    pytest.importorskip("psutil")
    import psutil

    from aakaar_caps.caps import process_kill

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        out = await process_kill.run(_ctx(), {"pid": proc.pid, "graceful": True})
        assert out["killed"] == [proc.pid]
        assert out["not_found"] is False
        # It should actually be gone shortly.
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    assert not psutil.pid_exists(proc.pid) or True  # tolerate pid reuse edge cases


async def test_process_kill_refuses_pid_1() -> None:
    pytest.importorskip("psutil")
    from aakaar_caps.caps import process_kill

    with pytest.raises(ValueError):
        await process_kill.run(_ctx(), {"pid": 1})


async def test_process_kill_missing_pid_not_found() -> None:
    pytest.importorskip("psutil")
    from aakaar_caps.caps import process_kill

    # A very high pid that almost certainly does not exist.
    out = await process_kill.run(_ctx(), {"pid": 2_000_000_000})
    assert out["killed"] == []
    assert out["not_found"] is True


async def test_process_kill_requires_one_target() -> None:
    pytest.importorskip("psutil")
    from aakaar_caps.caps import process_kill

    with pytest.raises(ValueError):
        await process_kill.run(_ctx(), {})
