"""The unclean-stop truth: two files that have to outlive a kill -9 and a reboot.

`session.json` means "a stack session started and nothing ever said it ended";
`stop.json` is the verdict of the most recently closed session. The whole feature
is this pair plus the judgement in `last_stop()`, so the interesting cases are the
ones a naive implementation gets wrong: a graceful `down` on a half-dead stack, a
reboot that erased run/state.json (process.py:110-112 - verified in the facts
pass, and the reason nothing here hangs off the stack record), a marker that is
older than the journal it claims to close, and junk in either file.
"""
import json

import pytest

from conftest import BOOT, services_up, write_state


def _marker(ns, name="session.json"):
    return json.loads((ns.paths.POWERLOSS / name).read_text())


# --------------------------------------------------------------- the silence --
def test_a_fresh_install_has_no_history_to_judge(pl):
    assert pl.powerloss.last_stop() is None
    assert pl.powerloss.verdict_lines() == []


def test_junk_markers_read_as_no_record(pl):
    """settings.json is shared with the SwiftUI app and these two files are plain
    JSON a human may have half-edited: garbage is "no record", never a crash and
    never a verdict."""
    pl.paths.POWERLOSS.mkdir(parents=True)
    (pl.paths.POWERLOSS / "session.json").write_text("{not json")
    (pl.paths.POWERLOSS / "stop.json").write_text("[1, 2, 3]")
    assert pl.powerloss.last_stop() is None


def test_reading_the_verdict_writes_nothing(pl, monkeypatch):
    """`status`/`doctor` judge history, they do not rewrite it: an OPEN marker
    must still be open after a hundred reads, or the next `up` would find a
    clean slate and the crash would vanish."""
    write_state(pl)
    pl.powerloss.note_session_start()
    before = sorted(p.name for p in pl.paths.POWERLOSS.iterdir())
    monkeypatch.setattr(pl.powerloss, "_boot_time", lambda: "999")
    for _ in range(3):
        pl.powerloss.last_stop()
        pl.powerloss.verdict_lines()
        pl.powerloss.doctor(query=False)
    assert sorted(p.name for p in pl.paths.POWERLOSS.iterdir()) == before
    assert (pl.paths.POWERLOSS / "session.json").exists()
    assert not (pl.paths.POWERLOSS / "stop.json").exists()


# ------------------------------------------------------------- opening a run --
def test_start_records_the_session_and_its_pids(pl):
    pids = write_state(pl)
    assert pl.powerloss.note_session_start() is True
    record = _marker(pl)
    assert record["boot_time"] == BOOT
    assert record["stack"]["klipper"] == pids["klipper"]
    assert record["started"]
    assert record["started_epoch"]


def test_start_sweeps_the_session_the_last_crash_left_open(pl):
    """The moment an interrupted session becomes history: sweep FIRST, so the
    verdict of the dead session is on disk before the new one opens."""
    write_state(pl)
    pl.powerloss.note_session_start()
    first = _marker(pl)["started_epoch"]
    write_state(pl, pids={"klipper": 20001, "moonraker": 20002})
    pl.powerloss.note_session_start()
    verdict = _marker(pl, "stop.json")
    assert verdict["clean"] is False
    assert verdict["reason"] == "no clean-stop marker"
    assert verdict["started_epoch"] == first


def test_no_state_json_still_opens_a_marker(pl):
    """start() writes state.json before calling us; if that write failed the
    marker still has to be made, or `down` has nothing to close."""
    assert pl.powerloss.note_session_start() is True
    assert _marker(pl)["stack"] == {}


# ------------------------------------------------------- the crash verdicts --
def test_an_open_marker_with_nothing_running_is_unclean(pl):
    write_state(pl)
    pl.powerloss.note_session_start()
    verdict = pl.powerloss.last_stop(stack_up=False)
    assert verdict["state"] == "unclean"
    assert verdict["clean"] is False
    assert "LAST STOP WAS UNCLEAN" in "\n".join(
        pl.powerloss.verdict_lines(stack_up=False))


