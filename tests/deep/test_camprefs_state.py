"""Deep battery: the cam state file (data/run/cam.json).

cam.py promises "a JSON state file re-validated on every read ... so a crash
or a reboot self-heals on the next command". This battery feeds that file every
kind of garbage - torn, non-JSON, non-object, wrong types, absurd numbers,
unicode, megabytes of noise, missing, unreadable, a directory - and checks what
running()/_save_state()/status()/doctor() make of it.

No process is ever launched: boot time and live-pid lookup are faked and
cam.subprocess is replaced by a blocker.
"""
import json
import os
import shutil
import subprocess
import time

import pytest
from hypothesis import HealthCheck, given, settings as hst, strategies as st

BOOT = "1700000000"
PID = 4242
PORT = 8082
GOOD_CMD = "/some/where/KlipperCam --stream --port 8082 --width 1280"
TEXT = st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=40)


class NoExec(BaseException):
    """Raised instead of ever launching a process from this battery."""


class BlockedSubprocess:
    DEVNULL = getattr(subprocess, "DEVNULL", -3)
    TimeoutExpired = subprocess.TimeoutExpired

    @staticmethod
    def run(*a, **k):
        raise NoExec("subprocess.run blocked: " + repr(a))

    @staticmethod
    def Popen(*a, **k):
        raise NoExec("subprocess.Popen blocked: " + repr(a))


def fake_ps(live):
    """_live_cmds stand-in: `live` maps pid -> cmdline of the running helpers."""
    def _cmds(pids):
        out = {}
        for p in pids:
            try:
                key = int(p)
            except (TypeError, ValueError):
                continue
            if key in live:
                out[str(key)] = live[key]
        return out
    return _cmds


@pytest.fixture
def state(stack, tmp_path, monkeypatch):
    """The stack with one fake streaming helper 'alive' and no exec possible."""
    monkeypatch.setattr(stack.cam, "GUI_BUNDLES",
                        (str(tmp_path / "absent.app/Contents/Resources/KlipperCam"),))
    monkeypatch.delenv("KLIPPERFORMAC_CAM_BIN", raising=False)
    monkeypatch.delenv("KLIPPERFORMAC_REPO_DIR", raising=False)
    monkeypatch.setattr(stack.cam, "subprocess", BlockedSubprocess)
    monkeypatch.setattr(stack.cam, "_process", lambda: stack.process)
    monkeypatch.setattr(stack.cam, "_port_free", lambda port: True)
    monkeypatch.setattr(stack.process, "_boot_time", lambda: BOOT)
    monkeypatch.setattr(stack.process, "_live_cmds", fake_ps({PID: GOOD_CMD}))
    stack.paths.RUN.mkdir(parents=True, exist_ok=True)
    return stack


def write_state(stack, obj=None, raw=None):
    stack.paths.RUN.mkdir(parents=True, exist_ok=True)
    text = raw if raw is not None else json.dumps(obj)
    stack.paths.CAM_STATE.write_text(text)
    return text


def good_state(**kw):
    data = {"boot_time": BOOT, "pid": PID, "port": PORT,
            "started": "2026-10-05T10:00:00",
            "width": 1280, "height": 720, "fps": 15, "quality": 80}
    data.update(kw)
    return data


# ------------------------------------------------------------ valid states


def test_valid_state_is_returned_untouched(state):
    blob = good_state(device="Back camera")
    write_state(state, blob)
    assert state.cam.running() == blob
    assert state.paths.CAM_STATE.exists(), "a valid state must not be cleared"


def test_save_state_then_running_roundtrip(state):
    prefs = {"width": 640, "height": 480, "fps": 24, "quality": 33,
             "device": "Cam, with \"quotes\"\nand a newline", "junk": "nope"}
    assert state.cam._save_state(PID, 8085, prefs) is True
    text = state.paths.CAM_STATE.read_text()
    blob = json.loads(text)
    assert blob["pid"] == PID and blob["port"] == 8085
    assert blob["boot_time"] == BOOT
    assert blob["device"] == prefs["device"]
    assert set(blob) == {"boot_time", "started", "pid", "port",
                         "width", "height", "fps", "quality", "device"}
    assert state.cam.running() == blob
    assert text.endswith("\n")


