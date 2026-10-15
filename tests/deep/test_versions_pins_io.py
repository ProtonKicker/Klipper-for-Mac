"""versions.load_pins / save_pins: corruption, atomicity, round-trip, huge files.

Everything is bound to the throwaway KLIPPERFORMAC_HOME from the 'stack'
fixture; the real ~/.klipperformac and the network are never touched.
"""
import json
import os
import stat

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

BAD_JSON = [
    pytest.param("", id="empty-file"),
    pytest.param("   \n\t ", id="whitespace-only"),
    pytest.param("{", id="lone-brace"),
    pytest.param('{"klipper": ', id="truncated-mid-value"),
    pytest.param('{"klipper": "abc",}', id="trailing-comma"),
    pytest.param("not json at all", id="plain-text"),
    pytest.param('{"a": 1}{"b": 2}', id="two-objects-concatenated"),
    pytest.param("﻿{\"klipper\": \"x\"}", id="utf8-bom"),
    pytest.param('{"klipper": "abc"', id="unterminated-object"),
    pytest.param(b'{"klipper": "\xff\xfe"}', id="invalid-utf8-bytes"),
    pytest.param(b'{"klipper": \x00}', id="nul-byte"),
]

NON_OBJECT_JSON = [
    pytest.param("[1, 2, 3]", id="list-of-ints"),
    pytest.param('["klipper", "moonraker"]', id="list-of-str"),
    pytest.param('"v0.11.0"', id="bare-string"),
    pytest.param("42", id="bare-int"),
    pytest.param("4.2", id="bare-float"),
    pytest.param("null", id="bare-null"),
    pytest.param("true", id="bare-true"),
]

WRONG_TYPE_VALUES = [
    pytest.param('{"moonraker": null}', id="null"),
    pytest.param('{"mainsail": 2.19}', id="float"),
    pytest.param('{"fluidd": [1, 37, 6]}', id="list"),
    pytest.param('{"klipper": {"tag": "v1"}}', id="nested-object"),
    pytest.param('{"moonraker": true}', id="bool"),
    pytest.param('{"mainsail": 0}', id="int"),
]


def _lockdir(ns):
    ns.paths.LOCKFILE.parent.mkdir(parents=True, exist_ok=True)


def _write(ns, payload):
    _lockdir(ns)
    if isinstance(payload, bytes):
        ns.paths.LOCKFILE.write_bytes(payload)
    else:
        ns.paths.LOCKFILE.write_text(payload)


# --------------------------------------------------------------------------
# happy path / invariants that must hold today
# --------------------------------------------------------------------------

def test_missing_lockfile_yields_default_pins(stack):
    assert not stack.paths.LOCKFILE.exists()
    pins = stack.versions.load_pins()
    assert pins == stack.versions.DEFAULT_PINS
    assert set(pins) >= set(stack.versions.REPOS)


def test_load_pins_returns_a_fresh_copy_each_time(stack):
    pins = stack.versions.load_pins()
    assert pins is not stack.versions.DEFAULT_PINS
    pins["klipper"] = "mutated"
    pins["injected"] = True
    again = stack.versions.load_pins()
    assert again["klipper"] == stack.versions.DEFAULT_PINS["klipper"]
    assert "injected" not in again
    assert "injected" not in stack.versions.DEFAULT_PINS


def test_save_then_load_round_trip(stack):
    pins = dict(stack.versions.DEFAULT_PINS)
    pins["klipper"] = "master"
    pins["moonraker_sha"] = "0" * 40
    stack.versions.save_pins(pins)
    assert stack.versions.load_pins() == pins
    # second load is stable (idempotent read)
    assert stack.versions.load_pins() == pins


def test_save_creates_missing_parents(stack):
    import shutil
    shutil.rmtree(stack.home, ignore_errors=True)
    assert not stack.home.exists()
    stack.versions.save_pins({"klipper": "abc"})
    assert stack.paths.LOCKFILE.is_file()
    assert stack.versions.load_pins()["klipper"] == "abc"


def test_saved_file_is_sorted_json_with_trailing_newline(stack):
    stack.versions.save_pins({"zzz": "1", "klipper": "abc", "aa": "2"})
    raw = stack.paths.LOCKFILE.read_text()
    assert raw.endswith("\n")
    assert list(json.loads(raw)) == sorted(json.loads(raw))
    assert raw == json.dumps(json.loads(raw), indent=2, sort_keys=True) + "\n"


def test_save_load_cycle_is_idempotent(stack):
    """save(load(save(x))) == save(load(save(save(x)))): pins converge."""
    stack.versions.save_pins({"klipper": "abc"})
    first = stack.versions.load_pins()
    stack.versions.save_pins(first)
    raw = stack.paths.LOCKFILE.read_text()
    stack.versions.save_pins(stack.versions.load_pins())
    assert stack.paths.LOCKFILE.read_text() == raw
    assert stack.versions.load_pins() == first


