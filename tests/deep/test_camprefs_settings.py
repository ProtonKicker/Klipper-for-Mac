"""Deep battery: cam preferences <-> home/settings.json.

Round trips, None deletion, non-dict `cam` values, hostile device names
(newlines, quotes, unicode, megabyte-long), idempotence, and what happens when
the prefs file itself is corrupt, unreadable or holds non-standard JSON.

Everything runs on the throwaway home+data from the `stack` fixture. The
camera helper is never executed (cam.subprocess is replaced by a blocker), no
process is launched for boot time / live pid lookup, and the real
/Applications bundle is never looked at.
"""
import argparse
import json
import os
import subprocess

import pytest
from hypothesis import HealthCheck, given, settings as hst, strategies as st

DEFAULTS = {"width": 1280, "height": 720, "fps": 15, "quality": 80}
PREF_KEYS_ORDER = ("width", "height", "fps", "quality", "device")
CLI_FLAGS = ("device", "width", "height", "fps", "quality")
BOOT = "1700000000"
TEXT = st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=60)


class NoExec(BaseException):
    """Raised instead of ever launching a process from this battery."""


class BlockedSubprocess:
    """Stand-in for the subprocess module: any real exec is a test failure."""
    DEVNULL = getattr(subprocess, "DEVNULL", -3)
    TimeoutExpired = subprocess.TimeoutExpired

    @staticmethod
    def run(*a, **k):
        raise NoExec("subprocess.run blocked: " + repr(a))

    @staticmethod
    def Popen(*a, **k):
        raise NoExec("subprocess.Popen blocked: " + repr(a))


@pytest.fixture(autouse=True)
def _hermetic(stack, tmp_path, monkeypatch):
    monkeypatch.setattr(stack.cam, "GUI_BUNDLES",
                        (str(tmp_path / "absent.app/Contents/Resources/KlipperCam"),))
    monkeypatch.delenv("KLIPPERFORMAC_CAM_BIN", raising=False)
    monkeypatch.delenv("KLIPPERFORMAC_REPO_DIR", raising=False)
    monkeypatch.setattr(stack.cam, "subprocess", BlockedSubprocess)
    monkeypatch.setattr(stack.cam, "_process", lambda: stack.process)
    monkeypatch.setattr(stack.process, "_boot_time", lambda: BOOT)
    monkeypatch.setattr(stack.process, "_live_cmds", lambda pids: {})
    monkeypatch.setattr(stack.cam, "_port_free", lambda port: True)
    return stack


def cam_config_args(**kw):
    base = dict(action="config", path=None, device=None, width=None,
                height=None, fps=None, quality=None, show=False)
    base.update(kw)
    return argparse.Namespace(**base)


def write_settings(stack, text):
    stack.home.mkdir(parents=True, exist_ok=True)
    stack.paths.SETTINGS.write_text(text)


def read_cam_blob(stack):
    return json.loads(stack.paths.SETTINGS.read_text())["cam"]


# ---------------------------------------------------------------- round trips


def test_prefs_are_defaults_when_nothing_was_saved(stack):
    assert stack.paths.cam_settings() == {}
    assert stack.cam.prefs() == DEFAULTS
    assert stack.cam.PREF_KEYS == PREF_KEYS_ORDER
    assert set(stack.cam.PREF_KEYS) == set(CLI_FLAGS)
    # reading prefs must not create anything
    assert not stack.home.exists()


def test_save_roundtrip_and_none_deletes(stack):
    cam = stack.cam.save_prefs(width=640, height=480, fps=24, quality=12,
                               device="FaceTime HD Camera")
    assert cam == {"width": 640, "height": 480, "fps": 24, "quality": 12,
                   "device": "FaceTime HD Camera"}
    assert stack.cam.prefs() == cam
    again = stack.cam.save_prefs(device=None, width=None)
    assert "device" not in again and "width" not in again
    merged = stack.cam.prefs()
    assert "device" not in merged          # deleted key is really gone
    assert merged["width"] == 1280          # ... and falls back to the default
    assert merged["height"] == 480 and merged["fps"] == 24


def test_save_prefs_is_byte_identical_when_repeated(stack):
    first = stack.cam.save_prefs(fps=20, device="cam")
    blob = stack.paths.SETTINGS.read_bytes()
    stack.cam.save_prefs(fps=20, device="cam")
    assert stack.paths.SETTINGS.read_bytes() == blob
    assert json.loads(blob.decode())["cam"] == first


