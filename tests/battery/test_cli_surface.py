"""The CLI surface and the exact REST sequence.

The contract a user sees is `klipperformac batteryguard on|off|status|set|check`,
and the contract the printer sees is a Moonraker call sequence: one pause per
print, not one per tick; cancel + graceful teardown at critical; a resume only
when the user opted in and the guard itself did the pausing. Both are asserted
here on recorded calls - the network layer, the notify layer and the stack
teardown are all fakes, so a test can say "zero POSTs" and mean it.
"""
import argparse
import json

import pytest

NS = argparse.Namespace


def test_rest_sequence_is_exactly_pause_once(guard, monkeypatch):
    """15 % twice is one PAUSE. The ticks after it post nothing at all - this
    is the bug every watchdog of this kind has: pausing a print 60 times a
    minute until the user unplugs."""
    bg = guard.bg
    monkeypatch.setattr(bg, "read_power", lambda env=None: None)
    readings = [{"percent": 20, "source": "battery"},
                {"percent": 12, "source": "battery"},
                {"percent": 12, "source": "battery"},
                {"percent": 11, "source": "battery"},
                {"percent": 11, "source": "battery"}]
    queue = iter(readings)
    monkeypatch.setattr(bg, "read_power", lambda env=None: next(queue))
    monkeypatch.setattr(bg, "print_state", lambda timeout=3: "printing")
    for _ in readings:
        bg.poll_once(cfg=bg.normalise({}))
    assert guard.rec["post"] == ["/printer/print/pause"]
    assert len(guard.rec["spawn"]) == 2   # osascript + say, once
    assert bg.episode_path().exists()
    episode = json.loads(bg.episode_path().read_text())
    assert episode["armed"] is False
    assert episode["guardPaused"] is True
    assert episode["lastAction"] == "pause"


def test_rest_sequence_at_critical_cancels_then_stops(guard, monkeypatch):
    bg, rec = guard.bg, guard.rec
    readings = [{"percent": 8, "source": "battery"},
                {"percent": 6, "source": "battery"},
                {"percent": 5, "source": "battery"}]
    queue = iter(readings)
    monkeypatch.setattr(bg, "read_power", lambda env=None: next(queue))
    monkeypatch.setattr(bg, "print_state", lambda timeout=3: "printing")
    for _ in readings:
        bg.poll_once(cfg=bg.normalise({}))
    assert rec["post"] == ["/printer/print/cancel"]
    assert rec["stop"] == 1                       # process.stop(), once
    assert len(rec["spawn"]) == 2                 # one critical notification


def test_rest_sequence_resume_needs_the_opt_in(guard, monkeypatch):
    bg, rec = guard.bg, guard.rec
    cfg = bg.normalise({})
    for reading in ({"percent": 12, "source": "battery"},
                    {"percent": 12, "source": "battery"},
                    {"percent": 80, "source": "ac"},
                    {"percent": 80, "source": "ac"}):
        monkeypatch.setattr(bg, "read_power",
                            lambda env=None, r=reading: dict(r))
        monkeypatch.setattr(bg, "print_state",
                            lambda timeout=3, s=reading: "printing"
                            if reading["source"] == "battery" else "paused")
        bg.poll_once(cfg=cfg)
    assert rec["post"] == ["/printer/print/pause"]     # no resume: opt-in off

    # A fresh episode, the cable back, and the opt-in switched on: exactly one
    # pause and the resume that belongs to it - and nothing else.
    bg.episode_path().unlink()
    rec["post"].clear()
    rec["spawn"].clear()
    cfg = bg.normalise({"resumeOnAc": True})
    for reading in ({"percent": 12, "source": "battery"},
                    {"percent": 12, "source": "battery"},
                    {"percent": 80, "source": "ac"},
                    {"percent": 80, "source": "ac"}):
        monkeypatch.setattr(bg, "read_power",
                            lambda env=None, r=reading: dict(r))
        monkeypatch.setattr(bg, "print_state",
                            lambda timeout=3, s=reading: "printing"
                            if reading["source"] == "battery" else "paused")
        bg.poll_once(cfg=cfg)
    assert rec["post"] == ["/printer/print/pause", "/printer/print/resume"]
    assert len(rec["spawn"]) == 2          # one banner for the pause only


