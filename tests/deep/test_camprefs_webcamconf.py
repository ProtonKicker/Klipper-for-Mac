"""Deep battery: camera prefs as they reach the managed moonraker.conf
[webcam maccam] section and the helper's argv.

The device name is free-form user data (`cam config --device`, also written by
the SwiftUI picker). None of it may be interpolated into moonraker.conf, and it
must reach the helper as one inert argv element - never through a shell.
Nothing is executed: cam.subprocess is a blocker and cam._popen is a recorder.
"""
import configparser
import json
import subprocess

import pytest
from hypothesis import HealthCheck, given, settings as hst, strategies as st

BOOT = "1700000000"
TEXT = st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=120)
MANAGED_LINES = ["enabled: True",
                 "service: mjpegstreamer",
                 "location: printer",
                 "stream_url: http://localhost:8082/?action=stream",
                 "snapshot_url: http://localhost:8082/?action=snapshot"]


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
    pid = 4242


@pytest.fixture
def conf(stack, tmp_path, monkeypatch):
    """Generated moonraker.conf + a fully faked, exec-free cam module."""
    monkeypatch.setattr(stack.cam, "GUI_BUNDLES",
                        (str(tmp_path / "absent.app/Contents/Resources/KlipperCam"),))
    monkeypatch.delenv("KLIPPERFORMAC_CAM_BIN", raising=False)
    monkeypatch.delenv("KLIPPERFORMAC_REPO_DIR", raising=False)
    monkeypatch.setattr(stack.cam, "subprocess", BlockedSubprocess)
    monkeypatch.setattr(stack.cam, "_process", lambda: stack.process)
    monkeypatch.setattr(stack.cam, "_port_free", lambda port: True)
    monkeypatch.setattr(stack.cam.time, "sleep", lambda s: None)
    monkeypatch.setattr(stack.process, "_boot_time", lambda: BOOT)
    monkeypatch.setattr(stack.process, "_live_cmds", lambda pids: {})
    text = stack.configgen.MOONRAKER_CFG.format(
        host="127.0.0.1", moonraker_port=7125,
        uds_address=str(stack.paths.API_SOCKET), web_port=8080,
        fluidd_port=8081)
    stack.paths.MOONRAKER_CONF.write_text(text)
    stack.conf_text = text
    return stack


def parse(path):
    cp = configparser.ConfigParser(strict=False)
    cp.read(path)
    return cp


def webcam_headers(text):
    return [line.strip() for line in text.splitlines()
            if line.strip().lower().startswith("[webcam ")]


def managed_body(text, name="maccam"):
    """Non-comment lines of the managed section, in file order."""
    lines = text.splitlines()
    out, inside = [], False
    for line in lines:
        s = line.strip()
        if s.lower().startswith("[webcam "):
            inside = s.lower() == "[webcam %s]" % name.lower()
            continue
        if s.startswith("["):
            inside = False
        if inside and s and not s.startswith(("#", ";")):
            out.append(s)
    return out


def fake_start(stack, monkeypatch):
    """cam.start() with the helper faked; returns (ok, lines, argv)."""
    helper = stack.paths.CAM_BIN
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_bytes(b"MACHO")
    captured = {}

    def fake_popen(cmd, **kw):
        captured["cmd"] = list(cmd)
        captured["kw"] = kw
        return FakeProc()

    monkeypatch.setattr(stack.cam, "_popen", fake_popen)
    monkeypatch.setattr(stack.cam, "_alive", lambda pid: True)
    monkeypatch.setattr(stack.cam, "_reap_orphans", lambda port: None)
    monkeypatch.setattr(stack.cam, "permission", lambda: ("authorized", True))
    monkeypatch.setattr(stack.cam, "_post_webcam", lambda payload: True)
    ok, lines = stack.cam.start()
    return ok, lines, captured.get("cmd", [])


# ------------------------------------------------------------ conf hygiene


