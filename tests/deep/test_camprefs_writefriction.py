"""Deep battery: the camera prefs/state files under FILESYSTEM friction.

cam.py and paths.py promise three things about persistence:

  * settings.json is written "write-then-replace", so a failure can never
    truncate the live prefs (configgen._write_conf says the same out loud);
  * paths.cam_settings() degrades to {} for anything unreadable, so
    cam prefs/status/doctor/on/off always answer;
  * cam._save_state() returns False instead of raising when the run
    directory cannot be written, and start() then warns the user.

This battery attacks exactly those: read-only homes, a settings.json (or its
sidecar temp file, or the run directory) replaced by a directory, dangling and
foreign symlinks, a huge device name, non-identifier keys, and values that JSON
allows but nobody expected. Everything happens inside the throwaway home+data
from the `stack` fixture; the helper is never executed (cam.subprocess is a
blocker), no camera device is opened and no real port is bound.
"""
import argparse
import json
import os
import subprocess

import pytest
from hypothesis import (HealthCheck, assume, given, settings as hst,
                        strategies as st)

BOOT = "1700000000"
DEFAULTS = {"width": 1280, "height": 720, "fps": 15, "quality": 80}
NEEDS_PERM = pytest.mark.skipif(os.geteuid() == 0,
                                 reason="chmod is a no-op for root")


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


def _block(path, mode):
    """chmod for the duration of one test; the fixture unlocks everything again."""
    os.chmod(str(path), mode)
    return path


def _unlock(root):
    """Make a whole throwaway tree writable again so pytest can delete it."""
    for dirpath, dirnames, filenames in os.walk(str(root)):
        for name in dirnames:
            try:
                os.chmod(os.path.join(dirpath, name), 0o700)
            except OSError:
                pass
        for name in filenames:
            try:
                os.chmod(os.path.join(dirpath, name), 0o600)
            except OSError:
                pass
    try:
        os.chmod(str(root), 0o700)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def hermetic(stack, tmp_path, monkeypatch):
    monkeypatch.setattr(stack.cam, "GUI_BUNDLES",
                        (str(tmp_path / "absent.app/Contents/Resources/KlipperCam"),))
    monkeypatch.setattr(stack.cam, "CAM_BIN_ENVS", ())
    monkeypatch.setattr(stack.cam, "REPO_DIR_ENVS", ())
    monkeypatch.setenv("KLIPPERFORMAC_CAM_BIN", str(tmp_path / "no-helper"))
    monkeypatch.setattr(stack.cam, "subprocess", BlockedSubprocess)
    monkeypatch.setattr(stack.cam, "_process", lambda: stack.process)
    monkeypatch.setattr(stack.cam, "_port_free", lambda port: True)
    monkeypatch.setattr(stack.cam.time, "sleep", lambda s: None)
    monkeypatch.setattr(stack.process, "_boot_time", lambda: BOOT)
    monkeypatch.setattr(stack.process, "_live_cmds", lambda pids: {})
    stack.paths.CONFIG.mkdir(parents=True, exist_ok=True)
    yield stack
    _unlock(stack.home)
    _unlock(stack.data)


def write_settings(stack, text):
    stack.home.mkdir(parents=True, exist_ok=True)
    stack.paths.SETTINGS.write_text(text)


def cam_args(**kw):
    """A parsed `klipperformac cam ...` namespace, like cli's own parser makes."""
    base = dict(action="config", path=None, device=None, width=None,
                height=None, fps=None, quality=None, show=False)
    base.update(kw)
    return argparse.Namespace(**base)


def read_settings_raw(stack):
    return stack.paths.SETTINGS.read_text()


def cam_blob(stack):
    return json.loads(read_settings_raw(stack))["cam"]


# ------------------------------------------------- what a good save guarantees


def test_save_creates_the_home_and_leaves_exactly_one_file(stack):
    assert not stack.home.exists()
    assert stack.cam.save_prefs(fps=20) == {"fps": 20}
    assert stack.home.is_dir()
    assert sorted(p.name for p in stack.home.iterdir()) == ["settings.json"]
    assert json.loads(read_settings_raw(stack)) == {"cam": {"fps": 20}}


