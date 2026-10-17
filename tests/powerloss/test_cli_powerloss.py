"""The user-visible surface: two commands, one gate, and no new lie in `status`.

What is pinned here is the part a user depends on and a unit test cannot see:
`status` on a machine with no history is still exactly one word, `up` says LAST
STOP WAS UNCLEAN before the first print of the new session, `powerloss on` writes
only its own settings block, and `powerloss-test` refuses to touch a board unless
KLIPPERFORMAC_ALLOW_LIVE=1 is set - in that order, before anything is constructed.
"""
import argparse
import json

import pytest

from klipperformac import launchd

NS = argparse.Namespace


@pytest.fixture
def cli(pl):
    """The stack's CLI bound to the throwaway install, with powerloss reloads
    wired through it: cli.powerloss IS the module the fixture patched."""
    return pl


def args(**kwargs):
    base = {"action": "status", "interval": None, "sweeps": None, "lines": 10,
            "ticks": 3}
    base.update(kwargs)
    return NS(**base)


# ------------------------------------------------------------------ routing --
def test_both_commands_are_routed(pl, monkeypatch):
    seen = []
    monkeypatch.setattr(pl.cli, "cmd_powerloss",
                        lambda a: seen.append(("powerloss", a.action)))
    monkeypatch.setattr(pl.cli, "cmd_powerloss_test", lambda a: seen.append(
        ("powerloss-test", a.ticks)))
    pl.cli.main(["powerloss", "tail", "-n", "3"])
    pl.cli.main(["powerloss-test", "--ticks", "2"])
    assert seen == [("powerloss", "tail"), ("powerloss-test", 2)]


def test_powerloss_action_defaults_to_status(pl, monkeypatch):
    captured = {}
    monkeypatch.setattr(pl.cli, "cmd_powerloss",
                        lambda a: captured.update(action=a.action))
    pl.cli.main(["powerloss"])
    assert captured == {"action": "status"}


def test_powerloss_rejects_an_unknown_action(pl):
    with pytest.raises(SystemExit) as caught:
        pl.cli.main(["powerloss", "resume"])       # there is no resume, on purpose
    assert caught.value.code == 2


# ---------------------------------------------------------------- the gate ----
def test_powerloss_test_refuses_without_the_live_env(pl, monkeypatch, capfd):
    """The gate is the first thing the command does, before a harness exists:
    this command SIGKILLs a printing klipper and no default may do that."""
    def _never(*a, **k):
        raise AssertionError("BUG: LiveHarness built before the live gate passed")

    monkeypatch.setattr(pl.powerloss, "LiveHarness", _never)
    monkeypatch.delenv("KLIPPERFORMAC_ALLOW_LIVE", raising=False)
    with pytest.raises(SystemExit) as caught:
        pl.cli.cmd_powerloss_test(args())
    assert caught.value.code == 1
    out = capfd.readouterr().err
    assert "KLIPPERFORMAC_ALLOW_LIVE=1" in out
    assert "SIGKILL" in out


def test_powerloss_test_still_refuses_when_the_stack_is_down(pl, monkeypatch,
                                                            capfd):
    monkeypatch.setenv("KLIPPERFORMAC_ALLOW_LIVE", "1")
    monkeypatch.setattr(pl.process, "running", lambda: {
        "klipper": False, "moonraker": True, "web_mainsail": True,
        "web_fluidd": True, "caffeinate": True})
    monkeypatch.setattr(pl.powerloss, "LiveHarness",
                        lambda *a, **k: pytest.fail("harness built with no stack"))
    with pytest.raises(SystemExit):
        pl.cli.cmd_powerloss_test(args())
    assert "not up" in capfd.readouterr().err


