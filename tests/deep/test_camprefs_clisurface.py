"""Deep battery: how the cam prefs/config surface behaves through the CLI.

`klipperformac cam on|off|status|config` and `klipperformac doctor` are the only
ways a user ever meets these settings, so this battery drives them the way
cli.py does (same argparse Namespace shape) and checks three properties:

  * every camera path degrades to a readable line, never to a traceback, when
    settings.json / run/cam.json / moonraker.conf are junk;
  * the ranges the CLI will write are exactly the ranges the doctor judges;
  * the stored device name reaches the helper as one inert argv element, and a
    name the OS cannot even encode is reported rather than crashed.

Nothing is executed: subprocess is a stub that performs only the filesystem
encoding step the OS itself performs, no camera is opened, no port is bound, no
network call is made (the Moonraker POST and the stack status are stubbed) and
`cam build` is stubbed so no Swift compile can start.
"""
import argparse
import json
import os
import subprocess

import pytest
from hypothesis import HealthCheck, given, settings as hst, strategies as st

BOOT = "1700000000"
HELPER_PID = 4242
MANAGED = "[webcam maccam]"
NUMERIC = {"width": (160, 4096), "height": (120, 4096),
           "fps": (1, 60), "quality": (1, 100)}

# Bytes no text decoder can read back.
UNDECODABLE = [
    ("invalid-utf8", b"[server]\nhost: 127.0.0.1\n\xff\xfe stray byte\n"),
    ("utf16-conf", "[server]\nhost: 127.0.0.1\n".encode("utf-16")),
    ("latin1-comment", b"[server]\n# caf\xe9\nhost: 127.0.0.1\n"),
]
# Legal UTF-8, but not a config anybody wrote on purpose.
WEIRD_BUT_DECODABLE = [
    ("empty-conf", b""),
    ("nul-inside", b"[server]\nhost: 127.0.0.1\n\x00\x00\n"),
    ("bom-conf", "﻿[server]\nhost: 127.0.0.1\n".encode("utf-8")),
    ("big-conf", b"[server]\nhost: 127.0.0.1\n" + b"# pad\n" * 200000),
]


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


class FakeProc:
    pid = HELPER_PID


class EncodingSubprocess:
    """subprocess stand-in that performs the one real step before exec: encode
    argv with the filesystem encoding (where a lone surrogate dies). It records
    the call and never runs anything."""
    DEVNULL = getattr(subprocess, "DEVNULL", -3)
    TimeoutExpired = subprocess.TimeoutExpired
    calls = []

    @staticmethod
    def _encode(cmd):
        args = list(cmd)
        for a in args:
            os.fsencode(a)
        return [str(a) for a in args]

    @staticmethod
    def run(cmd, **kw):
        EncodingSubprocess.calls.append(EncodingSubprocess._encode(cmd))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    @staticmethod
    def Popen(cmd, **kw):
        EncodingSubprocess.calls.append(EncodingSubprocess._encode(cmd))
        return FakeProc()


def cam_args(**kw):
    base = dict(action="config", path=None, device=None, width=None,
                height=None, fps=None, quality=None, show=False)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def camsite(stack, tmp_path, monkeypatch):
    """Generated moonraker.conf, fake helper, no exec / net / camera / compile."""
    monkeypatch.setattr(stack.cam, "GUI_BUNDLES",
                        (str(tmp_path / "absent.app/Contents/Resources/KlipperCam"),))
    monkeypatch.setattr(stack.cam, "CAM_BIN_ENVS", ())
    monkeypatch.setattr(stack.cam, "REPO_DIR_ENVS", ())
    monkeypatch.delenv("KLIPPERFORMAC_REPO_DIR", raising=False)
    monkeypatch.setattr(stack.cam, "subprocess", BlockedSubprocess)
    monkeypatch.setattr(stack.cam, "_process", lambda: stack.process)
    monkeypatch.setattr(stack.cam, "_port_free", lambda port: True)
    monkeypatch.setattr(stack.cam, "_reap_orphans", lambda port: None)
    monkeypatch.setattr(stack.cam, "build_helper",
                        lambda verbose=True: (False, ["build stubbed"]))
    monkeypatch.setattr(stack.cam.time, "sleep", lambda s: None)
    monkeypatch.setattr(stack.process, "_boot_time", lambda: BOOT)
    monkeypatch.setattr(stack.process, "_live_cmds", lambda pids: {})
    monkeypatch.setattr(stack.process, "running", lambda: None)
    text = stack.configgen.MOONRAKER_CFG.format(
        host="127.0.0.1", moonraker_port=7125,
        uds_address=str(stack.paths.API_SOCKET), web_port=8080, fluidd_port=8081)
    stack.paths.MOONRAKER_CONF.write_text(text)
    stack.conf_text = text
    return stack


