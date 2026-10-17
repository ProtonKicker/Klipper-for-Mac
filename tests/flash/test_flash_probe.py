"""`flash probe`: read-only direct serial handshake while the stack is down.

This is intentionally a separate command from `flash detect`:

- `detect` never opens a port and stays safe to run while klippy is live
- `probe` is the explicit "touch the serial link and ask the MCU who it is"
  tool, so it refuses while the stack is up

The helper itself is run through the stack venv python and the real upstream
klippy modules, but every test here stubs the subprocess seam, so nothing opens
`/dev/cu.*` and no child process is started.
"""
import argparse
import json

import pytest


def _checkout(fstack):
    root = fstack.paths.KLIPPER / "klippy"
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_probe_refuses_while_the_stack_is_running(fstack, capsys):
    fstack.running()
    code = fstack.flash.dispatch(argparse.Namespace(
        action="probe", port="/dev/cu.usbmodem14101", baud=250000, timeout=20))
    out = capsys.readouterr().out
    assert code == 1
    assert "Stop it first" in out
    assert "Nothing to flash." in out


def test_probe_needs_the_stack_venv_and_checkout(fstack):
    fstack.down()
    fstack.port_present(True)
    ok, lines = fstack.flash.probe_runtime("/dev/cu.usbmodem14101")
    assert not ok
    assert "No stack venv python" in "\n".join(lines)


def test_probe_reports_a_successful_handshake(fstack):
    _checkout(fstack)
    py = fstack.paths.VENV_PY
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    fstack.port_present(True)
    rec = fstack.use_recorder({py.name: (
        0, json.dumps({
            "ok": True,
            "device": "/dev/cu.usbmodem14101",
            "baud": 250000,
            "mcu": "rp2040",
            "clock_freq": 125000000,
            "version": "v0.12.0-123",
            "build_versions": "gcc: test",
        }), "")})
    ok, lines = fstack.flash.probe_runtime("/dev/cu.usbmodem14101")
    text = "\n".join(lines)
    assert ok
    assert "MCU probe OK" in text
    assert "rp2040" in text
    assert "125000000" in text
    assert "v0.12.0-123" in text
    assert rec.ran(py.name)


def test_probe_reports_a_failed_handshake_cleanly(fstack):
    _checkout(fstack)
    py = fstack.paths.VENV_PY
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    fstack.port_present(True)
    fstack.use_recorder({py.name: (
        0, json.dumps({
            "ok": False,
            "device": "/dev/cu.usbmodem14101",
            "baud": 250000,
            "error": "Unable to connect to MCU",
            "error_type": "MCUDumpError",
        }), "")})
    ok, lines = fstack.flash.probe_runtime("/dev/cu.usbmodem14101")
    text = "\n".join(lines)
    assert not ok
    assert "did not complete a Klipper handshake" in text
    assert "Unable to connect to MCU" in text


def test_probe_survives_non_json_helper_output(fstack):
    _checkout(fstack)
    py = fstack.paths.VENV_PY
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    fstack.port_present(True)
    fstack.use_recorder({py.name: (0, "not json", "trace")})
    ok, lines = fstack.flash.probe_runtime("/dev/cu.usbmodem14101")
    text = "\n".join(lines)
    assert not ok
    assert "did not return JSON" in text
    assert "stdout: not json" in text
    assert "stderr: trace" in text


def test_probe_turns_a_subprocess_timeout_into_a_clean_handshake_failure(fstack):
    _checkout(fstack)
    py = fstack.paths.VENV_PY
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    fstack.port_present(True)
    fstack.use_recorder({py.name: (None, "", "timed out after 20 seconds")})
    ok, lines = fstack.flash.probe_runtime("/dev/cu.usbmodem14101", timeout=20)
    text = "\n".join(lines)
    assert not ok
    assert "did not complete a Klipper handshake" in text
    assert "timed out after 20s" in text


def test_flash_help_lists_probe(fstack, capsys):
    with pytest.raises(SystemExit) as exc:
        fstack.cli.main(["flash", "--help"])
    assert exc.value.code == 0
    assert "probe" in capsys.readouterr().out
