"""Shared fixtures: rebind klipperformac to a throwaway home + data dir.

This is the only harness for the 10.15-support branch's test suite. It
intentionally only reloads modules that exist in the shipping CLI.
"""

import importlib
import types

import pytest

# Modules in the lean CLI on this branch. Tests that touch flash/mcus/
# backup/battery/cam live on the agent-experiment branch; they're not
# applicable here.
MODULES = ("paths", "configgen", "detect", "versions", "process", "installer", "cli")


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """Reloaded klipperformac package bound to a throwaway home/data dir.

    Returns a namespace: ns.paths, ns.cli, ns.configgen, ...
    """
    home = tmp_path / "home"
    data = tmp_path / "data"
    monkeypatch.setenv("KLIPPERFORMAC_HOME", str(home))
    monkeypatch.setenv("KLIPPERFORMAC_DATA", str(data))
    for var in (
        "KLIPPERFORMAC_WEB_PORT",
        "KLIPPERFORMAC_FLUIDD_PORT",
        "KLIPPERFORMAC_MOONRAKER_PORT",
    ):
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