@pytest.fixture
def live_helper(camsite, monkeypatch):
    """`cam on` end to end against a fake helper that encodes argv honestly."""
    helper = camsite.paths.CAM_BIN
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_bytes(b"MACHO")
    monkeypatch.setattr(camsite.cam, "subprocess", EncodingSubprocess)
    monkeypatch.setattr(camsite.cam, "permission", lambda: ("authorized", True))
    monkeypatch.setattr(camsite.cam, "_post_webcam", lambda payload: True)
    EncodingSubprocess.calls = []

    state = {"alive": False}

    def popen(cmd, **kw):
        argv = EncodingSubprocess._encode(cmd)   # the OS step, no exec
        EncodingSubprocess.calls.append(argv)
        state["alive"] = True
        return FakeProc()

    monkeypatch.setattr(camsite.cam, "_popen", popen)
    monkeypatch.setattr(camsite.cam, "_alive", lambda pid: state["alive"])
    camsite.alive = state
    return camsite


def put_conf(camsite, blob):
    camsite.paths.MOONRAKER_CONF.parent.mkdir(parents=True, exist_ok=True)
    if camsite.paths.MOONRAKER_CONF.is_dir():
        camsite.paths.MOONRAKER_CONF.rmdir()
    camsite.paths.MOONRAKER_CONF.write_bytes(blob)


def fake_streaming(site, monkeypatch, on_signal=None):
    """The faked ps agrees HELPER_PID is our streaming helper; signalling does
    on_signal (default nothing) and - unless the site tracks aliveness itself,
    as live_helper does - the process is reported dead, so stop() never waits
    for a real timer or a real pid."""
    monkeypatch.setattr(site.process, "_live_cmds",
                        lambda pids: {str(p): "x/KlipperCam --stream"
                                      for p in pids})
    monkeypatch.setattr(site.cam, "_signal", on_signal or
                        (lambda pid, sig: None))
    monkeypatch.setattr(site.cam, "_reap", lambda pid: None)
    if getattr(site, "alive", None) is None:
        monkeypatch.setattr(site.cam, "_alive", lambda pid: False)


# ------------------------------------------------ the cam half of doctor/status


@pytest.mark.parametrize("label, blob", UNDECODABLE + WEIRD_BUT_DECODABLE)
def test_cam_status_and_doctor_answer_for_any_conf_bytes(camsite, label, blob):
    """cam.py guards its conf reads; those guards must hold for real junk."""
    put_conf(camsite, blob)
    lines = camsite.cam.status()
    assert any("web UI" in l for l in lines), lines
    verdicts = camsite.cam.doctor()
    assert len(verdicts) == 6
    for ok, msg in verdicts:
        assert isinstance(ok, bool) and isinstance(msg, str) and msg
    assert camsite.cam.stop() == (False, ["Camera was not on."])
    assert isinstance(camsite.cam.register_webcam(), list)


