"""The control contract: how a recording is started, watched, stopped, reported.

Nothing here opens a socket or starts a process: spawns are recorded argvs whose
fake lives out the helper's contract (log lines, file, exit code), and the
loopback status endpoint is a canned key=value body. So every test says plainly
what this module sends and what it is willing to believe back - including the
answers that must never be dressed up as a success: exit 3 with no file, a movie
nobody ever closed, and a recording the helper finished picture-only because it
could not get the microphone.
"""
import re

import pytest

from conftest import IDLE_BODY, NOPICTURE, NO_SOUND, UNANSWERED, body

STATUS_URL = "http://127.0.0.1:8082/?action=recordstatus"


@pytest.fixture
def live(rig):
    """Camera up on a video-aware helper (started by an earlier command), with
    nothing recording yet."""
    rig.install_helper()
    ok, lines, _ = rig.start_camera()
    assert ok, lines
    rig.popens = []
    return rig


# ------------------------------------------------------------- starting one


def test_a_recording_starts_the_one_camera_process_with_its_own_flags(rig):
    rig.install_helper()
    ok, lines = rig.cam.record("bench")
    assert ok, lines
    assert lines[0] == "Recording: " + str(rig.recordings / "bench.mp4"), lines
    assert "klipperformac cam record off" in " ".join(lines), lines
    assert rig.cam._active_record()["file"] == str(rig.recordings /
                                                   "bench.mp4")


def test_a_streaming_camera_is_restarted_on_the_same_port_not_duplicated(live):
    live.recording_is_open()
    ok, lines = live.cam.record("bench")
    assert ok, lines
    assert len(live.popens) == 1, "one camera, one helper process"
    assert "--record" in live.last_argv()
    assert live.signals and live.signals[0][1] == 15, live.signals
    assert "restarted on the same port" in " ".join(lines), lines


def test_a_second_recording_is_refused_rather_than_fought(live):
    live.recording_is_open()
    live.start_recording("first")
    ok, lines = live.cam.record("second")
    assert not ok, lines
    assert "Already recording" in " ".join(lines), lines
    assert len(live.popens) == 1, "the open recording must not be disturbed"
    assert live.signals == [], lines


def test_no_camera_and_an_untracked_helper_is_refused_not_fought(live):
    live.paths.CAM_STATE.unlink()
    live.monkeypatch.setattr(live.cam, "_scan_pids", lambda: [9999])
    ok, lines = live.cam.record("bench")
    assert not ok, lines
    assert "no control of" in " ".join(lines), lines
    assert "killall" in " ".join(lines), lines
    assert live.popens == []


def test_a_bare_record_gets_a_timed_name_inside_the_recordings_folder(rig):
    rig.install_helper()
    ok, lines = rig.cam.record()
    assert ok, lines
    dest = rig.cam._active_record()["file"]
    assert dest.startswith(str(rig.recordings)) and dest.endswith(".mp4"), dest


# ------------------------------------------------------------------- timed


def test_a_timed_recording_returns_the_file_the_helper_closed(live):
    live.recording_finishes(size=8192, secs=2.0)
    ok, lines = live.cam.record("bench", secs=2)
    assert ok, lines
    text = "\n".join(lines)
    dest = live.recordings / "bench.mp4"
    assert "Saved: {}".format(dest) in text, lines
    assert "8.0 kB on disk" in text, lines
    assert "length 2s" in text and "sound off" in text, lines
    assert dest.is_file() and dest.stat().st_size == 8192


def test_a_timed_recording_leaves_no_camera_running(live):
    live.recording_finishes()
    ok, lines = live.cam.record("bench", secs=2)
    assert ok, lines
    assert live.cam.running() is None, "the helper exits with the file"
    assert not live.paths.CAM_STATE.exists(), "and the state goes with it"


def test_exit_three_leaves_no_file_and_no_claim_of_one(live):
    """The helper's hardest promise: a run that cannot finish deletes the file.
    Python must report exactly that - no size, no length, no `Saved:`."""
    live.recording_fails(exit_code=3, note=NOPICTURE)
    ok, lines = live.cam.record("bench", secs=2)
    assert not ok, lines
    text = "\n".join(lines)
    assert not (live.recordings / "bench.mp4").exists(), "no file, as promised"
    assert "Saved:" not in text, lines
    assert "No video was saved" in text, lines
    assert "exit 3" in text, lines
    assert "the camera sent no picture" in text, lines


