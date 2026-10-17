"""The battery watchdog's decision function, fuzzed and pinned.

`evaluate()` is the only place this feature decides to touch a print, so it is
the thing worth proving: it is pure (no I/O, no clock, no REST) and every
reading sequence can be replayed here on a fake battery series. Everything the
research spec lists as an invariant is asserted as a property over the whole
config grid, and the same rules are then pinned case by case so a regression
names the threshold it broke instead of a random example.

Offline: no socket (see conftest.no_network), no pmset, no process signal.
"""
import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

# The same profile as tests/battery/conftest.py HYP (a test module must not
# import its conftest: pytest owns that name, and the suite has two of them).
HYP = dict(deadline=None, max_examples=200,
           suppress_health_check=[HealthCheck.function_scoped_fixture])

DESTRUCTIVE = ("pause", "resume", "cancel", "down")
# The four things that move a print, as opposed to making noise about it.
PRINTER_ACTIONS = DESTRUCTIVE


def cfg(bg, **kw):
    base = dict(kw)
    return bg.normalise(base)


def drive(bg, readings, settings_=None):
    """Replay a battery series. Returns (batches, snapshots) where snapshots[i]
    is the episode as it stood BEFORE batch i was decided - which is exactly the
    evidence an invariant like "resume only when the guard itself paused" needs."""
    ep = bg.new_episode()
    batches, snapshots = [], []
    for reading in readings:
        snapshots.append(dict(ep))
        actions, ep = bg.evaluate(reading, ep, settings_)
        batches.append(list(actions))
    return batches, snapshots, ep


READING = st.fixed_dictionaries({
    "percent": st.one_of(st.none(), st.integers(-5, 110),
                         st.sampled_from(["12", "junk", 15.0, 12.5, True])),
    "source": st.sampled_from(["battery", "ac", "none", None, "ups"]),
    "printState": st.sampled_from([None, "printing", "paused", "standby",
                                   "complete"]),
})

CONFIGS = st.fixed_dictionaries({
    "pauseAt": st.integers(0, 100),
    "criticalAt": st.integers(0, 100),
    "atPause": st.sampled_from(["pause", "notify", "ignore"]),
    "atCritical": st.sampled_from(["cancel", "pause", "notify"]),
    "resumeOnAc": st.booleans(),
    "notify": st.booleans(),
})


@given(readings=st.lists(READING, max_size=14), config=CONFIGS)
@settings(**HYP)
def test_invariants_hold_for_any_battery_series_any_config(guard, readings,
                                                           config):
    """The rules of the watchdog, over the whole grid at once. Each one is
    checked against the episode as it stood BEFORE the batch was decided, which
    is the only honest way to ask "did it act because it was allowed to"."""
    bg = guard.bg
    batches, snapshots, _ep = drive(bg, readings, config)
    seen = cfg(bg, **config)
    pause_at, critical_at = seen["pauseAt"], min(seen["criticalAt"],
                                                 seen["pauseAt"])
    re_arm_above = pause_at + bg.HYSTERESIS

    def clean(reading):
        return bg.clean_percent(reading["percent"])

    def source(reading):
        return reading["source"] if reading["source"] in ("battery", "ac") \
            else None

    for index, actions in enumerate(batches):
        reading = readings[index]
        before = snapshots[index]
        percent, src = clean(reading), source(reading)

        # (f) an untrustworthy level never produces a destructive action.
        if percent is None or src is None:
            assert not [a for a in actions if a in DESTRUCTIVE], \
                "junk level {!r} on {} produced {}".format(
                    reading["percent"], reading["source"], actions)

        # (e) plugged in, nothing is ever paused, cancelled or torn down.
        if src == "ac":
            assert "pause" not in actions
            assert "cancel" not in actions
            assert "down" not in actions

        # (c) resume only ever for a pause this guard itself made, and only
        # with the opt-in switched on.
        if "resume" in actions:
            assert before["guardPaused"] is True
            assert seen["resumeOnAc"] is True

        # (d) the stack only comes down in a batch that cancels or pauses.
        if "down" in actions:
            assert "cancel" in actions or "pause" in actions

        # (a)+(b) a pause needs the guard armed, two readings in a row at or
        # below the threshold, and a critical not already handled.
        if "pause" in actions:
            assert src == "battery"
            assert percent <= pause_at
            assert before["armed"] is True
            assert before["criticalDone"] is False
            assert before["lowStreak"] >= bg.LOW_STREAK - 1

        # (g) a cancel happens once per episode and needs its own streak.
        if "cancel" in actions:
            assert src == "battery"
            assert percent <= critical_at
            assert before["criticalDone"] is False
            assert before["critStreak"] >= bg.LOW_STREAK - 1

    # (b) the other half: being armed again is only ever bought by crossing
    # pauseAt+HYSTERESIS, so a dithering battery can never flap.
    for index in range(1, len(batches)):
        was, now = snapshots[index - 1]["armed"], snapshots[index]["armed"]
        if not was and now:
            earlier = clean(readings[index - 1])
            assert earlier is not None and earlier > re_arm_above, (
                "re-armed at {} after {}%".format(readings[index - 1], earlier))
        was, now = (snapshots[index - 1]["criticalDone"],
                    snapshots[index]["criticalDone"])
        if was and not now:
            earlier = clean(readings[index - 1])
            assert earlier is not None and earlier > re_arm_above


