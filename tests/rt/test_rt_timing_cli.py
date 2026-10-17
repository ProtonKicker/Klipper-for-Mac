"""The CLI surface of `klipperformac timing`, and the doctor line it feeds.

Two things are pinned that a unit test of the parsers cannot see: the subcommand
is actually reachable through main() with the flags the GUI will pass it, and the
JSON keys are the stable snake_case contract the SwiftUI card reads (a renamed
key is a GUI bug, not a refactor). The last test runs the real command as a child
process against a throwaway KLIPPERFORMAC_HOME, so the argparse wiring, the
`paths` bootstrap and the real `ps`/`pmset`/`ioreg` binaries are all exercised -
reads only; nothing is started, stopped or rewritten.
"""
import argparse
import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


# --------------------------------------------------------------------------
# main() wiring
# --------------------------------------------------------------------------

def test_timing_is_a_registered_subcommand(stack, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(stack.cli, "cmd_timing",
                        lambda args: calls.append(args))
    stack.cli.main(["timing"])
    assert calls and calls[0].json is False
    stack.cli.main(["timing", "--json"])
    assert calls[-1].json is True


def test_timing_shows_up_in_the_help(stack, capsys):
    with pytest.raises(SystemExit) as stop:
        stack.cli.main(["--help"])
    assert stop.value.code == 0
    assert "timing" in capsys.readouterr().out


# --------------------------------------------------------------------------
# cmd_timing itself
# --------------------------------------------------------------------------

def _healthy_report():
    return {
        "generated": "2026-10-16T10:00:00",
        "stack_running": True,
        "printer_state": "printing",
        "klipper": {"pid": 16006, "running": True, "nice": 0, "pri": 31,
                    "class": "normal", "qos_env": "on"},
        "power": {"external_connected": True, "battery_lowpowermode": 1,
                  "battery_sleep": 1, "caffeinate_pid": 16010,
                  "caffeinate_assert_held": True, "cpu_speed_limit": 100,
                  "cpu_scheduler_limit": 100},
        "host_budget": {"counts": {"timer_too_close": 0,
                                   "missed_scheduling": 0,
                                   "rescheduled_timer": 0,
                                   "stepper_too_far": 0,
                                   "lost_communication": 0},
                        "srtt_max": 0.003, "rto_min": 0.025,
                        "bytes_retransmit_max": 9.0,
                        "mcu_task_stddev_max": 0.00012, "sysload_max": 6.68,
                        "print_stall_max": 0.0, "buffer_time_max": 9.75,
                        "print_time_max": 1234.0, "min_half_rtt": 0.000064,
                        "stats_lines": 300, "log_files": ["/tmp/klippy.log"],
                        "truncated": False},
        "linux_only_options": [],
        "problems": [],
        "ok": True,
    }


def _broken_report():
    report = _healthy_report()
    report["klipper"].update({"pri": 4, "class": "background"})
    report["power"]["caffeinate_assert_held"] = False
    report["host_budget"]["counts"]["timer_too_close"] = 3
    report["problems"] = ["klippy is at ps priority 4 (background)",
                          '3 "Timer too close" in klippy.log',
                          "no klipperformac caffeinate is holding "
                          "PreventUserIdleSystemSleep"]
    report["ok"] = False
    return report


@pytest.mark.parametrize("build,code", [(_healthy_report, 0),
                                        (_broken_report, 1)])
def test_timing_exit_code_tracks_the_problems(stack, monkeypatch, capsys,
                                              build, code):
    monkeypatch.setattr(stack.cli, "_timing_report", build)
    with pytest.raises(SystemExit) as stop:
        stack.cli.cmd_timing(argparse.Namespace(json=False))
    assert stop.value.code == code
    out = capsys.readouterr().out
    assert "[ok]" in out or "[!]" in out


def test_timing_json_is_the_stable_gui_contract(stack, monkeypatch, capsys):
    monkeypatch.setattr(stack.cli, "_timing_report", _broken_report)
    with pytest.raises(SystemExit) as stop:
        stack.cli.cmd_timing(argparse.Namespace(json=True))
    assert stop.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert set(report) >= {"generated", "stack_running", "printer_state",
                           "klipper", "power", "host_budget",
                           "linux_only_options", "problems", "ok"}
    assert set(report["klipper"]) == {"pid", "running", "nice", "pri", "class",
                                      "qos_env"}
    assert set(report["power"]) >= {"external_connected", "caffeinate_pid",
                                    "caffeinate_assert_held",
                                    "battery_lowpowermode", "cpu_speed_limit"}
    assert report["host_budget"]["counts"]["timer_too_close"] == 3
    assert all(key.islower() and " " not in key for key in report)


def test_timing_touches_no_file_and_no_child(stack, monkeypatch, capsys):
    """Read-only by design (a status command must never be able to kill a print,
    and AGENTS.md 2 forbids it rewriting anything): with the report forged, no
    file in the install appears and no subprocess is run."""
    monkeypatch.setattr(stack.cli, "_timing_report", _healthy_report)
    seen = []
    monkeypatch.setattr(stack.cli, "subprocess", _RecordingSubprocess(seen))
    with pytest.raises(SystemExit) as stop:
        stack.cli.cmd_timing(argparse.Namespace(json=False))
    assert stop.value.code == 0
    assert seen == []
    assert not stack.paths.STATE.exists()
    assert not list(stack.paths.LOGS.glob("*")) if stack.paths.LOGS.exists() \
        else True
    assert "[ok]" in capsys.readouterr().out


class _RecordingSubprocess:
    DEVNULL = subprocess.DEVNULL
    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, sink):
        self.sink = sink

    def run(self, cmd, *args, **kwargs):
        self.sink.append([str(c) for c in cmd])
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")


