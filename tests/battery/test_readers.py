"""Reading a battery: captured pmset/ioreg output, injected levels, junk.

The parsers are pure string functions, so every case here is a fixture string,
not a call to /usr/bin/pmset - including the shapes the research found in the
wild: wording that changes with macOS version, a desktop that has no battery,
an empty or truncated stdout, and the `AvgTimeToEmpty = 65535` sentinel that
looks like "18 hours left" and means "not on battery".

Nothing in this file touches the network or a process.
"""
import json

import pytest

PMSET_AC = """Now drawing from 'AC Power'
 -InternalBattery-0 (id=6226083)\t100%; charged;\t0:00 remaining\tpresent: yes
"""

PMSET_BATTERY = """Now drawing from 'Battery Power'
 -InternalBattery-0 (id=6226083)\t57%; discharging;\t4:21 remaining\tpresent: yes
"""

PMSET_CHARGING = """Now drawing from 'AC Power'
 -InternalBattery-0 (id=6226083)\t23%; charging;\t2:05 remaining\tpresent: yes
"""

PMSET_LOW = """Now drawing from 'Battery Power'
 -InternalBattery-0 (id=6226083)\t6%; discharging;\t0:19 remaining\tpresent: yes
"""

PMSET_NO_BATTERY = """Now drawing from 'AC Power'
 -InternalBattery-0 (id=7077987)\t100%; charged;\t0:00 remaining\tpresent: no
"""

PMSET_HEADER_ONLY = "Now drawing from 'Battery Power'\n"

PMSET_FRACTION = """Now drawing from 'Battery Power'
 -InternalBattery-0 (id=1)\t15.4%; discharging;\t1:00 remaining\tpresent: yes
"""

IOREG_BATTERY = """+-o AppleSmartBattery  <class AppleSmartBattery>
    {
      "AverageUnit" = 0
      "CurrentCapacity" = 62
      "MaxCapacity" = 100
      "ExternalConnected" = No
      "IsCharging" = No
      "AvgTimeToEmpty" = 65535
      "TimeRemaining" = 0
    }
"""

IOREG_AC = """+-o AppleSmartBattery  <class AppleSmartBattery>
    {
      "CurrentCapacity" = 38
      "MaxCapacity" = 100
      "ExternalConnected" = Yes
      "IsCharging" = Yes
    }
"""

IOREG_NO_MAX = '''+-o AppleSmartBattery
    {
      "CurrentCapacity" = 40
      "MaxCapacity" = 0
      "ExternalConnected" = No
    }
'''

IOREG_NO_SOURCE = '''{"CurrentCapacity" = 40, "MaxCapacity" = 100}'''

IOREG_NONNUMERIC = '''{"CurrentCapacity" = "lots", "MaxCapacity" = 100,
  "ExternalConnected" = No}'''

IOREG_FRACTION = '''{"CurrentCapacity" = 7, "MaxCapacity" = 64,
  "ExternalConnected" = No}'''


def reading(**kw):
    base = {"percent": 50, "source": "battery", "charging": False,
             "remaining": None, "engine": "pmset", "raw": ""}
    base.update(kw)
    return base


# ------------------------------------------------------------------ parsers --

def test_pmset_on_ac(guard):
    got = guard.bg.parse_pmset(PMSET_AC)
    assert got["percent"] == 100
    assert got["source"] == "ac"
    assert got["charging"] is False      # "charged", not "charging"
    assert got["remaining"] == "0:00"
    assert got["engine"] == "pmset"


def test_pmset_discharging_and_charging(guard):
    got = guard.bg.parse_pmset(PMSET_BATTERY)
    assert (got["percent"], got["source"], got["charging"]) == (57, "battery",
                                                               False)
    assert got["remaining"] == "4:21"
    charged = guard.bg.parse_pmset(PMSET_CHARGING)
    assert (charged["source"], charged["charging"]) == ("ac", True)
    low = guard.bg.parse_pmset(PMSET_LOW)
    assert low["percent"] == 6 and low["source"] == "battery"


def test_pmset_without_a_battery_is_none_not_zero(guard):
    """The desktop case: `present: no` with a 100% next to it. A guard that
    read that as a battery would be watching a part that is not in the Mac."""
    assert guard.bg.parse_pmset(PMSET_NO_BATTERY) is None


@pytest.mark.parametrize("text", ["", "   ", "\n", None,
                                  "pmset: command not found",
                                  PMSET_HEADER_ONLY])
def test_pmset_partial_or_junk_is_never_a_level(guard, text):
    got = guard.bg.parse_pmset(text)
    if text == PMSET_HEADER_ONLY:
        # A source with no level is honest and useless: it is "unreadable",
        # which the state machine can never act on.
        assert got["percent"] is None
        assert got["source"] == "battery"
        return
    assert got is None


def test_pmset_fractional_percent_is_truncated(guard):
    """15.4 -> 15: pmset does not print fractions, but a reader that did means
    "about 15", and the guard's threshold is a whole percent."""
    assert guard.bg.parse_pmset(PMSET_FRACTION)["percent"] == 15