def test_prefs_never_leak_into_module_defaults(stack):
    stack.cam.save_prefs(width=1, height=1, fps=1, quality=1, device="x" * 500)
    p = stack.cam.prefs()
    p["width"] = 99999
    p["device"] = "mutated"
    assert stack.cam.DEFAULT_PREFS == DEFAULTS
    assert stack.cam.PREF_KEYS == PREF_KEYS_ORDER
    assert stack.cam.prefs()["width"] == 1
    assert stack.paths.cam_settings()["width"] == 1


def test_other_settings_keys_survive_a_cam_save(stack):
    write_settings(stack, json.dumps({"data": "/Volumes/BigDrive/kd",
                                      "lan": True, "cam": {"fps": 5}}) + "\n")
    stack.cam.save_prefs(quality=90)
    blob = json.loads(stack.paths.SETTINGS.read_text())
    assert blob["data"] == "/Volumes/BigDrive/kd"
    assert blob["lan"] is True
    assert blob["cam"] == {"fps": 5, "quality": 90}


@pytest.mark.parametrize("junk", [
    "5", '"Cam"', "[1, 2]", "null", "true", "false", "[]", '""', "[[[]]]",
])
def test_non_dict_cam_value_falls_back_to_defaults(stack, junk):
    write_settings(stack, '{"data": "%s", "cam": %s}' % (stack.data, junk))
    assert stack.paths.cam_settings() == {}
    assert stack.cam.prefs() == DEFAULTS
    # and a save over a non-dict cam replaces the junk with a real dict
    assert stack.cam.save_prefs(fps=11) == {"fps": 11}
    assert json.loads(stack.paths.SETTINGS.read_text())["data"] == str(stack.data)


@pytest.mark.parametrize("junk", ['{"a": {"b": [1]}}', '{"1": 2}',
                                 '{"width": "640", "extra": []}'])
def test_dict_cam_value_is_merged_onto_defaults(stack, junk):
    write_settings(stack, '{"cam": %s}' % junk)
    stored = json.loads(junk)
    p = stack.cam.prefs()
    for k, v in stored.items():
        if v is not None:
            assert p[k] == v
        else:
            assert p[k] == DEFAULTS[k]
    for k in DEFAULTS:
        if k not in stored:
            assert p[k] == DEFAULTS[k]


def test_explicit_null_pref_means_default(stack):
    write_settings(stack, json.dumps(
        {"cam": {"width": None, "fps": None, "device": "Real"}}))
    p = stack.cam.prefs()
    assert p["width"] == DEFAULTS["width"]
    assert p["fps"] == DEFAULTS["fps"]
    assert p["device"] == "Real"


def test_unknown_pref_keys_are_kept_but_inert(stack):
    stack.cam.save_prefs(brightness="loud", device="cam")
    stored = read_cam_blob(stack)
    assert stored["brightness"] == "loud"
    p = stack.cam.prefs()
    assert p["brightness"] == "loud"                 # passthrough
    assert stack.cam._save_state(4242, 8082, p) is True
    blob = json.loads(stack.paths.CAM_STATE.read_text())
    assert "brightness" not in blob                  # but never persisted
    assert blob["device"] == "cam" and blob["pid"] == 4242


def test_empty_save_creates_an_empty_cam_dict(stack):
    assert stack.cam.save_prefs() == {}
    assert stack.paths.cam_settings() == {}
    assert stack.cam.prefs() == DEFAULTS


# ------------------------------------------------------------- hostile values

HOSTILE_DEVICES = [
    "Cam\nWith\nNewlines",
    'He said "no" and \'maybe\'',
    "tab\tsep\rcr",
    "[webcam evil]\nenabled: False",
    "--stream --port 1 --device /dev/cu.frontcamera",
    "$(rm -rf ~)`; rm -rf /; `",
    "../../../etc/cu.usbserial",
    "cam; shutdown -h now",
    "‮ ⁦fake bidi⁩",                     # bidi overrides
    "📷 🎥 ƈά",                          # emoji + math script
    "A\u0301́́́\u0000zero byte",               # combining marks + NUL
    " " * 40 + " ",
    "",
    "x" * 200_000,
]


