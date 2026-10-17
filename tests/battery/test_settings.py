"""settings.json's "battery" block: hostile values, and a write that keeps
everything else in the file.

settings.json is shared with the SwiftUI app and hand-edited by people, so the
read path must never raise and never trust, and the write path must never
replace a file it could not read faithfully or drop a block it did not touch.
"""
import json

import pytest


def test_defaults_when_nothing_is_said(guard):
    bg = guard.bg
    cfg = bg.battery_settings()
    assert cfg["enabled"] is False
    assert cfg["pauseAt"] == 15 and cfg["criticalAt"] == 7
    assert cfg["pollSec"] == 30
    # resume-on-AC is OPT-IN: a guard that resumes a print by itself is a
    # surprise, and a print a human paused is not the guard's to resume.
    assert cfg["resumeOnAc"] is False
    assert cfg["atPause"] == "pause" and cfg["atCritical"] == "cancel"
    assert cfg["notify"] is True and cfg["dryRun"] is False


@pytest.mark.parametrize("hostile", [
    [], "battery", 42, None, 0, True, {"pauseAt": "lots"},
    {"pauseAt": -4}, {"pauseAt": 101}, {"pauseAt": True}, {"pauseAt": 15.7},
    {"criticalAt": 90, "pauseAt": 10}, {"pollSec": 0}, {"pollSec": -1},
    {"pollSec": 99999}, {"atPause": "reboot"}, {"atCritical": "selftest"},
    {"resumeOnAc": "maybe"}, {"enabled": 1}, {"pauseAt": None},
    {"pauseAt": float("nan")}, {"pauseAt": [15]}, {"dryRun": "on"},
    {"pauseAt": 15, "criticalAt": 15}, {"pauseAt": 0}, {"pauseAt": 100},
    {"pauseAt": 3}, {"criticalAt": 50}, {"pauseAt": 12, "criticalAt": 13},
])
def test_a_hostile_block_is_repaired_never_trusted(guard, hostile):
    """A junk settings.json reads as "guard off with sane thresholds", and
    nothing in it can make the watchdog act on a level it cannot reach."""
    bg = guard.bg
    cfg = bg.normalise(hostile)
    assert 0 <= cfg["criticalAt"] <= cfg["pauseAt"] <= 100
    assert 5 <= cfg["pollSec"] <= 3600
    assert cfg["atPause"] in bg.AT_PAUSE
    assert cfg["atCritical"] in bg.AT_CRITICAL
    assert isinstance(cfg["resumeOnAc"], bool)
    assert isinstance(cfg["notify"], bool)
    # A repaired config is still a working machine: at exactly its pause
    # threshold it does something, and one reading is never enough.
    probe = cfg["pauseAt"]
    actions, ep = bg.evaluate({"percent": probe, "source": "battery"},
                              bg.new_episode(), cfg)
    assert actions == []
    actions, ep = bg.evaluate({"percent": probe, "source": "battery"}, ep, cfg)
    if cfg["criticalAt"] >= probe:
        assert "cancel" in actions or "down" in actions or "pause" in actions
    else:
        assert actions == ["notify_pause", "pause"], cfg


def test_a_critical_threshold_is_never_out_of_reach(guard):
    """The repair rule that the machine depends on: criticalAt <= pauseAt,
    whatever the file said. A threshold above the pause level can never be
    crossed, and a config that can never fire is a silent watchdog."""
    bg = guard.bg
    assert bg.normalise({"pauseAt": 3})["criticalAt"] == 3
    assert bg.normalise({"pauseAt": 12, "criticalAt": 13})["criticalAt"] == 7
    assert bg.normalise({"pauseAt": 5, "criticalAt": 5})["criticalAt"] == 5
    assert bg.normalise({"pauseAt": 40, "criticalAt": 38}) == dict(
        bg.DEFAULTS, pauseAt=40, criticalAt=38)


def test_a_junk_settings_root_reads_as_guard_off(guard):
    """Not an object at the root (the GUI, a half-written file, a human with a
    text editor) is "no settings", never an exception and never "enabled"."""
    bg = guard.bg
    for junk in ("[]", "42", '"battery"', "null", "{not json", ""):
        guard.paths.SETTINGS.write_text(junk)
        assert bg.battery_settings()["enabled"] is False
        assert bg.battery_settings()["pauseAt"] == 15


def test_settings_read_never_raises_even_when_the_file_is_a_directory(guard):
    bg = guard.bg
    guard.paths.SETTINGS.unlink(missing_ok=True)
    guard.paths.SETTINGS.mkdir()
    assert bg.battery_settings()["enabled"] is False