def test_dry_run_touches_nothing(guard, monkeypatch):
    """--dry-run: the decisions are made and said, and the printer, the desktop
    and the stack hear nothing at all."""
    bg, rec = guard.bg, guard.rec
    cfg = bg.normalise({"dryRun": True})
    for percent in (12, 12, 6, 6):
        monkeypatch.setattr(bg, "read_power",
                            lambda env=None, p=percent: {"percent": p,
                                                          "source": "battery",
                                                          "charging": False,
                                                          "remaining": None,
                                                          "engine": "env",
                                                          "raw": ""})
        actions, _ep = bg.poll_once(cfg=cfg)
        assert isinstance(actions, list)
    assert rec["post"] == []
    assert rec["spawn"] == []
    assert rec["stop"] == 0
    log = bg.log_path().read_text()
    assert "would pause" in log and "would cancel" in log
    assert "would stop the stack" in log
    assert "would notify" in log


def test_pause_resume_capability_probe_is_honest(guard, monkeypatch):
    """Four of the shipped presets have no [pause_resume]. Then the guard can
    only make noise, and it must say why instead of failing silently every
    time the battery drops."""
    bg, rec = guard.bg, guard.rec

    def no_capability(path, timeout=3):
        rec["get"].append(path)
        if "objects/list" in path:
            return {"result": {"objects": ["print_stats", "gcode_move"]}}
        return {"result": {"status": {"print_stats": {"state": "printing"}}}}

    monkeypatch.setattr(bg, "_moonraker_get", no_capability)
    monkeypatch.setattr(bg, "read_power",
                        lambda env=None: {"percent": 12, "source": "battery",
                                          "charging": False, "remaining": None,
                                          "engine": "env", "raw": ""})
    for _ in range(2):
        bg.poll_once(cfg=bg.normalise({}))
    assert rec["post"] == []                      # never a refused POST
    assert len(rec["spawn"]) == 2                 # the user is told instead
    episode = json.loads(bg.episode_path().read_text())
    assert "[pause_resume]" in episode["reason"]


def test_an_unreachable_moonraker_is_logged_not_thrown(guard, monkeypatch):
    """A watchdog that dies because the printer did not answer is worse than no
    watchdog: a failed POST must never propagate out of poll_once()."""
    bg = guard.bg

    def refuse(path, timeout=10):
        return False, "HTTP 503: Moonraker not reachable"

    monkeypatch.setattr(bg, "_moonraker_post", refuse)
    monkeypatch.setattr(bg, "read_power",
                        lambda env=None: {"percent": 12, "source": "battery",
                                          "charging": False, "remaining": None,
                                          "engine": "env", "raw": ""})
    for _ in range(2):
        actions, _ep = bg.poll_once(cfg=bg.normalise({}))
    log = bg.log_path().read_text()
    assert "pause: HTTP 503" in log
    assert "503" in json.loads(bg.episode_path().read_text())["reason"]


# ---------------------------------------------------------------- cli surface --

def args(action, **kw):
    base = {"action": action, "pause_at": None, "critical_at": None,
            "poll": None, "at_pause": None, "at_critical": None,
            "resume_on_ac": None, "notify": None, "dry_run": False,
            "percent": None, "source": None}
    base.update(kw)
    return NS(**base)


def test_cli_check_dry_run_is_read_only(guard, capsys, monkeypatch):
    """`batteryguard check --percent 12 --source battery --dry-run`: names the
    pause, performs zero POSTs and zero osascript spawns, and writes no state."""
    bg = guard.bg
    monkeypatch.setattr(bg, "read_power",
                        lambda env=None: pytest.fail("must not read this Mac"))
    guard.cli.main(["batteryguard", "check", "--percent", "12",
                    "--source", "battery", "--dry-run"])
    out = capsys.readouterr().out
    assert "would pause the print" in out
    assert "printer.cfg has no [pause_resume]" not in out
    assert guard.rec["post"] == []
    assert guard.rec["spawn"] == []
    assert not bg.episode_path().exists()