@pytest.mark.parametrize("name", HOSTILE_DEVICES)
def test_hostile_device_names_round_trip_exactly(stack, name):
    stack.cam.save_prefs(device=name)
    assert stack.cam.prefs()["device"] == name
    assert read_cam_blob(stack)["device"] == name
    # deleting by None must work for the nastiest names too
    assert "device" not in stack.cam.save_prefs(device=None)
    assert "device" not in stack.cam.prefs()


def test_huge_settings_file_is_still_processed(stack):
    blob = {"cam": {"device": "d" * 1_000_000, "fps": 30},
            "junk": ["y" * 100] * 2000}
    write_settings(stack, json.dumps(blob))
    assert stack.paths.cam_settings()["fps"] == 30
    assert len(stack.cam.prefs()["device"]) == 1_000_000
    out = stack.cam.save_prefs(quality=55)
    assert out["quality"] == 55 and len(out["device"]) == 1_000_000
    assert json.loads(stack.paths.SETTINGS.read_text())["cam"]["quality"] == 55


def test_non_object_settings_root_falls_back_to_empty(stack):
    for root in ("[1, 2]", '"just a string"', "7", "null", "true", "[]"):
        write_settings(stack, root)
        assert stack.paths.cam_settings() == {}
        assert stack.cam.prefs() == DEFAULTS
        assert stack.cam.save_prefs(fps=15) == {"fps": 15}


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: a corrupt settings.json is silently read as {} and the next cam "
    "save overwrites the file, destroying the 'data' folder pointer and every "
    "other stored setting (the stack then looks like a fresh install)"))
def test_corrupt_settings_json_save_does_not_lose_other_keys(stack):
    write_settings(stack, '{"data": "/Volumes/BigDrive/kd", "lan": tru')
    stack.cam.save_prefs(fps=20)
    text = stack.paths.SETTINGS.read_text()
    backups = list(stack.home.glob("*bak*"))
    assert "/Volumes/BigDrive/kd" in text or backups, \
        "the torn prefs file was replaced and the data pointer is gone"


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod is a no-op for root")
@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: same class as the corrupt-prefs bug - an EACCES on "
    "settings.json is swallowed by load_settings(), so saving cam prefs "
    "overwrites a prefs file the user can still see, losing the data pointer"))
def test_unreadable_settings_json_save_does_not_lose_other_keys(stack):
    write_settings(stack, '{"data": "/Volumes/BigDrive/kd"}')
    os.chmod(stack.paths.SETTINGS, 0o000)
    try:
        stack.cam.save_prefs(fps=20)
    finally:
        os.chmod(stack.paths.SETTINGS, 0o644)
    assert "/Volumes/BigDrive/kd" in stack.paths.SETTINGS.read_text()


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: paths.save_settings() uses json.dumps with the default "
    "allow_nan=True, so a NaN/Infinity that was ever in settings.json is "
    "written back as the non-standard literals NaN/Infinity; the SwiftUI app "
    "(JSONSerialization) and every strict JSON parser then reject the whole "
    "file, which silently puts the data folder back to its default"))
def test_saved_prefs_are_strict_valid_json(stack):
    write_settings(stack, '{"data": "/Volumes/X", "cam": {"quality": Infinity,'
                          ' "width": NaN}}')
    stack.cam.save_prefs(fps=20)

    def _strict(const):
        raise ValueError("not strict JSON: " + const)

    blob = json.loads(stack.paths.SETTINGS.read_text(), parse_constant=_strict)
    assert blob["data"] == "/Volumes/X"


@pytest.mark.parametrize("value", ["NaN", '"text"', "[]", "{}", "true",
                                  '"  "', "999999999999999", "-1"])
def test_doctor_gives_a_prefs_verdict_for_absurd_values(stack, value):
    """Doctor must answer with a readable verdict, never an internal crash."""
    write_settings(stack, '{"cam": {"quality": %s}}' % value)
    reports = [m for ok, m in stack.cam.doctor() if "preference" in m]
    assert reports, "the camera prefs check printed nothing: " + \
        str(stack.cam.doctor())
    assert not any("failed:" in m for m in reports), reports


