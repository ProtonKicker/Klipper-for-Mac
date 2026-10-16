"""Live tier, READ-ONLY: detect.identity() against the real SKR Pico on USB.

Marked BOTH `live` and `hardware`: it reads the actual IOKit registry of the
bench Mac, where a SKR Pico is attached at /dev/cu.usbmodem14101. The default
tiers filter both markers out (pytest.ini), and `make test-live` additionally
requires saying KLIPPERFORMAC_ALLOW_LIVE=1 out loud.

Why this test exists when the fake-ioreg suite is green: the fakes encode an
assumption (ioreg prints idVendor in DECIMAL, the serial lives under
"USB Serial Number", the port hangs off an IOSerialBSDClient under the USB
device). Only the real board proves the assumption. Read-only means read-only:
this file runs `ioreg` and nothing else -- no port is opened, no board is
reset, no service is restarted, nothing is flashed. Safe to run during a print.

Skips cleanly when the board is not attached, so a laptop on a train is never
blamed for a bench that is not there.
"""
import pytest

from klipperformac import detect

pytestmark = [pytest.mark.live, pytest.mark.hardware]

# Klipper's RP2040 CDC firmware id, from the vendor product descriptor
# (klipper's rp2040_canboot/usb_cdc defaults): 0x1d50 / 0x614e.
PICO_VID_PID = "1d50:614e"


@pytest.fixture(autouse=True)
def require_pico():
    """Skip (never fail) when the bench board is not plugged in."""
    ident = _the_pico()
    if ident is None:
        pytest.skip("no SKR Pico / RP2040 (1d50:614e) attached on USB")
    return ident


def _the_pico():
    """identity() dict for the attached Klipper board, or None. Read-only."""
    for port in detect.all_identities():
        if (port.get("vid_pid") or "").lower() == PICO_VID_PID:
            return port
    return None


def test_live_identity_of_the_attached_pico():
    """identity() on the real Pico reports 1d50:614e (case-insensitively), the
    firmware's own serial, and resolves straight back to the live node."""
    ident = _the_pico()
    assert ident is not None                       # require_pico already skipped
    assert ident["vid_pid"].lower() == PICO_VID_PID
    assert ident["serial"], "Klipper CDC firmware must expose a serial number"
    assert ident["name"], "product name must not be empty"
    # The four contract keys are present and populated.
    for key in ("vid_pid", "serial", "name", "ioservice_path"):
        assert ident[key]
    # The key is by-id, not by-path: this is what survives the rename.
    assert ident["key"] == "usb:{}:{}".format(PICO_VID_PID, ident["serial"])
    assert detect.find_by_identity(ident["vid_pid"], ident["serial"]) == \
        ident["path"]
    assert detect.find_by_identity("1D50:614E", ident["serial"].lower()) == \
        ident["path"]
    # Asking about a node that does not exist answers None, in bounded time.
    assert detect.identity("/dev/cu.usbmodem99999") is None