# --------------------------------------------------------------------------
# the doctor line, on a machine that is not fine
# --------------------------------------------------------------------------

@pytest.fixture
def quiet_doctor(stack, monkeypatch):
    """Everything doctor reads except the scheduling report, silenced."""
    monkeypatch.setattr(stack.cli.installer, "check_prereqs",
                        lambda strict=False: [])
    monkeypatch.setattr(stack.cli.installer, "verify_pristine", lambda: [])
    monkeypatch.setattr(stack.paths, "KLIPPER", stack.home / "klipper")
    (stack.home / "klipper").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(stack.paths, "VENV_PY", stack.home / "no-such-python")
    monkeypatch.setattr(stack.process, "running", lambda: None)
    monkeypatch.setattr(stack.shaper, "readiness",
                        lambda: {"needed": False, "healthy": True,
                                 "detail": "not configured"})
    monkeypatch.setattr(stack.cam, "running", lambda: None)
    monkeypatch.setattr(stack.cam, "doctor", lambda: [])
    monkeypatch.setattr(stack.configgen, "webcam_state",
                        lambda *a, **kw: (False, ""))
    return stack


def test_doctor_reports_and_fails_on_scheduling(quiet_doctor, monkeypatch,
                                                capsys):
    monkeypatch.setattr(quiet_doctor.cli, "_timing_report", _broken_report)
    with pytest.raises(SystemExit) as stop:
        quiet_doctor.cli.cmd_doctor(argparse.Namespace())
    out = capsys.readouterr().out
    assert stop.value.code == 1
    assert "host scheduling posture" in out
    assert "priority 4" in out


def test_doctor_is_green_when_scheduling_is_fine(quiet_doctor, monkeypatch,
                                                 capsys):
    monkeypatch.setattr(quiet_doctor.cli, "_timing_report", _healthy_report)
    with pytest.raises(SystemExit) as stop:
        quiet_doctor.cli.cmd_doctor(argparse.Namespace())
    out = capsys.readouterr().out
    assert stop.value.code == 0
    assert "[ok] host scheduling posture: klippy pri 31 (normal) , QoS shim on" \
        in out


def test_doctor_survives_telemetry_that_cannot_read(quiet_doctor, monkeypatch,
                                                   capsys):
    """A hung pmset or a missing ioreg must never be why doctor died."""
    def explode():
        raise OSError(2, "no pmset on this Mac")

    monkeypatch.setattr(quiet_doctor.cli, "_timing_report", explode)
    with pytest.raises(SystemExit) as stop:
        quiet_doctor.cli.cmd_doctor(argparse.Namespace())
    assert stop.value.code == 0
    assert "not measured" in capsys.readouterr().out


def test_doctor_reports_an_untracked_but_answering_stack(quiet_doctor,
                                                         monkeypatch, capsys):
    monkeypatch.setattr(quiet_doctor.process, "our_listeners",
                        lambda: [(quiet_doctor.paths.MOONRAKER_PORT, 42)])
    monkeypatch.setattr(quiet_doctor.cli, "_klippy_state",
                        lambda timeout=3: "ready")
    monkeypatch.setattr(quiet_doctor.cli, "_mcu_runtime_rows", lambda *a, **k: [])
    monkeypatch.setattr(quiet_doctor.cli, "_mcu_report_lines", lambda *a, **k: [])
    monkeypatch.setattr(quiet_doctor.cli, "_mcu_firmware_match_lines",
                        lambda *a, **k: [])
    monkeypatch.setattr(quiet_doctor.cli, "_mcu_log_provenance_lines",
                        lambda *a, **k: [])
    monkeypatch.setattr(quiet_doctor.cli, "_timing_report", _healthy_report)
    with pytest.raises(SystemExit) as stop:
        quiet_doctor.cli.cmd_doctor(argparse.Namespace())
    out = capsys.readouterr().out
    assert stop.value.code == 0
    assert "stack is not tracked, but Moonraker is answering on port" in out
    assert "[ok] klipper state: ready" in out


# --------------------------------------------------------------------------
# the real command, as a child process
# --------------------------------------------------------------------------

def test_real_timing_command_runs_read_only(tmp_path):
    """The whole path: argparse -> paths bootstrap -> the real ps, pmset and
    ioreg. Against an empty KLIPPERFORMAC_HOME there is no tracked stack, so the
    honest answer is "nothing measured about klippy, nothing to fix"."""
    home = tmp_path / "home"
    env = dict(os.environ)
    env.update({"KLIPPERFORMAC_HOME": str(home),
                "KLIPPERFORMAC_DATA": str(home / "data"),
                "PYTHONPATH": REPO_ROOT})
    env.pop("KLIPPERFORMAC_QOS", None)  # the child must not inherit the switch
    done = subprocess.run([sys.executable, "-m", "klipperformac", "timing",
                           "--json"], capture_output=True, env=env,
                          timeout=120, cwd=REPO_ROOT)
    assert done.returncode == 0, done.stderr.decode(errors="replace")
    report = json.loads(done.stdout.decode())
    assert report["klipper"]["running"] is False
    assert report["problems"] == []
    assert report["ok"] is True
    assert report["host_budget"]["log_files"] == []
    # the reads that need no stack still ran against the real machine
    assert report["power"]["external_connected"] in (True, False, None)