def test_register_webcam_adds_only_the_managed_section(conf):
    assert conf.cam.register_webcam() == []  # stack down: nothing to tell
    text = conf.paths.MOONRAKER_CONF.read_text()
    assert conf.conf_text.strip() in text, "the generated config was rewritten"
    assert webcam_headers(text) == ["[webcam maccam]"]
    assert managed_body(text) == MANAGED_LINES
    parse(conf.paths.MOONRAKER_CONF)
    assert conf.configgen.webcam_state() == (True, True)
    before = conf.paths.MOONRAKER_CONF.read_bytes()
    assert conf.configgen.ensure_webcam_section() is False
    assert conf.paths.MOONRAKER_CONF.read_bytes() == before


@pytest.mark.parametrize("device", [
    "Cam\nWith\nNewlines",
    '[webcam evil]\nenabled: False\nstream_url: http://evil/',
    'He said "no" and \'maybe\'',
    "# comment line\n[server]\nhost: 0.0.0.0",
    ";[webcam maccam]\nenabled: True",
    "--device --stream --port 1",
    "\r\n[webcam maccam]\r\n",
    "x" * 100000,
    "📷‮ ƈά",
])
def test_hostile_device_names_never_reach_moonraker_conf(conf, device):
    conf.cam.save_prefs(device=device)
    assert conf.cam.register_webcam() == []
    text = conf.paths.MOONRAKER_CONF.read_text()
    assert webcam_headers(text) == ["[webcam maccam]"], text[-400:]
    assert managed_body(text) == MANAGED_LINES, "the section grew user text"
    parse(conf.paths.MOONRAKER_CONF)
    assert conf.configgen.webcam_state() == (True, True)
    assert conf.configgen.set_webcam_enabled(False) is True
    assert conf.configgen.set_webcam_enabled(True) is True
    assert managed_body(conf.paths.MOONRAKER_CONF.read_text()) == MANAGED_LINES
    assert conf.cam.prefs()["device"] == device


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: configgen.ensure_webcam_section() - called by cam on via "
    "register_webcam() - replaces the whole managed section, so camera options "
    "the user added by hand there (target_fps, flip_horizontal, comments) are "
    "silently deleted on every 'klipperformac cam on', while cam off's "
    "set_webcam_enabled() deliberately preserves them"))
def test_hand_added_webcam_keys_survive_cam_on(conf):
    conf.configgen.ensure_webcam_section()
    text = conf.paths.MOONRAKER_CONF.read_text()
    text = text.replace("location: printer",
                        "location: printer\ntarget_fps: 12\n"
                        "flip_horizontal: true\n# my note")
    conf.paths.MOONRAKER_CONF.write_text(text)
    conf.cam.save_prefs(device="A\nB")
    conf.cam.register_webcam()                      # == what `cam on` does
    body = conf.paths.MOONRAKER_CONF.read_text()
    for needle in ("target_fps: 12", "flip_horizontal: true", "# my note"):
        assert needle in body, needle
    assert webcam_headers(body) == ["[webcam maccam]"]


def test_enable_disable_toggle_keeps_user_keys(conf):
    conf.configgen.ensure_webcam_section()
    text = conf.paths.MOONRAKER_CONF.read_text().replace(
        "location: printer", "location: printer\ntarget_fps: 12")
    conf.paths.MOONRAKER_CONF.write_text(text)
    assert conf.configgen.set_webcam_enabled(False) is True
    assert conf.configgen.set_webcam_enabled(True) is True
    body = conf.paths.MOONRAKER_CONF.read_text()
    assert "target_fps: 12" in body and "enabled: True" in body


def test_webcam_payload_never_carries_prefs(conf, monkeypatch):
    conf.cam.save_prefs(device="secret camera name")
    posted = []
    monkeypatch.setattr(conf.cam, "_post_webcam",
                        lambda payload: posted.append(payload) or True)
    monkeypatch.setattr(conf.process, "running", lambda: {"klipper": True})
    lines = conf.cam.register_webcam()
    assert posted and len(posted) == 1
    payload = posted[0]
    assert set(payload) == {"name", "enabled", "location", "service",
                            "stream_url", "snapshot_url"}
    assert "secret camera name" not in json.dumps(payload)
    assert payload["stream_url"] == "http://localhost:%d/?action=stream" % \
        conf.paths.CAM_PORT
    assert any("no restart needed" in l for l in lines), lines