def test_the_same_marker_while_the_stack_is_up_is_not_a_verdict(pl):
    """The discriminator is liveness the caller already knows (process.running()),
    not the file: `status` on a printing stack must not cry power loss."""
    write_state(pl)
    pl.powerloss.note_session_start()
    verdict = pl.powerloss.last_stop(stack_up=True)
    assert verdict["state"] == "running"
    assert pl.powerloss.verdict_lines(stack_up=True) == []


def test_a_reboot_makes_the_reason_a_reboot_not_a_missing_marker(pl, monkeypatch):
    """kern.boottime moving is the difference between "somebody killed klippy" and
    "this Mac lost power while printing" - and after a reboot the stack record is
    already gone (process._load() deletes it), so the marker has to carry it."""
    write_state(pl)
    pl.powerloss.note_session_start()
    monkeypatch.setattr(pl.powerloss, "_boot_time", lambda: "1791999999")
    verdict = pl.powerloss.last_stop(stack_up=False)
    assert verdict["state"] == "unclean"
    assert verdict["rebooted"] is True
    assert verdict["reason"] == "reboot with the stack running"
    assert "rebooted" in verdict["detail"]


def test_unclean_survives_state_json_being_deleted(pl):
    """Regression for the decisive gotcha (facts B4): the journal and the markers
    must NOT hang inside run/state.json, because our own _load() deletes that file
    before any recovery feature can read it."""
    write_state(pl)
    pl.powerloss.note_session_start()
    pl.paths.STATE.unlink()
    assert not pl.paths.STATE.exists()
    assert pl.powerloss.last_stop(stack_up=False)["state"] == "unclean"
    pl.powerloss.sweep()
    assert pl.powerloss.last_stop(stack_up=False)["state"] == "unclean"


# ---------------------------------------------------------- graceful closure --
def test_graceful_down_on_a_live_stack_is_clean(pl):
    write_state(pl)
    pl.powerloss.note_session_start()
    assert pl.powerloss.note_clean_stop(state=services_up()) is True
    verdict = pl.powerloss.last_stop(stack_up=False)
    assert verdict["state"] == "clean"
    assert verdict["reason"] == "down"
    assert not (pl.paths.POWERLOSS / "session.json").exists()
    assert "last stop was graceful" in verdict["detail"]


def test_down_on_a_half_dead_stack_is_not_a_clean_stop(pl):
    """A klipper that died an hour ago must not be remembered as a graceful even-
    ing. The caller hands in the liveness it read BEFORE signalling anything."""
    write_state(pl)
    pl.powerloss.note_session_start()
    pl.powerloss.note_clean_stop(state=services_up(dead=("klipper",)))
    verdict = pl.powerloss.last_stop(stack_up=False)
    assert verdict["state"] == "unclean"
    assert "klipper DOWN" in verdict["reason"]


def test_killall_is_an_unclean_stop_with_the_users_own_command_as_reason(pl):
    write_state(pl)
    pl.powerloss.note_session_start()
    pl.powerloss.note_forced_stop()
    verdict = pl.powerloss.last_stop(stack_up=False)
    assert verdict["state"] == "unclean"
    assert verdict["forced"] is True
    assert "SIGKILLed by `killall`" in verdict["detail"]


def test_a_down_without_a_session_marker_is_still_a_verdict(pl):
    """`down` after a hand-deleted marker: the graceful path ran, say so, and do
    not invent a session that was never recorded."""
    assert pl.powerloss.note_clean_stop(state=services_up()) is True
    verdict = pl.powerloss.last_stop()
    assert verdict["state"] == "clean"
    assert verdict["started"] is None