def test_webcam_state_gracefulness_ladder(camsite):
    """The raw layer is byte-safe now; every cam.py caller above it still is.

    Contract change (wave 2, hunt finding "a single non-UTF-8 byte in
    printer.cfg or moonraker.conf kills up/doctor/lan/presets"):
    configgen/paths used to read these files with a bare read_text(), so this
    test pinned UnicodeDecodeError escaping the raw layer and each caller
    translating it into a graceful line. The raw layer no longer raises - a
    vendor config with cp1252 comments must not stop the stack - so the ladder
    is asserted at the new level instead: the odd bytes are read as latin-1/cp1252
    and written back UNCHANGED, and no caller crashes.
    """
    put_conf(camsite, b"[server]\nhost: 127.0.0.1\n\xff\xfe\n")
    assert camsite.configgen.webcam_state() == (False, False)
    assert not any("failed" in m for ok, m in camsite.cam.doctor())
    assert isinstance(camsite.cam.register_webcam(), list)
    ok, lines = camsite.cam.stop()
    assert ok is False and lines == ["Camera was not on."]
    # the byte that used to abort the read is still there, unmangled
    assert b"\xff\xfe" in camsite.paths.MOONRAKER_CONF.read_bytes()


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod is a no-op for root")
def test_cam_off_flips_a_read_only_conf_file(camsite, monkeypatch):
    conf = camsite.paths.MOONRAKER_CONF
    camsite.configgen.ensure_webcam_section()
    camsite.paths.RUN.mkdir(parents=True, exist_ok=True)
    camsite.cam._save_state(HELPER_PID, 8082, camsite.cam.prefs())
    fake_streaming(camsite, monkeypatch)
    os.chmod(str(conf), 0o444)
    try:
        ok, lines = camsite.cam.stop()
    finally:
        os.chmod(str(conf), 0o644)
    assert ok is True, lines
    assert "enabled: False" in conf.read_text()


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod is a no-op for root")
def test_cam_off_still_stops_when_the_conf_dir_is_walled_off(camsite,
                                                             monkeypatch):
    conf = camsite.paths.MOONRAKER_CONF
    camsite.configgen.ensure_webcam_section()
    camsite.paths.RUN.mkdir(parents=True, exist_ok=True)
    camsite.cam._save_state(HELPER_PID, 8082, camsite.cam.prefs())
    fake_streaming(camsite, monkeypatch)
    os.chmod(str(conf.parent), 0o555)
    try:
        ok, lines = camsite.cam.stop()
    finally:
        os.chmod(str(conf.parent), 0o755)
    assert ok is True, lines
    assert any("Camera off" in l for l in lines), lines
    assert not camsite.paths.CAM_STATE.exists()
    assert "enabled: True" in conf.read_text(), "the conf was half-written"


# ------------------------------------------ cli.cmd_doctor's single camera line


@pytest.fixture
def doctor_cli(camsite, monkeypatch):
    """Run cli.cmd_doctor with everything that is not the camera stubbed out."""
    class NoProcesses:
        @staticmethod
        def run(*a, **k):
            raise RuntimeError("no processes from these tests")

    monkeypatch.setattr(camsite.cli, "subprocess", NoProcesses)
    monkeypatch.setattr(camsite.installer, "check_prereqs", lambda **kw: [])
    monkeypatch.setattr(camsite.installer, "verify_pristine", lambda: [])
    monkeypatch.setattr(camsite.installer, "test_chelper_build",
                        lambda: None)
    monkeypatch.setattr(camsite.installer, "test_libsodium", lambda: None)
    monkeypatch.setattr(camsite.shaper, "readiness",
                        lambda: {"needed": False, "healthy": True,
                                 "detail": "stubbed"})
    monkeypatch.setattr(camsite.cam, "_check_helper",
                        lambda: (False, "camera helper: stubbed"))
    monkeypatch.setattr(camsite.cam, "_check_permission",
                        lambda: (True, "camera permission: stubbed"))
    assert camsite.paths.cam_settings() == {}      # a fresh install
    assert camsite.cam.running() is None
    return camsite