def test_lockfile_overrides_only_the_keys_it_sets(stack):
    _write(stack, '{"moonraker": "v9.9.9"}')
    pins = stack.versions.load_pins()
    assert pins["moonraker"] == "v9.9.9"
    for key, value in stack.versions.DEFAULT_PINS.items():
        if key != "moonraker":
            assert pins[key] == value


def test_unknown_extra_keys_are_preserved(stack):
    _write(stack, '{"klipper": "master", "klipper_sha": "abc", "ui": "mainsail"}')
    pins = stack.versions.load_pins()
    assert pins["klipper_sha"] == "abc"
    assert pins["ui"] == "mainsail"


def test_load_pins_does_not_write_anything(stack):
    stack.versions.save_pins(dict(stack.versions.DEFAULT_PINS))
    before = stack.paths.LOCKFILE.read_bytes()
    before_stat = os.stat(stack.paths.LOCKFILE)
    stack.versions.load_pins()
    assert stack.paths.LOCKFILE.read_bytes() == before
    assert os.stat(stack.paths.LOCKFILE).st_mtime == before_stat.st_mtime


def test_home_dir_with_no_lockfile_is_read_only_safe(stack):
    """A half-created install (home exists, no lockfile) must still read pins."""
    stack.home.mkdir(parents=True, exist_ok=True)
    assert stack.versions.load_pins() == stack.versions.DEFAULT_PINS


def test_lockfile_is_symlink_to_valid_json(stack):
    _write(stack, '{"klipper": "sha-a"}')
    target = stack.paths.LOCKFILE.read_text()
    tmp = stack.home / "real-lockfile.json"
    tmp.write_text(target)
    stack.paths.LOCKFILE.unlink()
    os.symlink(str(tmp), str(stack.paths.LOCKFILE))
    assert stack.versions.load_pins()["klipper"] == "sha-a"


def test_unicode_and_awkward_but_valid_values_survive_round_trip(stack):
    pins = {
        "klipper": "v1.2.3-中文-emoji-😀",
        "moonraker": "v0.11.0",
        "mainsail": "v2.19.0",
        "fluidd": "v1.37.6",
        "ui": "fluidd",
        "note": "a\n\"quoted\" \\backslash\t tab",
        "empty": "",
        "spaced": "  leading and trailing  ",
    }
    stack.versions.save_pins(pins)
    assert stack.versions.load_pins() == pins


def test_huge_lockfile_is_parsed(stack):
    pins = {str(i): "v" + str(i) for i in range(60_000)}
    pins.update(stack.versions.DEFAULT_PINS)
    stack.versions.save_pins(pins)
    assert stack.paths.LOCKFILE.stat().st_size > 500_000
    loaded = stack.versions.load_pins()
    assert loaded["klipper"] == stack.versions.DEFAULT_PINS["klipper"]
    assert loaded["59999"] == "v59999"


def test_huge_single_pin_value_is_parsed(stack):
    blob = "v" + "x" * 2_000_000
    stack.versions.save_pins({"klipper": blob})
    assert stack.versions.load_pins()["klipper"] == blob


def test_repeated_saves_leave_exactly_one_file(stack):
    for i in range(20):
        stack.versions.save_pins({"klipper": "abc%d" % i})
    assert stack.versions.load_pins()["klipper"] == "abc19"
    assert sorted(p.name for p in stack.home.iterdir()) == ["lockfile.json"]


def test_save_pins_does_not_touch_settings_or_data(stack):
    stack.home.mkdir(parents=True, exist_ok=True)
    (stack.home / "settings.json").write_text('{"data": "x"}')
    stack.versions.save_pins({"klipper": "abc"})
    assert (stack.home / "settings.json").read_text() == '{"data": "x"}'


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_save_pins_open_failure_leaves_previous_file_intact(stack):
    """If the write cannot even start, the old pins must still be readable."""
    stack.versions.save_pins({"klipper": "good-sha"})
    before = stack.paths.LOCKFILE.read_text()
    os.chmod(stack.paths.LOCKFILE, stat.S_IRUSR)       # read-only lockfile
    try:
        with pytest.raises(OSError):
            stack.versions.save_pins({"klipper": "new-sha"})
    finally:
        os.chmod(stack.paths.LOCKFILE, 0o644)
    assert stack.paths.LOCKFILE.read_text() == before
    assert stack.versions.load_pins()["klipper"] == "good-sha"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_save_pins_into_read_only_dir_raises_os_error_not_silently(stack):
    stack.versions.save_pins({"klipper": "good-sha"})
    stack.paths.LOCKFILE.unlink()
    os.chmod(stack.home, stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(OSError):
            stack.versions.save_pins({"klipper": "sha2", "extra": "written"})
    finally:
        os.chmod(stack.home, 0o755)
    assert not stack.paths.LOCKFILE.exists()
    # nothing was half-written: pins fall back to the shipped defaults
    assert stack.versions.load_pins() == stack.versions.DEFAULT_PINS


@given(pins=st.dictionaries(st.text(min_size=1, max_size=24),
                            st.text(min_size=0, max_size=48), max_size=12))
@settings(deadline=None, max_examples=80,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_save_load_matches_dict_merge_semantics(stack, pins):
    stack.versions.save_pins(pins)
    loaded = stack.versions.load_pins()
    assert loaded == dict(stack.versions.DEFAULT_PINS, **pins)


# --------------------------------------------------------------------------
# corruption: BUG DEEP (product crashes with a raw traceback instead of
# degrading to defaults or a clean CLI error)
# --------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: load_pins() has no try/except around open()/json.load() "
    "(versions.py:30-32), so a truncated/empty/BOM/invalid-UTF-8 lockfile.json "
    "raises JSONDecodeError/UnicodeDecodeError out of `klipperformac status`, "
    "`update`, `ui` and `setup` — unlike paths.load_settings() and process.py "
    "which both swallow exactly these errors and fall back"))