@given(readings=st.lists(READING, max_size=12), config=CONFIGS)
@settings(**HYP)
def test_notify_only_modes_never_touch_the_printer(guard, readings, config):
    """atPause/atCritical = notify|ignore means zero printer actions - the
    guard's silent configuration: it sees the level, it says nothing to the
    printer."""
    bg = guard.bg
    config = dict(config, atPause="notify", atCritical="notify")
    batches, _snaps, _ep = drive(bg, readings, config)
    for actions in batches:
        assert not [a for a in actions if a in ("pause", "cancel", "down")]
    config = dict(config, atPause="ignore", atCritical="ignore")
    batches, _snaps, _ep = drive(bg, readings, config)
    for actions in batches:
        # "ignore" is silent about the printer; the only thing it may still say
        # is the one honest notification an unreadable battery is entitled to.
        assert not [a for a in actions if a in PRINTER_ACTIONS]
        assert set(actions) <= {"notify_unreadable"}


@given(readings=st.lists(READING, max_size=12), config=CONFIGS)
@settings(**HYP)
def test_unreadable_battery_is_said_once_and_never_acted_on(guard, readings,
                                                            config):
    """The only voice an unreadable battery gets is one notification per outage
    - and never an action. A new outage (a reading in between that WAS
    trustworthy) earns a new one, which is the whole point of the streak."""
    bg = guard.bg
    config = dict(config, pauseAt=50, criticalAt=25)
    batches, _snaps, ep = drive(bg, readings, config)

    def trustworthy(reading):
        return bg.clean_percent(reading["percent"]) is not None \
            and reading["source"] in ("battery", "ac")

    for index, actions in enumerate(batches):
        if not trustworthy(readings[index]):
            assert not [a for a in actions if a in DESTRUCTIVE]
    said = [i for i, b in enumerate(batches) if "notify_unreadable" in b]
    assert len(said) <= len(readings)
    for first, second in zip(said, said[1:]):
        assert any(trustworthy(r) for r in readings[first + 1:second]), (
            "notify_unreadable twice in one outage")


# --------------------------------------------------------------------------
# the same rules, pinned: a regression must name the threshold, not an example
# --------------------------------------------------------------------------

def test_default_thresholds_pause_once_not_per_tick(guard):
    bg = guard.bg
    batches, _snaps, ep = drive(bg, [
        {"percent": 12, "source": "battery"},
        {"percent": 12, "source": "battery"},
        {"percent": 12, "source": "battery"},
        {"percent": 12, "source": "battery"},
    ], cfg(bg))
    assert batches[0] == []           # one reading is never enough
    assert batches[1] == ["notify_pause", "pause"]
    assert batches[2] == []           # and it is not repeated every tick
    assert batches[3] == []
    assert ep["armed"] is False
    assert ep["guardPaused"] is True


def test_boundary_at_pause_at_and_one_above(guard):
    bg = guard.bg
    low, _s, _e = drive(bg, [{"percent": 15, "source": "battery"}] * 2, cfg(bg))
    assert "pause" in low[1]
    high, _s, _e = drive(bg, [{"percent": 16, "source": "battery"}] * 5, cfg(bg))
    assert high == [[], [], [], [], []]


