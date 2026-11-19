"""Offline harness for the battery watchdog.

Three things an offline test of this feature must never do: read this Mac's
real power state, talk to the developer's running Moonraker, or signal a
process. The `guard` fixture builds on tests/conftest.py's `stack` (a throwaway
KLIPPERFORMAC_HOME/DATA, which is also what keeps every guard file out of
~/.klipperformac) and replaces exactly those three with recorders, so "pause
once, not per tick" is a fact visible in the recorded call list rather than a
claim in a comment.

`no_network` is the hard proof of the second: any socket creation aborts the
test. (The live-tier test in this folder carries @pytest.mark.live, so the
default suite never runs it.)
"""
import socket

import pytest
from hypothesis import HealthCheck, settings

BATTERY_ENV = ("KLIPPERFORMAC_BATTERY_FILE", "KLIPPERFORMAC_BATTERY_PERCENT",
               "KLIPPERFORMAC_POWER_SOURCE", "KLIPPERFORMAC_BATTERY_POLL")

# Shared hypothesis profile for this folder: no wall-clock deadline (the state
# machine is pure and fast, the runner should not police it), a bounded example
# count, and function-scoped fixtures are the whole point here.
HYP = dict(deadline=None, max_examples=200,
           suppress_health_check=[HealthCheck.function_scoped_fixture])


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Hard offline proof: any socket creation aborts the test."""
    seen = []

    def _boom(*args, **kwargs):
        seen.append((args, kwargs))
        raise AssertionError("BUG IN TEST: the offline battery suite must not "
                            "open a socket (the live tier is @pytest.mark.live)")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    return seen


@pytest.fixture
def guard(stack, monkeypatch):
    """The guard modules on a throwaway home, with the outside world recorded."""
    for var in BATTERY_ENV:
        monkeypatch.delenv(var, raising=False)
    bg = stack.batteryguard
    # A real install has these folders; the settings/ledger/state/log files
    # below are only meaningful inside them.
    stack.paths.ensure_dirs()
    rec = {"post": [], "get": [], "spawn": [], "exec": [], "stop": 0,
           "signal": []}

    def fake_post(path, timeout=10):
        rec["post"].append(path)
        return True, None

    def fake_get(path, timeout=3):
        rec["get"].append(path)
        if "objects/list" in path:
            return {"result": {"objects": ["print_stats", "pause_resume"]}}
        return {"result": {"status": {"print_stats": {"state": "printing"}}}}

    def fake_exec(argv, timeout=5.0):
        """Every pmset/ioreg read of the offline suite lands here: a test can
        drive read_power() without a laptop that discharges, and still see
        which binary would have been asked (absolute path first)."""
        rec["exec"].append(list(argv))
        return ""

    def fake_spawn(argv):
        rec["spawn"].append(list(argv))
        return True

    def fake_stop(timeout=6.0):
        rec["stop"] += 1
        return True

    monkeypatch.setattr(bg, "_moonraker_post", fake_post)
    monkeypatch.setattr(bg, "_moonraker_get", fake_get)
    monkeypatch.setattr(bg, "_exec", fake_exec)
    monkeypatch.setattr(bg, "_spawn", fake_spawn)
    monkeypatch.setattr(stack.process, "stop", fake_stop)
    stack.rec = rec
    stack.bg = bg
    return stack