def run_doctor(doctor, capsys):
    with pytest.raises(SystemExit) as info:        # cmd_doctor always exits
        doctor.cli.cmd_doctor(argparse.Namespace())
    return info.value.code, capsys.readouterr().out


def test_doctor_cli_prints_the_camera_lines_on_a_sane_conf(doctor_cli, capsys):
    code, out = run_doctor(doctor_cli, capsys)
    assert code in (0, 1)
    assert "camera permission: stubbed" in out
    assert sum(1 for l in out.splitlines() if "camera:" in l) == 6


@pytest.mark.parametrize("label, blob", WEIRD_BUT_DECODABLE)
def test_doctor_cli_is_fine_with_strange_but_decodable_confs(doctor_cli,
                                                             capsys, label,
                                                             blob):
    put_conf(doctor_cli, blob)
    code, out = run_doctor(doctor_cli, capsys)
    assert code in (0, 1), label
    assert sum(1 for l in out.splitlines() if "camera:" in l) == 6, label


@pytest.mark.parametrize("label, blob", UNDECODABLE)
def test_doctor_cli_survives_an_undecodable_moonraker_conf(doctor_cli, capsys,
                                                           label, blob):
    put_conf(doctor_cli, blob)
    code, out = run_doctor(doctor_cli, capsys)
    assert code in (0, 1)
    assert "camera" in out


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod is a no-op for root")
def test_doctor_cli_survives_an_unreadable_conf(doctor_cli, capsys):
    put_conf(doctor_cli, b"[server]\nhost: 127.0.0.1\n")
    os.chmod(str(doctor_cli.paths.MOONRAKER_CONF), 0o000)
    try:
        code, out = run_doctor(doctor_cli, capsys)
    finally:
        os.chmod(str(doctor_cli.paths.MOONRAKER_CONF), 0o644)
    assert code in (0, 1)
    assert "camera" in out


def test_doctor_cli_survives_a_prefs_file_whose_root_is_a_list(doctor_cli,
                                                                capsys):
    doctor_cli.home.mkdir(parents=True, exist_ok=True)
    doctor_cli.paths.SETTINGS.write_text("[1, 2, 3]")
    code, out = run_doctor(doctor_cli, capsys)
    assert code in (0, 1)
    assert "camera" in out


# ------------------------------------------------------- CLI vs doctor ranges


@pytest.mark.parametrize("flag, bounds", sorted(NUMERIC.items()))
def test_the_cli_accepts_exactly_what_the_doctor_accepts(camsite, flag, bounds,
                                                         capsys):
    """No pref may live in the gap between the two surfaces: what `cam config`
    writes must be exactly what `cam doctor` calls sane."""
    lo, hi = bounds
    for value in (lo - 1, lo, lo + 1, hi - 1, hi, hi + 1):
        camsite.cam.save_prefs(**{flag: value})        # behind the CLI's back
        doctor_ok = camsite.cam._check_prefs()[0]
        assert camsite.cam.prefs()[flag] == value      # the store keeps it
        camsite.cam.save_prefs(**{flag: None})
        capsys.readouterr()
        try:
            camsite.cli.cmd_cam(cam_args(**{flag: value}))
            cli_ok = True
        except SystemExit:
            cli_ok = False
        capsys.readouterr()
        assert cli_ok is doctor_ok, (flag, value, cli_ok, doctor_ok)
        assert cli_ok is (lo <= value <= hi), (flag, value)
        # a rejected value must not be written anywhere at all
        assert camsite.paths.cam_settings().get(flag) == (value if cli_ok
                                                         else None)


@hst(max_examples=60, deadline=None,
     suppress_health_check=[HealthCheck.too_slow,
                            HealthCheck.function_scoped_fixture])
