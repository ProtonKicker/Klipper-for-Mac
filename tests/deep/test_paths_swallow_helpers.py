"""paths.is_file/is_dir/path_exists never raise on hostile user strings.

Python < 3.13 lets Path.exists()/is_file()/is_dir() propagate ENAMETOOLONG
and EACCES - the stack's own 3.11 venv included, so this is production
behavior, not a test artifact. These helpers are the sanctioned seam for
every path that came from env vars, CLI args, or config values.
"""
import os
import tempfile

import pytest


@pytest.mark.parametrize("value", [
    "x" * 5000,                      # ENAMETOOLONG by component length
    "a\x00b",                        # embedded NUL (ValueError on some versions)
    "line\nbreak",
    "\\ backslash \\ start",
    "/tmp/" + "段" * 3000,            # multibyte over component limit
])
def test_helpers_never_raise(stack, value):
    p = stack.paths.Path(value)
    assert stack.paths.is_file(p) is False
    assert stack.paths.is_dir(p) is False
    assert stack.paths.path_exists(p) is False


def test_unreadable_directory_reads_as_absent(stack):
    d = stack.home / "unread"
    d.mkdir(parents=True, exist_ok=True)
    inner = d / "state.json"
    inner.write_text("{}")
    d.chmod(0)
    try:
        assert stack.paths.is_file(inner) is False
        assert stack.paths.path_exists(inner) is False
    finally:
        d.chmod(0o755)


def test_cam_helper_path_survives_huge_env(stack, monkeypatch):
    huge = str(stack.paths.Path(tempfile.gettempdir()) / ("x" * 5000))
    monkeypatch.setenv("KLIPPERFORMAC_CAM_BIN", huge)
    got = stack.cam.helper_path()  # must not raise on any Python version
    assert got is None or str(got) != huge


def test_helpers_agree_with_os_on_normal_paths(stack):
    f = stack.data / "plain.txt"
    f.write_text("x")
    assert stack.paths.is_file(f) is os.path.isfile(str(f))
    assert stack.paths.is_dir(stack.data) is True
    assert stack.paths.path_exists(f) is True
