"""versions.load_pins/save_pins: filesystem hostility, atomicity, symlink traps.

Complements tests/deep/test_versions_pins_io.py (JSON corruption + permission
cases). This file attacks the *write path* as a filesystem operation: symlinked
lockfiles, dangling symlinks, a lockfile that is a directory, stray temp files,
umask-derived world-writable pins, and non-string keys.

Everything is bound to the throwaway KLIPPERFORMAC_HOME from 'stack' and stays
strictly inside pytest's tmp_path. No ~/.klipperformac, no devices, no network.
"""
import json
import os
import socket
import stat

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

HYP = dict(deadline=None, max_examples=60,
           suppress_health_check=[HealthCheck.function_scoped_fixture])

KILLER_KEYS = [
    "",                                    # empty key
    " ",                                   # blank key
    "\x00",                                # NUL key
    "klipper\nklipper",                    # newline (log/terminal injection)
    "a" * 10_000,                          # huge key
    "\u202e\u202dklipper",                 # bidi override characters
    "../../etc/passwd",                    # path-ish key (never used as a path, but
    "..\\..\\windows\\system32",           #  must not be written blindly)
    "klipper",                             # collides with a default key
]


@pytest.fixture(autouse=True)
def socket_trap(monkeypatch):
    """Hard offline proof: pins I/O must never reach the network stack."""
    seen = []

    def _boom(*args, **kwargs):
        seen.append((args, kwargs))
        raise AssertionError("BUG IN TEST: pins I/O opened a socket")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    return seen


def _lockdir(stack):
    stack.paths.LOCKFILE.parent.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# invariants that must hold today (these tests must pass)
# --------------------------------------------------------------------------

def test_save_then_load_of_every_nasty_but_valid_key(stack):
    """JSON object keys are opaque strings: hostile-looking keys must round-trip."""
    pins = dict(stack.versions.DEFAULT_PINS)
    for key in KILLER_KEYS:
        pins[key] = "v1.0.0"
    stack.versions.save_pins(pins)
    loaded = stack.versions.load_pins()
    for key in KILLER_KEYS:
        assert loaded[key] == "v1.0.0"
    assert loaded["klipper"] == "v1.0.0", "the lockfile wins over DEFAULT_PINS"
    assert loaded["moonraker"] == stack.versions.DEFAULT_PINS["moonraker"]


def test_save_pins_into_a_lockfile_that_is_a_directory_fails_cleanly(stack):
    _lockdir(stack)
    stack.paths.LOCKFILE.mkdir()
    with pytest.raises(OSError):
        stack.versions.save_pins({"klipper": "abc"})
    assert stack.paths.LOCKFILE.is_dir(), "the directory must not be removed/replaced"
    stack.paths.LOCKFILE.rmdir()


def test_save_pins_never_leaves_stray_files_behind(stack):
    _lockdir(stack)
    stack.versions.save_pins(dict(stack.versions.DEFAULT_PINS))
    assert sorted(p.name for p in stack.paths.LOCKFILE.parent.iterdir()) == \
        ["lockfile.json"]
    with pytest.raises(TypeError):
        stack.versions.save_pins({"unserialisable": {1, 2}})
    assert sorted(p.name for p in stack.paths.LOCKFILE.parent.iterdir()) == \
        ["lockfile.json"], "a failed save must not leave temp/partial files"


def test_stray_temp_artefacts_are_never_read_as_pins(stack):
    """paths.save_settings() writes *.json.tmp; a leftover must not feed load_pins()."""
    _lockdir(stack)
    stack.paths.LOCKFILE.write_text('{"klipper": "real-sha"}')
    (stack.paths.LOCKFILE.parent / "lockfile.json.tmp").write_text(
        '{"klipper": "half-written"}')
    (stack.paths.LOCKFILE.parent / "lockfile.json.bak").write_text("not json at all")
    assert stack.versions.load_pins()["klipper"] == "real-sha"
    assert stack.paths.LOCKFILE.read_text() == '{"klipper": "real-sha"}'


def test_save_pins_never_mutates_the_module_level_defaults(stack):
    shipped = dict(stack.versions.DEFAULT_PINS)
    repos = dict(stack.versions.REPOS)
    pins = stack.versions.load_pins()
    pins["klipper"] = "mutated"
    pins["brand_new"] = 1
    stack.versions.save_pins(pins)
    stack.versions.load_pins()
    assert stack.versions.DEFAULT_PINS == shipped
    assert stack.versions.REPOS == repos


def test_save_pins_of_an_empty_and_of_a_minimal_mapping(stack):
    _lockdir(stack)
    stack.versions.save_pins({})
    assert stack.versions.load_pins() == stack.versions.DEFAULT_PINS
    stack.versions.save_pins({"only_key": "only_value"})
    loaded = stack.versions.load_pins()
    assert loaded["only_key"] == "only_value"
    assert set(loaded) >= set(stack.versions.DEFAULT_PINS)


def test_load_pins_ignores_duplicate_json_keys_and_trailing_data_gaps(stack):
    _lockdir(stack)
    stack.paths.LOCKFILE.write_text(
        '{"klipper": "first", "klipper": "second", "moonraker": "v1.2.3"}')
    assert stack.versions.load_pins()["klipper"] == "second"