# ---------------------------------------------------------------- staleness --
def test_a_marker_older_than_the_journal_is_stale(pl):
    """The marker says "graceful down closed it", the journal says snapshots kept
    landing afterwards: the marker cannot describe the session that died."""
    write_state(pl)
    pl.powerloss.note_session_start()
    pl.powerloss.note_clean_stop(state=services_up())
    pl.powerloss.append({"at": "2026-10-16T10:00:00", "epoch": 9e9,
                         "filename": "cube.gcode", "state": "printing",
                         "file_position": 4096, "file_size": 300000})
    verdict = pl.powerloss.last_stop(stack_up=False)
    assert verdict["state"] == "unclean"
    assert verdict["stale"] is True
    assert "stale" in verdict["detail"]


def test_a_record_older_than_the_marker_is_not_stale(pl):
    write_state(pl)
    pl.powerloss.note_session_start()
    pl.powerloss.append({"at": "2026-10-16T09:00:00", "epoch": 1.0,
                         "filename": "cube.gcode", "state": "printing",
                         "file_position": 4096, "file_size": 300000})
    pl.powerloss.note_clean_stop(state=services_up())
    assert pl.powerloss.last_stop(stack_up=False)["state"] == "clean"


def test_the_verdict_names_the_last_journaled_seam(pl):
    write_state(pl)
    pl.powerloss.note_session_start()
    pl.powerloss.append({"at": "2026-10-16T09:50:00", "epoch": 5.0,
                         "filename": "cube.gcode", "state": "printing",
                         "file_position": 8192, "file_size": 300000,
                         "progress": 0.027})
    pl.powerloss.note_forced_stop()
    lines = pl.powerloss.verdict_lines(stack_up=False)
    assert any("cube.gcode at byte 8192" in line for line in lines)
    assert any("M23/M26/M24" in line for line in lines)


def test_a_journal_from_an_earlier_session_is_not_this_sessions_seam(pl):
    """`sweep()` reads only records at or after the dead session started: a
    filename from last week's crash must not be offered as this seam."""
    pl.powerloss.append({"at": "2026-10-01T09:00:00", "epoch": 100.0,
                         "filename": "old.gcode", "state": "printing",
                         "file_position": 10, "file_size": 20})
    write_state(pl)
    pl.powerloss.note_session_start()
    pl.powerloss.sweep()
    assert pl.powerloss.last_stop(stack_up=False)["journal"] is None


# ------------------------------------------------------------- unwriteable --
def test_markers_that_cannot_be_written_are_reported_not_raised(pl, monkeypatch):
    """data/powerloss/ sitting under a regular file: the marker write fails, and
    the answer is False + still a verdict, never a traceback out of `up`."""
    pl.paths.DATA.mkdir(parents=True, exist_ok=True)
    (pl.paths.POWERLOSS).write_text("in the way")
    monkeypatch.setattr(pl.powerloss, "_boot_time", lambda: None)
    assert pl.powerloss.note_session_start() is False
    assert pl.powerloss.note_clean_stop() is False
    assert pl.powerloss.note_forced_stop() is False
    assert pl.powerloss.sweep() is None
    assert pl.powerloss.last_stop() is None


def test_a_boot_time_that_cannot_be_read_is_never_read_as_a_reboot(pl, monkeypatch):
    """facts B3/B4: sysctl can fail. An unreadable boot time is "cannot tell", so
    the reason stays "no clean-stop marker" and not a fabricated power cut."""
    write_state(pl)
    monkeypatch.setattr(pl.powerloss, "_boot_time", lambda: None)
    pl.powerloss.note_session_start()
    verdict = pl.powerloss.last_stop(stack_up=False)
    assert verdict["rebooted"] is False
    assert verdict["reason"] == "no clean-stop marker"


@pytest.mark.parametrize("boot,expect", [(BOOT, False), ("1791000000", True)])
def test_sweep_persists_the_reboot_evidence(pl, monkeypatch, boot, expect):
    write_state(pl)
    pl.powerloss.note_session_start()
    monkeypatch.setattr(pl.powerloss, "_boot_time", lambda: boot)
    verdict = pl.powerloss.sweep()
    assert verdict["rebooted"] is expect
    assert json.loads(
        (pl.paths.POWERLOSS / "stop.json").read_text())["rebooted"] is expect