def test_cli_check_is_a_one_shot_not_a_tick(guard, capsys):
    """`check --percent 12` says what the guard would DO at that level. The
    two-reading jitter rule governs the live sampling, not a deliberate
    one-shot - and nothing else is bypassed: the episode on disk still decides
    whether this print has already been handled."""
    guard.cli.main(["batteryguard", "check", "--percent", "12",
                    "--source", "battery", "--dry-run"])
    out = capsys.readouterr().out
    assert "would pause the print" in out
    guard.cli.main(["batteryguard", "check", "--percent", "5",
                    "--source", "battery", "--dry-run"])
    out = capsys.readouterr().out
    assert "would cancel the print" in out
    assert "would stop the stack" in out
    # on AC the same level is never an emergency
    guard.cli.main(["batteryguard", "check", "--percent", "5", "--source",
                    "ac", "--dry-run"])
    assert "would do nothing" in capsys.readouterr().out


def test_cli_check_needs_a_source_with_a_percent(guard, capsys):
    with pytest.raises(SystemExit) as caught:
        guard.cli.main(["batteryguard", "check", "--percent", "12"])
    assert caught.value.code == 1
    assert "--source" in capsys.readouterr().err


@pytest.mark.parametrize("percent", ["-4", "abc", "101", "1e9"])
def test_cli_check_refuses_an_unusable_percent(guard, capsys, percent):
    with pytest.raises(SystemExit) as caught:
        guard.cli.main(["batteryguard", "check", "--percent", percent,
                        "--source", "battery"])
    assert caught.value.code == 1
    assert "0-100" in capsys.readouterr().err


def test_cli_status_with_no_ledger_says_off(guard, capsys):
    guard.cli.main(["batteryguard", "status"])
    out = capsys.readouterr().out
    assert "Battery guard: off" in out
    assert "pause 15%" in out and "critical 7%" in out


def test_cli_status_shows_the_thresholds_it_saved(guard, capsys):
    guard.cli.main(["batteryguard", "set", "--pause-at", "25",
                    "--critical-at", "9"])
    out = capsys.readouterr().out
    assert "pause 25%" in out and "critical 9%" in out
    assert json.loads(guard.paths.SETTINGS.read_text())["battery"] == {
        "pauseAt": 25, "criticalAt": 9}


def test_cli_set_refuses_a_critical_above_the_pause(guard, capsys):
    with pytest.raises(SystemExit) as caught:
        guard.cli.main(["batteryguard", "set", "--pause-at", "10",
                        "--critical-at", "20"])
    assert caught.value.code == 1
    assert "above pauseAt" in capsys.readouterr().err
    # nothing was written
    assert "battery" not in json.loads(
        guard.paths.SETTINGS.read_text()) if guard.paths.SETTINGS.exists() \
        else True


def test_cli_on_refuses_a_mac_with_no_battery(guard, capsys, monkeypatch):
    bg = guard.bg
    monkeypatch.setattr(bg, "read_power", lambda env=None: None)
    with pytest.raises(SystemExit) as caught:
        guard.cli.main(["batteryguard", "on"])
    assert caught.value.code == 1
    assert "no battery" in capsys.readouterr().out
    # the intent was still saved, so `up` can honour it later
    assert bg.battery_settings()["enabled"] is True


def test_cli_off_when_it_was_never_on(guard, capsys):
    with pytest.raises(SystemExit) as caught:
        guard.cli.main(["batteryguard", "off"])
    assert caught.value.code == 1
    assert "not on" in capsys.readouterr().out
    assert guard.bg.battery_settings()["enabled"] is False


