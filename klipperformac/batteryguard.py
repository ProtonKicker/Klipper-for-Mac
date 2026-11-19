"""Low-battery watchdog: pause the print before the laptop's battery dies.

`klipperformac batteryguard on` starts a tracked detached child that reads this
Mac's battery every ~30 s and, ON BATTERY ONLY, uses upstream Moonraker's own
public REST API: pause at ``battery.pauseAt`` (default 15 %), cancel plus a
graceful stack teardown at ``battery.criticalAt`` (default 7 %), and a desktop
notification for each. Resume when the cable comes back is opt-in
(``battery.resumeOnAc``), because a print a human paused is not ours to resume.

Upstream stays untouched (AGENTS.md 1): the guard is a REST client exactly like
`klipperformac gcode` is. No Klipper or Moonraker file is read or written, no
printer.cfg semantic changes, no new dependency, and it never asks the Mac to
stay awake - it ends the stack, which reduces wakefulness.

Every input the state machine judges is injectable (``KLIPPERFORMAC_BATTERY_*``
or a parameter), so the offline tests drive the exact same ``evaluate()`` the
child runs, and no test ever needs a discharging laptop. A battery level that
cannot be read - None, negative, a bool, "1e9", junk - is never trusted: it
produces no destructive action, only a notification after a streak of them.
"""
import fcntl
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import paths, process

# ----------------------------------------------------------------- identity --
# The child's argv tail after the interpreter. process.is_guard_cmd() matches it
# exactly, which is what makes the pid in the ledger provably ours; it is
# deliberately NOT a process.SERVICES member (an optional watchdog is not part
# of the stack, and `status`/state.json/the GUI stack row would all have to
# learn a new name for it).
ARGV_TAIL = process.GUARD_ARGV

DEFAULTS = {
    "enabled": False,      # `batteryguard on` is the only thing that sets it
    "pauseAt": 15,         # % battery -> pause the print
    "criticalAt": 7,       # % battery -> cancel + bring the stack down
    "pollSec": 30,         # how often the child reads the battery
    "atPause": "pause",    # pause | notify | ignore
    "atCritical": "cancel",  # cancel | pause | notify | ignore
    "resumeOnAc": False,   # OPT-IN: resume only what the guard itself paused
    "notify": True,        # osascript banner + /usr/bin/say + `say` fallback
    "dryRun": False,       # log what would happen, never touch the printer
}
AT_PAUSE = ("pause", "notify", "ignore")
AT_CRITICAL = ("cancel", "pause", "notify", "ignore")

# Two readings at or below a threshold before acting: the SMC jitters by a
# percent around a threshold, and a pause caused by one noisy sample is a print
# ruined for nothing (research: same reason the re-arm needs a margin).
LOW_STREAK = 2
# ... and the margin above pauseAt that re-arms the pause for a later print.
HYSTERESIS = 3
# "I cannot read the battery" is said out loud, once, after this many polls -
# and never acted on.
UNREADABLE_STREAK = 3

NO_PAUSE_RESUME = ("no [pause_resume] in printer.cfg - add it to get an "
                   "automatic pause")

_NO_BATTERY = object()
_NOTIFY_MAX = 120  # AppleScript source: keep the strings short and plain
_CMD_TIMEOUT = 5.0

# Absolute paths first, bare name second: a stripped launchd/CI PATH omits
# /usr/sbin, which is exactly where ioreg lives (same discipline as
# process._boot_time()'s /usr/sbin/sysctl then sysctl).
_PMSET = ("/usr/bin/pmset", "pmset")
_IOREG = ("/usr/sbin/ioreg", "ioreg")

_PM_SOURCE = re.compile(r"drawing from '([^']+)'", re.I)
_PM_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%;")
# ... and it is a whole number on purpose: "15.4%;" must read as 15, not
# as the 4 in the fraction, which would be a false critical and a cancelled
# print on a Mac that is at three quarters charged.
_PM_REMAIN = re.compile(r"(\d+:\d+)\s*remaining", re.I)
_PM_ABSENT = re.compile(r"present:\s*no", re.I)
_IO_NUM = r'"{}"\s*=\s*(-?\d+)'

_pause_unavailable = [None]  # cached probe result: True / False / None


# --------------------------------------------------------------------- files --
# Derived at call time, never cached in a module constant: `klipperformac data
# --set` rebinds paths.RUN/paths.LOGS at runtime, and a guard file that kept
# pointing at the old data folder would leave a watchdog nobody can stop.
def ledger_path():
    return paths.RUN / "batteryguard.json"


def episode_path():
    return paths.RUN / "batteryguard-state.json"


def log_path():
    return paths.LOGS / "batteryguard.log"


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _log(message):
    """One timestamped line in logs/batteryguard.log, best effort: a watchdog
    that cannot write its log still has to watch the battery."""
    try:
        log_path().parent.mkdir(parents=True, exist_ok=True)
        with open(log_path(), "a") as f:
            f.write("{} {}\n".format(_now(), message))
    except OSError:
        pass


def log_tail(count=4):
    """The last few log lines, bounded read (the file can grow big)."""
    try:
        with open(log_path(), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4096))
            text = f.read().decode(errors="replace")
    except OSError:
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return [line[:200] for line in lines[-count:]]


# ------------------------------------------------------------------ settings --
def _is_index(value):
    """A whole number that can index a percentage: int (not bool), or a string
    of digits. Floats are refused rather than rounded - a settings file saying
    15.7 is somebody's typo, and silently reading it as 15 or 16 changes when a
    print gets paused."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        return bool(re.fullmatch(r"[+-]?\d+", value.strip()))
    return False


def _clean_percent(value):
    """value -> 0..100 int, or None when it is not a battery level.

    The one guard against acting blind: None, -4, 101, NaN, infinity, True and
    "abc" all read as "no trustworthy number", and nothing downstream may treat
    them as "0 %" (which would cancel a print on a machine that merely lied).
    A float that is whole (15.0) is a number; a fractional one (12.5) is not,
    because a half percent is not what pmset reports and a reader that invented
    it is guessing. A numeric string is truncated ("15.4" -> 15), which is what
    the reader that printed it means."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value) or value != int(value):
            return None
        value = int(value)
    if isinstance(value, str):
        text = value.strip()
        # No sign is accepted in a *string* level either, and that is not
        # tidiness: "-0.5" would otherwise truncate to 0, and 0 % is a critical
        # - a lying reader could cancel a print and stop the stack on a Mac
        # that is nearly full. A battery level is never signed.
        if not re.fullmatch(r"\d+(\.\d+)?", text):
            return None
        value = int(float(text))
    if not isinstance(value, int):
        return None
    return value if 0 <= value <= 100 else None


# Public spelling, for the CLI and the GUI-facing helpers: same gate, no
# underscore, so "is this a battery level?" is asked in exactly one place.
clean_percent = _clean_percent