def test_powerloss_test_refuses_to_cut_off_a_live_print(pl, monkeypatch, capfd):
    """"the board is mine to kill" is not "this 9-hour print is mine to ruin".
    The stack is asked what it is doing, before anything is constructed."""
    monkeypatch.setenv("KLIPPERFORMAC_ALLOW_LIVE", "1")
    monkeypatch.setattr(pl.process, "running", lambda: {
        name: True for name in pl.process.SERVICES})
    monkeypatch.setattr(pl.powerloss, "query_stats", lambda timeout=3: {
        "state": "printing", "filename": "heirloom-vase.gcode",
        "file_position": 120000, "file_size": 900000})
    monkeypatch.setattr(pl.powerloss, "LiveHarness",
                        lambda *a, **k: pytest.fail("harness built over a live print"))
    with pytest.raises(SystemExit) as caught:
        pl.cli.cmd_powerloss_test(args())
    assert caught.value.code == 1
    err = capfd.readouterr().err
    assert "heirloom-vase.gcode" in err and "SIGKILL" in err


def test_powerloss_test_runs_the_sequence_when_authorized(pl, monkeypatch, capfd):
    monkeypatch.setenv("KLIPPERFORMAC_ALLOW_LIVE", "1")
    monkeypatch.setattr(pl.process, "running", lambda: {
        name: True for name in pl.process.SERVICES})
    built = []

    class Harness:
        pass

    monkeypatch.setattr(pl.powerloss, "LiveHarness",
                        lambda *a: built.append(a) or Harness())
    monkeypatch.setattr(pl.powerloss, "run_kill_sequence",
                        lambda harness, ticks=3: (True, [("x", True, "")]))
    pl.cli.cmd_powerloss_test(args())
    assert len(built) == 1
    assert "PASS" in capfd.readouterr().out


def test_powerloss_test_exits_non_zero_when_a_check_fails(pl, monkeypatch, capfd):
    monkeypatch.setenv("KLIPPERFORMAC_ALLOW_LIVE", "1")
    monkeypatch.setattr(pl.process, "running", lambda: {
        name: True for name in pl.process.SERVICES})
    monkeypatch.setattr(pl.powerloss, "LiveHarness", lambda *a: object())
    monkeypatch.setattr(pl.powerloss, "run_kill_sequence",
                        lambda harness, ticks=3: (False, [("x", False, "")]))
    with pytest.raises(SystemExit) as caught:
        pl.cli.cmd_powerloss_test(args())
    assert caught.value.code == 1
    assert "A check failed" in capfd.readouterr().out


# ------------------------------------------------------------- the subcommands --
def test_on_and_off_write_only_their_own_block(pl, capfd):
    pl.paths.SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    pl.paths.SETTINGS.write_text(json.dumps({"lan": False,
                                             "cam": {"device": "FaceTime"}}))
    pl.cli.cmd_powerloss(args(action="on", interval=2.0))
    saved = json.loads(pl.paths.SETTINGS.read_text())
    assert saved["lan"] is False and saved["cam"] == {"device": "FaceTime"}
    assert saved["powerloss"]["journal"] is True
    assert saved["powerloss"]["intervalSec"] == 2.0
    out = capfd.readouterr().out
    assert "ON" in out and "resumed automatically" in out.lower()
    pl.cli.cmd_powerloss(args(action="off"))
    assert json.loads(pl.paths.SETTINGS.read_text())["powerloss"]["journal"] is False


def test_snapshot_says_so_when_nothing_prints(pl, monkeypatch, capfd):
    monkeypatch.setattr(pl.powerloss, "_http_get", lambda path, timeout=3: None)
    pl.cli.cmd_powerloss(args(action="snapshot"))
    assert "Nothing to journal" in capfd.readouterr().out


def test_tail_reports_an_empty_journal(pl, capfd):
    pl.cli.cmd_powerloss(args(action="tail", lines=5))
    assert "Journal is empty" in capfd.readouterr().out


def test_watch_refuses_when_the_journal_is_off(pl, capfd):
    with pytest.raises(SystemExit) as caught:
        pl.cli.cmd_powerloss(args(action="watch"))
    assert caught.value.code == 1
    assert "powerloss on" in capfd.readouterr().err