def test_critical_wins_and_cancels_then_down(guard):
    bg = guard.bg
    batches, _snaps, ep = drive(bg, [
        {"percent": 12, "source": "battery"},
        {"percent": 12, "source": "battery"},   # paused here
        {"percent": 6, "source": "battery"},
        {"percent": 5, "source": "battery"},
    ], cfg(bg))
    assert batches[1] == ["notify_pause", "pause"]
    assert batches[3] == ["notify_critical", "cancel", "down"]
    assert ep["criticalDone"] is True


def test_critical_at_pause_at_both_hit_on_one_reading(guard):
    """criticalAt == pauseAt is a legal config: the critical branch wins, and a
    print is never paused and then cancelled by the same sample."""
    bg = guard.bg
    batches, _snaps, _ep = drive(bg, [{"percent": 15, "source": "battery"}] * 2,
                                 cfg(bg, pauseAt=15, criticalAt=15))
    assert batches[0] == []
    assert batches[1] == ["notify_critical", "cancel", "down"]


def test_negative_and_none_level_never_trusted(guard):
    bg = guard.bg
    for junk in (None, -1, -15, 101, "abc", True, float("nan"),
                 float("inf")):
        batches, _snaps, ep = drive(bg, [{"percent": junk,
                                         "source": "battery"}] * 6, cfg(bg))
        flat = [a for b in batches for a in b]
        assert not [a for a in flat if a in DESTRUCTIVE], junk
        assert ep["percent"] is None, junk
    # Three unreadable polls in a row are said out loud, once.
    batches, _snaps, _ep = drive(bg, [{"percent": None, "source": "battery"}]
                                 * 4, cfg(bg))
    flat = [a for b in batches for a in b]
    assert flat == ["notify_unreadable"]


def test_ac_restore_resumes_only_with_the_opt_in(guard):
    bg = guard.bg
    paused = [{"percent": 12, "source": "battery"},
              {"percent": 12, "source": "battery"}]
    # Default: off. The guard paused the print and the cable came back, and it
    # still does not touch it - a print is only ever resumed by the user.
    batches, _snaps, ep = drive(bg, paused + [{"percent": 40, "source": "ac"}],
                                cfg(bg))
    assert [a for b in batches for a in b] == ["notify_pause", "pause"]
    assert ep["guardPaused"] is True

    # Opt-in: exactly one resume, and only because this guard paused it.
    batches, _snaps, ep = drive(bg, paused + [{"percent": 40, "source": "ac"}],
                                cfg(bg, resumeOnAc=True))
    assert batches[2] == ["resume"]
    assert ep["guardPaused"] is False
    # and not twice: the second AC poll has nothing left to resume.
    again, _s, _e = drive(bg, paused + [{"percent": 40, "source": "ac"}] * 2,
                          cfg(bg, resumeOnAc=True))
    assert [a for b in again for a in b] == ["notify_pause", "pause", "resume"]


def test_resume_never_resurrects_a_human_pause(guard):
    """guardPaused False - a person paused it - is not the guard's to resume."""
    bg = guard.bg
    ep = bg.new_episode()
    ep.update({"armed": False, "guardPaused": False})
    actions, _ep = bg.evaluate({"percent": 60, "source": "ac"}, ep,
                               cfg(bg, resumeOnAc=True))
    assert actions == []


def test_rearm_needs_the_hysteresis_margin(guard):
    bg = guard.bg
    # Discharged by the pause at 12%, wobbling at 16-17%: still disarmed, so no
    # second pause, until it crosses 15+3.
    batches, _snaps, ep = drive(bg, [
        {"percent": 12, "source": "battery"},
        {"percent": 12, "source": "battery"},      # pause
        {"percent": 16, "source": "battery"},
        {"percent": 17, "source": "battery"},      # 17 == pauseAt+2: no re-arm
        {"percent": 12, "source": "battery"},      # must not pause again
        {"percent": 19, "source": "battery"},      # above 15+3: re-armed
        {"percent": 12, "source": "battery"},
        {"percent": 12, "source": "battery"},      # a NEW print may be paused
    ], cfg(bg))
    assert [i for i, b in enumerate(batches) if "pause" in b] == [1, 7]
    assert ep["armed"] is False


