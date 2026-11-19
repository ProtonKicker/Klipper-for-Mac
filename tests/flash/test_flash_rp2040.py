"""`flash rp2040`: macOS-native update path for RP2040 runtime USB devices."""
import argparse


def _uf2(fstack, uf2bytes):
    return fstack.artifacts(name="klipper.uf2", blob=uf2bytes.uf2_image())


def test_rp2040_dry_run_needs_only_a_runtime_port(fstack, uf2bytes):
    fstack.down()
    fstack.port_present(True)
    ok, lines = fstack.flash.flash_rp2040(_uf2(fstack, uf2bytes),
                                          "/dev/cu.usbmodem14101",
                                          dry_run=True)
    text = "\n".join(lines)
    assert ok
    assert "Dry run" in text
    assert "/dev/cu.usbmodem14101" in text


def test_rp2040_times_out_cleanly_when_bootloader_never_appears(
        fstack, uf2bytes, monkeypatch):
    fstack.down()
    fstack.port_present(True)
    monkeypatch.setattr(fstack.flash, "request_rp2040_bootloader",
                        lambda port: (True, ""))
    monkeypatch.setattr(fstack.flash, "_wait_rp2040_bootloader",
                        lambda timeout, chip=None, sleep=None:
                        (None, None, ["  timed out after 20 s. Nothing was written."]))
    ok, lines = fstack.flash.flash_rp2040(_uf2(fstack, uf2bytes),
                                          "/dev/cu.usbmodem14101")
    text = "\n".join(lines)
    assert not ok
    assert "timed out after 20 s" in text
    assert "BOOTSEL" in text


def test_rp2040_delegates_to_uf2_volume_when_that_is_what_appears(
        fstack, uf2bytes, monkeypatch):
    fstack.down()
    fstack.port_present(True)
    seen = {}
    monkeypatch.setattr(fstack.flash, "request_rp2040_bootloader",
                        lambda port: (True, ""))
    monkeypatch.setattr(fstack.flash, "_wait_rp2040_bootloader",
                        lambda timeout, chip=None, sleep=None:
                        ("volume", "RPI-RP2", ["  /Volumes/RPI-RP2 appeared."]))
    monkeypatch.setattr(fstack.flash, "flash_uf2",
                        lambda bin_path, volume=None, timeout=None,
                        dry_run=False, sleep=None:
                        (seen.setdefault("volume", volume) == "RPI-RP2",
                         ["uf2 ok"]))
    ok, lines = fstack.flash.flash_rp2040(_uf2(fstack, uf2bytes),
                                          "/dev/cu.usbmodem14101")
    assert ok
    assert seen["volume"] == "RPI-RP2"
    assert lines[-1] == "uf2 ok"


def test_rp2040_uses_rp2040_flash_when_picoboot_usb_appears(
        fstack, uf2bytes, monkeypatch):
    fstack.down()
    fstack.port_present(True)
    monkeypatch.setattr(fstack.flash, "request_rp2040_bootloader",
                        lambda port: (True, ""))
    monkeypatch.setattr(fstack.flash, "_wait_rp2040_bootloader",
                        lambda timeout, chip=None, sleep=None:
                        ("usb", {"vidpid": "2e8a:0003"}, ["  bootloader USB 2e8a:0003 appeared."]))
    monkeypatch.setattr(fstack.flash, "ensure_rp2040_flash_tool",
                        lambda: (True, "/tmp/rp2040_flash"))
    rec = fstack.use_recorder({"rp2040_flash": (0, "ok", "")})
    ok, lines = fstack.flash.flash_rp2040(_uf2(fstack, uf2bytes),
                                          "/dev/cu.usbmodem14101")
    text = "\n".join(lines)
    assert ok
    assert rec.ran("rp2040_flash")
    assert "Flashing via rp2040_flash" in text


def test_dispatch_rp2040_requires_a_port(fstack, capsys, uf2bytes):
    code = fstack.flash.dispatch(argparse.Namespace(
        action="rp2040", bin=_uf2(fstack, uf2bytes), path=None, port=None,
        chip="rp2040", timeout=20, dry_run=False))
    out = capsys.readouterr().out
    assert code == 1
    assert "--port is required" in out
