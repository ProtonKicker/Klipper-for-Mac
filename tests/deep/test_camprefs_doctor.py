"""Deep battery: cam.doctor() and its _check_* branches, on fabricated dirs
and files only.

doctor() promises "[(ok, msg)] ... Never raises". Each branch is built out of
fake home/data paths: no helper, a helper that answers with each exit code, an
unwritable/read-only log dir, a bloated log, a taken port, a stale state file
and preferences that span every legal and illegal value.

No process is launched (cam.subprocess is a blocker, boot time and ps are
faked) and no real port is bound (_port_free is faked).
"""
import json
import os
import subprocess

import pytest
from hypothesis import HealthCheck, given, settings as hst, strategies as st

BOOT = "1700000000"
PID = 4242
PORT = 8082
GOOD_CMD = "/x/KlipperCam --stream --port 8082"
TEXT = st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=24)


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


@pytest.fixture(autouse=True)
def _hermetic(stack, tmp_path, monkeypatch):
    monkeypatch.setattr(stack.cam, "GUI_BUNDLES",
                        (str(tmp_path / "absent.app/Contents/Resources/KlipperCam"),))
    monkeypatch.delenv("KLIPPERFORMAC_CAM_BIN", raising=False)
    monkeypatch.delenv("KLIPPERFORMAC_REPO_DIR", raising=False)
    monkeypatch.setattr(stack.cam, "subprocess", BlockedSubprocess)
    monkeypatch.setattr(stack.cam, "_process", lambda: stack.process)
    monkeypatch.setattr(stack.process, "_boot_time", lambda: BOOT)
    monkeypatch.setattr(stack.process, "_live_cmds",
                        lambda pids: {str(PID): GOOD_CMD}
                        if any(str(p) == str(PID) for p in pids) else {})
    monkeypatch.setattr(stack.cam, "_port_free", lambda port: True)
    return stack


def write_state(stack, obj):
    stack.paths.RUN.mkdir(parents=True, exist_ok=True)
    stack.paths.CAM_STATE.write_text(json.dumps(obj))


def helper_installed(stack, body=b"MACHO"):
    stack.paths.CAM_BIN.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.CAM_BIN.write_bytes(body)
    os.chmod(stack.paths.CAM_BIN, 0o755)
    return stack.paths.CAM_BIN


def exit_code_subprocess(code, recorder):
    class Fake:
        returncode = code

    class SP(BlockedSubprocess):
        @staticmethod
        def run(cmd, **kw):
            recorder.append((list(cmd), kw))
            return Fake()
    return SP


# ------------------------------------------------------------------- shape


def test_doctor_shape_on_a_bare_install(stack):
    out = stack.cam.doctor()
    assert len(out) == 6, out
    for ok, msg in out:
        assert isinstance(ok, bool) and isinstance(msg, str) and msg.strip()
    assert out[0][0] is False and "cam build" in out[0][1]
    assert out[1] == (True, "camera permission: not checkable without the helper")
    assert out[2] == (True, "camera port %d: free" % PORT)
    assert out[3] == (True, "camera off")
    assert out[4] == (True, "camera preferences: 1280x720 @ 15fps, quality 80")
    assert out[5] == (True, "camera log: none yet")


def test_doctor_creates_no_files_and_deletes_nothing(stack):
    before = sorted(str(p.relative_to(stack.data)) for p in stack.data.rglob("*"))
    stack.cam.doctor()
    after = sorted(str(p.relative_to(stack.data)) for p in stack.data.rglob("*"))
    assert before == after
    assert not stack.paths.SETTINGS.exists()


def test_doctor_survives_checks_that_explode(stack, monkeypatch):
    for name in ("_check_helper", "_check_permission", "_check_port",
                 "_check_state", "_check_prefs", "_check_log"):
        def boom(n=name):
            raise RuntimeError("kaboom")

        boom.__name__ = name
        monkeypatch.setattr(stack.cam, name, boom)
        out = stack.cam.doctor()
        assert len(out) == 6
        bad = [m for ok, m in out if not ok]
        assert any(name in m and "kaboom" in m for m in bad), (name, out)