def _clean_bool(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("on", "true", "yes", "1"):
            return True
        if low in ("off", "false", "no", "0"):
            return False
    return default


def normalise(raw):
    """settings.json's "battery" block + DEFAULTS, always safe to use.

    A hostile stored value (pauseAt -4, criticalAt above pauseAt, atPause
    "reboot") is repaired here rather than trusted, because both the child and
    `status`/`check`/`doctor` read this. The read path never raises; the write
    path (validate) does."""
    cfg = dict(DEFAULTS)
    src = raw if isinstance(raw, dict) else {}
    for key in ("enabled", "resumeOnAc", "notify", "dryRun"):
        cfg[key] = _clean_bool(src.get(key), cfg[key])
    if _is_index(src.get("pauseAt")):
        value = int(str(src["pauseAt"]).strip())
        cfg["pauseAt"] = value if 0 <= value <= 100 else DEFAULTS["pauseAt"]
    if _is_index(src.get("criticalAt")):
        value = int(str(src["criticalAt"]).strip())
        # A criticalAt above pauseAt would make the PAUSE branch unreachable
        # (critical is tested first, and wins): that is a config doing nothing
        # while pretending to watch, so it falls back instead of being trusted.
        cfg["criticalAt"] = value if 0 <= value <= cfg["pauseAt"] \
            else DEFAULTS["criticalAt"]
    # ... and a low pauseAt needs a default criticalAt pulled under it too:
    # {"pauseAt": 3} must not read as "critical at 7, pause at 3", which is a
    # threshold a watchdog can never reach.
    cfg["criticalAt"] = min(cfg["criticalAt"], cfg["pauseAt"])
    if _is_index(src.get("pollSec")):
        value = int(str(src["pollSec"]).strip())
        cfg["pollSec"] = value if 5 <= value <= 3600 else DEFAULTS["pollSec"]
    if src.get("atPause") in AT_PAUSE:
        cfg["atPause"] = src["atPause"]
    if src.get("atCritical") in AT_CRITICAL:
        cfg["atCritical"] = src["atCritical"]
    return cfg


def battery_settings():
    """The guard's prefs from settings.json, merged over DEFAULTS."""
    raw = paths.load_settings().get("battery")
    return normalise(raw if isinstance(raw, dict) else {})


def validate(**kwargs):
    """The patched fields, checked. Raises ValueError with one honest line -
    `batteryguard set` turns that into exit 1 with nothing written."""
    out = {}
    for key, value in kwargs.items():
        if key in ("enabled", "resumeOnAc", "notify", "dryRun"):
            # No silent default: `--resume-on-ac maybe` is a typo, and a
            # watchdog that guessed "off" would be a surprised user at 6 %.
            parsed = _clean_bool(value, None)
            if parsed is None:
                raise ValueError("{}: say on or off (got {!r})".format(
                    key, value))
            out[key] = parsed
            continue
        if key == "atPause":
            if value not in AT_PAUSE:
                raise ValueError("atPause: use one of {} (got {!r})".format(
                    "|".join(AT_PAUSE), value))
            out[key] = value
            continue
        if key == "atCritical":
            if value not in AT_CRITICAL:
                raise ValueError("atCritical: use one of {} (got {!r})".format(
                    "|".join(AT_CRITICAL), value))
            out[key] = value
            continue
        if key == "pollSec":
            if not _is_index(value):
                raise ValueError("pollSec: needs a whole number of seconds "
                                 "(got {!r})".format(value))
            number = int(str(value).strip())
            if not 5 <= number <= 3600:
                raise ValueError("pollSec: 5-3600 (got {})".format(number))
            out[key] = number
            continue
        if key in ("pauseAt", "criticalAt"):
            if not _is_index(value):
                raise ValueError("{}: needs a whole battery percentage "
                                 "0-100 (got {!r})".format(key, value))
            number = int(str(value).strip())
            if not 0 <= number <= 100:
                raise ValueError("{}: 0-100 (got {})".format(key, number))
            out[key] = number
            continue
        raise ValueError("unknown battery setting {!r}".format(key))
    merged = battery_settings()
    merged.update(out)
    if merged["criticalAt"] > merged["pauseAt"]:
        raise ValueError("criticalAt {} is above pauseAt {}: the pause would "
                         "never happen. Lower criticalAt or raise pauseAt."
                         .format(merged["criticalAt"], merged["pauseAt"]))
    return out


def save_battery_settings(**kwargs):
    """Write-then-replace the "battery" block, keeping every other block
    (cam/lan/data) exactly as it is. Raises what paths.save_settings raises;
    the caller turns that into one clean line."""
    settings = paths._load_settings_for_update()
    if settings is paths._UNREADABLE_SETTINGS:
        raise OSError("could not read {} faithfully".format(paths.SETTINGS))
    block = settings.get("battery")
    block = dict(block) if isinstance(block, dict) else {}
    for key, value in kwargs.items():
        if value is None:
            block.pop(key, None)
        else:
            block[key] = value
    settings["battery"] = block
    paths.save_settings(settings)
    return battery_settings()


# -------------------------------------------------------------------- reading --
def _exec(argv, timeout=_CMD_TIMEOUT):
    """stdout of a read-only power command, "" when it said nothing, None when
    it could not be run at all (the same contract cli._timed_run() uses)."""
    try:
        done = subprocess.run(argv, capture_output=True, timeout=timeout)
    except Exception:
        return None
    if isinstance(done, dict):  # a test double
        return done.get("stdout")
    return _decode(done.stdout)


def _decode(data):
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode(errors="replace")
    return str(data)


def _first_ok(binaries, args, timeout=_CMD_TIMEOUT):
    for binary in binaries:
        out = _exec([binary] + list(args), timeout=timeout)
        if out:
            return out
    return ""


def parse_pmset(text):
    """`pmset -g batt` -> reading dict, or None when it says nothing useful.

    Wording varies with macOS version and with whether the battery is charging,
    so this only ever reads three things: which power it is drawing from, the
    percent, and the time remaining. A desktop says `present: no` - that is
    "this Mac has no battery", which is None too, never "0 %".
    """
    if not text:
        return None
    source = None
    m = _PM_SOURCE.search(text)
    if m:
        phrase = m.group(1).strip().lower()
        if "batter" in phrase:
            source = "battery"
        elif "ac" in phrase:
            source = "ac"
    percent = None
    m = _PM_PCT.search(text)
    if m:
        percent = _clean_percent(m.group(1))
    if source is None and percent is None:
        return None
    if _PM_ABSENT.search(text):
        # "present: no" is this Mac saying it has no battery at all. The 100%
        # beside it is a lie about a part that is not there: reading it as a
        # battery level would make `on` start a watchdog with something to
        # watch, and the honest answer is None.
        return None
    low = text.lower()
    charging = ("charging" in low and "discharging" not in low
                and "charged" not in low)
    remaining = _PM_REMAIN.search(text)
    return {"percent": percent, "source": source, "charging": charging,
            "remaining": remaining.group(1) if remaining else None,
            "engine": "pmset", "raw": " ".join(text.split())[:400]}


def parse_ioreg(text):
    """`ioreg -rn AppleSmartBattery` -> reading dict, or None.

    MaxCapacity 0/missing means the plist is not describing a battery, and
    dividing by it would be a crash, not an answer. ExternalConnected is the
    same key doctor reads (its wording does not vary the way pmset's does);
    without it the source is unknown, which is a refusal to act, not a guess.
    Time-remaining keys (AvgTimeToEmpty 65535, TimeRemaining 0) are deliberately
    not read at all: they are sentinels on AC and never a trigger."""
    if not text:
        return None

    def number(key):
        m = re.search(_IO_NUM.format(key), text)
        return int(m.group(1)) if m else None

    maximum = number("MaxCapacity")
    current = number("CurrentCapacity")
    if not maximum or maximum <= 0 or current is None or current < 0:
        return None
    m = re.search(r'"ExternalConnected"\s*=\s*(Yes|No)', text)
    if m is None:
        return None
    percent = _clean_percent(round(100.0 * current / maximum))
    charging = re.search(r'"IsCharging"\s*=\s*(Yes|No)', text)
    return {"percent": percent, "source": "ac" if m.group(1) == "Yes"
            else "battery",
            "charging": charging is not None and charging.group(1) == "Yes",
            "remaining": None, "engine": "ioreg",
            "raw": " ".join(text.split())[:400]}


def _reading_from_file(path):
    """The test/CI hook: a JSON file {"percent":12,"source":"battery",
    "charging":false}. Junk inside it is an unreadable battery, never a 0 %."""
    try:
        doc = json.loads(paths.read_config_text(path))
    except (OSError, ValueError, SystemExit):
        return {"percent": None, "source": "none", "charging": False,
                "remaining": None, "engine": "fixture", "raw": "unreadable"}
    if not isinstance(doc, dict):
        return {"percent": None, "source": "none", "charging": False,
                "remaining": None, "engine": "fixture", "raw": "not an object"}
    source = doc.get("source")
    source = source if source in ("battery", "ac") else "none"
    return {"percent": _clean_percent(doc.get("percent")), "source": source,
            "charging": _clean_bool(doc.get("charging"), source == "ac"),
            "remaining": None, "engine": "fixture",
            "raw": " ".join(json.dumps(doc).split())[:400]}


def read_power(env=None):
    """This Mac's power state, or None when there is no battery to watch.

    Priority, every one of them injectable so no test needs a discharging
    laptop: KLIPPERFORMAC_BATTERY_FILE (JSON fixture), then
    KLIPPERFORMAC_BATTERY_PERCENT + KLIPPERFORMAC_POWER_SOURCE, then the real
    readers pmset then ioreg. None -> `on` refuses and the child exits 0."""
    environ = os.environ if env is None else env
    path = (environ.get("KLIPPERFORMAC_BATTERY_FILE") or "").strip()
    if path:
        return _reading_from_file(path)
    raw = environ.get("KLIPPERFORMAC_BATTERY_PERCENT")
    if raw is not None and str(raw).strip():
        source = (environ.get("KLIPPERFORMAC_POWER_SOURCE")
                  or "battery").strip().lower()
        if source not in ("battery", "ac"):
            return {"percent": None, "source": "none", "charging": False,
                    "remaining": None, "engine": "env",
                    "raw": "unknown source {!r}".format(source)[:400]}
        return {"percent": _clean_percent(raw), "source": source,
                "charging": source == "ac", "remaining": None,
                "engine": "env", "raw": " ".join(str(raw).split())[:400]}
    reading = parse_pmset(_first_ok(_PMSET, ["-g", "batt"]))
    if reading is None:
        reading = parse_ioreg(_first_ok(_IOREG, ["-rn", "AppleSmartBattery"]))
    return reading


def synth_reading(percent=None, source=None, print_state=None):
    """A reading from parameters instead of the Mac - what `check --percent`
    and every offline test use, through the same _clean_percent gate."""
    clean = _clean_percent(percent)
    src = source if source in ("battery", "ac") else None
    reading = {"percent": clean, "source": src,
               "charging": src == "ac", "remaining": None,
               "engine": "param", "raw": "percent={} source={}".format(
                   percent, source)}
    if print_state is not None:
        reading["printState"] = print_state
    return reading


# ------------------------------------------------------------- the machine ---
def new_episode():
    """The guard's own runtime state: what it did, and the counters that stop it
    doing it twice."""
    return {"updatedAt": _now(), "pid": None, "percent": None, "source": None,
            "printState": None, "armed": True, "guardPaused": False,
            "criticalDone": False, "cancelAttempted": False, "lowStreak": 0,
            "critStreak": 0, "unreadableStreak": 0, "unreadableNotified":
            False, "polls": 0, "actions": [], "lastAction": None,
            "lastActionAt": None, "reason": None}


def evaluate(reading, episode=None, cfg=None):
    """(actions, episode) for one reading. Pure: no I/O, no clock, no REST.

    The rules, all of them tested:
      * on AC nothing is ever paused or cancelled;
      * a pause happens once per print (armed -> disarmed), after LOW_STREAK
        readings at or below pauseAt, and re-arms only above pauseAt+HYSTERESIS;
      * critical wins over pause and happens once (criticalDone), and it blocks
        any later pause in the same episode (a cancelled print is never
        "paused" afterwards);
      * resume is emitted only while guardPaused - the guard resumes its own
        pause and nothing a human paused - and only when resumeOnAc is on;
      * an untrustworthy percent (None/negative/junk) never triggers anything
        destructive; it only ever produces one notification after a streak.
    """
    cfg = normalise(cfg if isinstance(cfg, dict) else {})
    ep = new_episode()
    if isinstance(episode, dict):
        ep.update(episode)
    actions = []
    data = reading if isinstance(reading, dict) else {}
    percent = _clean_percent(data.get("percent"))
    source = data.get("source")
    source = source if source in ("battery", "ac") else None
    print_state = data.get("printState")
    if isinstance(print_state, str):
        print_state = print_state.strip().lower() or None
    else:
        print_state = None

    ep["percent"] = percent
    ep["source"] = source
    ep["printState"] = print_state
    ep["polls"] = int(ep.get("polls", 0)) + 1
    pause_at = cfg["pauseAt"]
    critical_at = min(cfg["criticalAt"], pause_at)

    if percent is None or source is None:
        # Never act blind: an unreadable battery is reported, not obeyed.
        ep["unreadableStreak"] = int(ep.get("unreadableStreak", 0)) + 1
        ep["lowStreak"] = 0
        ep["critStreak"] = 0
        if (ep["unreadableStreak"] >= UNREADABLE_STREAK
                and not ep.get("unreadableNotified")):
            ep["unreadableNotified"] = True
            actions.append("notify_unreadable")
        ep["reason"] = "battery level unreadable {} poll(s) in a row".format(
            ep["unreadableStreak"])
        return _finish(ep, actions, cfg)

    ep["unreadableStreak"] = 0
    ep["unreadableNotified"] = False

    if source == "ac":
        # Plugged in, the guard has no business touching the print: a low
        # percent on AC means charging, not dying (research: AvgTimeToEmpty is
        # a 65535 sentinel on AC, so time-remaining is never read either).
        if ep.get("guardPaused"):
            if cfg["resumeOnAc"] and print_state in (None, "paused"):
                actions.append("resume")
                ep["guardPaused"] = False
            elif print_state not in (None, "paused"):
                # Nobody we paused is waiting: the print finished or a human
                # resumed it, so no resume is owed.
                ep["guardPaused"] = False
            else:
                ep["reason"] = ("on AC: this print was paused by the guard, "
                                "and resume-on-AC is off (opt-in: "
                                "batteryguard set --resume-on-ac on)")
        if percent > pause_at + HYSTERESIS:
            ep["armed"] = True
            ep["criticalDone"] = False
            ep["lowStreak"] = 0
            ep["critStreak"] = 0
        if not actions and ep.get("reason") is None:
            ep["reason"] = "on AC: the guard never pauses or cancels a print " \
                           "that is plugged in"
        return _finish(ep, actions, cfg)

    # On battery from here.
    if percent <= critical_at:
        ep["critStreak"] = int(ep.get("critStreak", 0)) + 1
        ep["lowStreak"] = int(ep.get("lowStreak", 0)) + 1
    elif percent <= pause_at:
        ep["critStreak"] = 0
        ep["lowStreak"] = int(ep.get("lowStreak", 0)) + 1
    else:
        ep["lowStreak"] = 0
        ep["critStreak"] = 0
        if percent > pause_at + HYSTERESIS:
            ep["armed"] = True
            ep["criticalDone"] = False

    if percent <= critical_at:
        if ep.get("criticalDone"):
            ep["reason"] = "critical already handled in this episode"
        elif int(ep.get("critStreak", 0)) < LOW_STREAK:
            ep["reason"] = "critical battery at {}% ({} of {} readings in a " \
                           "row): acting on the {}".format(
                               percent, ep["critStreak"], LOW_STREAK,
                               LOW_STREAK)
        else:
            ep["criticalDone"] = True
            ep["armed"] = False
            ep["reason"] = None
            if cfg["atCritical"] == "cancel":
                ep["guardPaused"] = False  # cancelled: nothing to resume
                actions.extend(["notify_critical", "cancel", "down"])
            elif cfg["atCritical"] == "pause":
                ep["guardPaused"] = True
                actions.extend(["notify_critical", "pause"])
            elif cfg["atCritical"] == "notify":
                actions.append("notify_critical")
                ep["reason"] = "atCritical=notify: nothing sent to the printer"
            else:
                # "ignore", mirroring atPause: a critical battery may make
                # noise (the unreadable-battery channel above still speaks)
                # but never move this print or tear the stack down.
                ep["reason"] = "atCritical=ignore: nothing done"
    elif percent <= pause_at:
        if ep.get("criticalDone"):
            ep["reason"] = "critical already handled in this episode: a " \
                           "cancelled print is never paused again"
        elif int(ep.get("lowStreak", 0)) < LOW_STREAK:
            ep["reason"] = "low battery at {}% ({} of {} readings in a row): " \
                           "acting on the {}".format(
                               percent, ep["lowStreak"], LOW_STREAK, LOW_STREAK)
        elif not ep.get("armed", True):
            ep["reason"] = "not armed: this print was already handled by the " \
                           "guard; it re-arms above {}%".format(
                               pause_at + HYSTERESIS)
        elif print_state not in (None, "printing"):
            # Nothing ours to pause: idle, finished, halted - or paused by a
            # human, which is exactly the state the guard must not stamp on.
            # Staying armed is deliberate: a print started at 12 % is still
            # paused on the poll after it starts.
            ep["reason"] = "printer is {} - nothing for the guard to pause" \
                .format(print_state)
        else:
            ep["armed"] = False
            ep["reason"] = None
            if cfg["atPause"] == "pause":
                ep["guardPaused"] = True
                actions.extend(["notify_pause", "pause"])
            elif cfg["atPause"] == "notify":
                actions.append("notify_pause")
                ep["reason"] = "atPause=notify: nothing sent to the printer"
            else:
                ep["reason"] = "atPause=ignore: nothing done"
    elif ep.get("reason") is None:
        ep["reason"] = "nothing to do: {}% is above the thresholds (re-arm at " \
                       "{}%)".format(percent, pause_at + HYSTERESIS)
    return _finish(ep, actions, cfg)


def _finish(ep, actions, cfg):
    """Record the gated action list on the episode and hand back the pair."""
    gated = _notify_gate(actions, cfg)
    ep["actions"] = gated
    return gated, ep


def _notify_gate(actions, cfg):
    """notify=False keeps the printer actions and drops the noise. The episode
    file still records what happened, which is the channel that cannot be
    silenced."""
    if cfg.get("notify"):
        return actions
    return [a for a in actions if not a.startswith("notify")]


# --------------------------------------------------------------- Moonraker ---
def _url(path):
    return "http://localhost:{}{}".format(paths.MOONRAKER_PORT, path)


def _moonraker_get(path, timeout=3):
    """GET on our own API; None when it does not answer. Read-only."""
    try:
        with urllib.request.urlopen(_url(path), timeout=timeout) as r:
            return json.loads(r.read().decode(errors="replace"))
    except Exception:
        return None


def _error_reason(error):
    """Moonraker's own words from an HTTPError body: {"error":{"message":...}}.
    A refusal that says only "HTTP Error 400" sends people checking their
    network instead of their printer.cfg."""
    try:
        body = error.read().decode(errors="replace")
    except Exception:
        body = ""
    try:
        answer = json.loads(body)
        return ((answer.get("error") or {}).get("message")
                or json.dumps(answer)[:200])
    except (ValueError, AttributeError):
        return " ".join(body.split())[:300] or "(no reason given)"


def _moonraker_post(path, timeout=10):
    """POST with an empty body (the `cli.cmd_gcode` idiom). (ok, reason)."""
    req = urllib.request.Request(_url(path), method="POST", data=b"")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return True, None
    except urllib.error.HTTPError as e:
        return False, "HTTP {}: {}".format(e.code, _error_reason(e))
    except Exception as e:
        return False, "Moonraker not reachable: {}".format(e)


def print_state(timeout=3):
    """print_stats.state ('printing', 'paused', 'standby'...), or None when the
    stack does not answer (unknown is never read as 'not printing')."""
    answer = _moonraker_get("/printer/objects/query?print_stats", timeout)
    try:
        state = answer["result"]["status"]["print_stats"]["state"]
    except (TypeError, KeyError, AttributeError):
        return None
    return state if isinstance(state, str) else None


def has_pause_resume(refresh=False):
    """True/False/None: does this printer even have PAUSE/RESUME/CANCEL?

    Four of the shipped presets have no [pause_resume], and Moonraker's
    /printer/print/pause then refuses them. The answer is cached for the
    process (the child's lifetime) because the config cannot change under a
    running stack; None means Moonraker did not answer, which is NOT a licence
    to skip the pause - only a proven absence is."""
    if _pause_unavailable[0] is not None and not refresh:
        return _pause_unavailable[0]
    answer = _moonraker_get("/printer/objects/list")
    try:
        objects = answer["result"]["objects"]
    except (TypeError, KeyError, AttributeError):
        return None
    if not isinstance(objects, list):
        return None
    found = "pause_resume" in objects
    _pause_unavailable[0] = found
    return found


def _wait_not_printing(limit=10.0):
    """Let a cancel land before the stack goes down: klipper mid-command is not
    a place to pull the plug. Bounded, and None (unreachable) proceeds."""
    deadline = time.time() + limit
    while time.time() < deadline:
        state = print_state(timeout=2)
        if state is None:
            return None
        if state != "printing":
            return state
        time.sleep(0.5)
    return print_state(timeout=2)


# ------------------------------------------------------------------- notify --
def _as_text(value, limit=_NOTIFY_MAX):
    """AppleScript-safe: the strings below are interpolated into a script
    source, so quotes, newlines and control characters are dropped and the
    text is truncated."""
    text = re.sub(r"[\"'\\\r\n\t\x00-\x1f\x7f]", " ", str(value))
    text = " ".join(text.split())
    return text[:limit]


def _binary(names):
    for name in names:
        if os.path.exists(name):
            return name
    return os.path.basename(names[-1])


def _spawn(argv):
    """Run a notify helper (osascript/say) and swallow everything. A seam on
    purpose: the offline tests record these calls instead of making noise on
    the developer's Mac, and a hung notification tool can only ever cost
    _CMD_TIMEOUT seconds of a watchdog that is trying to save a print."""
    try:
        subprocess.run(argv, capture_output=True, timeout=_CMD_TIMEOUT)
        return True
    except Exception as e:
        _log("notify channel {} failed: {}".format(argv[0], e))
        return False


def notify(body, subtitle="battery guard", title="Klipper for Mac", dry=False):
    """The desktop banner plus /usr/bin/say.

    Both channels, because the detached child's osascript banner has no app
    identity and Focus/notification permissions can swallow it; `say` is the
    channel that survives. Every part is best effort: a watchdog that cannot
    make noise still watches the battery, and nothing here may raise."""
    if dry:
        _log("would notify: {}".format(body))
        return
    script = ("display notification \"{}\" with title \"{}\" subtitle \"{}\" "
              "sound name \"Glass\"".format(_as_text(body), _as_text(title),
                                            _as_text(subtitle)))
    _spawn([_binary(("/usr/bin/osascript", "osascript")), "-e", script])
    _spawn([_binary(("/usr/bin/say", "say")), _as_text(body)])


# ----------------------------------------------------------------- actions ---
def _cli_lock_nb():
    """The CLI lock, taken without waiting (3 attempts, then try next poll).

    cli._cli_lock() blocks indefinitely behind another command, and a watchdog
    that queues behind a `setup` is a watchdog that pauses a print at the wrong
    moment - so the guard asks, and if the answer is no it comes back later."""
    for _ in range(3):
        try:
            paths.RUN.mkdir(parents=True, exist_ok=True)
            fh = open(paths.CLI_LOCK, "a+")
        except OSError:
            return None
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            time.sleep(0.2)
            continue
        return fh
    return None


def _stop_the_stack():
    """process.stop() under the CLI lock, or 'busy, retry next poll'.

    Safe for the caller: process.stop() never signals the guard itself
    (_service_kind() does not recognise this argv, and _scan_service_pids()
    skips os.getpid()), so the child can bring the stack down around itself."""
    handle = _cli_lock_nb()
    if handle is None:
        _log("down refused: another klipperformac command holds the lock; "
             "retrying on the next poll")
        return False
    try:
        process.stop()
    except Exception as e:
        _log("down failed: {}".format(e))
        return False
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()
    _log("stack stopped by the battery guard")
    return True


def adjust_for_capability(actions, episode):
    """A pause this printer cannot obey is replaced by one honest message.

    The order matters: this runs BEFORE the notifications, because
    "Print paused at 12 %" would be a lie on a printer.cfg with no
    [pause_resume] - and four of the shipped presets have none. Unknown (None)
    is deliberately left alone: Moonraker not answering is not evidence the
    section is missing, and the printer being unpausable must not be decided by
    a timeout."""
    if "pause" not in actions or has_pause_resume() is not False:
        return list(actions), episode
    episode["reason"] = NO_PAUSE_RESUME
    out = ["notify_no_pause" if a in ("pause", "notify_pause",
                                     "notify_critical") else a
           for a in actions if a not in ("notify_pause", "notify_critical")]
    if "notify_no_pause" not in out:
        out.insert(0, "notify_no_pause")
    return out, episode


def act(action, cfg, episode, dry=False):
    """One action, best effort, logged, never raising. Returns True/False (or
    None for "nothing to do"), and records the episode fields as it goes."""
    dry = bool(dry or cfg.get("dryRun"))
    percent = episode.get("percent")
    if action == "notify_pause":
        notify("Print paused at {}% battery. Heaters stay on for 10 minutes, "
               "then Klipper's idle timeout turns them off.".format(percent),
               subtitle="low battery", dry=dry)
        return None
    if action == "notify_critical":
        notify("Critical battery at {}%: print cancelled and the Klipper stack "
               "stopped. Plug in before printing again.".format(percent),
               subtitle="battery critical", dry=dry)
        return None
    if action == "notify_unreadable":
        notify("Battery level has not been readable for {} polls; the guard "
               "has not paused your print. Check pmset/ioreg, or plug in."
               .format(UNREADABLE_STREAK), subtitle="battery unknown",
               dry=dry)
        return None
    if dry:
        label = {"pause": "would pause", "resume": "would resume",
                 "cancel": "would cancel",
                 "down": "would stop the stack"}[action]
        _log("{} ({}% battery)".format(label, percent))
        return None
    if action == "notify_no_pause":
        notify("Print NOT paused at {}% battery: this printer.cfg has no "
               "[pause_resume], so the guard cannot pause it. Pause it "
               "yourself, or add the section.".format(percent),
               subtitle="no [pause_resume]", dry=dry)
        return None
    if action == "pause":
        if has_pause_resume() is False:
            episode["reason"] = NO_PAUSE_RESUME
            _log("pause skipped: " + episode["reason"])
            notify("Print not paused by the guard at {}% battery: printer.cfg "
                   "has no [pause_resume]. Pause it yourself.".format(percent),
                   subtitle="battery guard", dry=dry)
            return False
        ok, reason = _moonraker_post("/printer/print/pause")
        _log("pause: {}".format("ok" if ok else reason))
        if ok:
            episode["lastAction"] = "pause"
            episode["lastActionAt"] = _now()
        else:
            episode["reason"] = "pause refused: {}".format(reason)
        return ok
    if action == "resume":
        ok, reason = _moonraker_post("/printer/print/resume")
        _log("resume: {}".format("ok" if ok else reason))
        if ok:
            episode["lastAction"] = "resume"
            episode["lastActionAt"] = _now()
            episode["armed"] = True
        else:
            episode["reason"] = "resume refused: {}".format(reason)
        return ok
    if action == "cancel":
        episode["cancelAttempted"] = True
        ok, reason = _moonraker_post("/printer/print/cancel")
        _log("cancel: {}".format("ok" if ok else reason))
        if ok:
            episode["lastAction"] = "cancel"
            episode["lastActionAt"] = _now()
            state = _wait_not_printing()
            if state == "printing":
                episode["reason"] = ("cancel did not land within 10s (printer "
                                     "still printing)")
        else:
            episode["reason"] = "cancel refused: {}".format(reason)
        return ok
    if action == "down":
        episode["lastAction"] = ("cancel+down"
                                 if episode.get("lastAction") == "cancel"
                                 else "down")
        episode["lastActionAt"] = _now()
        return _stop_the_stack() is True
    _log("unknown action {} (ignored)".format(action))
    return False


def run_actions(actions, cfg, episode, dry=False):
    """Run a batch in order, keeping the one promise the spec is strict about:
    the stack only comes down once the cancel in front of it has been asked for
    - and, off a dry run, has actually landed (or been refused, which still
    means the print is not worth keeping: a dying battery is not negotiated
    with)."""
    out = []
    for index, action in enumerate(actions):
        if action == "down":
            asked = any(a in ("cancel", "pause") for a in actions[:index])
            landed = episode.get("lastAction") in ("cancel", "pause")
            if dry:
                if not (asked or landed):
                    _log("would not stop the stack: no cancel in this batch")
                    continue
            elif not (landed or asked or episode.get("cancelAttempted")):
                _log("down skipped: no cancel in this episode")
                continue
        out.append((action, act(action, cfg, episode, dry=dry)))
    return out


# ----------------------------------------------------------- episode on disk --
def load_episode():
    try:
        doc = json.loads(paths.read_config_text(episode_path()))
    except (OSError, ValueError, SystemExit):
        return None
    return doc if isinstance(doc, dict) else None


def save_episode(episode):
    """Write-then-replace (paths.write_config), so a crash mid-write leaves the
    previous state, not a truncated file `status` then mis-reads."""
    episode["updatedAt"] = _now()
    episode["pid"] = os.getpid()
    try:
        episode_path().parent.mkdir(parents=True, exist_ok=True)
        paths.write_config(episode_path(), json.dumps(episode, indent=2) + "\n")
        return True
    except (OSError, TypeError, ValueError) as e:
        _log("could not write {}: {}".format(episode_path(), e))
        return False


def _clear_episode():
    try:
        episode_path().unlink()
    except OSError:
        pass


# ----------------------------------------------------------------- lifecycle --
def _alive(pid):
    return bool(process._live_cmds([pid]).get(str(pid)))


def running():
    """The live guard's episode dict (with "pid"), or None.

    A ledger we do not own (another boot, another KLIPPERFORMAC_HOME) or a pid
    whose command line is not our guard's exact argv means "off", and the state
    files go with it - the same self-healing cam.running()/caffeinate discipline
    that keeps `down` from ever reporting a guard it cannot stop."""
    pids = process.live_guard_pids()
    if not pids:
        # Nothing ours is running: drop OUR record and the state file so the
        # next start() (and `status`) cannot read a dead guard as alive. A
        # ledger naming another app home is left alone - it is not ours.
        if process.guard_record_ours():
            process.clear_guard_record()
        _clear_episode()
        return None
    episode = load_episode() or new_episode()
    episode["pid"] = pids[-1]
    episode["pids"] = pids
    return episode


def poll_once(cfg=None, reading=None, dry=None):
    """One poll: read -> evaluate -> act -> save. The child's loop body.

    The printer's state is read before deciding (a print that is not printing
    needs no pause, and the guard says so), but never when the answer cannot
    reach the printer anyway: an unknown state is not read as 'idle', so a
    Moonraker that is slow to answer cannot talk the guard out of its job."""
    cfg = battery_settings() if cfg is None else normalise(dict(cfg))
    if dry is None:
        dry = bool(cfg.get("dryRun"))
    if reading is None:
        reading = read_power()
    if reading is None:
        _log("no battery on this Mac: nothing to watch")
        return [], None
    if reading.get("printState") is None:
        state = print_state()  # a GET: also what a dry run judges against
        if state is not None:
            reading = dict(reading)
            reading["printState"] = state
    actions, episode = evaluate(reading, load_episode(), cfg)
    actions, episode = adjust_for_capability(actions, episode)
    if actions:
        run_actions(list(actions), cfg, episode, dry=dry)
    save_episode(episode)
    return actions, episode


def start(cfg=None):
    """Turn the guard on: spawn the tracked child. (ok, lines), the cam.start()
    shape the CLI prints verbatim."""
    live = running()
    if live is not None:
        return True, ["Battery guard already on (pid {}).".format(
            live.get("pid")),
            "Turn it off with: klipperformac batteryguard off"]
    cfg = battery_settings() if cfg is None else normalise(dict(cfg))
    reading = read_power()
    if reading is None:
        return False, ["This Mac has no battery, so there is nothing for the "
                       "guard to watch: it was not started."]
    interval = cfg["pollSec"]
    try:
        log_path().parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path(), "ab")
    except OSError as e:
        return False, ["Could not write {} : {}".format(
            log_path(), getattr(e, "strerror", None) or e)]
    try:
        pid, err = process.launch_guard(interval, log_file)
    finally:
        # Popen dup's the descriptors; our own handle is done once it has them.
        log_file.close()
    if pid is None:
        return False, ["Battery guard not started: {}".format(err)]
    time.sleep(0.6)
    if not _alive(pid):
        process.clear_guard_record()
        _clear_episode()
        return False, ["Battery guard stopped as soon as it started."] + [
            "  " + line for line in log_tail()] + [
            "Read the whole log: {}".format(log_path())]
    save_episode(new_episode())
    lines = ["Battery guard on (pid {}): pauses the print at {}% battery, "
             "cancels and stops the stack at {}%.".format(
                 pid, cfg["pauseAt"], cfg["criticalAt"]),
             "  Polling every {}s, on battery only.".format(interval)]
    if cfg["resumeOnAc"]:
        lines.append("  Resume when the cable comes back: ON (only a print "
                     "this guard paused).")
    else:
        lines.append("  Resume when the cable comes back: off (opt-in: "
                     "klipperformac batteryguard set --resume-on-ac on)")
    if cfg["dryRun"]:
        lines.append("  DRY RUN: the guard logs what it would do and sends "
                     "nothing to the printer.")
    if reading.get("percent") is None and reading.get("source") != "ac":
        lines.append("  [!] It could not read the battery just now ({}): the "
                     "guard notifies after {} unreadable polls and never acts "
                     "blind.".format(reading.get("engine"),
                                     UNREADABLE_STREAK))
    lines.append("  Lid close still sleeps this Mac: the guard cannot prevent "
                 "clamshell sleep - it only ends the print before the battery "
                 "does.")
    lines.append("  Turn it off: klipperformac batteryguard off (also done by "
                 "`klipperformac down`)")
    return True, lines