def test_status_action_prints_the_whole_truth(pl, monkeypatch, capfd):
    monkeypatch.setattr(pl.powerloss, "_http_get", lambda path, timeout=3: None)
    pl.cli.cmd_powerloss(args(action="status"))
    out = capfd.readouterr().out
    for needle in ("journal is off", "recover_at_seam", "M26", "interrupted"):
        assert needle in out, needle


# ------------------------------------------------------------------- status ----
@pytest.fixture
def quiet(pl, monkeypatch):
    """`status` with no network, no processes, no camera, no shaper probe and no
    sysctl - the shape tests/deep pins for the one-word answer."""
    monkeypatch.setattr(pl.process, "running", lambda: None)
    monkeypatch.setattr(pl.shaper, "readiness",
                        lambda: {"needed": False, "healthy": True,
                                 "python_found": None, "venv_exists": False,
                                 "detail": "not needed"})
    monkeypatch.setattr(pl.cam, "running", lambda: None)
    monkeypatch.setattr(pl.cam, "helper_path", lambda: None)
    monkeypatch.setattr(pl.versions, "check_updates", lambda: ([], []))
    monkeypatch.setattr(pl.cli, "_moonraker_get", lambda path, timeout=3: None)
    return pl


def test_status_on_a_bare_install_is_still_one_word(quiet, capfd):
    """AGENTS.md's own regression: this feature must not put a line on a machine
    that has no history - `powerloss.verdict_lines()` returns [] with no record."""
    quiet.cli.cmd_status(NS())
    assert capfd.readouterr().out.strip() == "stopped"


def test_status_reads_no_network_and_writes_no_markers(quiet, capfd):
    before = sorted(str(p) for p in quiet.paths.DATA.rglob("*"))
    quiet.cli.cmd_status(NS())
    capfd.readouterr()
    assert sorted(str(p) for p in quiet.paths.DATA.rglob("*")) == before
    assert not quiet.paths.POWERLOSS.exists()


def test_status_of_a_stopped_stack_names_the_unclean_stop(quiet, monkeypatch,
                                                          capfd):
    write = json.dumps({"started": "2026-10-16T09:00:00", "started_epoch": 10.0,
                        "boot_time": "1791000000", "stack": {"klipper": 4242}})
    quiet.paths.POWERLOSS.mkdir(parents=True)
    (quiet.paths.POWERLOSS / "session.json").write_text(write)
    monkeypatch.setattr(quiet.powerloss, "_boot_time", lambda: "1791932927")
    quiet.cli.cmd_status(NS())
    out = capfd.readouterr().out
    assert "LAST STOP WAS UNCLEAN" in out
    assert "rebooted" in out


def test_status_hides_stale_unclean_history_when_untracked_ports_are_live(
        quiet, monkeypatch, capfd):
    write = json.dumps({"started": "2026-10-16T09:00:00", "started_epoch": 10.0,
                        "boot_time": "1791000000", "stack": {"klipper": 4242}})
    quiet.paths.POWERLOSS.mkdir(parents=True)
    (quiet.paths.POWERLOSS / "session.json").write_text(write)
    monkeypatch.setattr(quiet.process, "our_listeners",
                        lambda: [(quiet.paths.MOONRAKER_PORT, 42),
                                 (quiet.paths.WEB_PORT, 43)])
    quiet.cli.cmd_status(NS())
    out = capfd.readouterr().out
    assert out.startswith("running (untracked)\n")
    assert "LAST STOP WAS UNCLEAN" not in out


def test_status_of_a_running_stack_shows_the_journal_switch(quiet, monkeypatch,
                                                            capfd):
    monkeypatch.setattr(quiet.process, "running", lambda: {
        name: True for name in quiet.process.SERVICES})
    quiet.cli.cmd_status(NS())
    out = capfd.readouterr().out
    assert "powerloss    journal off" in out
    quiet.powerloss.save_settings(journal=True)
    quiet.cli.cmd_status(NS())
    assert "powerloss    journal on" in capfd.readouterr().out


