"""process.py: state file semantics + read-only liveness, no real kills.

These tests only READ process state (or write throwaway state whose pids can
never match our own live processes), so nothing real is ever signalled here.
"""

import json


def test_load_returns_none_without_state(stack):
    assert stack.process._load() is None


def test_load_clears_stale_boot_time(stack):
    stack.paths.RUN.mkdir(parents=True, exist_ok=True)
    stack.paths.STATE.write_text(json.dumps({"boot_time": "1"}))
    assert stack.process._load() is None
    assert not stack.paths.STATE.exists()  # stale state self-heals


def test_load_accepts_matching_boot_time(stack):
    stack.paths.RUN.mkdir(parents=True, exist_ok=True)
    bt = stack.process._boot_time()
    stack.paths.STATE.write_text(json.dumps({"boot_time": bt, "klipper": 999999}))
    state = stack.process._load()
    assert state is not None
    assert state["klipper"] == 999999


def test_running_reports_all_down_for_unknown_pids(stack):
    """A state file whose pids don't exist must read as a stopped stack
    (dict of False), never a crash and never None-ambiguity."""
    stack.paths.RUN.mkdir(parents=True, exist_ok=True)
    bt = stack.process._boot_time()
    data = {"boot_time": bt}
    for i, name in enumerate(stack.process.SERVICES):
        data[name] = 999_999 + i  # pids that don't exist
    stack.paths.STATE.write_text(json.dumps(data))
    state = stack.process.running()
    assert state is not None
    assert state == {name: False for name in stack.process.SERVICES}


def test_running_none_without_state(stack):
    assert stack.process.running() is None


def test_stop_no_state_returns_false(stack):
    assert stack.process.stop() is False


def test_kill_all_empty_when_nothing_tracked(stack):
    assert stack.process.kill_all() == []


def test_boot_time_parses(stack, monkeypatch):
    import subprocess

    def fake_run(cmd, **kw):
        out = b"{ sec = 1791932927, usec = 560431 } Tue Oct 13 16:08:47 2026\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out)

    monkeypatch.setattr(stack.process, "_boot_cache", [0.0, None])
    monkeypatch.setattr(stack.process.subprocess, "run", fake_run)
    assert stack.process._boot_time() == "1791932927"