@pytest.mark.parametrize("payload", BAD_JSON)
def test_load_pins_survives_unparseable_json(stack, payload):
    _write(stack, payload)
    assert stack.versions.load_pins() == stack.versions.DEFAULT_PINS


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: load_pins() does merged.update(pins) on whatever JSON was in the "
    "file (versions.py:35-36); a top-level list/string/number/null raises "
    "TypeError/ValueError instead of falling back to DEFAULT_PINS"))
@pytest.mark.parametrize("payload", NON_OBJECT_JSON)
def test_load_pins_survives_non_object_json(stack, payload):
    _write(stack, payload)
    assert stack.versions.load_pins() == stack.versions.DEFAULT_PINS


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: load_pins() returns non-string values verbatim, and "
    "check_updates() then calls _sort_key(pins[comp]) outside its try/except "
    "(versions.py:103) -> AttributeError traceback. Pins must be validated to "
    "non-empty strings (falling back to the default) at load time"))
@pytest.mark.parametrize("payload", WRONG_TYPE_VALUES)
def test_load_pins_only_returns_string_pins(stack, payload):
    _write(stack, payload)
    pins = stack.versions.load_pins()
    assert all(isinstance(v, str) for v in pins.values()), pins


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: load_pins() treats LOCKFILE.exists() as 'readable' "
    "(versions.py:30-31); a lockfile.json that is a directory (chmod 000 "
    "behaves the same way) raises IsADirectoryError/PermissionError rather "
    "than falling back to DEFAULT_PINS — a root-owned lockfile from a sudo'd "
    "install bricks every CLI command"))
def test_load_pins_survives_unreadable_lockfile(stack):
    stack.paths.LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.LOCKFILE.mkdir()
    assert stack.versions.load_pins() == stack.versions.DEFAULT_PINS
    stack.paths.LOCKFILE.rmdir()
    stack.paths.LOCKFILE.write_text('{"klipper": "sha"}')
    os.chmod(stack.paths.LOCKFILE, 0o000)
    try:
        assert stack.versions.load_pins() == stack.versions.DEFAULT_PINS
    finally:
        os.chmod(stack.paths.LOCKFILE, 0o644)


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: save_pins() writes the object it is given without validating it "
    "(versions.py:42-43), so it happily writes a JSON array lockfile that "
    "its own load_pins() cannot read back — the write should be rejected or "
    "coerced to a mapping"))
def test_save_pins_refuses_to_poison_the_lockfile(stack):
    stack.versions.save_pins(dict(stack.versions.DEFAULT_PINS))
    good = stack.paths.LOCKFILE.read_text()
    try:
        stack.versions.save_pins(["klipper", "moonraker"])
    except (TypeError, ValueError):
        return                       # rejected up front would be acceptable
    assert stack.paths.LOCKFILE.read_text() == good
    assert stack.versions.load_pins() == stack.versions.DEFAULT_PINS


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: save_pins() truncates the live lockfile with open(path,'w') and "
    "serialises into it (versions.py:42-44), so a failure part-way through "
    "json.dump (unserialisable value, killed process, ENOSPC) leaves a half-written "
    "lockfile and the previously good pins are lost — paths.save_settings() gets "
    "this right with tmp + os.replace()"))
def test_save_pins_is_atomic_when_serialisation_fails(stack):
    stack.versions.save_pins(dict(stack.versions.DEFAULT_PINS))
    before = stack.paths.LOCKFILE.read_text()
    bad = dict(stack.versions.DEFAULT_PINS)
    bad["aaa_first"] = "written-before-the-crash"
    bad["zzz_last"] = {1, 2, 3}          # not JSON-serialisable
    with pytest.raises(TypeError):
        stack.versions.save_pins(bad)
    assert stack.paths.LOCKFILE.read_text() == before
    assert stack.versions.load_pins() == stack.versions.DEFAULT_PINS