def test_save_is_atomic_on_success_and_reuses_the_same_sidecar_name(stack):
    """The temp file is a sibling; after a good save nothing but prefs is left."""
    write_settings(stack, '{"data": "%s", "cam": {"fps": 9}}\n' % stack.data)
    assert stack.cam.save_prefs(quality=50) == {"fps": 9, "quality": 50}
    assert sorted(p.name for p in stack.home.iterdir()) == ["settings.json"]
    # a second save rewrites (not appends to) the same sidecar path
    assert stack.paths.SETTINGS.with_suffix(".json.tmp").parent == stack.home
    second = stack.cam.save_prefs(width=1024)
    assert sorted(p.name for p in stack.home.iterdir()) == ["settings.json"]
    assert second == {"fps": 9, "quality": 50, "width": 1024}


@NEEDS_PERM
def test_the_live_prefs_are_never_opened_for_writing(stack):
    """A blocked sidecar proves the write goes to a temp file first: the live
    settings.json keeps its exact bytes when the swap cannot happen."""
    write_settings(stack, '{"data": "/Volumes/BigDrive/kd", "cam": {"fps": 9}}')
    before = stack.paths.SETTINGS.read_bytes()
    side = stack.paths.SETTINGS.with_suffix(".json.tmp")
    side.mkdir()
    try:
        with pytest.raises(OSError):
            stack.cam.save_prefs(fps=20)
    finally:
        side.rmdir()
    assert stack.paths.SETTINGS.read_bytes() == before
    assert "/Volumes/BigDrive/kd" in read_settings_raw(stack)
    assert stack.cam.prefs()["fps"] == 9


def test_a_stale_sidecar_is_ignored_on_read_and_replaced_on_write(stack):
    """A settings.json.tmp left by a crash must not shadow the real prefs."""
    write_settings(stack, '{"cam": {"fps": 9}}')
    side = stack.paths.SETTINGS.with_suffix(".json.tmp")
    side.write_text('{"cam": {"fps": 111}} garbage not even json {')
    assert stack.cam.prefs()["fps"] == 9
    assert stack.cam.save_prefs(fps=30) == {"fps": 30}
    assert stack.cam.prefs()["fps"] == 30
    assert json.loads(read_settings_raw(stack))["cam"] == {"fps": 30}


# --------------------------------------- blocked/odd homes: reads stay alive


@NEEDS_PERM
def test_reads_still_work_when_the_home_is_read_only(stack):
    write_settings(stack, '{"data": "/Volumes/X", "cam": {"fps": 9}}')
    _block(stack.home, 0o555)
    assert stack.paths.cam_settings() == {"fps": 9}
    assert stack.cam.prefs()["fps"] == 9
    assert [m for ok, m in stack.cam.doctor() if "preference" in m]
    assert any("9fps" in l for l in stack.cam.status())


def test_settings_json_as_a_directory_reads_as_defaults(stack):
    stack.home.mkdir(parents=True, exist_ok=True)
    stack.paths.SETTINGS.mkdir()
    assert stack.paths.load_settings() == {}
    assert stack.paths.cam_settings() == {}
    assert stack.cam.prefs() == DEFAULTS
    reports = stack.cam.doctor()
    assert len(reports) == 6 and all(isinstance(m, str) for ok, m in reports)
    stack.paths.SETTINGS.rmdir()


def test_settings_json_unreadable_still_reads_as_defaults(stack):
    write_settings(stack, '{"cam": {"fps": 9}, "data": "/Volumes/X"}')
    if os.geteuid() != 0:
        _block(stack.paths.SETTINGS, 0o000)
        assert stack.paths.cam_settings() == {}
    assert stack.cam.prefs() == DEFAULTS
    assert len(stack.cam.doctor()) == 6


def test_settings_json_symlinked_elsewhere_is_read_through(stack, tmp_path):
    real = tmp_path / "elsewhere.json"
    real.write_text('{"data": "/Volumes/X", "cam": {"fps": 7}}')
    stack.home.mkdir(parents=True, exist_ok=True)
    stack.paths.SETTINGS.symlink_to(real)
    assert stack.cam.prefs()["fps"] == 7
    # a dangling symlink is just "no prefs", never a crash
    stack.paths.SETTINGS.unlink()
    stack.paths.SETTINGS.symlink_to(tmp_path / "gone.json")
    assert stack.paths.cam_settings() == {}
    assert stack.cam.prefs() == DEFAULTS