def test_ioreg_battery_and_ac(guard):
    got = guard.bg.parse_ioreg(IOREG_BATTERY)
    assert (got["percent"], got["source"], got["charging"]) == (62, "battery",
                                                               False)
    assert got["engine"] == "ioreg"
    # Never "18 hours left": AvgTimeToEmpty 65535 / TimeRemaining 0 are AC
    # sentinels and this reader does not look at them at all.
    # ... and the sentinel is never turned into a promise about time.
    assert got["remaining"] is None
    plugged = guard.bg.parse_ioreg(IOREG_AC)
    assert (plugged["percent"], plugged["source"], plugged["charging"]) == \
        (38, "ac", True)


@pytest.mark.parametrize("text", [
    "", None, "no matches",
    IOREG_NO_MAX,        # MaxCapacity 0: dividing by it is a crash, not an answer
    IOREG_NO_SOURCE,     # no ExternalConnected: the source would be a guess
    IOREG_NONNUMERIC,    # "lots" is not a capacity
])
def test_ioreg_junk_is_none(guard, text):
    assert guard.bg.parse_ioreg(text) is None


def test_ioreg_rounds_a_real_ratio(guard):
    assert guard.bg.parse_ioreg(IOREG_FRACTION)["percent"] == 11


# --------------------------------------------------------- injection points --

def test_env_percent_and_source(guard):
    bg = guard.bg
    got = bg.read_power(env={"KLIPPERFORMAC_BATTERY_PERCENT": "12",
                             "KLIPPERFORMAC_POWER_SOURCE": "battery"})
    assert (got["percent"], got["source"], got["engine"]) == (12, "battery",
                                                             "env")
    got = bg.read_power(env={"KLIPPERFORMAC_BATTERY_PERCENT": "80",
                             "KLIPPERFORMAC_POWER_SOURCE": "ac"})
    assert (got["percent"], got["source"], got["charging"]) == (80, "ac", True)


@pytest.mark.parametrize("raw", ["-4", "abc", "101", "nan", "True", "1e9",
                                 "12.7.8", "-0.5", "  -3"])
def test_an_injected_level_is_still_a_percentage_or_nothing(guard, raw):
    """The percentage source is injectable by design - and the injection goes
    through the same gate, so a test (or a stray env var in someone's shell)
    cannot hand the watchdog a negative or a non-number and have it acted on."""
    bg = guard.bg
    assert bg.clean_percent(raw) is None
    got = bg.read_power(env={"KLIPPERFORMAC_BATTERY_PERCENT": raw,
                             "KLIPPERFORMAC_POWER_SOURCE": "battery"})
    assert got["percent"] is None, raw
    assert got["source"] == "battery"   # the Mac is real, the level is not
    # and an unreadable level can never pause anything
    actions, _ep = bg.evaluate(got, bg.new_episode(), bg.normalise({}))
    assert not [a for a in actions if a in ("pause", "cancel", "down", "resume")]


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_a_blank_injection_means_not_injected(guard, raw):
    """`export KLIPPERFORMAC_BATTERY_PERCENT=` is "unset", not "0 %": a blank
    must not read as an empty battery. With nothing else to ask in the harness,
    read_power() answers None (no battery here), which `on` refuses."""
    bg = guard.bg
    env = {"KLIPPERFORMAC_POWER_SOURCE": "battery"}
    if raw is not None:
        env["KLIPPERFORMAC_BATTERY_PERCENT"] = raw
    assert bg.read_power(env=env) is None


def test_unknown_source_is_not_a_battery(guard):
    bg = guard.bg
    got = bg.read_power(env={"KLIPPERFORMAC_BATTERY_PERCENT": "12",
                             "KLIPPERFORMAC_POWER_SOURCE": "ups"})
    assert got["percent"] is None and got["source"] == "none"


def test_battery_file_fixture_wins(guard, tmp_path):
    bg = guard.bg
    fixture = tmp_path / "battery.json"
    fixture.write_text(json.dumps({"percent": 12, "source": "battery",
                                   "charging": False}))
    got = bg.read_power(env={"KLIPPERFORMAC_BATTERY_FILE": str(fixture),
                             "KLIPPERFORMAC_BATTERY_PERCENT": "99"})
    assert (got["percent"], got["source"], got["engine"]) == (12, "battery",
                                                             "fixture")
    # a fixture naming AC is read as AC: the guard does nothing there
    fixture.write_text(json.dumps({"percent": 12, "source": "ac"}))
    got = bg.read_power(env={"KLIPPERFORMAC_BATTERY_FILE": str(fixture)})
    assert got["source"] == "ac"


@pytest.mark.parametrize("body", ["", "not json", "[]", "{}", '"x"',
                                  '{"percent": -9}', '{"percent": "lots"}',
                                  '{"percent": true}'])
def test_a_junk_battery_file_is_an_unreadable_battery(guard, tmp_path, body):
    """Never a 0 %, never a crash: the fixture hook cannot make the guard act
    blind."""
    bg = guard.bg
    fixture = tmp_path / "battery.json"
    fixture.write_text(body)
    got = bg.read_power(env={"KLIPPERFORMAC_BATTERY_FILE": str(fixture)})
    assert got is not None
    assert got["percent"] is None


