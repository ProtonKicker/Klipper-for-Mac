"""Port env vars: bad values must produce a clean CLI error, not a traceback."""
import importlib
import os

import pytest


def _reload_paths_with(monkeypatch, tmp_path, var, value):
    monkeypatch.setenv("KLIPPERFORMAC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KLIPPERFORMAC_DATA", str(tmp_path / "data"))
    monkeypatch.setenv(var, value)
    import klipperformac.paths as paths
    return importlib.reload(paths)


@pytest.mark.parametrize("value", ["abc", "8.5", "", "-1", "70000",
                                   "11111111111111111111"])
def test_bad_port_is_clean_error(monkeypatch, tmp_path, value):
    with pytest.raises(SystemExit):
        _reload_paths_with(monkeypatch, tmp_path,
                           "KLIPPERFORMAC_WEB_PORT", value)


def test_good_port_override(monkeypatch, tmp_path):
    paths = _reload_paths_with(monkeypatch, tmp_path,
                               "KLIPPERFORMAC_WEB_PORT", "9090")
    assert paths.WEB_PORT == 9090


def test_tilde_in_data_env_is_expanded(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KLIPPERFORMAC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KLIPPERFORMAC_DATA", "~/kpf-tilde-test")
    import klipperformac.paths as paths
    importlib.reload(paths)
    assert paths.DATA == tmp_path / "kpf-tilde-test"
