"""Offline harness for the power-loss slice.

Three things an offline test of this feature must never do: talk to a socket,
shell out to `sysctl`/`ps`, or touch the developer's real ~/.klipperformac. The
`pl` fixture builds on tests/conftest.py's `stack` (a throwaway
KLIPPERFORMAC_HOME/DATA) and replaces exactly those three with knobs, so "the
marker survived a reboot" is a fact visible in tmp_path files rather than a claim
in a comment.

What stays REAL on purpose: the marker state machine, the JSONL journal, the
rotation, the settings coercion and the CLI surface. Faking those would test the
fake, not the feature - and the feature is a file that has to outlive a kill -9.
"""
import importlib
import json
import socket

import pytest

BOOT = "1791932927"  # this Mac's real kern.boot sec, from the facts pass
ALL_UP = ("klipper", "moonraker", "web_mainsail", "web_fluidd", "caffeinate")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Hard offline proof: any socket creation aborts the test."""
    def _boom(*args, **kwargs):
        raise AssertionError(
            "BUG IN TEST: the offline power-loss suite must not open a socket "
            "(the live tier is `klipperformac powerloss-test`, gated by "
            "KLIPPERFORMAC_ALLOW_LIVE=1)")

    monkeypatch.setattr(socket, "socket", _boom)


@pytest.fixture
def pl(stack, monkeypatch):
    """`stack` plus a reloaded powerloss with every machine read turned into a
    knob: boot time, ps liveness, and the two HTTP verbs."""
    powerloss = importlib.reload(
        importlib.import_module("klipperformac.powerloss"))
    monkeypatch.setattr(powerloss, "_boot_time", lambda: BOOT)
    monkeypatch.setattr(powerloss, "_http_get", lambda path, timeout=3: None)
    monkeypatch.setattr(powerloss, "_http_post", lambda path, timeout=5: None)
    monkeypatch.setattr(powerloss, "_stack_state", lambda: None)
    stack.powerloss = powerloss
    return stack


def stats_payload(filename="cube.gcode", state="printing", position=1024,
                  size=300000, progress=None, layer=3):
    """One response shaped exactly like what the live Moonraker answers on
    /printer/objects/query?print_stats&virtual_sdcard&toolhead (verified)."""
    if progress is None:
        progress = position / float(size)
    return {"result": {"eventtime": 262026.03, "status": {
        "print_stats": {"filename": filename, "total_duration": 61.0,
                        "print_duration": 55.5, "filament_used": 1234.5,
                        "state": state, "message": "",
                        "info": {"total_layer": 40, "current_layer": layer}},
        "virtual_sdcard": {"file_path": None, "progress": progress,
                           "is_active": state == "printing",
                           "file_position": position, "file_size": size},
        "toolhead": {"position": [10.5, 20.25, 0.3, 123.4]}}}}


def history_payload(*statuses):
    """/server/history/list with one row per status, like upstream's own."""
    jobs = [{"job_id": "{:06d}".format(index), "user": "_TRUSTED_USER_",
             "filename": "job-{}.gcode".format(index), "status": status,
             "start_time": 1791252431.0 + index, "end_time": None,
             "print_duration": 100.0 + index, "total_duration": 120.0 + index,
             "filament_used": 1.0}
            for index, status in enumerate(statuses, 1)]
    return {"result": {"count": len(jobs), "jobs": jobs}}


def rest(ns, *answers):
    """Install a GET recorder on the fixture's powerloss and return the recorder:
    the list of paths called, with `answers` served in order (the last repeats)."""
    powerloss = ns.powerloss
    calls = []

    def _get(path, timeout=3):
        calls.append(path)
        index = min(len(calls) - 1, len(answers) - 1)
        return answers[index] if answers else None

    powerloss._http_get = _get
    return calls


def write_state(ns, pids=None, boot=BOOT, started="2026-10-16T09:42:13"):
    """run/state.json exactly as process._save() writes it (paths.py:293-294,
    process.py:142-166), so `_recorded_stack()` reads a real document."""
    ns.paths.RUN.mkdir(parents=True, exist_ok=True)
    data = {"boot_time": boot, "started": started}
    data.update(pids if pids is not None
                else {name: 16000 + index
                      for index, name in enumerate(ALL_UP)})
    ns.paths.STATE.write_text(json.dumps(data))
    return data


def services_up(dead=()):
    """A process.running() dict with everything alive except `dead`."""
    return {name: name not in tuple(dead) for name in ALL_UP}
