"""Bounds and surface: where a video may be written, how much room it needs,
what gets pruned, what `doctor` says, and how the CLI grammar arrives.

Everything here is offline: the helper is a recorded argv, the disk is an env
var, and no test deletes a file it did not recognise as its own.
"""
import json

import pytest

from conftest import NOPICTURE, body


@pytest.fixture
def live(rig):
    """A video-aware helper and a camera this command is free to restart."""
    rig.install_helper()
    return rig


def record(live, name="bench", **kw):
    """Start a recording and hand back (ok, lines) with the file it named."""
    ok, lines = live.cam.record(name, **kw)
    return ok, lines


# ------------------------------------------------------ output path guards

BAD_NAMES = ["/tmp/evil.mp4", "/etc/passwd.mp4", "../escape.mp4",
             "sub/dir.mp4", "~/out.mp4", ".hidden.mp4", "-flag.mp4",
             "a" * 120 + ".mp4", "sound.m4a", "two\nlines.mp4",
             "back\x01slash.mp4", "recordings/../../escape.mp4"]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_a_name_that_could_escape_the_recordings_folder_is_refused(live, name):
    ok, lines = record(live, name)
    assert not ok, (name, lines)
    assert live.popens == [], name
    assert lines, name
    assert not live.urls, name


def test_a_path_inside_the_recordings_folder_is_honoured(live):
    where = str(live.recordings / "bench.mp4")
    ok, lines = record(live, where)
    assert ok, lines
    assert live.last_argv()[live.last_argv().index("--record") + 1] == where


def test_a_bare_name_becomes_a_video_in_the_recordings_folder(live):
    ok, lines = record(live, "bench")
    assert ok, lines
    dest = str(live.recordings / "bench.mp4")
    assert lines[0] == "Recording: " + dest, lines
    assert dest in live.last_argv(), live.last_argv()


def test_a_symlink_parked_on_the_target_name_is_not_followed(live, tmp_path):
    live.recordings.mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "somewhere-else.mp4"
    elsewhere.write_bytes(b"NOT MINE")
    (live.recordings / "mine.mp4").symlink_to(elsewhere)
    ok, lines = record(live, "mine.mp4")
    assert not ok, lines
    assert "outside the recordings folder" in " ".join(lines), lines
    assert elsewhere.read_bytes() == b"NOT MINE"
    assert live.popens == []


def test_an_existing_video_is_never_clobbered(live):
    held = live.make_video("2026-10-16_080000.mp4", 4096)
    ok, lines = record(live, held.name)
    assert not ok, lines
    assert "already holds a video" in " ".join(lines), lines
    assert held.stat().st_size == 4096


def test_a_zero_byte_leftover_may_be_reused(live):
    held = live.make_video("2026-10-16_080001.mp4", 0)
    ok, lines = record(live, held.name)
    assert ok, lines


def test_a_recordings_folder_that_is_a_file_is_refused(live):
    live.paths.DATA.mkdir(parents=True, exist_ok=True)
    (live.paths.DATA / "recordings").write_text("not a folder")
    ok, lines = record(live)
    assert not ok, lines
    assert "Cannot create" in " ".join(lines), lines
    assert live.popens == []


def test_recordings_follow_the_data_folder_it_was_moved_to(rig, monkeypatch):
    """`klipperformac data <dir>` relocation is honoured: recordings are made
    where the data folder is now, not where it used to be."""
    rig.install_helper()
    elsewhere = rig.paths.DATA.parent / "moved-data"
    monkeypatch.setattr(rig.paths, "DATA", elsewhere)
    assert rig.cam.recordings_dir() == elsewhere / "recordings"


# ------------------------------------------------------------ size bounds


def test_a_full_disk_refuses_before_asking_for_video(live, monkeypatch):
    monkeypatch.setenv("KLIPPERFORMAC_RECORD_MIN_FREE_MB", "999999999")
    ok, lines = record(live)
    assert not ok, lines
    assert "free" in " ".join(lines), lines
    assert live.popens == []


