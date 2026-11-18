"""paths.py: port env resolution, data-path derivation, settings roundtrip."""

import json
from pathlib import Path


def test_ports_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("KLIPPERFORMAC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KLIPPERFORMAC_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("KLIPPERFORMAC_WEB_PORT", "9001")
    monkeypatch.setenv("KLIPPERFORMAC_FLUIDD_PORT", "9002")
    monkeypatch.setenv("KLIPPERFORMAC_MOONRAKER_PORT", "9003")
    import importlib

    paths = importlib.reload(__import__("klipperformac.paths", fromlist=["*"]))
    assert paths.WEB_PORT == 9001
    assert paths.FLUIDD_PORT == 9002
    assert paths.MOONRAKER_PORT == 9003
    assert paths.ui_port("mainsail") == 9001
    assert paths.ui_port("fluidd") == 9002


def test_settings_roundtrip(stack):
    p = stack.paths
    assert p.load_settings() == {}
    p.save_settings({"lan": True, "data": str(stack.data)})
    assert p.load_settings() == {"lan": True, "data": str(stack.data)}
    assert p.lan_enabled() is True
    # corrupt file degrades to {}
    p.SETTINGS.write_text("{not json")
    assert p.load_settings() == {}


def test_set_data_derives_every_path(stack):
    p = stack.paths
    p.set_data("/tmp/x/klipper-data")
    assert p.DATA == Path("/tmp/x/klipper-data")
    assert p.PRINTER_CFG == Path("/tmp/x/klipper-data/config/printer.cfg")
    assert p.KLIPPY_LOG == Path("/tmp/x/klipper-data/logs/klippy.log")
    assert p.MOONRAKER_CONF.name == "moonraker.conf"
    assert p.API_SOCKET.name == "klippy.sock"


def test_web_url_defaults_then_lockfile(stack):
    p = stack.paths
    assert p.web_url().endswith(":8081")  # defaults to fluidd
    p.LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    p.LOCKFILE.write_text(json.dumps({"ui": "mainsail"}))
    assert p.web_url().endswith(":8080")
    p.LOCKFILE.write_text("{bad json")
    assert p.web_url().endswith(":8081")  # corrupt lockfile falls back


def test_ensure_dirs_creates_expected_layout(stack):
    p = stack.paths
    p.ensure_dirs()
    for d in (p.APP_HOME, p.DATA, p.CONFIG, p.PRESETS, p.RUN, p.LOGS, p.WEB):
        assert d.is_dir()
