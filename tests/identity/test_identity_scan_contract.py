"""detect.scan() is a wire format: the Swift GUI decodes it, byte for byte.

Klipper-for-Mac's SwiftUI app (KlipperMac/Sources/KlipperMac/Core/
SerialScanner.swift) parses `klipperformac serial` output into
`SerialPort(path, description)`. Adding identity() next to scan() must not
change one byte of what scan() returns, so this file pins the shape:
  * exactly the keys {path, description}, nothing more
  * every entry a /dev/cu.* candidate
  * sorted, de-duplicated
  * description is the driver-family hint, unchanged
If a later wave wants identity inside scan(), it may ADD keys -- and this test
is the one that has to be updated, deliberately, with the Swift side.
"""
import json

import pytest

from klipperformac import detect


def test_scan_shape_is_two_keys_per_entry(monkeypatch, tmp_path):
    monkeypatch.setattr(detect, "PATTERNS",
                        (str(tmp_path / "cu.usbmodem*"),
                         str(tmp_path / "cu.URT*")))
    (tmp_path / "cu.usbmodem14101").write_text("")
    (tmp_path / "cu.URT0").write_text("")
    ports = detect.scan()
    assert [p["path"] for p in ports] == [str(tmp_path / "cu.URT0"),
                                          str(tmp_path / "cu.usbmodem14101")]
    for p in ports:
        assert set(p) == {"path", "description"}


def test_scan_never_calls_ioreg(monkeypatch):
    """scan() is the fast pure-glob path the GUI polls every couple of seconds;
    identity work must never leak into it. (identity() is the opt-in call.)"""
    calls = []

    def boom(*a, **kw):
        calls.append(a)
        raise AssertionError("scan() must not shell out")

    monkeypatch.setattr(detect.subprocess, "run", boom)
    detect.scan()
    assert calls == []


def test_scan_descriptions_are_unchanged():
    """The driver-family labels shown in the GUI picker."""
    cases = {
        "/dev/cu.usbmodem14101": "USB-CDC (STM32 / SKR / Pi Pico class boards)",
        "/dev/cu.URT0": "WCH CH340/CH341",
    }
    for path, label in cases.items():
        assert detect._describe(path) == label


def test_scan_output_is_json_stable():
    """What `serial --list-json` hands Swift must stay an array of flat objects
    with string values -- no None, no nesting, no sets."""
    ports = detect.scan()
    assert isinstance(json.loads(json.dumps(ports)), list)
    for p in ports:
        assert all(isinstance(v, str) for v in p.values())


@pytest.mark.parametrize("name", ["identity", "find_by_identity",
                                  "find_all_by_identity", "all_identities"])
def test_identity_api_exists_with_a_stable_signature(name):
    """Wave 3 / S2 code against these names; a rename here is a breaking change
    to another agent's wave, so it is a test failure, not a refactor."""
    import inspect
    fn = getattr(detect, name)
    sig = inspect.signature(fn)
    assert callable(fn)
    if name == "identity":
        assert list(sig.parameters) == ["dev", "timeout"]
    elif name == "all_identities":
        assert list(sig.parameters) == ["timeout"]
    else:
        assert list(sig.parameters) == ["vid_pid", "serial", "timeout"]
        assert sig.parameters["serial"].default is None