def test_the_space_floor_is_honest_about_the_default(live):
    """The floor is a real number, not a formality: 500 MB is what a forgotten
    recording needs room to grow into."""
    assert live.cam._env_mb(live.cam.FREE_ENVS, live.cam.RECORD_MIN_FREE_MB) \
        == 500 * 1024 * 1024


def test_free_space_is_measured_on_the_volume_that_holds_it(live):
    assert live.cam.free_bytes(live.recordings) > 0
    assert live.cam.free_bytes("/nope/nope/nowhere.mp4") >= 0


def test_prune_deletes_only_our_own_old_videos(rig):
    rig.install_helper()
    keep = rig.make_video("2026-10-16_100000.mp4", 1024 * 1024)
    old = rig.make_video("2026-10-15_100000.mp4", 1024 * 1024)
    named = rig.make_video("holiday.mp4", 1024 * 1024)           # user's own
    side = rig.make_video("2026-10-14_100000.m4a", 1024 * 1024)  # ours, audio
    (rig.recordings / "notes.txt").write_text("keep me")
    (rig.recordings / "2026-10-13_100000.mp4").mkdir()            # not a file
    freed, gone = rig.cam.prune_videos(limit=1024 * 1024)
    assert keep.exists() and named.exists()
    assert not old.exists() and not side.exists()
    assert sorted(gone) == sorted([old.name, side.name]), gone
    assert freed == 2 * 1024 * 1024
    assert (rig.recordings / "notes.txt").exists()
    assert (rig.recordings / "2026-10-13_100000.mp4").is_dir()


def test_prune_keeps_the_file_being_written(rig):
    rig.install_helper()
    old = rig.make_video("2026-10-01_100000.mp4", 4 * 1024 * 1024)
    live_now = rig.make_video("2026-10-16_100000.mp4", 4 * 1024 * 1024)
    freed, gone = rig.cam.prune_videos(limit=4 * 1024 * 1024,
                                       keep=[str(live_now)])
    assert live_now.exists() and old not in gone and gone == [old.name]
    assert freed == 4 * 1024 * 1024


def test_a_recording_that_overshoots_the_cap_is_pruned_when_it_stops(live,
                                                                    monkeypatch):
    monkeypatch.setenv("KLIPPERFORMAC_RECORDINGS_MAX_MB", "1")
    old = live.make_video("2026-10-01_100000.mp4", 3 * 1024 * 1024)
    live.recording_finishes(size=2097152, secs=30.0)
    ok, lines = live.cam.record("bench", secs=2)
    assert ok, lines
    assert not old.exists(), "the older one of ours should have made room"
    assert "freed 3.0 MB" in " ".join(lines), lines


def test_the_open_recording_is_never_pruned(live):
    """Pruning happens when a recording closes, and the file being written (or
    just closed) is always kept."""
    live.recording_fails(note=NOPICTURE)
    started = live.cam.record("bench")          # camera restarted, recording up
    assert started[0], started[1]
    dest = live.recordings / "bench.mp4"
    dest.write_bytes(b"x" * 300000)
    freed, gone = live.cam.prune_videos(limit=1024, keep=[str(dest)])
    assert dest.exists() and not gone, (freed, gone)


# ---------------------------------------------------------------- doctor


def test_doctor_stays_quiet_when_there_is_no_helper(rig):
    out = rig.cam.doctor()
    assert len(out) == 6, out
    assert not [m for ok, m in out if "recording" in m], out


def test_doctor_gains_one_recording_line_and_spawns_nothing(rig):
    rig.install_helper()
    out = rig.cam.doctor()
    assert len(out) == 7, out
    lines = [m for ok, m in out if m.startswith("recording:")]
    assert len(lines) == 1, out
    assert "recordings" in lines[0]
    assert not rig.cmd.asked("--help"), rig.cmd.runs


def test_doctor_calls_a_full_disk_a_problem(rig, monkeypatch):
    rig.install_helper()
    monkeypatch.setenv("KLIPPERFORMAC_RECORD_MIN_FREE_MB", "999999999")
    ok, msg = rig.cam._check_recording()
    assert ok is False and "only" in msg, msg