def test_save_state_drops_none_prefs_and_unknown_keys(state):
    state.cam._save_state(PID, PORT, {"width": None, "fps": 20,
                                     "brightness": 5, "device": None})
    blob = json.loads(state.paths.CAM_STATE.read_text())
    assert "width" not in blob and "device" not in blob
    assert "brightness" not in blob
    assert blob["fps"] == 20


def test_save_state_creates_the_run_dir(state):
    shutil.rmtree(str(state.paths.RUN))
    assert state.cam._save_state(PID, PORT, {"fps": 10}) is True
    assert state.paths.CAM_STATE.is_file()


def test_save_state_fails_cleanly_when_run_is_a_file(state):
    shutil.rmtree(str(state.paths.RUN))
    state.paths.RUN.write_text("not a dir")
    assert state.cam._save_state(PID, PORT, {"fps": 10}) is False


def test_save_state_fails_cleanly_when_state_is_a_directory(state):
    state.paths.CAM_STATE.mkdir(parents=True)
    assert state.cam._save_state(PID, PORT, {"fps": 10}) is False


@pytest.mark.parametrize("prefs", [
    {"width": 2000000000000, "fps": -5, "quality": "lots"},
    {"device": "x" * 300000},
    {"width": {"nested": [1, 2]}, "height": []},
    {"width": 1280},
])
def test_save_state_persists_any_json_pref_without_crashing(state, prefs):
    assert state.cam._save_state(PID, PORT, prefs) is True
    blob = json.loads(state.paths.CAM_STATE.read_text())
    for k, v in prefs.items():
        if k in state.cam.PREF_KEYS and v is not None:
            assert blob[k] == v


# --------------------------------------------------------- corrupted states

CORRUPT = [
    ("empty file", ""),
    ("whitespace", "   \n\t "),
    ("torn json", '{"pid": 4242, "boot_time": "17'),
    ("plain garbage", 'not json at all'),
    ("null root", "null"),
    ("number root", "17"),
    ("string root", '"camera on"'),
    ("list root", "[4242, 8082]"),
    ("empty object", "{}"),
    ("pid null", '{"pid": null, "boot_time": "%s"}' % BOOT),
    ("pid string", '{"pid": "4242", "boot_time": "%s"}' % BOOT),
    ("pid list", '{"pid": [4242], "boot_time": "%s"}' % BOOT),
    ("pid missing boot", '{"pid": 4242}'),
    ("boot mismatch", '{"pid": 4242, "boot_time": "1"}'),
    ("boot int not str", '{"pid": 4242, "boot_time": %s}' % BOOT),
    ("boot null", '{"pid": 4242, "boot_time": null}'),
    ("pid float", '{"pid": 4242.5, "boot_time": "%s"}' % BOOT),
    ("pid zero", '{"pid": 0, "boot_time": "%s"}' % BOOT),
    ("pid negative", '{"pid": -1, "boot_time": "%s"}' % BOOT),
    ("pid huge", '{"pid": 99999999999999999999, "boot_time": "%s"}' % BOOT),
    ("torn object", '{"a"'),
]


@pytest.mark.parametrize("label,text", CORRUPT,
                         ids=[label for label, _ in CORRUPT])
def test_corrupt_state_reads_as_off_and_self_heals(state, label, text):
    write_state(state, raw=text)
    assert state.cam.running() is None, label
    assert not state.paths.CAM_STATE.exists(), \
        "%s: stale state was left behind" % label


def test_giant_garbage_state_is_cleared_quickly(state):
    write_state(state, raw="[" + ("1," * 400000) + "]")
    assert state.cam.running() is None
    assert not state.paths.CAM_STATE.exists()


def test_duplicate_json_keys_last_value_wins(state):
    write_state(state, raw='{"pid": 1, "pid": %d, "boot_time": "%s"}'
                % (PID, BOOT))
    assert state.cam.running() == {"pid": PID, "boot_time": BOOT}


def test_wrong_marker_in_cmdline_is_cleared(state, monkeypatch):
    write_state(state, good_state())
    monkeypatch.setattr(state.process, "_live_cmds",
                        fake_ps({PID: "/bin/sh -c echo_KlipperCam"}))
    assert state.cam.running() is None
    assert not state.paths.CAM_STATE.exists()