def test_save_pins_accepts_mapping_types_and_preserves_insertion_order_in_content(stack):
    from collections import OrderedDict
    ordered = OrderedDict([("z", "1"), ("a", "2"), ("klipper", "sha")])
    stack.versions.save_pins(ordered)
    raw = stack.paths.LOCKFILE.read_text()
    assert raw.index('"a"') < raw.index('"klipper"') < raw.index('"z"'), \
        "sort_keys=True must normalise ordering"
    assert stack.versions.load_pins()["klipper"] == "sha"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_save_pins_recovers_a_lockfile_directory_that_was_deleted(stack):
    """A user who rm -rf'd ~/.klipperformac still gets a working, readable pin store."""
    import shutil
    stack.versions.save_pins({"klipper": "old"})
    shutil.rmtree(stack.paths.LOCKFILE.parent)
    stack.versions.save_pins({"klipper": "new"})
    assert stack.paths.LOCKFILE.is_file()
    assert stack.versions.load_pins()["klipper"] == "new"
    files = sorted(p.name for p in stack.paths.LOCKFILE.parent.iterdir())
    assert files == ["lockfile.json"]


@given(pins=st.dictionaries(
    st.text(alphabet="abv.019-_ \n\t\x00\u202e\U0001f600", max_size=20),
    st.text(max_size=20), max_size=8))
@settings(**HYP)
def test_string_keyed_pins_always_round_trip(stack, pins):
    stack.versions.save_pins(pins)
    assert stack.versions.load_pins() == dict(stack.versions.DEFAULT_PINS, **pins)


# --------------------------------------------------------------------------
# BUG DEEP: the write path follows symlinks
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["existing-target", "dangling-target"])
def test_save_pins_does_not_write_through_a_symlinked_lockfile(stack, tmp_path, kind):
    outside = tmp_path / "outside-home"
    outside.mkdir()
    victim = outside / "victim.json"
    if kind == "existing-target":
        victim.write_text("ORIGINAL CONTENT THAT MUST SURVIVE")
    else:
        assert not victim.exists()

    _lockdir(stack)
    stack.versions.save_pins(dict(stack.versions.DEFAULT_PINS))
    stack.paths.LOCKFILE.unlink()
    os.symlink(str(victim), str(stack.paths.LOCKFILE))
    assert stack.paths.LOCKFILE.is_symlink()

    stack.versions.save_pins({"ui": "mainsail", "klipper": "attacker-sha"})

    if kind == "existing-target":
        assert victim.read_text() == "ORIGINAL CONTENT THAT MUST SURVIVE"
    else:
        assert not victim.exists(), \
            "save_pins escaped KLIPPERFORMAC_HOME and created {}".format(victim)
    assert stack.paths.LOCKFILE.is_file() and not stack.paths.LOCKFILE.is_symlink()
    assert stack.versions.load_pins()["ui"] == "mainsail"


# --------------------------------------------------------------------------
# BUG DEEP: the write path trusts whatever it is given
# --------------------------------------------------------------------------

def test_save_pins_rejects_non_string_keys_before_touching_the_lockfile(stack):
    stack.versions.save_pins(dict(stack.versions.DEFAULT_PINS))
    good = stack.paths.LOCKFILE.read_text()

    with pytest.raises((TypeError, ValueError)):
        stack.versions.save_pins({1: "a", 2: "b"})
    assert stack.paths.LOCKFILE.read_text() == good

    with pytest.raises((TypeError, ValueError)):
        stack.versions.save_pins({1: "a", "moonraker": "v0.11.0"})
    assert stack.paths.LOCKFILE.read_text() == good
    assert stack.versions.load_pins() == stack.versions.DEFAULT_PINS


def test_save_pins_creates_a_lockfile_that_is_not_world_writable(stack):
    """Baseline that holds under the normal macOS umask: no group/other write bit."""
    old_mask = os.umask(0o022)
    try:
        stack.versions.save_pins(dict(stack.versions.DEFAULT_PINS))
        mode = stat.S_IMODE(os.stat(stack.paths.LOCKFILE).st_mode)
    finally:
        os.umask(old_mask)
    assert mode & 0o022 == 0, "lockfile.json is group/other writable: {}".format(
        oct(mode))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_save_pins_ignores_a_permissive_umask(stack):
    old_mask = os.umask(0o000)
    try:
        stack.versions.save_pins(dict(stack.versions.DEFAULT_PINS))
        mode = stat.S_IMODE(os.stat(stack.paths.LOCKFILE).st_mode)
    finally:
        os.umask(old_mask)
    assert mode & 0o022 == 0, "lockfile.json is group/other writable: {}".format(
        oct(mode))


@pytest.mark.parametrize("payload, key, expected", [
    pytest.param('{"moonraker": ""}', "moonraker", "v0.11.0", id="empty-tag"),
    pytest.param('{"moonraker": "   "}', "moonraker", "v0.11.0", id="blank-tag"),
    pytest.param('{"mainsail": "v2.19.0\\n"}', "mainsail", "v2.19.0", id="newline"),
    pytest.param('{"fluidd": "v1.37.6\\t-rc"}', "fluidd", "v1.37.6", id="tab"),
    pytest.param('{"ui": "gcode"}', "ui", "fluidd", id="ui-not-a-repo"),
    pytest.param('{"ui": ""}', "ui", "fluidd", id="ui-empty"),
])
def test_load_pins_rejects_unusable_pin_values(stack, payload, key, expected):
    _lockdir(stack)
    stack.paths.LOCKFILE.write_text(payload)
    assert stack.versions.load_pins()[key] == expected
