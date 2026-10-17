"""Boot contract: what the helper is handed, and what may never be assumed.

Two promises hold this file together. First, an install that did not ask for
video sees exactly what it always saw: the same argv, the same prose, the same
state keys. Second, capability is DISCOVERED, never assumed: the installed
helper may be an old build with no --record at all, and an old helper exits 1 on
an option it does not know - which would take the camera down along with the
recording. So --help is probed, and nothing is ever spawned with flags and
pretended about.
"""
import json
import os
import stat

import pytest

from conftest import IDLE_BODY

LEGACY_TAIL = ["--stream", "--port", "8082", "--width", "1280", "--height",
               "720", "--fps", "15", "--quality", "80"]


# ------------------------------------------------- the helper we already have


def test_cam_on_says_nothing_new_to_a_helper_without_video(rig):
    rig.install_helper(video=False)
    ok, lines, _ = rig.start_camera()
    assert ok, lines
    assert rig.last_argv() == LEGACY_TAIL, "the legacy argv must not gain a flag"
    # and no --help probe either: `cam on` owes the user one spawn, not two
    assert not rig.cmd.asked("--help"), rig.cmd.runs


def test_cam_on_prose_is_untouched_for_a_plain_camera(rig):
    rig.install_helper(video=False)
    ok, lines, _ = rig.start_camera()
    assert lines[0].startswith("Camera on: http://localhost:8082")
    assert lines[1] == "  Only this Mac can watch it (localhost)."
    assert lines[2] == "  Stop anytime: klipperformac cam off"
    assert not [text for text in lines if "record" in text.lower()
                or "mic" in text.lower()], lines


def test_a_video_helper_starts_with_the_same_argv_when_no_video_is_asked(rig):
    rig.install_helper()
    ok, lines, _ = rig.start_camera()
    assert ok, lines
    assert rig.last_argv() == LEGACY_TAIL
    assert not rig.cmd.asked("--help"), "cam on must not pay for a probe"


def test_the_state_file_is_private_and_names_the_camera(rig):
    rig.install_helper()
    ok, lines, _ = rig.start_camera()
    assert ok, lines
    data = json.loads(rig.paths.CAM_STATE.read_text())
    assert data["pid"] == 4242 and data["port"] == 8082, data
    mode = stat.S_IMODE(os.stat(str(rig.paths.CAM_STATE)).st_mode)
    assert mode == 0o600, "a file naming the lens must not be world readable"


def test_a_state_with_no_record_block_reads_as_running(rig):
    rig.install_helper()
    rig.state()
    assert rig.cam.running() is not None
    assert rig.cam._active_record() is None


# ------------------------------------------------------ the capability probe


def test_the_probe_is_the_helpers_own_help_and_is_cached(rig):
    rig.install_helper()
    assert rig.cam.recording_supported() is True
    assert rig.cam.recording_supported() is True
    assert len(rig.cmd.asked("--help")) == 1, "the answer is cached per build"


def test_an_old_helper_says_no(rig):
    rig.install_helper(video=False)
    assert rig.cam.recording_supported() is False
    assert rig.cam.mic_supported() is False


def test_a_rebuild_is_picked_up_not_cached_forever(rig):
    rig.install_helper(video=False)
    assert rig.cam.recording_supported() is False
    rig.install_helper()                      # same path, new bytes
    assert rig.cam.recording_supported() is True


def test_an_old_helper_is_refused_with_update_advice_and_never_spawned(rig):
    rig.install_helper(video=False)
    ok, lines = rig.cam.record("bench")
    assert not ok, lines
    text = " ".join(lines).lower()
    assert "cannot record video" in text, lines
    assert "update the camera helper" in text, lines
    assert "klipperformac cam build" in text, lines
    assert rig.popens == [], "an old helper must never be handed --record"
    assert rig.urls == [], "nothing may be asked of a camera that cannot answer"


def test_no_helper_means_no_recording_and_no_mystery(rig):
    ok, lines = rig.cam.record("bench")
    assert not ok, lines
    assert "cam build" in " ".join(lines), lines
    assert rig.popens == []


def test_a_record_aware_helper_is_handed_its_own_flags(rig):
    rig.install_helper()
    rig.recording_finishes(secs=7.0)
    ok, lines = rig.cam.record("bench", secs=7, mic="USB Condenser")
    assert ok, lines
    argv = rig.last_argv()
    assert argv[:2] == ["--stream", "--port"], argv
    assert argv[argv.index("--record") + 1] == str(
        rig.recordings / "bench.mp4"), argv
    assert argv[argv.index("--seconds") + 1] == "7", argv
    assert argv[argv.index("--mic") + 1] == "USB Condenser", argv
    assert lines[0].startswith("Saved: "), lines


def test_bare_mic_is_the_bare_flag_and_no_name(rig):
    rig.install_helper()
    ok, lines = rig.cam.record("bench", mic="")
    assert ok, lines
    argv = rig.last_argv()
    assert argv[-1] == "--mic", "a trailing --mic means the default mic"