def stop(timeout=3.0):
    """Turn the guard off: reap the tracked child, drop ledger and episode.
    (ok, lines). Idempotent: off twice is still off."""
    pids = process.live_guard_pids()
    if not pids:
        if process.guard_record_ours():
            process.clear_guard_record()
        _clear_episode()
        return False, ["Battery guard was not on."]
    stopped = process.reap_guard(pids, timeout=timeout)
    still = [pid for pid in stopped if _alive(pid)]
    if still:
        return False, ["[!] The battery guard (pid {}) refused to die. Force "
                       "it with: klipperformac killall".format(
                           ", ".join(str(p) for p in still))]
    process.clear_guard_record()
    _clear_episode()
    return True, ["Battery guard off: nothing is watching the battery "
                  "(settings kept - the guard comes back with `up` when "
                  "enabled)."]


def kill_orphans():
    """(name, pid) list for `killall`/`uninstall`: every guard of THIS install,
    in the ledger or not. The ledger is the fast path; when it was deleted by
    hand the argv scan is the only thing between a watchdog and immortality -
    and it returns only command lines is_guard_cmd() vouches for, never
    another install's identically-shaped argv. Idempotent, clears the state
    files, and reaps children we spawned so a killed guard is really gone
    (a zombie still answers kill(pid, 0))."""
    pids = process.live_guard_pids()
    pids += [pid for pid in process.scan_guard_orphans() if pid not in pids]
    for pid in pids:
        process._signal(pid, signal.SIGKILL)
    deadline = time.time() + 2.0
    live = [pid for pid in pids if _alive(pid)]
    while live and time.time() < deadline:
        time.sleep(0.1)
        live = [pid for pid in pids if _alive(pid)]
    process.reap_children(pids)
    if process.guard_record_ours():
        process.clear_guard_record()
    _clear_episode()
    return [("batteryguard", pid) for pid in pids]