def test_doctor_survives_a_check_returning_garbage(stack, monkeypatch):
    monkeypatch.setattr(stack.cam, "_check_port", lambda: (None, 7))
    out = stack.cam.doctor()
    assert len(out) == 6 and out[2] == (None, 7)


# ---------------------------------------------------------------- permission


@pytest.mark.parametrize("code, word, usable, expect_ok", [
    (0, "authorized", True, True),
    (2, "denied", False, False),
    (3, "restricted", False, False),
    (4, "not determined", False, True),
    (9, "unknown", False, False),
])
def test_doctor_permission_branches(stack, monkeypatch, code, word, usable,
                                    expect_ok):
    helper_installed(stack)
    calls = []
    monkeypatch.setattr(stack.cam, "subprocess", exit_code_subprocess(code, calls))
    ok, msg = stack.cam._check_permission()
    assert ok is expect_ok
    assert stack.cam.permission() == (word, usable)
    assert calls and calls[0][0] == [str(stack.paths.CAM_BIN),
                                     "--check-permission"]
    if word == "not determined":
        assert "prompts once" in msg
    else:
        assert word in msg
    if word == "denied":
        assert "System Settings" in msg
    if word == "unknown":
        assert "did not answer" in msg


def test_doctor_permission_when_the_helper_cannot_run(stack, monkeypatch):
    helper_installed(stack)

    class SP(BlockedSubprocess):
        @staticmethod
        def run(cmd, **kw):
            raise OSError(13, "Permission denied")

    monkeypatch.setattr(stack.cam, "subprocess", SP)
    assert stack.cam._check_permission() == (
        False, "camera permission: unknown (the helper did not answer)")


# ---------------------------------------------------------------------- port


def test_doctor_port_when_streaming(stack):
    write_state(stack, {"boot_time": BOOT, "pid": PID, "port": 9100})
    assert stack.cam._check_port() == (True, "camera port 9100: streaming")


def test_doctor_port_when_state_has_no_port(stack):
    write_state(stack, {"boot_time": BOOT, "pid": PID})
    assert stack.cam._check_port() == (True, "camera port %d: streaming" % PORT)


def test_doctor_port_taken(stack, monkeypatch):
    monkeypatch.setattr(stack.cam, "_port_free", lambda port: False)
    ok, msg = stack.cam._check_port()
    assert ok is False and "killall" in msg


def test_doctor_port_free(stack, monkeypatch):
    monkeypatch.setattr(stack.cam, "_port_free", lambda port: True)
    assert stack.cam._check_port() == (True, "camera port %d: free" % PORT)


def test_doctor_rejects_a_non_numeric_port_from_the_state(stack):
    write_state(stack, {"boot_time": BOOT, "pid": PID, "port": "banana"})
    ok, msg = stack.cam._check_port()
    assert ok is False or "banana" not in msg, msg


# --------------------------------------------------------------------- prefs


@pytest.mark.parametrize("cam, expect_ok", [
    ({}, True),
    ({"width": 160, "height": 120, "fps": 1, "quality": 1}, True),
    ({"width": 4096, "height": 4096, "fps": 60, "quality": 100}, True),
    ({"width": 4097}, False),
    ({"height": 119}, False),
    ({"fps": 0}, False),
    ({"fps": 61}, False),
    ({"quality": -1}, False),
    ({"quality": 101}, False),
    ({"width": "wide"}, False),
    ({"fps": None}, True),                      # None means "use the default"
    ({"fps": []}, False),
    ({"fps": {"a": 1}}, False),
    ({"width": True}, False),                   # int(True) == 1 -> too small
    ({"fps": 10 ** 308}, False),
    ({"fps": "20"}, True),                      # numeric strings are accepted
    ({"width": 1280, "junk": "x"}, True),
    ({"device": "any\nname"}, True),
])
def test_doctor_prefs_verdicts(stack, cam, expect_ok):
    stack.paths.SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.SETTINGS.write_text(json.dumps({"cam": cam}))
    ok, msg = stack.cam._check_prefs()
    assert ok is expect_ok, (cam, msg)
    assert msg.startswith("camera preferences") or "look wrong" in msg