def test_no_sound_asked_means_no_mic_flag(rig):
    rig.install_helper()
    ok, lines = rig.cam.record("bench")
    assert ok, lines
    assert "--mic" not in rig.last_argv(), rig.last_argv()
    assert "Sound: off" in " ".join(lines), lines


def test_mic_is_never_sent_without_a_recording(rig):
    """The helper itself dies on `--mic` without `--record` (its own words), and
    an audio preference alone must therefore never reach a plain `cam on`."""
    rig.install_helper()
    rig.cam.save_prefs(audio=True, mic="USB Condenser")
    ok, lines, _ = rig.start_camera()
    assert ok, lines
    assert rig.last_argv() == LEGACY_TAIL, "the camera would not have started"
    assert not [t for t in lines if "mic" in t.lower()], lines


def test_the_record_block_names_the_file_and_where_its_lines_start(rig):
    rig.install_helper()
    ok, lines = rig.cam.record("bench", mic="USB Condenser")
    assert ok, lines
    rec = json.loads(rig.paths.CAM_STATE.read_text())["record"]
    assert rec["file"] == str(rig.recordings / "bench.mp4"), rec
    assert rec["secs"] == 0 and rec["audio"] is True, rec
    assert isinstance(rec["log_at"], int), "where this run's own lines start"
    assert rec["started"], rec


# --------------------------------------------------------- the microphone UI


def test_mics_lists_the_names_the_helper_gives(rig):
    rig.install_helper()
    ok, lines = rig.cam.mics()
    assert ok, lines
    assert lines == ["0\tMacBook Pro Microphone", "1\tUSB Condenser"], lines
    assert len(rig.cmd.asked("--list-mics")) == 1


def test_mics_of_an_old_helper_is_update_advice_not_a_guess(rig):
    rig.install_helper(video=False)
    ok, lines = rig.cam.mics()
    assert not ok, lines
    assert "update the camera helper" in " ".join(lines).lower(), lines
    assert not rig.cmd.asked("--list-mics"), "it would have exited 1"


def test_mics_with_no_microphone_at_all(rig):
    rig.install_helper()
    rig.cmd.mics = ""
    ok, lines = rig.cam.mics()
    assert ok and lines == ["No microphone found on this Mac."], lines


@pytest.mark.parametrize("word", ["authorized", "denied", "restricted",
                                 "notDetermined"])
def test_mic_permission_is_the_words_and_codes_the_helper_prints(rig, word):
    rig.install_helper(mic=word)
    got, usable = rig.cam.mic_permission()
    assert got == word, got
    assert usable is (word == "authorized")
    asked = rig.cmd.asked("--check-mic-permission")
    assert len(asked) == 1 and asked[0][-1] == "--check-mic-permission"


def test_mic_permission_never_prompts_and_never_records(rig):
    rig.install_helper(mic="notDetermined")
    ok, lines = rig.cam.mic_permission_status()
    assert ok, lines                       # nothing has been asked yet: not a fault
    text = " ".join(lines)
    assert "notDetermined" in text and "never triggers" in text, lines
    assert rig.popens == [], "a permission question must not start a camera"


@pytest.mark.parametrize("word,ok_expected", [("authorized", True),
                                             ("denied", False),
                                             ("restricted", False)])
def test_a_denied_microphone_is_a_failing_answer(rig, word, ok_expected):
    rig.install_helper(mic=word)
    ok, lines = rig.cam.mic_permission_status()
    assert ok is ok_expected, lines
    if not ok_expected:
        assert "System Settings" in " ".join(lines), lines


def test_mic_permission_of_an_old_helper_is_refused(rig):
    rig.install_helper(video=False)
    ok, usable = rig.cam.mic_permission()
    assert ok == "unknown" and usable is False
    assert not rig.cmd.asked("--check-mic-permission"), rig.cmd.runs


# ------------------------------------------------------------ state damage


CORRUPT = [
    ("torn json", '{"pid": 424, "boot'),
    ("not an object", "[1, 2, 3]"),
    ("empty file", ""),
    ("null", "null"),
    ("record not a dict", '{"record": ["a"]}'),
    ("record file not a str", '{"record": {"file": 42}}'),
    ("record file with a NUL", '{"record": {"file": "a\\u0000b"}}'),
    ("record log_at a word", '{"record": {"file": "/a.mp4", "log_at": "x"}}'),
    ("no pid", '{"port": 8082}'),
]


def corrupt_state(rig, raw):
    rig.paths.RUN.mkdir(parents=True, exist_ok=True)
    rig.paths.CAM_STATE.write_text(raw)


@pytest.mark.parametrize("label,raw", CORRUPT, ids=[c[0] for c in CORRUPT])
def test_a_mangled_state_file_is_healed_never_trusted(rig, label, raw):
    corrupt_state(rig, raw)
    rig.install_helper()
    assert rig.cam.running() is None, label        # healed, not believed
    assert rig.cam._active_record() is None, label
    assert rig.cam.stop() == (False, ["Camera was not on."])


def test_a_garbage_record_block_is_ignored_by_status(rig):
    rig.install_helper()
    rig.state(record="not-a-dict")
    rig.reply(200, IDLE_BODY)
    ok, lines = rig.cam.record("status")
    assert ok and "idle" in " ".join(lines), lines