def test_doctor_calls_an_oversized_folder_a_problem(rig, monkeypatch):
    rig.install_helper()
    monkeypatch.setenv("KLIPPERFORMAC_RECORDINGS_MAX_MB", "1")
    rig.make_video("2026-10-16_100000.mp4", 3 * 1024 * 1024)
    ok, msg = rig.cam._check_recording()
    assert ok is False and "over" in msg, msg


def test_doctor_reports_a_helper_that_cannot_record(rig):
    rig.install_helper(video=False)
    rig.cam.helper_flags()                     # this command may ask --help
    ok, msg = rig.cam._check_recording()
    assert ok is True and "not in this helper build" in msg, msg


def test_doctor_passes_on_what_the_helper_says_about_an_open_recording(live):
    dest = str(live.recordings / "bench.mp4")
    live.state(record={"file": dest, "secs": 0, "log_at": 0,
                       "started": "2026-10-16_090000"})
    live.reply(200, body(path=dest, bytez="2097152"))
    ok, msg = live.cam._check_recording()
    assert ok is True and "capturing " + dest in msg, msg
    assert "2.0 MB" in msg, msg


# ----------------------------------------------------- mic commands at the UI


def test_cam_mics_hands_the_answer_straight_to_the_user(stack, monkeypatch,
                                                        capsys):
    seen = []
    monkeypatch.setattr(stack.cam, "mics",
                        lambda: (True, ["0\tMacBook Pro Microphone",
                                        "1\tUSB Condenser"]))
    stack.cli.cmd_cam(parsed(["cam", "mics"], monkeypatch, stack.cli))
    out = capsys.readouterr().out
    assert "USB Condenser" in out and not seen, out


def test_cam_mics_of_an_old_helper_exits_nonzero(stack, monkeypatch, capsys):
    monkeypatch.setattr(stack.cam, "mics",
                        lambda: (False, ["Update the camera helper once."]))
    args = parsed(["cam", "mics"], monkeypatch, stack.cli)
    with pytest.raises(SystemExit) as gone:
        stack.cli.cmd_cam(args)
    assert gone.value.code == 1
    assert "Update the camera helper" in capsys.readouterr().out


def test_cam_mic_permission_exits_nonzero_on_a_denied_microphone(
        stack, monkeypatch, capsys):
    monkeypatch.setattr(stack.cam, "mic_permission_status",
                        lambda: (False, ["Microphone permission: denied."]))
    args = parsed(["cam", "mic-permission"], monkeypatch, stack.cli)
    with pytest.raises(SystemExit) as gone:
        stack.cli.cmd_cam(args)
    assert gone.value.code == 1
    assert "denied" in capsys.readouterr().out
    monkeypatch.setattr(stack.cam, "mic_permission_status",
                        lambda: (True, ["Microphone permission: authorized"]))
    stack.cli.cmd_cam(parsed(["cam", "mic-permission"], monkeypatch, stack.cli))
    assert "authorized" in capsys.readouterr().out


# ------------------------------------------------------------ CLI grammar


def parsed(argv, monkeypatch, cli):
    """The argv as cli.main() parses it, with the real cmd_cam put back so a
    test can run it."""
    real = cli.cmd_cam
    seen = {}

    def spy(args):
        seen["args"] = args

    monkeypatch.setattr(cli, "cmd_cam", spy)
    cli.main(argv)
    monkeypatch.setattr(cli, "cmd_cam", real)
    return seen["args"]


def test_the_record_grammar_arrives_intact(stack, monkeypatch):
    args = parsed(["cam", "record", "bench.mp4"], monkeypatch, stack.cli)
    assert (args.action, args.path, args.seconds, args.mic) == (
        "record", "bench.mp4", None, None)
    args = parsed(["cam", "record", "bench", "--seconds", "30"],
                  monkeypatch, stack.cli)
    assert (args.path, args.seconds) == ("bench", "30")
    args = parsed(["cam", "record", "bench", "--seconds", "30",
                   "--mic", "USB Condenser"], monkeypatch, stack.cli)
    assert args.mic == "USB Condenser"
    args = parsed(["cam", "record", "off"], monkeypatch, stack.cli)
    assert args.path == "off"
    args = parsed(["cam", "record", "status"], monkeypatch, stack.cli)
    assert args.path == "status"