def test_a_battery_file_without_a_usable_source_has_no_say(guard, tmp_path):
    """percent 12 with source 7: the number is kept (it is honest data from the
    file) and the source is refused, which is what makes the level unactible -
    a guard that cannot say "on battery" must not pause a print."""
    bg = guard.bg
    fixture = tmp_path / "battery.json"
    fixture.write_text(json.dumps({"percent": 12, "source": 7}))
    got = bg.read_power(env={"KLIPPERFORMAC_BATTERY_FILE": str(fixture)})
    assert got["source"] == "none"
    actions, _ep = bg.evaluate(got, bg.new_episode(), bg.normalise({}))
    assert actions == []


def test_missing_battery_file_is_unreadable_not_fatal(guard, tmp_path):
    bg = guard.bg
    got = bg.read_power(env={"KLIPPERFORMAC_BATTERY_FILE":
                             str(tmp_path / "gone.json")})
    assert got["percent"] is None and got["source"] == "none"


def test_read_power_real_path_asks_absolute_first(guard):
    """A stripped launchd/CI PATH omits /usr/sbin, where ioreg lives: every
    helper binary is asked for by its absolute path first, then by name (the
    /usr/sbin/sysctl-then-sysctl discipline already proven in process.py)."""
    bg = guard.bg
    assert bg.read_power(env={}) is None
    assert guard.rec["exec"] == [
        ["/usr/bin/pmset", "-g", "batt"],
        ["pmset", "-g", "batt"],
        ["/usr/sbin/ioreg", "-rn", "AppleSmartBattery"],
        ["ioreg", "-rn", "AppleSmartBattery"],
    ]


def test_read_power_pmset_then_ioreg(guard, monkeypatch):
    """ioreg is the fallback, and only the fallback: a Mac whose pmset answered
    is never asked twice."""
    bg = guard.bg
    asked = []

    def fake_exec(argv, timeout=5.0):
        asked.append(" ".join(argv))
        if "pmset" in argv[0]:
            return ""                      # pmset said nothing
        return IOREG_BATTERY

    monkeypatch.setattr(bg, "_exec", fake_exec)
    got = bg.read_power(env={})
    assert (got["percent"], got["engine"]) == (62, "ioreg")
    assert any("pmset -g batt" in cmd for cmd in asked)
    assert any("ioreg -rn AppleSmartBattery" in cmd for cmd in asked)

    asked.clear()
    monkeypatch.setattr(bg, "_exec",
                        lambda argv, timeout=5.0: (asked.append(" ".join(argv)),
                                                   PMSET_BATTERY)[1])
    got = bg.read_power(env={})
    assert (got["percent"], got["engine"]) == (57, "pmset")
    assert not any("ioreg" in cmd for cmd in asked)


def test_default_read_uses_the_environment(guard, monkeypatch):
    """No env argument means os.environ, which is what `check` and the child
    use; the fixture guarantees no stray injection leaks in from the shell."""
    bg = guard.bg
    monkeypatch.setenv("KLIPPERFORMAC_BATTERY_PERCENT", "9")
    monkeypatch.setenv("KLIPPERFORMAC_POWER_SOURCE", "battery")
    assert bg.read_power()["percent"] == 9
    monkeypatch.delenv("KLIPPERFORMAC_BATTERY_PERCENT")
    monkeypatch.delenv("KLIPPERFORMAC_POWER_SOURCE")
    monkeypatch.setattr(bg, "_exec", lambda argv, timeout=5.0: PMSET_LOW)
    assert bg.read_power()["percent"] == 6


def test_synth_reading_is_the_same_gate(guard):
    bg = guard.bg
    assert bg.synth_reading("12", "battery")["percent"] == 12
    assert bg.synth_reading(-4, "battery")["percent"] is None
    assert bg.synth_reading(12, "ups")["source"] is None
    assert bg.synth_reading(12, "battery", "printing")["printState"] == \
        "printing"


def test_thresholds_fire_on_the_injected_level(guard):
    """The spec's injection table, end to end through read_power()'s env hook:
    15/7 defaults pause at 12, cancel at 7, do nothing at 16, and 15.4 (read as
    15) pauses."""
    bg = guard.bg
    env_var = {"KLIPPERFORMAC_BATTERY_PERCENT": "12",
               "KLIPPERFORMAC_POWER_SOURCE": "battery"}
    for percent, expected in ((12, "pause"), (15, "pause"), (16, "nothing"),
                              (7, "cancel"), (6, "cancel"), (50, "nothing")):
        env = dict(env_var, KLIPPERFORMAC_BATTERY_PERCENT=str(percent))
        actions = []
        ep = bg.new_episode()
        for _ in range(2):          # two readings, as the jitter guard asks
            found, ep = bg.evaluate(bg.read_power(env=env), ep,
                                    bg.normalise({}))
            actions.extend(found)
        flat = [a.split("_")[-1] for a in actions]
        if expected == "pause":
            assert "pause" in flat and "cancel" not in flat, percent
        elif expected == "cancel":
            assert "cancel" in flat and "down" in flat, percent
        else:
            assert flat == [], percent
