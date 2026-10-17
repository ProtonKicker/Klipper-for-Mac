"""Live tier, READ-ONLY: the per-MCU doctor probe against the running stack.

One GET with a 3 s timeout (`/printer/objects/query?mcu%20<name>=mcu_version`),
the same endpoint Mainsail uses. Nothing here restarts, flashes, resets the bench
SKR Pico, or opens a serial port: it is safe to run during a print, and it is the
only test in the tier that proves the fakes in test_mcu_runtime_report.py encode
what Moonraker really answers (including the null that means "klippy has no such
MCU", which no offline fixture can assert).

Skips cleanly when the stack is not up: a laptop on a train is not a broken
install.
"""
import pytest

import importlib
import types

from klipperformac import cli, paths

pytestmark = pytest.mark.live


@pytest.fixture
def real_stack(monkeypatch):
    for var in ("KLIPPERFORMAC_HOME", "KLIPPERFORMAC_DATA",
                "KLIPPERFORMAC_VOLUMES_DIR"):
        monkeypatch.delenv(var, raising=False)
    real_paths = importlib.reload(importlib.import_module("klipperformac.paths"))
    real_cli = importlib.reload(importlib.import_module("klipperformac.cli"))
    return types.SimpleNamespace(paths=real_paths, cli=real_cli)


@pytest.fixture(autouse=True)
def require_stack(real_stack):
    if not real_stack.paths.PRINTER_CFG.is_file():
        pytest.skip("no printer.cfg: the stack was never set up on this Mac")
    if real_stack.cli._klippy_state(timeout=3) is None:
        pytest.skip("klippy is not answering /printer/info: live probe skipped")


def test_every_configured_mcu_is_answered_for(real_stack):
    """The doctor line per section, from the real printer.cfg on this Mac.

    'unknown' is tolerated (it is a soft degrade by design); an MCU klippy
    did not load while claiming ready is a real fault and fails here."""
    entries = real_stack.cli._mcu_entries(cfg_path=real_stack.paths.PRINTER_CFG)
    names = [e["mcu"] for e in entries if e.get("configured", True)]
    assert names, "printer.cfg has no [mcu] section on this bench"
    lines = real_stack.cli._mcu_report_lines(cfg_path=real_stack.paths.PRINTER_CFG)
    assert [text for _tag, text, _v in lines], "one line per section"
    assert real_stack.cli._mcu_lines_fail(
        lines, real_stack.cli._klippy_state(timeout=3)) is False
    ok = [line for line in lines if line[2] == "ok"]
    if real_stack.cli._klippy_state(timeout=3) == "ready":
        assert len(ok) == len(names), lines


def test_the_master_reports_the_firmware_klipper_is_running(real_stack):
    """The detail of the one line that must always be true while ready: the
    version the MCU object reports is the version /printer/info reports."""
    if real_stack.cli._klippy_state(timeout=3) != "ready":
        pytest.skip("klippy is not ready on this bench right now")
    verdict, detail = real_stack.cli._mcu_runtime("mcu")
    assert verdict == "ok", detail
    info = real_stack.cli._moonraker_get("/printer/info", timeout=3) or {}
    version = (info.get("result") or {}).get("software_version")
    assert detail == version, (detail, version)


def test_the_master_matches_the_installed_klipper_checkout(real_stack):
    """The board on USB should be flashed with the firmware this install's
    Klipper checkout expects, not just any firmware that happens to boot."""
    if real_stack.cli._klippy_state(timeout=3) != "ready":
        pytest.skip("klippy is not ready on this bench right now")
    expected = real_stack.cli._klipper_checkout_version()
    if not expected:
        pytest.skip("could not read the installed Klipper checkout revision")
    entries = [e for e in real_stack.cli._mcu_entries(
        cfg_path=real_stack.paths.PRINTER_CFG)
        if e.get("configured", True) and e.get("mcu") == "mcu"]
    assert entries, "printer.cfg has no configured [mcu] section on this bench"
    rows = real_stack.cli._mcu_runtime_rows(entries=entries)
    lines = real_stack.cli._mcu_firmware_match_lines(rows=rows, expected=expected)
    assert lines, "no firmware-match line was produced for the master MCU"
    assert lines[0][2] == "match", lines


def test_the_contract_from_the_real_bench_is_parseable(real_stack, capsys):
    """`serial --json` on the real machine: exit 0, one JSON document, and the
    bench's SKR Pico present under whatever node macOS gave it today."""
    real_stack.cli.cmd_serial(real_stack.cli.argparse.Namespace(
        set=None, auto=False, json=True, mcu=None))
    import json
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"mcus", "single"}
    master = [e for e in data["mcus"] if e["mcu"] == "mcu"]
    assert master, data
    assert (master[0]["identity"] or {}).get("vid_pid") in (None, "1d50:614e")
