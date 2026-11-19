"""Offline test harness: every test runs against a throwaway KLIPPERFORMAC_HOME.

The stack's modules read paths at import time, so fixtures set env vars and
reload the whole module graph rather than monkeypatching constants in place.
"""
import importlib
import os
import types

import pytest

MODULES = ("paths", "configgen", "detect", "versions", "shaper", "process",
           "cam", "installer", "cli", "batteryguard")


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """Reloaded klipperformac package bound to a throwaway home/data dir.

    Returns a namespace: ns.paths, ns.cli, ns.configgen, ...
    """
    home = tmp_path / "home"
    data = tmp_path / "data"
    monkeypatch.setenv("KLIPPERFORMAC_HOME", str(home))
    monkeypatch.setenv("KLIPPERFORMAC_DATA", str(data))
    for var in ("KLIPPERFORMAC_WEB_PORT", "KLIPPERFORMAC_FLUIDD_PORT",
                "KLIPPERFORMAC_MOONRAKER_PORT", "KLIPPERFORMAC_CAM_PORT"):
        monkeypatch.delenv(var, raising=False)
    ns = types.SimpleNamespace()
    for name in MODULES:
        mod = importlib.import_module("klipperformac." + name)
        mod = importlib.reload(mod)
        setattr(ns, name, mod)
    ns.home = home
    ns.data = data
    ns.paths.set_data(data)
    ns.paths.CONFIG.mkdir(parents=True, exist_ok=True)
    ns.paths.PRESETS.mkdir(parents=True, exist_ok=True)
    return ns


@pytest.fixture
def moonraker_generated(stack):
    """The shipped generated moonraker.conf written verbatim; returns text."""
    text = stack.configgen.MOONRAKER_CFG.format(
        host="127.0.0.1", moonraker_port=7125,
        uds_address=str(stack.paths.API_SOCKET), web_port=8080,
        fluidd_port=8081)
    stack.paths.MOONRAKER_CONF.write_text(text)
    return text