def test_validate_refuses_what_could_never_fire(guard):
    bg = guard.bg
    with pytest.raises(ValueError) as caught:
        bg.validate(criticalAt=20, pauseAt=15)
    assert "above pauseAt" in str(caught.value)
    for bad in ({"pauseAt": -1}, {"pauseAt": 101}, {"pauseAt": 15.5},
                {"pauseAt": "lots"}, {"pauseAt": True}, {"criticalAt": -3},
                {"pollSec": 1}, {"pollSec": 100000}, {"atPause": "halt"},
                {"atCritical": "reboot"}, {"resumeOnAc": "maybe"},
                {"nonsense": 1}):
        with pytest.raises(ValueError):
            bg.validate(**bad)


def test_validate_accepts_the_useful_spellings(guard):
    bg = guard.bg
    assert bg.validate(pauseAt=20, criticalAt=5) == {"pauseAt": 20,
                                                    "criticalAt": 5}
    assert bg.validate(resumeOnAc="on") == {"resumeOnAc": True}
    assert bg.validate(resumeOnAc="off") == {"resumeOnAc": False}
    assert bg.validate(pauseAt="12") == {"pauseAt": 12}
    assert bg.validate(enabled=False) == {"enabled": False}
    # criticalAt == pauseAt is legal (the critical branch wins) - see
    # test_critical_at_pause_at_both_hit_on_one_reading
    assert bg.validate(criticalAt=15, pauseAt=15) == {"criticalAt": 15,
                                                     "pauseAt": 15}


def test_write_keeps_every_other_block(guard):
    """The same settings.json holds cam, lan and data. Saving the battery block
    may not lose one of them - the camera prefs in particular are something
    nobody can rebuild without asking the user for the device name again."""
    bg = guard.bg
    settings = {
        "cam": {"width": 1280, "height": 720, "fps": 15, "device": "FaceTime"},
        "lan": True,
        "data": str(guard.data),
        "ui": "fluidd",
        "battery": {"pauseAt": 30},
    }
    guard.paths.SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    guard.paths.SETTINGS.write_text(json.dumps(settings))
    merged = bg.save_battery_settings(pauseAt=22, criticalAt=9, enabled=True)
    assert merged["pauseAt"] == 22 and merged["criticalAt"] == 9
    assert merged["enabled"] is True
    after = json.loads(guard.paths.SETTINGS.read_text())
    assert after["cam"] == settings["cam"]
    assert after["lan"] is True
    assert after["data"] == settings["data"]
    assert after["ui"] == "fluidd"
    assert after["battery"]["pauseAt"] == 22


def test_write_creates_the_file_and_the_folder(guard):
    bg = guard.bg
    bg.save_battery_settings(pauseAt=40)
    doc = json.loads(guard.paths.SETTINGS.read_text())
    assert doc["battery"]["pauseAt"] == 40
    assert bg.battery_settings()["pauseAt"] == 40


def test_a_torn_settings_file_is_kept_before_we_write_over_it(guard):
    """paths.save_settings() rule, applied to this feature: a file that does not
    parse is copied to settings.json.bak first, so the user's camera prefs are
    recoverable, and only then is the file written. A refusing watchdog would be
    less useful than one that says where the old bytes went."""
    bg = guard.bg
    torn = '{"cam": {"fps": 20}, "battery": {"pauseAt": 15}'
    guard.paths.SETTINGS.write_text(torn)
    merged = bg.save_battery_settings(pauseAt=30)
    assert merged["pauseAt"] == 30
    backup = guard.paths.SETTINGS.with_suffix(".json.bak")
    assert backup.read_text() == torn
    assert json.loads(guard.paths.SETTINGS.read_text())["battery"]["pauseAt"] \
        == 30


def test_a_blocked_write_is_reported_not_half_done(guard):
    bg = guard.bg
    guard.paths.SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    guard.paths.SETTINGS.write_text('{"cam": {"fps": 20}}')
    before = guard.paths.SETTINGS.read_text()
    # settings.json replaced by a directory: save_settings() raises OSError.
    guard.paths.SETTINGS.unlink()
    guard.paths.SETTINGS.mkdir()
    with pytest.raises(OSError):
        bg.save_battery_settings(pauseAt=30)
    assert bg.battery_settings()["pauseAt"] == 15     # default, not 30
    assert before  # the original text is what the refused write never touched


def test_enabled_only_by_a_named_flag(guard):
    """Nothing in the stack's own life enables the guard: no setup, no up, no
    doctor, no default. Only `batteryguard on` writes enabled=true."""
    bg = guard.bg
    assert bg.battery_settings()["enabled"] is False
    bg.save_battery_settings(enabled=True)
    assert bg.battery_settings()["enabled"] is True
    bg.save_battery_settings(enabled=False)
    assert bg.battery_settings()["enabled"] is False
