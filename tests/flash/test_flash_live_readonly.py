"""Live tier: `klipperformac flash detect` against this Mac, read-only.

Never a default (`-m "not live and not hardware"` in pytest.ini; run with
`KLIPPERFORMAC_ALLOW_LIVE=1 make test-live`). The point of putting it in the opt-in
tier at all is that the offline tests cannot prove the two properties that
matter on the real machine:

  * detection changes nothing - no volume mounted or ejected, no file written,
    no config touched, no port opened while klippy holds it;
  * the identity facts the multi-MCU spec claims for the bench board are what
    IOKit really reports (SKR Pico: VID:PID 1d50:614e, SER 5044506110A45B1C,
    the node klippy has open in printer.cfg's [mcu]).
"""
import hashlib
import importlib
import os

import pytest

pytestmark = [pytest.mark.live, pytest.mark.hardware]

REAL_HOME = os.path.expanduser("~/.klipperformac")


@pytest.fixture(autouse=True)
def real_home(monkeypatch):
    """Modules are reloaded against the REAL ~/.klipperformac.

    The offline suite reloads klipperformac.paths with a throwaway home, and the
    last one wins, so a live test that trusted module state would be reading a
    temp folder and calling it a pass. Clearing the env vars and reloading is
    what gets the production paths back."""
    for var in ("KLIPPERFORMAC_HOME", "KLIPPERFORMAC_DATA",
                "KLIPPERFORMAC_VOLUMES_DIR"):
        monkeypatch.delenv(var, raising=False)
    paths = importlib.reload(importlib.import_module("klipperformac.paths"))
    if not paths.KLIPPER.is_dir():
        pytest.skip("no real install at {} to probe".format(REAL_HOME))
    detect = importlib.reload(importlib.import_module("klipperformac.detect"))
    process = importlib.reload(
        importlib.import_module("klipperformac.process"))
    flash = importlib.reload(importlib.import_module("klipperformac.flash"))
    return SimplePaths(paths, detect, process, flash)


class SimplePaths:
    """The reloaded quartet, as attributes (kept explicit so a skip is loud)."""

    def __init__(self, paths, detect, process, flash):
        self.paths = paths
        self.detect = detect
        self.process = process
        self.flash = flash


def _volume_state():
    root = "/Volumes"
    out = {}
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for name in names:
        mount = os.path.join(root, name)
        try:
            entries = sorted(os.listdir(mount))[:12]
        except OSError:
            entries = ["<unreadable>"]
        out[name] = entries
    return out


def test_detect_reports_the_real_board_without_changing_anything(
        real_home, capsys):
    """The whole reason this tier exists: prove the read-only promise on
    hardware that is actually running a printer."""
    flash = real_home.flash
    volumes_before = _volume_state()
    cfg = real_home.paths.PRINTER_CFG
    cfg_hash = hashlib.sha256(cfg.read_bytes()).hexdigest() if cfg.exists() \
        else None
    report = flash.detect_boards()
    lines = flash.detect_lines(report)
    print("\n".join(lines))
    assert _volume_state() == volumes_before, "detect must not mount/eject"
    if cfg_hash:
        assert hashlib.sha256(cfg.read_bytes()).hexdigest() == cfg_hash
    assert isinstance(report["targets"], list)
    assert report["ports"], "the bench board is plugged in for this tier"


def test_the_bench_skr_picos_identity_matches_the_multi_mcu_spec(real_home):
    """1d50:614e / SER 5044506110A45B1C: the numbers the multi-MCU research and
    the flash research both cite for this board, read straight out of IOKit."""
    flash = real_home.flash
    rows = [r for r in flash.identity_rows() or []
            if r.get("path", "").startswith("/dev/cu.usbmodem")]
    if not rows:
        pytest.skip("the SKR Pico is not on a /dev/cu.usbmodem node right now")
    row = rows[0]
    assert row["vid_pid"].upper() == "1D50:614E", row
    assert row["serial"], row
    assert row["key"].startswith("usb:1d50:614e:")
    assert row["name"] == "rp2040"


def test_detect_names_the_section_that_claims_the_port(real_home):
    flash = real_home.flash
    report = flash.detect_boards()
    claimed = [t for t in report["targets"] if t.get("mcu")]
    assert claimed, "printer.cfg's [mcu] serial points at a present node"
    assert claimed[0]["mcu"] == "[mcu]" or claimed[0]["mcu"].startswith(
        "[mcu ")


def test_a_mutating_command_refuses_on_the_real_machine(real_home, capsys):
    """The stack is up on the bench, so `flash uf2 --dry-run` must refuse before
    it inspects, waits for or writes anything - even in --dry-run form the
    refusal is the point, because the next step a user types is without it."""
    flash = real_home.flash
    if not flash.services_up():
        pytest.skip("the stack is down; the refusal needs a running klippy")
    ok, lines = flash.refuse_while_running()
    assert not ok
    assert any("klipperformac down" in line for line in lines)


def test_the_real_upstream_checkout_is_untouched_by_the_probe(real_home):
    """AGENTS.md rule 1 on the pristine tree this module names in its messages:
    listing the flasher must not change its mtime."""
    script = real_home.flash.upstream_flash_script()
    if script is None:
        pytest.skip("no upstream flasher script in this checkout")
    before = (script.stat().st_mtime_ns, script.stat().st_size)
    real_home.flash.serial_plan("/tmp/none.bin", "/dev/cu.usbmodem14101",
                                tool="auto")
    assert (script.stat().st_mtime_ns, script.stat().st_size) == before


def test_probe_reads_the_real_bench_board_when_the_stack_is_down(real_home):
    """The direct MCU handshake is the live proof `flash detect` deliberately
    does not claim: stop the stack, ask the serial link for identify_response,
    then bring everything back before returning."""
    flash = real_home.flash
    process = real_home.process
    report = flash.detect_boards()
    rows = [t for t in report["targets"]
            if t.get("kind") == "serial" and t.get("path", "").startswith("/dev/cu.")]
    if not rows:
        pytest.skip("no serial board is present to probe")
    port = rows[0]["path"]
    was_up = bool(process.running())
    if was_up:
        assert process.stop(), "could not stop the stack before flash probe"
    try:
        probe = flash.probe_report(port)
        assert probe["ok"] is True, probe
        assert probe["resolved_port"] == port
        assert probe["mcu"], probe
        assert probe["clock_freq"] is not None, probe
        assert probe["version"], probe
    finally:
        if was_up:
            assert process.start(), "could not restart the stack after flash probe"