def test_dangling_parent_symlink_for_settings_does_not_confuse_reads(stack,
                                                                    tmp_path):
    loop = tmp_path / "loopdir"
    loop.symlink_to(loop.name)          # a self-referencing symlink: ELOOP
    stack.home.mkdir(parents=True, exist_ok=True)
    stack.paths.SETTINGS.symlink_to(loop / "a" / "settings.json")
    assert stack.paths.cam_settings() == {}
    assert stack.cam.prefs() == DEFAULTS


# ------------------------------------- the same blocked homes on the write path


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: paths.save_settings() (used by cam.save_prefs, i.e. every "
    "`klipperformac cam config ...` and the tail of `cam on`) has no error "
    "handling at all, so an unwritable ~/.klipperformac - a root-owned "
    "settings.json after one sudo run, a read-only home, a directory in the "
    "place of the prefs or of the settings.json.tmp sidecar - makes the CLI die "
    "with a raw PermissionError/IsADirectoryError traceback instead of the "
    "one-line error every other cam path prints (compare cam._save_state, "
    "cam.build_helper and cli._die)"))
@NEEDS_PERM
@pytest.mark.parametrize("layout", [
    "read-only home", "settings.json is a directory",
    "sidecar is a directory", "home is a regular file",
    "sidecar is an unwritable file",
])
def test_cam_config_reports_a_blocked_prefs_file_instead_of_tracebacking(
        stack, layout, capsys):
    stack.home.mkdir(parents=True, exist_ok=True)
    side = stack.paths.SETTINGS.with_suffix(".json.tmp")
    if layout == "read-only home":
        stack.paths.SETTINGS.write_text('{"cam": {"fps": 9}}')
        _block(stack.home, 0o555)
    elif layout == "settings.json is a directory":
        stack.paths.SETTINGS.mkdir()
    elif layout == "sidecar is a directory":
        stack.paths.SETTINGS.write_text('{"cam": {"fps": 9}}')
        side.mkdir()
    elif layout == "home is a regular file":
        stack.home.rmdir()
        stack.home.write_text("not a folder")
    else:
        side.write_text("old")
        _block(side, 0o444)
    args = cam_args(fps=20)
    with pytest.raises(SystemExit) as info:      # wanted: a clean error + exit
        stack.cli.cmd_cam(args)
    assert info.value.code in (0, 1)
    assert "Traceback" not in capsys.readouterr().err


# ---------------------------------------------------- the run/ state file write


def test_save_state_reports_false_when_the_run_dir_is_read_only(stack):
    stack.paths.RUN.mkdir(parents=True, exist_ok=True)
    if os.geteuid() != 0:
        _block(stack.paths.RUN, 0o555)
        assert stack.cam._save_state(4242, 8082, DEFAULTS) is False
        assert not stack.paths.CAM_STATE.exists()
    else:
        assert stack.cam._save_state(4242, 8082, DEFAULTS) is True


def test_save_state_reports_false_when_the_state_is_a_dangling_symlink(stack):
    stack.paths.RUN.mkdir(parents=True, exist_ok=True)
    stack.paths.CAM_STATE.symlink_to(stack.paths.RUN.parent / "nope" / "cam.json")
    assert stack.cam._save_state(4242, 8082, DEFAULTS) is False
    assert stack.cam.running() is None


def test_save_state_then_clearing_a_symlinked_state_never_follows_it_out(
        stack, tmp_path):
    outside = tmp_path / "important.json"
    outside.write_text("do not touch")
    stack.paths.RUN.mkdir(parents=True, exist_ok=True)
    stack.cam._save_state(4242, 8082, DEFAULTS)
    stack.paths.CAM_STATE.unlink()
    stack.paths.CAM_STATE.symlink_to(outside)
    assert stack.cam.running() is None          # marker check fails -> heal
    assert outside.read_text() == "do not touch"