def test_the_mic_flags_arrive_as_the_helper_reads_them(stack, monkeypatch):
    """`--mic` bare and `--mic NAME` are different answers: the first means the
    microphone macOS picks, the second a name from `cam mics`."""
    args = parsed(["cam", "record", "bench", "--mic"], monkeypatch, stack.cli)
    assert args.mic == "", args.mic
    args = parsed(["cam", "record", "bench", "--mic", "off"],
                  monkeypatch, stack.cli)
    assert args.mic == "off"
    args = parsed(["cam", "record", "bench"], monkeypatch, stack.cli)
    assert args.mic is None


def test_the_mic_and_permission_commands_are_actions(stack, monkeypatch):
    assert parsed(["cam", "mics"], monkeypatch, stack.cli).action == "mics"
    assert parsed(["cam", "mic-permission"],
                  monkeypatch, stack.cli).action == "mic-permission"


def test_the_old_grammar_is_untouched(stack, monkeypatch):
    args = parsed(["cam", "snapshot", "one.jpg"], monkeypatch, stack.cli)
    assert (args.action, args.path) == ("snapshot", "one.jpg")
    args = parsed(["cam", "on"], monkeypatch, stack.cli)
    assert (args.action, args.path) == ("on", None)
    args = parsed(["cam", "config", "--fps", "20"], monkeypatch, stack.cli)
    assert (args.action, args.fps, args.audio, args.mic) == ("config", 20,
                                                            None, None)
    args = parsed(["cam", "list"], monkeypatch, stack.cli)
    assert args.action == "list"


def test_cmd_cam_hands_the_record_flags_straight_over(stack, monkeypatch):
    calls = []
    monkeypatch.setattr(stack.cam, "record",
                        lambda *a, **kw: calls.append((a, kw)) or
                        (True, ["ok"]))
    args = parsed(["cam", "record", "bench", "--seconds", "45", "--mic",
                   "USB"], monkeypatch, stack.cli)
    stack.cli.cmd_cam(args)
    assert calls == [(("bench",), {"secs": "45", "mic": "USB",
                                   "audio": None})], calls


def test_cmd_cam_hands_the_bare_mic_and_no_seconds(stack, monkeypatch):
    calls = []
    monkeypatch.setattr(stack.cam, "record",
                        lambda *a, **kw: calls.append((a, kw)) or
                        (True, ["ok"]))
    args = parsed(["cam", "record", "bench", "--mic"], monkeypatch, stack.cli)
    stack.cli.cmd_cam(args)
    assert calls == [(("bench",), {"secs": None, "mic": "",
                                   "audio": None})], calls


def test_cmd_cam_prints_and_exits_like_every_other_cam_command(
        stack, monkeypatch, capsys):
    monkeypatch.setattr(stack.cam, "record",
                        lambda *a, **kw: (False, ["no can do"]))
    args = parsed(["cam", "record", "bench"], monkeypatch, stack.cli)
    with pytest.raises(SystemExit) as gone:
        stack.cli.cmd_cam(args)
    assert gone.value.code == 1
    assert capsys.readouterr().out.strip() == "no can do"


def test_cam_config_stores_the_mic_flags(stack, monkeypatch, capsys):
    stack.paths.APP_HOME.mkdir(parents=True, exist_ok=True)
    args = parsed(["cam", "config", "--audio", "on", "--mic", "USB"],
                  monkeypatch, stack.cli)
    stack.cli.cmd_cam(args)
    saved = json.loads(stack.paths.SETTINGS.read_text())["cam"]
    assert saved == {"audio": True, "mic": "USB"}
    args = parsed(["cam", "config", "--audio", "off"], monkeypatch, stack.cli)
    stack.cli.cmd_cam(args)
    assert json.loads(stack.paths.SETTINGS.read_text())["cam"]["audio"] is False
    stack.cli.cmd_cam(parsed(["cam", "config"], monkeypatch, stack.cli))
    out = capsys.readouterr().out
    assert "audio" in out and "mic" in out, out
    assert "audio     off" in out, out
    assert "mic       USB" in out, out