def test_helper_without_stream_marker_is_not_ours(state, monkeypatch):
    write_state(state, good_state())
    monkeypatch.setattr(state.process, "_live_cmds",
                        fake_ps({PID: "/x/KlipperCam --list"}))
    assert state.cam.running() is None


def test_unreadable_state_reads_as_off(state):
    """An EACCES state file must read as 'off', never raise."""
    if os.geteuid() == 0:
        pytest.skip("chmod is a no-op for root")
    write_state(state, good_state())
    os.chmod(state.paths.CAM_STATE, 0o000)
    try:
        assert state.cam.running() is None
    finally:
        if state.paths.CAM_STATE.exists():
            os.chmod(state.paths.CAM_STATE, 0o644)


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod is a no-op for root")
def test_state_in_a_read_only_dir_reads_as_off_without_crashing(state):
    write_state(state, raw='{"pid": "4242"}')     # corrupt, so unlink is tried
    os.chmod(state.paths.RUN, 0o500)
    try:
        assert state.cam.running() is None
        assert state.paths.CAM_STATE.exists()     # unlink failed, no crash
    finally:
        os.chmod(state.paths.RUN, 0o755)


def test_state_as_a_directory_reads_as_off(state):
    state.paths.CAM_STATE.mkdir(parents=True)
    assert state.cam.running() is None


def test_no_state_file_means_off(state):
    assert not state.paths.CAM_STATE.exists()
    assert state.cam.running() is None


@pytest.mark.parametrize("raw", [
    '{"pid": %d, "boot_time": "%s", "zz": [{}], "note": "whatever"}' % (PID, BOOT),
    '{"pid": %d, "boot_time": "%s", "d": "\\ud83d\\udcf7\\u0304\\u202e\\u0000"}'
    % (PID, BOOT),
    '{"pid": %d, "boot_time": "%s", "width": {"nested": 1}}' % (PID, BOOT),
    '{"pid": %d, "boot_time": "%s", "started": null}' % (PID, BOOT),
    '[{"pid": %d, "boot_time": "%s"}]' % (PID, BOOT),
])
def test_unknown_or_hostile_extra_fields_do_not_confuse_the_reader(state, raw):
    """Keys the module does not own are ignored; a list root is not a state."""
    write_state(state, raw=raw)
    got = state.cam.running()
    if raw.startswith("{"):
        assert got == json.loads(raw)
    else:
        assert got is None and not state.paths.CAM_STATE.exists()


# --------------------------------------------------------------- consumers


def test_status_shows_a_valid_streaming_state(state):
    write_state(state, good_state())
    state.cam.save_prefs(device="Front")
    lines = "\n".join(state.cam.status())
    assert "on (pid %d" % PID in lines
    assert "http://localhost:%d/?action=stream" % PORT in lines
    assert "device Front" in lines


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: running() re-validates pid/boot_time/cmdline but never the "
    "'port' field, so a corrupt run/cam.json makes stream_url()/snapshot_url() "
    "raise an uncaught ValueError - 'klipperformac cam status', 'cam on' and "
    "the TUI redraw die with a traceback instead of self-healing the state"))
def test_status_survives_a_non_numeric_port_in_state(state):
    write_state(state, good_state(port="banana"))
    lines = state.cam.status()          # currently: ValueError
    assert any("stream" in l for l in lines)
    assert state.cam.running() is None, "an unusable port must self-heal"


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: the state file's 'port' is never range-checked (paths._port "
    "demands 1..65535 for the env var), so an absurd port is published "
    "verbatim as the stream URL by cam status"))
def test_status_rejects_an_out_of_range_port(state):
    write_state(state, good_state(port=99999999999))
    lines = "\n".join(state.cam.status())
    assert "99999999999" not in lines


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: cam._uptime() catches only (TypeError, ValueError); a "
    "pre-1970 'started' in run/cam.json makes time.mktime raise OverflowError, "
    "crashing 'klipperformac cam status' - the self-heal promise the rest of "
    "the state reader keeps"))
def test_status_survives_an_absurd_started_timestamp(state):
    write_state(state, good_state(started="1000-01-01T00:00:00"))
    lines = "\n".join(state.cam.status())
    assert "up ?" in lines or "up 0s" in lines, lines