def test_doctor_prefs_lists_every_bad_key_at_once(stack):
    stack.paths.SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.SETTINGS.write_text(json.dumps(
        {"cam": {"width": 1, "height": 2, "fps": 0, "quality": 101}}))
    ok, msg = stack.cam._check_prefs()
    assert ok is False
    for key in ("width", "height", "fps", "quality"):
        assert key + "=" in msg, msg


def test_doctor_prefs_with_a_huge_device_name(stack):
    stack.paths.SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.SETTINGS.write_text(json.dumps({"cam": {"device": "d" * 500000}}))
    ok, msg = stack.cam._check_prefs()
    assert ok is True and len(msg) < 200


# ----------------------------------------------------------------------- log


def test_doctor_log_missing(stack):
    assert stack.cam._check_log() == (True, "camera log: none yet")


def test_doctor_log_small(stack):
    stack.paths.CAM_LOG.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.CAM_LOG.write_bytes(b"x" * 1024)
    ok, msg = stack.cam._check_log()
    assert ok is True and msg.startswith("camera log: 0.0 MB")


def test_doctor_log_at_the_warning_boundary(stack):
    stack.paths.CAM_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(stack.paths.CAM_LOG, "wb") as f:
        f.truncate(stack.cam.LOG_WARN_BYTES)          # exactly: still fine
    assert stack.cam._check_log()[0] is True
    with open(stack.paths.CAM_LOG, "r+b") as f:
        f.truncate(stack.cam.LOG_WARN_BYTES + 1)
    ok, msg = stack.cam._check_log()
    assert ok is False
    assert "safe to delete" in msg and str(stack.paths.CAM_LOG) in msg


def test_doctor_log_multi_gigabyte_sparse(stack):
    stack.paths.CAM_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(stack.paths.CAM_LOG, "wb") as f:
        f.truncate(3 * 1024 * 1024 * 1024)
    try:
        ok, msg = stack.cam._check_log()
        assert ok is False and "3072 MB" in msg
    finally:
        stack.paths.CAM_LOG.unlink()


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod is a no-op for root")
def test_doctor_log_unreadable_but_present(stack):
    helper_installed(stack)
    stack.paths.CAM_LOG.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.CAM_LOG.write_bytes(b"secret\n")
    os.chmod(stack.paths.CAM_LOG, 0o000)
    try:
        ok, msg = stack.cam._check_log()
        assert ok is True and msg.endswith("MB")
        assert stack.cam._log_tail() == []       # unreadable: no crash, no lines
    finally:
        os.chmod(stack.paths.CAM_LOG, 0o644)


def test_doctor_log_a_broken_symlink_reads_as_none(stack):
    stack.paths.CAM_LOG.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.CAM_LOG.symlink_to(stack.paths.CAM_LOG.parent / "gone")
    assert stack.cam._check_log() == (True, "camera log: none yet")


def test_doctor_log_when_the_log_is_a_folder(stack):
    stack.paths.CAM_LOG.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.CAM_LOG.mkdir()
    ok, msg = stack.cam._check_log()
    assert ok is True and msg.startswith("camera log:")


def test_log_tail_reads_the_end_of_a_big_file(stack):
    stack.paths.CAM_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(stack.paths.CAM_LOG, "wb") as f:
        f.write(b"old noise\n" * 200000)
        f.write(b"first\nsecond\nthird\n")
    assert stack.cam._log_tail() == ["first", "second", "third"]
    assert stack.cam._log_tail(3) == ["first", "second", "third"]
    assert stack.cam._log_tail(1) == ["third"]
    assert stack.cam._log_tail(2) == ["second", "third"]
    big = stack.cam._log_tail(1000)
    assert big[-3:] == ["first", "second", "third"]
    assert all(isinstance(l, str) and l for l in big)


def test_log_tail_of_a_line_bigger_than_the_read_window(stack):
    stack.paths.CAM_LOG.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.CAM_LOG.write_bytes(b"A" * 100000 + b"\n")
    tail = stack.cam._log_tail()
    assert len(tail) == 1 and len(tail[0]) == 200      # truncated for display