def test_save_state_survives_a_huge_device_name(stack):
    p = dict(DEFAULTS)
    p["device"] = "d" * 2_000_000
    assert stack.cam._save_state(4242, 8082, p) is True
    blob = json.loads(stack.paths.CAM_STATE.read_text())
    assert len(blob["device"]) == 2_000_000 and blob["pid"] == 4242
    assert stack.cam._clear_state() is None
    assert not stack.paths.CAM_STATE.exists()


def test_save_state_stores_false_but_not_none(stack):
    """`None` means "use the default"; a JSON false is a real value."""
    assert stack.cam._save_state(4242, 8082, {"device": False, "fps": None,
                                             "width": 0}) is True
    blob = json.loads(stack.paths.CAM_STATE.read_text())
    assert blob["device"] is False and blob["width"] == 0
    assert "fps" not in blob


# ------------------------------------------------- adversarial but legal values


def test_a_five_megabyte_device_round_trips_and_deletes(stack):
    name = "📷" * 1_000_000
    write_settings(stack, '{"data": "/Volumes/X", "cam": {}}')
    assert stack.cam.save_prefs(device=name)["device"] == name
    assert stack.cam.prefs()["device"] == name
    assert "/Volumes/X" in read_settings_raw(stack)
    assert "device" not in stack.cam.save_prefs(device=None)
    assert len(stack.cam.doctor()) == 6


def test_nul_and_control_characters_in_the_device_round_trip(stack):
    for name in ("cam\x00name", "\x01\x02\x1b[31mred\x1b[0m", "\x7f\x1c",
                 "line\r\nfeed\x0b\x0c", "\\u0000 escaped literally"):
        stack.cam.save_prefs(device=name)
        assert stack.cam.prefs()["device"] == name
        assert cam_blob(stack)["device"] == name
        assert json.loads(read_settings_raw(stack)), name
        assert stack.cam.save_prefs(device=None) == {}


def test_two_hundred_unknown_keys_do_not_disturb_the_managed_ones(stack):
    junk = {"k%d" % i: i for i in range(200)}
    junk.update({"fps": 20, "device": "cam"})
    stack.cam.save_prefs(**junk)
    stored = cam_blob(stack)
    assert stored == junk
    p = stack.cam.prefs()
    assert p["fps"] == 20 and p["device"] == "cam"
    assert p["width"] == DEFAULTS["width"]
    for ok, msg in stack.cam.doctor():
        assert isinstance(msg, str) and msg


def test_saving_a_key_named_cam_nests_harmlessly(stack):
    out = stack.cam.save_prefs(cam={"inner": 1}, fps=12)
    assert out == {"cam": {"inner": 1}, "fps": 12}
    p = stack.cam.prefs()
    assert p["fps"] == 12 and p["width"] == DEFAULTS["width"]
    assert p["cam"] == {"inner": 1}
    assert len(stack.cam.doctor()) == 6


def test_a_bignum_pref_gets_a_verdict_not_a_crash(stack):
    write_settings(stack, '{"cam": {"width": %s, "quality": -%s}}'
                   % (10 ** 400, 10 ** 400))
    reports = [(ok, m) for ok, m in stack.cam.doctor() if "preference" in m]
    assert reports and reports[0][0] is False
    assert "width" in reports[0][1] and "quality" in reports[0][1]


def test_a_settings_tree_full_of_containers_is_kept_verbatim(stack):
    cam = {"width": [1, 2, {"3": None}], "device": {"a": [None, True]},
           "fps": []}
    write_settings(stack, json.dumps({"cam": cam}))
    assert stack.cam.prefs()["fps"] == []          # [] is not None: kept
    assert stack.cam.prefs()["width"] == cam["width"]
    assert stack.cam.save_prefs(quality=1) is not None
    assert json.loads(read_settings_raw(stack))["cam"]["device"] == cam["device"]
    assert len(stack.cam.doctor()) == 6


def test_utf16_and_bom_prefs_are_inert_rather_than_fatal(stack):
    """Not a crash, and not a silent wipe of the *read* path either."""
    stack.home.mkdir(parents=True, exist_ok=True)
    for label, blob in (("bom", '﻿{"cam": {"fps": 9}}'.encode("utf-8")),
                        ("latin1", b'{"cam": {"device": "caf\xe9"}}'),
                        ("utf16", '{"cam": {"fps": 9}}'.encode("utf-16"))):
        stack.paths.SETTINGS.write_bytes(blob)
        assert stack.paths.cam_settings() == {}, label
        assert stack.cam.prefs() == DEFAULTS, label
        assert len(stack.cam.doctor()) == 6, label