# ---------------------------------------------------------------------- up ----
@pytest.fixture
def up_ready(pl, monkeypatch):
    """`up` with the stack faked at the process boundary and no readiness poll."""
    started = []
    monkeypatch.setattr(pl.paths, "ensure_dirs", lambda: None)
    pl.paths.VENV_PY.parent.mkdir(parents=True, exist_ok=True)
    pl.paths.VENV_PY.write_text("")
    monkeypatch.setattr(pl.configgen, "sync_data_paths", lambda: False)
    monkeypatch.setattr(pl.process, "running", lambda: None)

    def _start():
        # the real start() opens the session marker (and sweeps the dead one);
        # faking it away would make the assertion below prove nothing
        pl.powerloss.note_session_start()
        started.append("start")
        return True

    monkeypatch.setattr(pl.process, "start", _start)
    monkeypatch.setattr(launchd, "want_set", lambda reason: None)
    monkeypatch.setattr(pl.shaper, "interpreter_for_klipper",
                        lambda: (pl.paths.VENV_PY, None))
    monkeypatch.setattr(pl.cli, "_wait_ready", lambda timeout=35: True)
    monkeypatch.setattr(pl.cli, "_power_warning", lambda: None)
    monkeypatch.setattr(pl.cli, "_lan_urls", lambda indent="  ": [])
    return pl, started


def test_up_names_the_unclean_stop_before_the_user_prints_again(up_ready, capfd):
    """The up-time truth, captured before start() rolls it into history: a Mac
    that lost power has to say so on the way back up, not after a second print."""
    pl, started = up_ready
    write = json.dumps({"started": "2026-10-16T09:00:00", "started_epoch": 10.0,
                        "boot_time": "1791000000", "stack": {"klipper": 4242}})
    pl.paths.POWERLOSS.mkdir(parents=True)
    (pl.paths.POWERLOSS / "session.json").write_text(write)
    pl.cli._cmd_up(NS(open=False))
    out = capfd.readouterr().out
    assert started == ["start"]
    assert "LAST STOP WAS UNCLEAN" in out
    assert "rebooted" in out
    # and the crash is now history: the marker pair is intact for `status` too
    assert (pl.paths.POWERLOSS / "stop.json").exists()


def test_an_ordinary_up_says_nothing_about_power_loss(up_ready, capfd):
    pl, _started = up_ready
    pl.cli._cmd_up(NS(open=False))
    out = capfd.readouterr().out
    assert "UNCLEAN" not in out and "powerloss" not in out


# ------------------------------------------------------------------- doctor ----
@pytest.fixture
def bare_doctor(pl, monkeypatch):
    """`doctor` on a machine with nothing installed but a throwaway data folder:
    every other block is silenced, so the lines this test reads are powerloss's."""
    from klipperformac import backup
    monkeypatch.setattr(pl.installer, "check_prereqs",
                        lambda strict=False: [])
    monkeypatch.setattr(pl.process, "running", lambda: None)
    monkeypatch.setattr(pl.shaper, "readiness",
                        lambda: {"needed": False, "healthy": True,
                                 "python_found": None, "venv_exists": False,
                                 "detail": "not needed"})
    monkeypatch.setattr(pl.cam, "doctor", lambda: [])
    monkeypatch.setattr(pl.batteryguard, "doctor", lambda: [])
    monkeypatch.setattr(pl.batteryguard, "battery_settings",
                        lambda: {"enabled": False, "pauseAt": 15, "criticalAt": 7})
    monkeypatch.setattr(backup, "doctor_lines", lambda: [])

    def _no_timing():
        # cmd_doctor wraps this block: a machine that cannot be measured must
        # report it and carry on to the power-loss lines.
        raise RuntimeError("no timing on this machine")

    monkeypatch.setattr(pl.cli, "_timing_report", _no_timing)
    monkeypatch.setattr(pl.configgen, "webcam_state", lambda: (False, {}))
    return pl