def status_lines(cfg=None):
    cfg = battery_settings() if cfg is None else normalise(dict(cfg))
    live = running()
    lines = []
    if live is None:
        lines.append("Battery guard: {}".format(
            "on (but not running: it will start with the stack)"
            if cfg["enabled"] else "off"))
    else:
        lines.append("Battery guard: on (pid {})".format(
            ", ".join(str(p) for p in live.get("pids", [live.get("pid")])
                      if p) or "?"))
        lines.append("  last poll      {} at {}% ({})".format(
            live.get("updatedAt"), live.get("percent"), live.get("source")))
        lines.append("  print state    {}".format(live.get("printState")
                                                  or "unknown"))
        lines.append("  this print     {}".format(
            "paused by the guard" if live.get("guardPaused") else
            ("critical already handled" if live.get("criticalDone") else
             ("armed to pause" if live.get("armed") else "disarmed"))))
        if live.get("lastAction"):
            lines.append("  last action    {} at {}".format(
                live["lastAction"], live.get("lastActionAt")))
        if live.get("reason"):
            lines.append("  [!] " + str(live["reason"]))
    lines.append("  thresholds     pause {}%  critical {}%  poll {}s".format(
        cfg["pauseAt"], cfg["criticalAt"], cfg["pollSec"]))
    lines.append("  at pause       {}   at critical {}   resume on AC {}".format(
        cfg["atPause"], cfg["atCritical"],
        "on (opt-in)" if cfg["resumeOnAc"] else "off"))
    if cfg["dryRun"]:
        lines.append("  DRY RUN: nothing will be sent to the printer.")
    reading = read_power()
    if reading is None:
        lines.append("  battery        none: this Mac has no battery")
    else:
        lines.append("  battery        {}{} ({}, engine {})".format(
            "{}%".format(reading["percent"]) if reading["percent"] is not None
            else "level unreadable",
            " on {}".format(reading["source"]) if reading["source"] else "",
            "charging" if reading["charging"] else "not charging",
            reading["engine"]))
        if reading["source"] == "battery":
            lines.append("  [!] On battery: the guard pauses the print but "
                         "cannot stop lid-close sleep, and heaters keep "
                         "drawing until Klipper's idle timeout (10 min) "
                         "switches them off.")
    if cfg["enabled"] and live is None:
        lines.append("  (the guard runs while the stack runs: klipperformac "
                     "up starts it, klipperformac down stops it)")
    lines.append("  settings       {}".format(paths.SETTINGS))
    lines.append("  log            {}".format(log_path()))
    for line in log_tail(2):
        lines.append("  log tail       " + line)
    return lines