def test_a_paused_by_guard_print_is_never_paused_twice(guard):
    """printState is the second half of "once per print": the guard pauses a
    printer that is printing, then leaves the paused print alone - it never
    re-pauses, and it never re-pauses something a person paused."""
    bg = guard.bg
    batches, _snaps, _ep = drive(bg, [
        {"percent": 12, "source": "battery", "printState": "printing"},
        {"percent": 12, "source": "battery", "printState": "printing"},
        {"percent": 12, "source": "battery", "printState": "paused"},
        {"percent": 12, "source": "battery", "printState": "paused"},
    ], cfg(bg))
    assert batches[0] == []
    assert batches[1] == ["notify_pause", "pause"]
    assert batches[2] == []
    assert batches[3] == []
    # A print a human paused at low battery: nothing at all is sent, and the
    # guard keeps its arming for the next print that actually runs.
    batches, _snaps, ep = drive(bg, [
        {"percent": 12, "source": "battery", "printState": "paused"},
        {"percent": 12, "source": "battery", "printState": "paused"},
    ], cfg(bg))
    assert batches == [[], []]
    assert ep["armed"] is True


def test_standby_at_low_battery_stays_armed(guard):
    """Nothing is printing: the guard must not burn its arming, so a print
    started at 12% is still paused on the next poll."""
    bg = guard.bg
    batches, _snaps, ep = drive(bg, [
        {"percent": 12, "source": "battery", "printState": "standby"},
        {"percent": 12, "source": "battery", "printState": "standby"},
        {"percent": 12, "source": "battery", "printState": "printing"},
        {"percent": 12, "source": "battery", "printState": "printing"},
    ], cfg(bg))
    assert batches[0] == [] and batches[1] == []
    assert batches[2] == ["notify_pause", "pause"]
    assert ep["armed"] is False


def test_at_pause_and_at_critical_variants(guard):
    bg = guard.bg
    low = [{"percent": 10, "source": "battery"}] * 2
    crit = [{"percent": 3, "source": "battery"}] * 2
    assert drive(bg, low, cfg(bg, atPause="notify"))[0][1] == ["notify_pause"]
    assert drive(bg, low, cfg(bg, atPause="ignore"))[0][1] == []
    assert drive(bg, crit, cfg(bg, atCritical="pause"))[0][1] == [
        "notify_critical", "pause"]
    assert drive(bg, crit, cfg(bg, atCritical="notify"))[0][1] == [
        "notify_critical"]
    # notify=False silences the banner and keeps the printer action.
    assert drive(bg, low, cfg(bg, notify=False))[0][1] == ["pause"]


def test_critical_blocks_a_later_pause_in_the_same_episode(guard):
    bg = guard.bg
    batches, _snaps, _ep = drive(bg, [
        {"percent": 6, "source": "battery"},
        {"percent": 6, "source": "battery"},      # cancel + down
        {"percent": 12, "source": "battery"},
        {"percent": 12, "source": "battery"},
    ], cfg(bg))
    assert batches[1] == ["notify_critical", "cancel", "down"]
    assert batches[3] == []


def test_down_is_refused_without_a_cancel_in_the_batch(guard):
    """The runner's own gate: an episode that never cancelled cannot tear the
    stack down, even if something hands it a bare "down"."""
    bg = guard.bg
    ep = bg.new_episode()
    bg.run_actions(["notify_pause", "down"], cfg(bg), ep, dry=False)
    assert guard.rec["stop"] == 0
    assert guard.rec["post"] == []
    # and the same batch with the cancel first is the documented behaviour
    ep = bg.new_episode()
    bg.run_actions(["notify_critical", "cancel", "down"], cfg(bg), ep,
                   dry=False)
    assert guard.rec["post"] == ["/printer/print/cancel"]
    assert guard.rec["stop"] == 1
    assert ep["lastAction"] == "cancel+down"


def test_hostile_config_is_repaired_not_trusted(guard):
    """evaluate() normalises whatever it is handed: a critical above a pause
    cannot make the pause branch unreachable, and a -5 pause cannot pause at
    every reading."""
    bg = guard.bg
    for hostile in ({"pauseAt": -5}, {"pauseAt": 500}, {"criticalAt": 90,
                                                       "pauseAt": 10},
                    {"atPause": "reboot"}, {"pollSec": 0}, {"pauseAt": True}):
        config = bg.normalise(hostile)
        assert 0 <= config["criticalAt"] <= config["pauseAt"] <= 100, hostile
        assert config["atPause"] in bg.AT_PAUSE, hostile
        assert 5 <= config["pollSec"] <= 3600, hostile
        # and a series never acts on a level the repaired config cannot reach
        batches, _snaps, _ep = drive(bg, [{"percent": -3,
                                          "source": "battery"}] * 4, config)
        assert not [a for b in batches for a in b if a in DESTRUCTIVE]