def test_webcam_payload_urls_are_loopback_only(conf):
    conf.cam.save_prefs(device="0.0.0.0 evil")
    payload = conf.cam._webcam_payload()
    for url in (payload["stream_url"], payload["snapshot_url"]):
        assert url.startswith("http://localhost:"), url
    assert payload["name"] == conf.configgen.WEBCAM_NAME


def test_register_webcam_reports_a_missing_conf_without_raising(stack,
                                                                monkeypatch):
    monkeypatch.setattr(stack.cam, "_post_webcam", lambda payload: True)
    assert stack.paths.MOONRAKER_CONF.exists() is False
    lines = stack.cam.register_webcam()
    assert any("No moonraker.conf" in l for l in lines), lines


def test_register_webcam_survives_a_broken_conf_writer(stack, monkeypatch):
    def boom(*a, **k):
        raise IOError("disk on fire")

    monkeypatch.setattr(stack.configgen, "ensure_webcam_section", boom)
    lines = stack.cam.register_webcam()
    assert any("moonraker.conf" in l for l in lines), lines
    assert any("8082" in l for l in lines), lines


# ----------------------------------------------------------------- argv


def test_device_reaches_the_helper_as_one_inert_argv_element(conf, monkeypatch):
    name = 'Naughty "cam"\n; rm -rf / --stream'
    conf.cam.save_prefs(device=name, width=640, height=480, fps=24,
                        quality=33)
    ok, lines, argv = fake_start(conf, monkeypatch)
    assert ok is True, lines
    assert argv[0] == str(conf.paths.CAM_BIN)
    assert argv[1] == "--stream"
    assert argv[argv.index("--device") + 1] == name        # one element, exact
    assert argv.count("--device") == 1
    for flag, value in (("--width", "640"), ("--height", "480"),
                        ("--fps", "24"), ("--quality", "33"),
                        ("--port", str(conf.paths.CAM_PORT))):
        assert argv[argv.index(flag) + 1] == value
    assert "/bin/sh" not in argv and "-c" not in argv
    text = conf.paths.MOONRAKER_CONF.read_text()
    assert managed_body(text) == MANAGED_LINES
    assert webcam_headers(text) == ["[webcam maccam]"]
    state = json.loads(conf.paths.CAM_STATE.read_text())
    assert state["device"] == name and state["pid"] == 4242
    assert any("Camera on:" in l for l in lines), lines


def test_prefs_absent_keys_are_simply_not_passed(conf, monkeypatch):
    ok, lines, argv = fake_start(conf, monkeypatch)
    assert ok is True, lines
    assert "--device" not in argv
    assert argv[argv.index("--width") + 1] == "1280"
    assert len(argv) == 12, argv


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: cam.start() and cam.snapshot() gate the device on "
    "`if p.get('device')`, so a device stored as the JSON number 0 (a legal "
    "camera index; the CLI stores the string '0', which works) is silently "
    "dropped and the automatic camera is used instead"))
def test_numeric_device_index_zero_is_still_honoured(conf, monkeypatch):
    conf.paths.SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    conf.paths.SETTINGS.write_text('{"cam": {"device": 0}}')
    assert conf.cam.prefs()["device"] == 0
    ok, lines, argv = fake_start(conf, monkeypatch)
    assert ok is True, lines
    assert "--device" in argv, argv
    assert argv[argv.index("--device") + 1] == "0"


def test_non_string_prefs_are_stringified_into_argv(conf, monkeypatch):
    conf.paths.SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    conf.paths.SETTINGS.write_text(
        '{"cam": {"width": 640, "fps": "20", "quality": 55.5, '
        '"device": {"odd": true}}}')
    ok, lines, argv = fake_start(conf, monkeypatch)
    assert ok is True, lines
    assert argv[argv.index("--width") + 1] == "640"
    assert argv[argv.index("--fps") + 1] == "20"
    assert argv[argv.index("--device") + 1] == str({"odd": True})