def test_doctor_reports_the_last_stop_without_flipping_its_verdict(
        bare_doctor, monkeypatch, capfd):
    """The gate AGENTS.md 4 puts on "done" is doctor's exit code, so a badly-ended
    LAST SESSION must be loud ([!]) and must not change it: history is not a
    broken install, and `make release-check` would then fail on a Mac that is
    perfectly configured. Measured against the same machine with no history, so
    the comparison is about the unclean stop and nothing else."""
    with pytest.raises(SystemExit) as baseline:
        bare_doctor.cli.cmd_doctor(NS())
    capfd.readouterr()
    bare_doctor.paths.POWERLOSS.mkdir(parents=True)
    (bare_doctor.paths.POWERLOSS / "session.json").write_text(json.dumps(
        {"started": "2026-10-16T09:00:00", "started_epoch": 10.0,
         "boot_time": "1791000000", "stack": {"klipper": 4242}}))
    with pytest.raises(SystemExit) as after:
        bare_doctor.cli.cmd_doctor(NS())
    out = capfd.readouterr().out
    assert after.value.code == baseline.value.code
    assert "[!] LAST STOP WAS UNCLEAN" in out
    assert "[ok] powerloss journal: off" in out
    assert "interrupted" in out            # what upstream actually offers


def test_doctor_is_quiet_when_there_is_no_history(bare_doctor, capfd):
    with pytest.raises(SystemExit):
        bare_doctor.cli.cmd_doctor(NS())
    out = capfd.readouterr().out
    assert "UNCLEAN" not in out
    assert "no session recorded yet" in out
    assert not bare_doctor.paths.POWERLOSS.exists()   # reading is not writing


def test_doctor_hides_stale_unclean_history_when_moonraker_is_live_untracked(
        bare_doctor, monkeypatch, capfd):
    bare_doctor.paths.POWERLOSS.mkdir(parents=True)
    (bare_doctor.paths.POWERLOSS / "session.json").write_text(json.dumps(
        {"started": "2026-10-16T09:00:00", "started_epoch": 10.0,
         "boot_time": "1791000000", "stack": {"klipper": 4242}}))
    monkeypatch.setattr(bare_doctor.process, "our_listeners",
                        lambda: [(bare_doctor.paths.MOONRAKER_PORT, 42)])
    monkeypatch.setattr(bare_doctor.cli, "_klippy_state",
                        lambda timeout=3: "ready")
    monkeypatch.setattr(bare_doctor.cli, "_mcu_runtime_rows", lambda *a, **k: [])
    monkeypatch.setattr(bare_doctor.cli, "_mcu_report_lines", lambda *a, **k: [])
    monkeypatch.setattr(bare_doctor.cli, "_mcu_firmware_match_lines",
                        lambda *a, **k: [])
    monkeypatch.setattr(bare_doctor.cli, "_mcu_log_provenance_lines",
                        lambda *a, **k: [])
    with pytest.raises(SystemExit):
        bare_doctor.cli.cmd_doctor(NS())
    out = capfd.readouterr().out
    assert "stack is not tracked, but Moonraker is answering on port" in out
    assert "LAST STOP WAS UNCLEAN" not in out


def test_powerloss_status_hides_stale_unclean_history_when_stack_is_live(
        pl, monkeypatch, capfd):
    pl.paths.POWERLOSS.mkdir(parents=True)
    (pl.paths.POWERLOSS / "session.json").write_text(json.dumps(
        {"started": "2026-10-16T09:00:00", "started_epoch": 10.0,
         "boot_time": "1791000000", "stack": {"klipper": 4242}}))
    monkeypatch.setattr(pl.process, "running", lambda: None)
    monkeypatch.setattr(pl.process, "our_listeners",
                        lambda: [(pl.paths.MOONRAKER_PORT, 42)])
    monkeypatch.setattr(pl.powerloss, "_http_get", lambda path, timeout=3: None)
    pl.cli.cmd_powerloss(args(action="status"))
    out = capfd.readouterr().out
    assert "LAST STOP WAS UNCLEAN" not in out
    assert "journal is off" in out