def test_log_tail_on_a_unicode_file_is_replaced_not_crashing(stack):
    stack.paths.CAM_LOG.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.CAM_LOG.write_bytes(b"\xff\xfe\x00not utf-8 at all\x80\x81\n")
    assert all(isinstance(l, str) for l in stack.cam._log_tail())


# ------------------------------------------------------------- whole doctor


def test_doctor_all_green_on_a_healthy_setup(stack, monkeypatch):
    helper_installed(stack)
    calls = []
    monkeypatch.setattr(stack.cam, "subprocess", exit_code_subprocess(0, calls))
    out = stack.cam.doctor()
    assert all(ok for ok, m in out), out
    assert len(calls) == 1, "the helper must be asked about permission once"


def test_doctor_reports_a_missing_moonraker_conf_gracefully(stack):
    """webcam_state() has no config file: the 'web UI' line is still a string."""
    lines = stack.cam.status()
    assert any("no [webcam maccam] section" in l for l in lines), lines


def test_status_with_a_hostile_device_does_not_crash(stack):
    stack.cam.save_prefs(device="Multi\nline \"quoted\" [webcam x] camera")
    lines = stack.cam.status()
    assert any("prefs" in l for l in lines)
    assert sum(1 for l in lines if l.startswith("  camera ")) >= 1


@pytest.mark.parametrize("conf", [
    "", "[webcam maccam]\nenabled: True\n",
    "[webcam maccam]\nenabled: False\n",
    "[webcam MACCAM]\nenabled: yes\n",
    "  [webcam maccam]  \nenabled: 1\n",
    "[webcam maccam]",
    "[webcam maccamish]\nenabled: True\n",
    "\r\n[webcam maccam]\r\nenabled: True\r\n",
])
def test_status_web_ui_line_tracks_the_conf(stack, conf):
    stack.paths.MOONRAKER_CONF.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.MOONRAKER_CONF.write_text(conf)
    lines = "\n".join(stack.cam.status())
    present, enabled = stack.configgen.webcam_state()
    if not present:
        assert "no [webcam maccam] section" in lines
    elif enabled:
        assert "[webcam maccam] on" in lines
    else:
        assert "[webcam maccam] disabled" in lines


# ---------------------------------------------------------------- fuzz zone


@hst(max_examples=30, deadline=None,
     suppress_health_check=[HealthCheck.too_slow,
                            HealthCheck.function_scoped_fixture])
@given(cam=st.dictionaries(st.sampled_from(
    ("width", "height", "fps", "quality", "device")),
    st.one_of(st.none(), st.integers(-10 ** 6, 10 ** 6), TEXT,
              st.booleans(), st.lists(st.integers(), max_size=2)),
    max_size=5),
    port_free=st.booleans(), has_helper=st.booleans(),
    state=st.one_of(st.none(), st.fixed_dictionaries({
        "boot_time": st.just(BOOT), "pid": st.just(PID),
        "port": st.one_of(st.none(), st.integers(1, 65535))})))
def test_doctor_always_answers_with_six_verdicts(cam, port_free, has_helper,
                                                 state, stack, monkeypatch):
    monkeypatch.setattr(stack.cam, "_port_free", lambda p: port_free)
    if has_helper:
        helper_installed(stack)
        monkeypatch.setattr(stack.cam, "subprocess",
                            exit_code_subprocess(0, []))
    stack.paths.SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.SETTINGS.write_text(json.dumps({"cam": cam}))
    stack.paths.RUN.mkdir(parents=True, exist_ok=True)
    if state is None:
        stack.paths.CAM_STATE.unlink(missing_ok=True)
    else:
        stack.paths.CAM_STATE.write_text(json.dumps(
            {k: v for k, v in state.items() if v is not None}))
    out = stack.cam.doctor()
    assert len(out) == 6
    for ok, msg in out:
        assert isinstance(ok, bool) and isinstance(msg, str) and msg.strip()