def check(percent=None, source=None, dry=True, cfg=None, show=True):
    """One-shot, no child, no state write: read (or take the injected percent),
    look at the real printer, run evaluate() against the on-disk episode and
    say what it would do. `status` uses it, the GUI's "Test now" button calls
    it, and it is the only way to prove the thresholds without discharging a
    laptop. Read-only against Moonraker unless dry=False."""
    cfg = battery_settings() if cfg is None else normalise(dict(cfg))
    if percent is None:
        reading = read_power()
    else:
        reading = synth_reading(percent, source)
    if reading is None:
        lines = ["This Mac has no battery: the guard has nothing to watch, so "
                 "it does nothing."]
        if show:
            print("\n".join(lines))
        return True, lines, None
    reading = dict(reading)
    state = print_state()
    if state is not None:
        reading["printState"] = state
    episode = load_episode()
    if percent is not None:
        # A one-shot is not a tick in a series. The two-reading rule exists to
        # stop the *sampling* from flapping on SMC jitter; someone who types
        # `check --percent 12` has already decided what the level is, so the
        # streak counters are pre-filled and the answer is the decision, not a
        # "waiting for the second reading". Nothing else is bypassed: the
        # armed/guardPaused/criticalDone history is read from disk exactly as
        # the running guard would read it.
        probe = dict(episode) if isinstance(episode, dict) else {}
        probe["lowStreak"] = LOW_STREAK
        probe["critStreak"] = LOW_STREAK
        episode = probe
    actions, episode = evaluate(reading, episode, cfg)
    actions, episode = adjust_for_capability(actions, episode)
    would = []
    for action in actions:
        label = {"notify_pause": "notify (pause)",
                 "notify_no_pause": "notify only (no [pause_resume])",
                 "notify_critical": "notify (critical)",
                 "notify_unreadable": "notify (battery unreadable)",
                 "pause": "pause the print", "resume": "resume the print",
                 "cancel": "cancel the print",
                 "down": "stop the stack (graceful)"}[action]
        would.append("would " + label if dry else label)
    lines = ["battery: {}% on {} (engine {})".format(
        "{}".format(reading.get("percent"))
        if reading.get("percent") is not None else "unreadable",
        reading.get("source") or "unknown", reading.get("engine"))]
    lines.append("printer: {}{}".format(
        state or "state unknown (Moonraker did not answer)",
        "" if has_pause_resume() is not False else
        " - printer.cfg has no [pause_resume]"))
    lines.append("thresholds: pause at {}%, critical at {}% (poll {}s)".format(
        cfg["pauseAt"], cfg["criticalAt"], cfg["pollSec"]))
    if not actions:
        reason = episode.get("reason") or (
            "nothing to do: above the thresholds" if reading.get("percent")
            is not None else "battery level not trustworthy, never acted on")
        lines.append("would do nothing: " + reason)
    else:
        lines.append("{}: {}".format("would do" if dry else "did",
                                     ", ".join(would)))
    for action in actions:
        if action == "notify_no_pause":
            lines.append("  [!] would notify only: printer.cfg has no "
                         "[pause_resume] - add it to get an automatic pause")
    if dry:
        run_actions(list(actions), cfg, dict(episode), dry=True)
    else:
        run_actions(list(actions), cfg, episode, dry=False)
        save_episode(episode)
    if show:
        print("\n".join(lines))
    return True, lines, episode


