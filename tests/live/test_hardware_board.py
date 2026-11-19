"""Hardware tier: assertions that need a real board on USB serial.

Doubly guarded: skipped unless KLIPPERFORMAC_ALLOW_LIVE=1 (needs the stack to
talk to the board) AND a /dev/cu.usbmodem*/usbserial* device is present.
The bench scenario assumes printer.cfg is the 'skr-pico-bench' preset (no
heaters attached); firmware restarts are safe because no components are wired.
"""
import json
import time
import urllib.request

import pytest

from klipperformac import detect

pytestmark = [pytest.mark.live, pytest.mark.hardware]

BASE = "http://127.0.0.1:7125"


def _get(url, timeout=8):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def _ports():
    return detect.scan()


@pytest.fixture(autouse=True)
def require_board():
    if not _ports():
        pytest.skip("no USB serial board attached")


def test_serial_scanner_finds_the_board():
    paths = [p["path"] for p in _ports()]
    assert any("usbmodem" in p or "usbserial" in p or "URT" in p
               for p in paths), paths


def test_mcu_is_connected_and_reporting():
    data = _get(BASE + "/printer/objects/query?mcu")
    mcu = data["result"]["status"]["mcu"]
    assert mcu.get("mcu_version") or mcu.get("last_stats")


def test_firmware_restart_recovers_the_board():
    """Full MCU power-cycle through the real board: must come back ready.
    This is the mildest hostile hardware action available and the core of
    what a real print interruption exercises at the link layer."""
    req = urllib.request.Request(
        BASE + "/printer/gcode/script?script=FIRMWARE_RESTART", method="POST")
    urllib.request.urlopen(req, timeout=10).read()
    deadline = time.time() + 40
    state = None
    while time.time() < deadline:
        time.sleep(2)
        state = _get(BASE + "/printer/info")["result"]["state"]
        if state == "ready":
            return
    pytest.fail("klipper did not return to ready after FIRMWARE_RESTART "
                "(last state: {})".format(state))