@pytest.mark.parametrize("code,expect", [
    (1, "exit code 1"),
    (-9, "stopped while recording"),
])
def test_a_refusal_or_a_crash_is_called_what_it_was(live, code, expect):
    live.recording_fails(exit_code=code, note="KlipperCam: No camera found.")
    ok, lines = live.cam.record("bench", secs=1)
    assert not ok, lines
    assert expect in " ".join(lines), lines
    assert not (live.recordings / "bench.mp4").exists()


def test_a_movie_nobody_closed_is_never_reported_as_saved(live):
    """Bytes with no close are not a video. A helper killed with SIGKILL leaves
    them, and this module must name them raw bytes rather than a success."""
    live.start_recording("bench")
    dest = live.recordings / "bench.mp4"
    assert dest.is_file(), "the recording was open and had bytes"
    live.ignores_sigterm = True              # SIGTERM bounces, SIGKILL lands
    live.reply(200, body(path=str(dest)))
    ok, lines = live.cam.record("off")
    assert not ok, lines
    text = " ".join(lines)
    assert "never closed" in text and "raw bytes" in text, lines
    assert "Saved:" not in text, lines
    assert any(sig == 9 for _, sig in live.signals), live.signals


def test_a_soundless_recording_is_reported_as_the_helpers_choice(live):
    """--mic was asked for; the helper could not get the microphone and said so.
    The file is real and picture-only: honest on both counts."""
    live.recording_finishes(size=6000, secs=3.0, sound="off", note=NO_SOUND)
    ok, lines = live.cam.record("bench", secs=3, mic="USB Condenser")
    assert ok, lines
    assert "--mic" in live.last_argv() and "USB Condenser" in live.last_argv()
    text = "\n".join(lines)
    assert "Saved: " in text and "sound off" in text, lines
    assert "No sound:" in text, lines
    assert "Recording the picture only" in text, lines


def test_an_unanswered_microphone_prompt_is_passed_through(live):
    live.recording_finishes(sound="off", note=UNANSWERED)
    ok, lines = live.cam.record("bench", secs=2, mic="")
    assert ok, lines
    assert "went unanswered" in " ".join(lines), lines


def test_a_recording_that_missed_its_own_deadline_is_stopped_the_usual_way(live):
    """The helper overshot --seconds and is still up: `record off` finalises it
    with the same SIGTERM, and the file it closes is a real one."""
    live.hangs = True
    live.recording_finishes(size=5000, secs=9.0)
    ok, lines = live.cam.record("bench", secs=1)
    assert ok, lines
    text = " ".join(lines)
    assert "did not close on its own" in text, lines
    assert "Saved: " in text and "9s" in text, lines
    assert any(sig == 15 for _, sig in live.signals), live.signals


# ---------------------------------------------------------------- stopping


def test_record_off_finalises_the_file_then_reports_it(live):
    live.recording_is_open(size=4194304, secs=12.0)
    live.start_recording("bench")
    dest = live.recordings / "bench.mp4"
    live.reply(200, body(path=str(dest), bytez="4194304", secs="12.0"))
    ok, lines = live.cam.record("off")
    assert ok, lines
    text = "\n".join(lines)
    assert "Camera off - nothing is being captured." in text, lines
    assert "Saved: {}".format(dest) in text, lines
    assert "4.0 MB" in text and "length 12s" in text, lines
    assert "Closing the recording first" in text, lines


def test_record_off_asks_the_helper_before_it_is_signalled(live):
    """The status answer dies with the process, so it is read first; the only
    network call ever made is the read-only status."""
    live.recording_is_open()
    live.start_recording("bench")
    live.reply(200, body(path=str(live.recordings / "bench.mp4")))
    live.cam.record("off")
    assert live.urls == [STATUS_URL], live.urls
    assert not [url for url in live.urls if "op=" in url or "token" in url]
    assert not [url for url in live.urls if not url.startswith("http://127.0.0.1")], \
        "loopback only - nothing reaches another computer"


