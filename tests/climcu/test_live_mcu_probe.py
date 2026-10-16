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

from klipperformac import cli, paths

pytestmark = pytest.mark.live


def _stack_up():
    return cli._klippy_state(timeout=3) is not None


@pytest.fixture(autouse=True)
def require_stack():
    if not paths.PRINTER_CFG.is_file():
        pytest.skip("no printer.cfg: the stack was never set up on this Mac")
    if not _stack_up():
        pytest.skip("klippy is not answering /printer/info: live probe skipped")


def test_every_configured_mcu_is_answered_for():
    """The doctor line per section, from the real printer.cfg on this Mac.

    'unknown' is tolerated (it is a soft degrade by design); an MCU klippy
    did not load while claiming ready is a real fault and fails here."""
    entries = cli._mcu_entries(cfg_path=paths.PRINTER_CFG)
    names = [e["mcu"] for e in entries if e.get("configured", True)]
    assert names, "printer.cfg has no [mcu] section on this bench"
    lines = cli._mcu_report_lines(cfg_path=paths.PRINTER_CFG)
    assert [text for _tag, text, _v in lines], "one line per section"
    assert cli._mcu_lines_fail(lines, cli._klippy_state(timeout=3)) is False
    ok = [line for line in lines if line[2] == "ok"]
    if cli._klippy_state(timeout=3) == "ready":
        assert len(ok) == len(names), lines


def test_the_master_reports_the_firmware_klipper_is_running():
    """The detail of the one line that must always be true while ready: the
    version the MCU object reports is the version /printer/info reports."""
    if cli._klippy_state(timeout=3) != "ready":
        pytest.skip("klippy is not ready on this bench right now")
    verdict, detail = cli._mcu_runtime("mcu")
    assert verdict == "ok", detail
    info = cli._moonraker_get("/printer/info", timeout=3) or {}
    version = (info.get("result") or {}).get("software_version")
    assert detail == version, (detail, version)


def test_the_contract_from_the_real_bench_is_parseable(capsys):
    """`serial --json` on the real machine: exit 0, one JSON document, and the
    bench's SKR Pico present under whatever node macOS gave it today."""
    cli.cmd_serial(cli.argparse.Namespace(set=None, auto=False, json=True,
                                          mcu=None))
    import json
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"mcus", "single"}
    master = [e for e in data["mcus"] if e["mcu"] == "mcu"]
    assert master, data
    assert master[0]["identity"]["vid_pid"] in (None, "1d50:614e")