def test_start_reports_the_registered_webcam_and_localhost_only(
        conf, monkeypatch):
    ok, lines, argv = fake_start(conf, monkeypatch)
    assert ok is True, lines
    joined = "\n".join(lines)
    assert "localhost" in joined
    assert "Only this Mac can watch it" in joined
    assert "0.0.0.0" not in joined


# ------------------------------------------------------------------- fuzz


@hst(max_examples=30, deadline=None,
     suppress_health_check=[HealthCheck.too_slow,
                            HealthCheck.function_scoped_fixture])
@given(device=TEXT, width=st.integers(160, 4096), fps=st.integers(1, 60))
def test_the_managed_section_always_survives_any_device_text(device, width, fps,
                                                             conf):
    conf.cam.save_prefs(device=device, width=width, fps=fps)
    conf.cam.register_webcam()
    conf.configgen.set_webcam_enabled(False)
    conf.configgen.set_webcam_enabled(True)
    conf.configgen.remove_webcam_section()
    conf.cam.register_webcam()
    text = conf.paths.MOONRAKER_CONF.read_text()
    assert webcam_headers(text) == ["[webcam maccam]"], text[-200:]
    assert managed_body(text) == MANAGED_LINES
    parse(conf.paths.MOONRAKER_CONF)
    assert conf.configgen.webcam_state() == (True, True)
    assert conf.cam.prefs()["device"] == device
    assert "[server]" in text and "[authorization]" in text


@hst(max_examples=20, deadline=None,
     suppress_health_check=[HealthCheck.too_slow,
                            HealthCheck.function_scoped_fixture])
@given(device=st.text(alphabet=st.characters(exclude_categories=("Cs",)),
                      max_size=300))
def test_register_webcam_never_touches_other_sections(device, conf, monkeypatch):
    """The camera text may not leak anywhere: only the managed section moves."""
    monkeypatch.setattr(conf.cam, "_post_webcam", lambda payload: True)
    monkeypatch.setattr(conf.process, "running", lambda: {"klipper": True})
    conf.cam.save_prefs(device=device)
    server_before = conf.conf_text[conf.conf_text.index("[server]"):
                                   conf.conf_text.index("[authorization]")]
    for _ in range(3):
        conf.cam.register_webcam()
    text = conf.paths.MOONRAKER_CONF.read_text()
    server_after = text[text.index("[server]"):text.index("[authorization]")]
    assert server_before == server_after
    assert webcam_headers(text) == ["[webcam maccam]"]
    assert managed_body(text) == MANAGED_LINES


@hst(max_examples=20, deadline=None,
     suppress_health_check=[HealthCheck.too_slow,
                            HealthCheck.function_scoped_fixture])
# min_size=1: the empty string is not a camera name, it is "no device chosen",
# which start() deliberately turns into "no --device at all" (the helper picks
# automatically). test_empty_device_string_means_automatic pins that case.
@given(name=st.text(alphabet=st.characters(exclude_categories=("Cs",)),
                    min_size=1, max_size=60))
def test_helper_argv_roundtrips_any_device(name, conf, monkeypatch):
    conf.cam.save_prefs(device=name)
    ok, lines, argv = fake_start(conf, monkeypatch)
    assert ok is True, lines
    assert argv[argv.index("--device") + 1] == name
    assert len(argv) == 14, argv
    assert conf.cam.prefs()["device"] == name


def test_empty_device_string_means_automatic(conf, monkeypatch):
    """'' is not a camera: it must behave exactly like 'device unset'."""
    conf.cam.save_prefs(device="")
    ok, lines, argv = fake_start(conf, monkeypatch)
    assert ok is True, lines
    assert "--device" not in argv, argv
    assert len(argv) == 12, argv
    assert conf.cam.prefs()["device"] == ""