@pytest.mark.parametrize("junk, expect", [
    ('{"cam": {"fps": 1}, "cam": {"fps": 2}}', {"fps": 2}),
    ('{"cam": {"fps": 2}, "cam": {"fps": 1}}', {"fps": 1}),
    ('{"cam": {"device": "a"}, "cam": {"fps": 3}}', {"fps": 3}),
])
def test_duplicate_cam_keys_follow_the_json_last_wins_rule(stack, junk, expect):
    write_settings(stack, junk)
    assert stack.paths.cam_settings() == expect
    assert stack.cam.prefs()["fps"] == expect.get("fps", DEFAULTS["fps"])


# ------------------------------------------------------------------ fuzz zones

ANY_PREF = st.one_of(
    st.none(),
    st.integers(min_value=-10 ** 30, max_value=10 ** 30),
    st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=200),
    st.booleans(),
    st.lists(st.integers(), max_size=3),
    st.dictionaries(st.text(max_size=8), st.integers(), max_size=3),
)


@hst(max_examples=40, deadline=None,
     suppress_health_check=[HealthCheck.too_slow,
                            HealthCheck.function_scoped_fixture])
@given(ops=st.lists(st.dictionaries(st.sampled_from(
                          ("width", "height", "fps", "quality", "device",
                           "weird key", "", "cam")),
                          ANY_PREF, max_size=4),
                     min_size=1, max_size=6))
def test_any_sequence_of_saves_matches_a_plain_dict_model(ops, stack):
    """Differential test: save_prefs(**kw) must behave exactly like
    dict.update() with None acting as del - never partial, never reordered."""
    write_settings(stack, '{"cam": {}, "data": "%s"}' % stack.data)
    model = {}
    for op in ops:
        got = stack.cam.save_prefs(**op)
        for k, v in op.items():
            if v is None:
                model.pop(k, None)
            else:
                model[k] = v
        assert got == model
    assert stack.paths.cam_settings() == model
    merged = stack.cam.prefs()
    for k, v in model.items():
        if v is not None:
            assert merged[k] == v
    blob = json.loads(read_settings_raw(stack))
    assert blob["cam"] == model
    assert len(stack.cam.doctor()) == 6


@hst(max_examples=30, deadline=None,
     suppress_health_check=[HealthCheck.too_slow,
                            HealthCheck.function_scoped_fixture])
@given(name=st.text(alphabet=st.characters(exclude_categories=("Cs",)),
                    max_size=400))
def test_every_encodable_device_name_survives_the_whole_prefs_pipeline(name,
                                                                      stack):
    stack.cam.save_prefs(device=name)
    assert stack.cam.prefs()["device"] == name
    assert stack.cam._save_state(4242, 8082, stack.cam.prefs()) is True
    assert json.loads(stack.paths.CAM_STATE.read_text())["device"] == name
    assert stack.cam._clear_state() is None
    assert any(name in l or "device" in l for l in stack.cam.status())
    assert len(stack.cam.doctor()) == 6
    stack.cam.save_prefs(device=None)
    assert "device" not in stack.cam.prefs()


@hst(max_examples=25, deadline=None,
     suppress_health_check=[HealthCheck.too_slow,
                            HealthCheck.function_scoped_fixture])
@given(raw=st.text(alphabet=st.characters(exclude_categories=("Cs",)),
                   max_size=3000))
def test_garbage_prefs_text_never_raises_and_never_eats_a_good_save(raw, stack):
    """Unparseable junk only: the JSON-root-that-is-not-an-object case is a
    separately filed bug, and valid prefs files are covered next door."""
    try:
        json.loads(raw)
    except ValueError:
        pass
    else:
        assume(False)
    write_settings(stack, raw)
    assert stack.paths.cam_settings() == {}
    assert stack.cam.prefs() == DEFAULTS
    assert len(stack.cam.doctor()) == 6
    assert stack.cam.save_prefs(fps=15) == {"fps": 15}
    assert json.loads(read_settings_raw(stack))["cam"] == {"fps": 15}