@pytest.mark.parametrize("started", ["", "nope", "2026-13-45T99:99:99",
                                     "2026-02-30T00:00:00",
                                     "9999-12-31T23:59:59",
                                     "2038-01-19T03:14:08",
                                     "1970-01-01T00:00:00"])
def test_uptime_handles_bad_timestamps(state, started):
    got = state.cam._uptime(started)
    assert isinstance(got, str) and got


@pytest.mark.parametrize("started", [None, 12345, [], {}, True, 1.5])
def test_uptime_handles_wrong_types(state, started):
    assert state.cam._uptime(started) == "?"


def test_uptime_clamps_the_future(state):
    past = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 7500))
    assert "h" in state.cam._uptime(past)
    future = time.strftime("%Y-%m-%dT%H:%M:%S",
                           time.localtime(time.time() + 7500))
    assert state.cam._uptime(future) == "0s"


def test_status_of_a_corrupt_state_is_off_and_self_heals(state):
    write_state(state, raw='{"pid": "4242"}')
    lines = "\n".join(state.cam.status())
    assert "off (nothing is being captured)" in lines
    assert not state.paths.CAM_STATE.exists()


# ---------------------------------------------------------------- doctor


def test_doctor_reports_a_valid_state(state):
    write_state(state, good_state())
    msgs = [m for ok, m in state.cam.doctor()]
    assert any("camera running (pid %d)" % PID in m for m in msgs), msgs


def test_doctor_self_heals_a_stale_state(state):
    """NOTE: _check_state's 'stale state ... cleaned up' wording is dead in
    practice, because _check_port runs first in doctor() and already unlinks
    the file; doctor therefore always reports the plain 'camera off'."""
    write_state(state, good_state(boot_time="1"))
    msgs = [m for ok, m in state.cam.doctor()]
    assert not state.paths.CAM_STATE.exists(), "stale state must be gone"
    assert any(m == "camera off" for m in msgs), msgs


def test_doctor_direct_state_check_names_the_cleanup(state):
    """The cleanup wording only shows when _check_state runs on its own."""
    write_state(state, good_state(boot_time="1"))
    assert state.cam._check_state() == (
        True, "camera off (stale state from a crash was cleaned up)")


def test_doctor_reports_plain_off(state):
    msgs = [m for ok, m in state.cam.doctor()]
    assert any(m == "camera off" for m in msgs), msgs


@pytest.mark.parametrize("label,text", CORRUPT,
                         ids=[label for label, _ in CORRUPT])
def test_doctor_never_raises_on_any_state(state, label, text):
    write_state(state, raw=text)
    out = state.cam.doctor()
    assert len(out) == 6
    for ok, msg in out:
        assert isinstance(ok, bool) and isinstance(msg, str) and msg


def test_doctor_is_a_pure_read_after_a_state_is_healed(state):
    write_state(state, raw="garbage")
    first = state.cam.doctor()
    assert len(first) == 6
    assert not state.paths.CAM_STATE.exists()
    second = state.cam.doctor()
    assert len(second) == 6
    assert first == second, "doctor must be stable once the state is healed"


# -------------------------------------------------------------- fuzz zone


@hst(max_examples=40, deadline=None,
     suppress_health_check=[HealthCheck.too_slow,
                            HealthCheck.function_scoped_fixture])
@given(blob=st.fixed_dictionaries({
    "boot_time": st.one_of(st.none(), st.text(max_size=12), st.integers()),
    "pid": st.one_of(st.none(), TEXT, st.booleans(),
                     st.integers(min_value=-10 ** 9, max_value=10 ** 9),
                     st.floats(allow_nan=False)),
    "port": st.one_of(st.none(), st.text(max_size=8),
                      st.integers(min_value=-300000, max_value=300000)),
    "started": st.one_of(st.none(), TEXT),
    "width": st.one_of(st.none(), st.integers()),
}))
def test_running_and_doctor_never_raise_for_any_state_dict(blob, state):
    write_state(state, blob)
    got = state.cam.running()
    assert got is None or isinstance(got, dict)
    assert len(state.cam.doctor()) == 6