def test_record_off_when_nothing_was_recording(live):
    live.reply(200, IDLE_BODY)
    ok, lines = live.cam.record("off")
    assert not ok, lines
    assert "Nothing was recording" in " ".join(lines), lines
    assert live.signals == [], "nothing open, nothing to signal"


def test_record_off_reports_a_video_that_was_never_kept(live):
    live.recording_fails(exit_code=3, note=NOPICTURE)
    live.start_recording("bench")
    dest = live.recordings / "bench.mp4"
    live.reply(200, body(path=str(dest), bytez="0"))
    ok, lines = live.cam.record("off")
    assert not ok, lines
    assert "No video was saved" in " ".join(lines), lines
    assert not dest.exists()


def test_record_off_when_the_helper_will_not_answer_lives_to_tell_the_truth(live):
    live.recording_is_open()
    live.start_recording("bench")
    live.reply(0, "")
    ok, lines = live.cam.record("off")
    assert not ok, lines
    assert "did not answer" in " ".join(lines), lines
    assert live.signals == [], "a camera we cannot read is a camera we do not kill"


def test_cam_off_closes_an_open_video_the_same_way(live):
    live.recording_is_open(size=2048, secs=5.0)
    live.start_recording("bench")
    dest = live.recordings / "bench.mp4"
    live.reply(200, body(path=str(dest), bytez="2048", secs="5.0"))
    ok, lines = live.cam.stop()
    assert ok, lines
    assert lines[0] == "Camera off - nothing is being captured.", lines
    assert "Saved: {}".format(dest) in " ".join(lines), lines
    assert lines[-1] == "Turn it back on: klipperformac cam on", lines


def test_a_stubborn_camera_is_said_so_not_papered_over(live):
    live.recording_is_open()
    live.start_recording("bench")
    live.stubborn = True
    live.reply(200, body(path=str(live.recordings / "bench.mp4")))
    ok, lines = live.cam.stop()
    assert not ok, lines
    assert "refuses to die" in " ".join(lines), lines
    assert "Force it with: klipperformac killall" in " ".join(lines), lines
    assert live.paths.CAM_STATE.exists(), "a camera we could not stop is still up"


# ----------------------------------------------------------------- status


def test_status_shows_the_live_recording_and_the_room_left(live):
    live.recording_is_open(size=1048576, sound="on")
    live.start_recording("bench", mic="USB Condenser")
    dest = str(live.recordings / "bench.mp4")
    live.reply(200, body(path=dest, bytez="1048576", secs="8.0", audio="on",
                         mic="authorized"))
    ok, lines = live.cam.record("status")
    assert ok, lines
    text = "\n".join(lines)
    assert "capturing " + dest in text, lines
    assert "1.0 MB" in text and "8s" in text, lines
    assert re.search(r"sound\s+on", text), lines
    assert re.search(r"mic perm\s+authorized", text), lines
    assert str(live.recordings) in text and "free" in text, lines
    assert len(live.popens) == 1, "status must never touch the camera again"
    assert not live.cmd.asked("--check-mic-permission"), live.cmd.runs


def test_status_with_the_camera_off_says_idle_and_how_to_start(rig):
    rig.install_helper()
    ok, lines = rig.cam.record("status")
    assert ok, lines
    text = "\n".join(lines)
    assert "idle (the camera is off)" in text, lines
    assert "cam record <name>" in text, lines


def test_status_of_an_untracked_helper_names_the_pid(live):
    live.paths.CAM_STATE.unlink()
    live.monkeypatch.setattr(live.cam, "_scan_pids", lambda: [9999])
    ok, lines = live.cam.record("status")
    assert ok and "untracked helper holds it, pid 9999" in " ".join(lines), lines


def test_status_of_a_closed_or_failed_run_is_not_called_recording(live):
    live.recording_is_open()
    live.start_recording("bench")
    dest = str(live.recordings / "bench.mp4")
    live.reply(200, body(state="done", path=dest))
    ok, lines = live.cam.record("status")
    assert ok and "closed" in " ".join(lines), lines
    live.recording_is_open()
    live.start_recording("second")
    live.reply(200, body(state="failed", path=dest,
                         error="nothing_was_recorded"))
    ok, lines = live.cam.record("status")
    assert "FAILED" in " ".join(lines), lines
    assert "nothing was recorded" in " ".join(lines), lines