def test_down_stops_the_guard_before_the_stack(guard, monkeypatch, capsys):
    """The order is the contract: the guard pauses and cancels through
    Moonraker, so it must be off before the stack goes away, or its last act is
    a POST to a dead API."""
    bg, p, cli = guard.bg, guard.process, guard.cli
    order = []
    monkeypatch.setattr(bg, "stop",
                        lambda timeout=3.0: (True, order.append("guard")
                                             or ["guard stopped"]))
    monkeypatch.setattr(p, "stop",
                        lambda timeout=6.0: order.append("stack") or True)
    monkeypatch.setattr(p, "our_listeners", lambda: order.append("check") or [])
    monkeypatch.setattr(cli.cam, "stop",
                        lambda: (False, ["Camera was not on."]))
    cli.main(["down"])
    assert order[:2] == ["guard", "stack"]
    assert "Battery guard stopped with the stack" in capsys.readouterr().out


def test_killall_includes_the_guard(guard, monkeypatch, capsys):
    """`killall` is the command that must leave nothing of ours running, and it
    names the guard in its report like every other process it stopped."""
    bg, p, cli = guard.bg, guard.process, guard.cli
    monkeypatch.setattr(p, "kill_all", lambda: [("klipper", 11)])
    monkeypatch.setattr(cli.cam, "kill_orphans", lambda: [])
    monkeypatch.setattr(bg, "kill_orphans", lambda: [("batteryguard", 22)])
    cli.main(["killall"])
    out = capsys.readouterr().out
    assert "batteryguard" in out and "pid 22" in out
    assert "klipper" in out


def test_status_names_the_guard(guard, monkeypatch, capsys):
    """`status` reports the watchdog the way it reports the camera: a row with
    its state, never a service that reads as DOWN - and, like the camera, only
    where there is something to say. An armed or live guard is named even with
    the stack down; a guard nobody ever turned on keeps the stopped branch's
    older, pinned contract: `status` is then exactly the word "stopped"
    (tests/deep/test_clmisc_data_ui.py::test_status_with_nothing_installed_
    says_stopped). The original assertion here demanded an "off" row inside
    that one-word output - two contracts for the same bytes, and the older
    one (parsed by scripts and the GUI) wins; "never a DOWN service" is kept.
    """
    cli, bg, p = guard.cli, guard.bg, guard.process
    monkeypatch.setattr(p, "running", lambda: None)
    monkeypatch.setattr(p, "our_listeners", lambda: [])
    monkeypatch.setattr(cli.versions, "check_updates", lambda: ([], []))
    monkeypatch.setattr(cli.cam, "helper_path", lambda: None)
    cli.main(["status"])
    out = capsys.readouterr().out
    assert out.strip() == "stopped"
    assert "batteryguard" not in out and "DOWN" not in out
    bg.save_battery_settings(enabled=True)
    cli.main(["status"])
    assert "armed" in capsys.readouterr().out


def test_up_autostarts_only_when_enabled(guard, monkeypatch, capsys):
    """`up` never ENABLES anything: it only brings back a guard the user turned
    on. Silent when off, one line when on."""
    cli, bg = guard.cli, guard.bg
    started = []
    monkeypatch.setattr(bg, "start",
                        lambda cfg=None: (started.append(1) or
                                          (True, ["Battery guard on"])))
    cli._guard_autostart()
    assert started == []
    bg.save_battery_settings(enabled=True)
    cli._guard_autostart()
    assert started == [1]
    out = capsys.readouterr().out
    assert "Battery guard: on (pause 15%, critical 7%)" in out


def test_doctor_reports_the_guard(guard, capsys, monkeypatch):
    bg = guard.bg
    monkeypatch.setattr(bg, "read_power",
                        lambda env=None: {"percent": 44, "source": "battery",
                                          "charging": False, "remaining": None,
                                          "engine": "pmset", "raw": ""})
    lines = bg.doctor()
    text = "\n".join(msg for _ok, msg in lines)
    assert "battery: 44% on battery" in text
    assert "battery guard: off" in text
    assert "battery thresholds: pause 15%" in text
    # the lid-close truth is said whatever else is true (AGENTS.md 5)
    assert "nothing can stop lid-close sleep" in text