@given(flag=st.sampled_from(sorted(NUMERIC)), value=st.integers(-300, 6000))
def test_no_pref_can_be_saved_that_the_doctor_cannot_judge(flag, value,
                                                           camsite, capsys):
    lo, hi = NUMERIC[flag]
    camsite.cam.save_prefs(**{k: None for k in NUMERIC})
    camsite.cam.save_prefs(**{flag: value})
    doctor_ok = camsite.cam._check_prefs()[0]
    assert doctor_ok is (lo <= value <= hi), (flag, value)
    camsite.cam.save_prefs(**{flag: None})
    capsys.readouterr()
    try:
        camsite.cli.cmd_cam(cam_args(**{flag: value}))
        cli_ok = True
    except SystemExit:
        cli_ok = False
    capsys.readouterr()
    assert cli_ok is doctor_ok, (flag, value)
    assert camsite.cam.prefs()[flag] == (value if cli_ok
                                         else camsite.cam.DEFAULT_PREFS[flag])
    assert isinstance(camsite.cam.doctor(), list)


def test_doctor_accepts_boundary_prefs_the_cli_wrote(camsite):
    camsite.cli.cmd_cam(cam_args(width=160, height=120, fps=1, quality=1))
    assert camsite.cam._check_prefs()[0] is True
    camsite.cli.cmd_cam(cam_args(width=4096, height=4096, fps=60, quality=100))
    assert camsite.cam._check_prefs()[0] is True
    assert len(camsite.cam.doctor()) == 6


# --------------------------------------------------------- the argv round trip


def test_a_normal_device_name_reaches_the_helper_after_os_encoding(live_helper):
    live_helper.cam.save_prefs(device="USB Camera")
    ok, lines = live_helper.cam.start()
    assert ok is True, lines
    argv = EncodingSubprocess.calls[0]
    assert argv[0] == str(live_helper.paths.CAM_BIN)
    assert argv[argv.index("--device") + 1] == "USB Camera"
    assert all(isinstance(a, str) and os.fsencode(a) for a in argv)


def test_start_uses_every_saved_pref_verbatim(live_helper):
    live_helper.cam.save_prefs(device="Uni V2.1", width=800, height=600,
                               fps=30, quality=40)
    ok, lines = live_helper.cam.start()
    assert ok is True, lines
    argv = EncodingSubprocess.calls[-1]
    assert argv[argv.index("--device") + 1] == "Uni V2.1"
    assert argv[argv.index("--width") + 1] == "800"
    assert argv[argv.index("--height") + 1] == "600"
    assert argv[argv.index("--fps") + 1] == "30"
    assert argv[argv.index("--quality") + 1] == "40"
    assert argv[argv.index("--port") + 1] == str(live_helper.paths.CAM_PORT)
    assert "-c" not in argv and "/bin/sh" not in argv


def test_cam_on_survives_a_device_name_the_os_cannot_encode(live_helper):
    live_helper.home.mkdir(parents=True, exist_ok=True)
    live_helper.paths.SETTINGS.write_text('{"cam": {"device": "\\ud800"}}')
    assert live_helper.cam.prefs()["device"] == "\ud800"
    ok, lines = live_helper.cam.start()
    assert isinstance(ok, bool)
    assert all(isinstance(l, str) and l for l in lines)


def test_cam_snapshot_survives_an_unencodable_device_name(live_helper):
    live_helper.home.mkdir(parents=True, exist_ok=True)
    live_helper.paths.SETTINGS.write_text('{"cam": {"device": "\\udfff"}}')
    ok, lines = live_helper.cam.snapshot(str(
        live_helper.data / "snap" / "one.jpg"))
    assert isinstance(ok, bool)
    assert all(isinstance(l, str) and l for l in lines)


@hst(max_examples=40, deadline=None,
     suppress_health_check=[HealthCheck.too_slow,
                            HealthCheck.function_scoped_fixture])
@given(name=st.text(alphabet=st.characters(exclude_categories=("Cs",)),
                    min_size=1, max_size=300))