def test_a_404_is_read_as_no_video_support_not_a_mystery(live):
    live.reply(404, "Not found")
    ok, lines = live.cam.record("status")
    assert ok, lines
    assert "no recording status" in " ".join(lines), lines
    live.reply(404, "Not found")
    ok, lines = live.cam.record("off")
    assert not ok, lines
    assert "cannot record video" in " ".join(lines), lines
    assert "no video was lost" in " ".join(lines), lines


def test_a_helper_that_answers_in_prose_is_believed_as_nothing(live):
    live.reply(200, "Not found at all")
    assert live.cam._record_info(live.state())["head"] == "unreachable"
    live.reply(200, "")
    assert live.cam._record_info(live.state())["head"] == "unreachable"


def test_a_path_with_spaces_survives_the_status_line(live):
    where = "/x/bench test.mp4"
    live.reply(200, body(path=where))
    info = live.cam._record_info(live.state())
    assert info["file"] == where, info
    assert info["bytes"] == 1048576 and info["secs"] == 8, info


def test_the_recorded_line_is_read_only_from_this_runs_bytes(live):
    """The log is shared between runs: the close line of an older recording must
    never be credited to this one."""
    dest = live.recordings / "bench.mp4"
    live.write_video("bench.mp4")
    live.paths.CAM_LOG.parent.mkdir(parents=True, exist_ok=True)
    live.paths.CAM_LOG.write_text(
        "Recording to {} (picture only)\n".format(dest) +
        "Recorded {} (99.0 s, 999999 bytes, sound on)\n".format(dest) +
        "Camera ready: built-in 1280x720 at 15 fps.\n")
    whole = live.paths.CAM_LOG.stat().st_size
    info = {"file": str(dest)}
    before = live.cam._saved_lines(info, {"file": str(dest), "log_at": 0})
    assert any("999999 bytes" in line for line in before), before
    after = live.cam._saved_lines(info, {"file": str(dest), "log_at": whole})
    assert not any("999999" in line for line in after), after
    assert any("never closed" in line for line in after), after


# ------------------------------------------------------------ the two parsers


def test_seconds_are_parsed_then_bounded(rig):
    assert rig.cam._record_secs("30s") == (30, None)
    assert rig.cam._record_secs("30 sec") == (30, None)
    assert rig.cam._record_secs(45) == (45, None)
    assert rig.cam._record_secs(None) == (None, None)
    for bad in ("0", "-4", "3601", "999999", "soon", "1.5.3", "off"):
        secs, why = rig.cam._record_secs(bad)
        assert secs is None and why, bad


def test_an_absurd_duration_is_refused_before_anything_is_asked(live):
    ok, lines = live.cam.record("bench", secs="999999")
    assert not ok, lines
    assert "3600" in " ".join(lines), lines
    assert live.popens == [] and live.urls == []


def test_the_mic_value_is_the_flag_the_helper_understands(rig):
    off = {"audio": False, "mic": None}
    on = {"audio": True, "mic": "USB Condenser"}
    assert rig.cam._record_mic(None, None, off) == (None, None)
    assert rig.cam._record_mic(None, None, on) == ("USB Condenser", None)
    assert rig.cam._record_mic("", None, off) == ("", None)      # bare --mic
    assert rig.cam._record_mic("on", None, off) == ("", None)    # same words
    assert rig.cam._record_mic("off", None, on) == (None, None)  # takes it back
    assert rig.cam._record_mic("USB", None, off) == ("USB", None)
    assert rig.cam._record_mic(None, False, on) == (None, None)
    assert rig.cam._record_mic("", False, on) == (None, None)
    assert rig.cam._record_mic("MacBook", None, on) == ("MacBook", None)


def test_an_unusable_mic_name_refuses_the_recording_not_the_camera(rig):
    rig.install_helper()
    rig.cam.save_prefs(audio=True)
    data = rig.paths.cam_settings()
    data["mic"] = "\ud800"                         # a lone surrogate
    rig.paths.save_cam_settings(**data)
    ok, lines = rig.cam.record("bench")
    assert not ok, lines
    assert "cannot be handed to the helper" in " ".join(lines), lines
    assert rig.popens == []