@pytest.mark.parametrize("value", ["Infinity", "-Infinity"])
def test_doctor_survives_infinity_pref(stack, value):
    write_settings(stack, '{"cam": {"quality": %s}}' % value)
    reports = [m for ok, m in stack.cam.doctor() if "preference" in m]
    assert reports, "the camera prefs check printed nothing"


# ------------------------------------------------------------------ fuzz zone

ANY_VALUE = st.one_of(
    st.none(),
    st.integers(min_value=-10 ** 12, max_value=10 ** 12),
    TEXT,
    st.booleans(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.lists(st.integers() | TEXT, max_size=4),
    st.dictionaries(TEXT, st.integers(), max_size=3),
)


@hst(max_examples=40, deadline=None,
     suppress_health_check=[HealthCheck.too_slow,
                            HealthCheck.function_scoped_fixture])
@given(values=st.dictionaries(st.sampled_from(PREF_KEYS_ORDER), ANY_VALUE,
                              max_size=5))
def test_save_prefs_roundtrip_property(values, stack):
    write_settings(stack, '{"cam": {}, "data": "%s"}' % stack.data)
    stored = stack.cam.save_prefs(**values)
    expected = {k: v for k, v in values.items() if v is not None}
    assert stored == expected
    merged = stack.cam.prefs()
    for k, v in expected.items():
        assert merged[k] == v
    for k in DEFAULTS:
        if k not in expected:
            assert merged[k] == DEFAULTS[k]
    assert json.loads(stack.paths.SETTINGS.read_text())["cam"] == expected
    # idempotent: saving the same view again changes nothing byte-wise
    blob = stack.paths.SETTINGS.read_bytes()
    stack.cam.save_prefs(**values)
    assert stack.paths.SETTINGS.read_bytes() == blob
    assert stack.cam.DEFAULT_PREFS == DEFAULTS


@hst(max_examples=40, deadline=None,
     suppress_health_check=[HealthCheck.too_slow,
                            HealthCheck.function_scoped_fixture])
@given(cam=st.dictionaries(TEXT, ANY_VALUE, max_size=6))
def test_prefs_and_doctor_never_raise_for_any_cam_dict(cam, stack):
    write_settings(stack, json.dumps({"cam": cam, "data": str(stack.data)}))
    p = stack.cam.prefs()
    assert isinstance(p, dict) and p["width"] is not None
    for ok, msg in stack.cam.doctor():
        assert isinstance(ok, bool) and isinstance(msg, str) and msg
    again = stack.cam.save_prefs()
    assert again == cam                      # an empty save is a pure no-op merge
    assert json.loads(stack.paths.SETTINGS.read_text())["cam"] == cam


@hst(max_examples=25, deadline=None,
     suppress_health_check=[HealthCheck.too_slow,
                            HealthCheck.function_scoped_fixture])
@given(name=TEXT)
def test_any_unicode_device_survives_the_cli_path(name, stack, capsys):
    stack.cli.cmd_cam(cam_config_args(device=name))
    printed = capsys.readouterr().out
    assert "Saved camera settings" in printed
    assert stack.cam.prefs()["device"] == name


# ------------------------------------------------------------------- cli view


def test_cli_config_validation_rejects_and_writes_nothing(stack):
    for kw in (dict(fps=0), dict(quality=101), dict(width=159),
               dict(height=119), dict(fps=-1)):
        with pytest.raises(SystemExit):
            stack.cli.cmd_cam(cam_config_args(**kw))
    assert not stack.paths.SETTINGS.exists()


def test_cli_config_shows_defaults_without_writing(stack, capsys):
    stack.cli.cmd_cam(cam_config_args())
    out = capsys.readouterr().out
    assert "Camera settings:" in out and "1280" in out
    assert not stack.paths.SETTINGS.exists()


def test_cli_config_accepts_boundary_values(stack):
    stack.cli.cmd_cam(cam_config_args(fps=60, quality=100, width=4096,
                                      height=4096))
    p = stack.cam.prefs()
    assert (p["fps"], p["quality"], p["width"], p["height"]) == \
        (60, 100, 4096, 4096)


def test_cli_device_is_a_string_even_for_indices(stack):
    """--device has no type=int: '0' is stored as the text '0'."""
    stack.cli.cmd_cam(cam_config_args(device="0"))
    assert stack.cam.prefs()["device"] == "0"
    assert read_cam_blob(stack)["device"] == "0"