def doctor(cfg=None):
    """[(ok, msg)] for `klipperformac doctor`. Never raises.

    The guard's own questions: is there a battery, what level is it at, is the
    watchdog on, are its thresholds reachable, and can this printer be paused
    at all. `klipperformac timing` keeps answering the scheduling/thermal ones
    from _timing_report(), so the two commands read the same machine without
    duplicating each other's verdict."""
    out = []
    cfg = battery_settings() if cfg is None else normalise(dict(cfg))
    reading = None
    try:
        reading = read_power()
    except Exception as e:  # a Mac without pmset must not stop doctor
        out.append((False, "battery: could not be read ({})".format(e)))
    if reading is None:
        out.append((True, "battery: none detected - the guard has nothing to "
                         "watch here and refuses to start"))
    else:
        percent = reading.get("percent")
        out.append((percent is not None, "battery: {}{} (engine {})".format(
            "{}%".format(percent) if percent is not None else "unreadable",
            " on {}".format(reading["source"]) if reading["source"] else "",
            reading["engine"])))
        if reading["source"] == "battery":
            out.append((True, "battery: the guard pauses the print, but "
                        "nothing can stop lid-close sleep - a closed lid "
                        "sleeps this Mac whatever the percent. Plug in for a "
                        "long print"))
    try:
        out.append((True, "battery guard: {}".format(
            "on (pid {})".format(", ".join(str(p) for p in
                                           process.live_guard_pids()))
            if running() is not None else
            ("enabled, starts with the stack" if cfg["enabled"] else "off"))))
    except Exception as e:
        out.append((False, "battery guard: state unknown ({})".format(e)))
    sane = 0 <= cfg["criticalAt"] <= cfg["pauseAt"] <= 100 \
        and 5 <= cfg["pollSec"] <= 3600
    out.append((sane, "battery thresholds: pause {}%, critical {}%, poll {}s".format(
        cfg["pauseAt"], cfg["criticalAt"], cfg["pollSec"])))
    if has_pause_resume() is False:
        out.append((False, "battery: printer.cfg has no [pause_resume] - the "
                    "guard can only notify; add the section to get an "
                    "automatic pause"))
    elif has_pause_resume() is True:
        out.append((True, "battery: Moonraker answers pause/resume/cancel"))
    return out