def test_every_encodable_device_name_reaches_argv_untouched(name, live_helper):
    live_helper.cam.save_prefs(device=name)
    ok, lines = live_helper.cam.start()
    assert ok is True, lines
    argv = EncodingSubprocess.calls[-1]
    assert argv[argv.index("--device") + 1] == name
    assert argv.count("--device") == 1
    assert all(isinstance(a, str) for a in argv)


# ------------------------------------------------- on/off cycle idempotence


def test_repeated_on_off_cycles_stabilise_conf_prefs_and_state(live_helper,
                                                               monkeypatch):
    fake_streaming(live_helper, monkeypatch,
                   on_signal=lambda pid, sig: live_helper.alive.update(
                       alive=False))
    live_helper.cam.save_prefs(device="Cycle Cam", fps=25)
    prefs_before = live_helper.cam.prefs()
    seen = []
    for _ in range(5):
        ok, lines = live_helper.cam.start()
        assert ok is True, lines
        assert live_helper.cam.running() is not None
        ok, lines = live_helper.cam.stop()
        assert ok is True, lines
        assert live_helper.cam.running() is None
        seen.append(live_helper.paths.MOONRAKER_CONF.read_bytes())
    assert live_helper.cam.prefs() == prefs_before
    assert seen[1:] == seen[:1] * 4, "the conf keeps drifting across cycles"
    body = live_helper.paths.MOONRAKER_CONF.read_text()
    assert body.count(MANAGED) == 1
    assert "enabled: False" in body
    assert "cycle cam" not in body.lower(), "prefs leaked into moonraker.conf"
    assert live_helper.conf_text.strip() in body


def test_cam_config_show_and_save_through_the_cli_never_traceback(camsite,
                                                                 capsys):
    for args in (cam_args(), cam_args(show=True),
                 cam_args(device="x" * 50_000),
                 cam_args(device="cam\x00\n\"'`;--stream"),
                 cam_args(fps=60, quality=100, width=4096, height=4096)):
        camsite.cli.cmd_cam(args)
        assert capsys.readouterr().out.strip()
    assert camsite.cam.prefs()["fps"] == 60
    assert len(camsite.cam.doctor()) == 6
    assert json.loads(camsite.paths.SETTINGS.read_text())["cam"]["fps"] == 60


def test_cam_on_without_a_helper_is_a_clean_no(camsite, capsys):
    ok, lines = camsite.cam.start()
    assert ok is False
    assert any("cam build" in l for l in lines), lines
    with pytest.raises(SystemExit) as info:
        camsite.cli.cmd_cam(cam_args(action="on"))
    assert info.value.code == 1
    assert "Traceback" not in capsys.readouterr().err


def test_cam_build_action_never_compiles_anything(camsite, capsys):
    with pytest.raises(SystemExit) as info:
        camsite.cli.cmd_cam(cam_args(action="build"))
    assert info.value.code == 1
    assert "build stubbed" in capsys.readouterr().out


def test_cam_list_and_snapshot_without_a_helper_are_clean(camsite, capsys):
    for action in ("list", "snapshot"):
        ok, lines = (camsite.cam.list_devices() if action == "list"
                     else camsite.cam.snapshot())
        assert ok is False and any("cam build" in l for l in lines), lines
        with pytest.raises(SystemExit) as info:
            camsite.cli.cmd_cam(cam_args(action=action))
        assert info.value.code == 1


def test_doctor_stays_one_verdict_per_check_with_a_forging_device(camsite):
    """A device name is free text; it must not be able to add or fake a line
    of the doctor report (which is what scripts and the TUI parse)."""
    camsite.cam.save_prefs(
        device="a\nB\x1b[31mred\n  camera       on (pid 666, up 999h)\n"
               "[!] camera helper missing")
    verdicts = camsite.cam.doctor()
    assert len(verdicts) == 6
    for ok, msg in verdicts:
        assert isinstance(ok, bool)
        assert "\n" not in msg and len(msg) < 1000, msg
    out = "\n".join(msg for ok, msg in verdicts)
    assert "pid 666" not in out
    assert out.count("camera helper") <= 1