# -------------------------------------------------------------------- child ---
_STOP = [False]


def _on_signal(signum, frame):
    _STOP[0] = True


def poll_interval(cfg=None, environ=None):
    """Seconds between polls for the child.

    Floor 2 s, because a watchdog that spins burns the CPU it is trying to
    leave enough power for. KLIPPERFORMAC_BATTERY_POLL may go below the floor
    only when KLIPPERFORMAC_HOME names a throwaway app home - i.e. a test -
    so a stray env var in a user's shell cannot make the guard hammer pmset."""
    cfg = battery_settings() if cfg is None else cfg
    env = os.environ if environ is None else environ
    value = cfg["pollSec"]
    override = (env.get("KLIPPERFORMAC_BATTERY_POLL") or "").strip()
    if override:
        try:
            candidate = float(override)
        except ValueError:
            candidate = None
        if candidate is not None and not math.isnan(candidate) \
                and candidate > 0:
            floor = 2.0
            if env.get("KLIPPERFORMAC_HOME") and candidate < floor:
                floor = candidate
            return max(candidate, floor)
    return max(float(value), 2.0)


def run_child(interval=None, dry=None, cfg=None, limit=None):
    """The guard's loop. Returns the exit code; SIGTERM exits 0 cleanly, a Mac
    with no battery exits 0 immediately (nothing to watch), and no iteration is
    ever allowed to raise - a watchdog that dies at 3 % is worse than none."""
    cfg = battery_settings() if cfg is None else normalise(dict(cfg))
    if interval is None:
        interval = poll_interval(cfg)
    if dry is None:
        dry = bool(cfg["dryRun"])
    others = [pid for pid in process.live_guard_pids() if pid != os.getpid()]
    if others:
        _log("another guard is already running (pid {}): exiting".format(
            ", ".join(str(p) for p in others)))
        return 0
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    save_episode(new_episode())
    _log("guard started (pid {}, pause {}%, critical {}%, poll {}s{})".format(
        os.getpid(), cfg["pauseAt"], cfg["criticalAt"], interval,
        ", dry run" if dry else ""))
    polls = 0
    try:
        while not _STOP[0]:
            try:
                actions, episode = poll_once(cfg, dry=dry)
                if episode is None:
                    return 0  # no battery on this Mac: nothing to watch
            except Exception as e:
                _log("poll failed: {}".format(e))
            polls += 1
            if limit is not None and polls >= limit:
                return 0
            deadline = time.time() + max(float(interval), 0.05)
            while not _STOP[0] and time.time() < deadline:
                time.sleep(0.1)
    finally:
        _log("guard exiting after {} poll(s)".format(polls))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv[0] if argv else "run"
    if action != "run":
        print("usage: python -m klipperformac.batteryguard run "
              "[--interval SECONDS] [--dry-run] [--polls N]")
        return 2
    interval = None
    dry = None
    limit = None
    rest = argv[1:]
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--interval":
            index += 1
            try:
                interval = max(float(rest[index]), 0.05)
            except (IndexError, ValueError):
                print("batteryguard run: --interval needs a number")
                return 2
        elif token == "--dry-run":
            dry = True
        elif token == "--polls":
            index += 1
            try:
                limit = int(rest[index])
            except (IndexError, ValueError):
                print("batteryguard run: --polls needs a whole number")
                return 2
        else:
            print("batteryguard run: ignoring unknown argument {!r}".format(
                token))
        index += 1
    return run_child(interval=interval, dry=dry, limit=limit)


if __name__ == "__main__":
    # `python -m klipperformac.batteryguard run ...` is the argv the ledger
    # identifies this child by (process.GUARD_ARGV), so the module has to be
    # runnable as a script: imported anywhere else it is only definitions.
    sys.exit(main())
