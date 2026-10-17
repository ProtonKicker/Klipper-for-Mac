"""Process lifecycle for the single Klipper instance (klipper -> moonraker -> UIs).

Modeled on the startup sequence of the `instances` app, adapted for macOS:
  * c_helper.so compiles at first launch with CPATH pointed at our compat
    shims; upstream Klipper sources stay untouched.
  * Process identity is re-validated on every read (pid + cmdline marker +
    boot time), so stale state files after crashes/reboots are tolerated;
    a fully-dead state self-heals on next start().
  * Children get their own sessions (start_new_session) so closing the
    terminal cannot kill a running print, and caffeinate guards sleep.
  * start()/stop()/kill_all() also write the power-loss markers
    (klipperformac/powerloss.py): an open session file while nothing runs is how
    a crash or a power cut is recognised at the next read. They live in
    data/powerloss/ and NOT here, because _load() deletes run/state.json the
    moment kern.boottime moves - evidence that erases itself cannot testify.
"""
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time

from . import paths, powerloss, shaper
from .compat import (QOS_ENV_VALUE, QOS_ENV_VAR, build_env,
                     ensure_runtime_shims, invalidate_stale_build, shim_env)

SERVICES = ("web_mainsail", "web_fluidd", "moonraker", "klipper", "caffeinate")
# The ports the stack serves on: `up` refuses to start a stack a squatter would
# half-blind (klipper+moonraker spawn, HTTP never answers, `status` lies).
STACK_PORTS = ("moonraker", "web_mainsail", "web_fluidd")
MARKERS = {
    "klipper": "klippy.py",
    "moonraker": "moonraker",
    "web_mainsail": "klipperformac.proxy {} ".format(paths.WEB_PORT),
    "web_fluidd": "klipperformac.proxy {} ".format(paths.FLUIDD_PORT),
    "caffeinate": "caffeinate",
}
_STOP_ORDER = ("web_mainsail", "web_fluidd", "moonraker", "klipper", "caffeinate")

# macOS pids stop at 99999: `ps -p 100000` refuses the query, and one refused
# token empties the WHOLE batched answer, so every pid read back from
# run/state.json is range-checked before it reaches ps or os.kill. That file is
# plain JSON a human, the SwiftUI reader or a half-finished write may have left
# behind, and pid <= 0 addresses a process *group* rather than a process.
_MAX_PID = 99999

# Written as boot_time when kern.boottime could not be read at save time.
# _load() reads it as "boot unverified" instead of "foreign boot", so a record
# of a live stack is never thrown away; identity then rests on the pid +
# cmdline markers, which is what running()/stop() check anyway.
_BOOT_UNKNOWN = "unknown"

# This install's own folders: _under_install() reads them from paths on every
# scan, because `klipperformac data --set` rebinds paths.DATA at runtime and a
# cached prefix would hide this install's own strays. A service command line
# has to carry a path of OURS: another account's ~/.klipperformac, or a beta
# copy next to it, is not this instance's to reap.

_boot_cache = [0.0, None]  # [monotonic, value]; sysctl is slow per keystroke


def _boot_time():
    now = time.monotonic()
    if now - _boot_cache[0] < 5.0:
        return _boot_cache[1]
    out = None
    argv = ("-n", "kern.boottime")
    # Absolute first: macOS keeps sysctl in /usr/sbin, which stripped-PATH
    # environments (launchd agents, cron, CI) often omit; plain name as a
    # portable fallback.
    for binary in ("/usr/sbin/sysctl", "sysctl"):
        try:
            out = subprocess.run([binary] + list(argv), capture_output=True,
                                 check=True).stdout.decode()
            break
        except Exception:
            continue
    if out is None:
        bt = None
    else:
        # \b anchors the field: without it the pattern also fires inside
        # "usec = ", which would hand microseconds back as a boot time.
        m = re.search(r"\bsec = (\d+)", out)
        bt = m.group(1) if m else None
    _boot_cache[0] = now
    _boot_cache[1] = bt
    return bt


def _load():
    """The recorded stack, or None when nothing usable is known.

    None means "unknown" as often as "not running", so a read that cannot prove
    the file stale must not delete it: that record is the only thing down/
    killall know about a printing stack, and without it `up` reaps it.
    """
    if not paths.STATE.exists():
        return None
    try:
        with open(paths.STATE) as f:
            state = json.load(f)
    except Exception:
        return None
    if not isinstance(state, dict):
        return None  # a bare list/number/string documents no stack
    recorded = state.get("boot_time")
    if recorded == _BOOT_UNKNOWN:
        return state  # boot time unverified when saved; markers still vouch
    bt = _boot_time()
    if bt is None:
        return None  # cannot tell: leave the file for the next read
    if recorded != bt:
        _clear()  # a document from another boot is stale by definition
        return None
    return state


def _clear():
    try:
        paths.STATE.unlink()
    except OSError:
        pass


def _previous_record(boot_time):
    """The document already on disk, as far as it belongs to this boot.

    Only a dict whose boot_time is the one being written down (or one saved
    while kern.boottime was unreadable) is carried over; corrupt content or a
    record from another boot is replaced, never inherited.
    """
    try:
        with open(paths.STATE) as f:
            state = json.load(f)
    except Exception:
        return {}
    if not isinstance(state, dict):
        return {}
    if state.get("boot_time") not in (boot_time, _BOOT_UNKNOWN):
        return {}
    return state


def _save(procs):
    paths.RUN.mkdir(parents=True, exist_ok=True)
    bt = _boot_time()
    boot = bt if bt is not None else _BOOT_UNKNOWN
    # start() writes the record twice (a pid-less one before the children
    # exist, the full one after), so what is already recorded is kept: a later
    # save adds pids, it never silently forgets the ones written down before.
    data = _previous_record(boot)
    data.update({"boot_time": boot,
                 "started": time.strftime("%Y-%m-%dT%H:%M:%S")})
    for name, proc in procs.items():
        data[name] = proc.pid
    # temp file + rename, like paths.save_settings: a write that fails half-way
    # (disk full, EIO, killed) must leave the previous record intact.
    tmp = paths.STATE.with_suffix(".json.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(paths.STATE)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _ps_pid(value):
    """value as a pid `ps` can be asked about, or None.

    Strings, floats, bools (JSON true is pid 1 == launchd) and numbers past
    macOS' pid range are not processes; handing one to the batched `ps -p` query
    would empty the answer for every service in the batch.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 1 <= value <= _MAX_PID:
        return None
    return value


def _own_pid(value):
    """A pid this code may signal: os.kill(0) hits our whole process group and
    a negative pid the group of that number, so only real child-sized pids
    pass. Anything else in state.json is treated as "not ours"."""
    pid = _ps_pid(value)
    return pid if pid is not None and pid > 1 else None


def _live_cmds(pids):
    """pid -> command in one `ps` call; zombie entries are dropped.

    Unusable pids are dropped up front rather than handed to ps, which would
    refuse the entire batch and make live services read as dead.
    """
    tokens = []
    for p in pids:
        pid = _ps_pid(p)
        if pid is not None:
            tokens.append(str(pid))
    if not tokens:
        return {}
    try:
        out = subprocess.run(
            ["ps", "-p", ",".join(tokens), "-o", "pid=,state=,command="],
            capture_output=True).stdout.decode()
    except Exception:
        out = ""
    result = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and "Z" not in parts[1]:
            result[parts[0]] = parts[2]
    return result


def running():
    """{name: bool} from persisted state, or None if not started at all."""
    state = _load()
    if state is None:
        return None
    pids = {name: state.get(name) for name in SERVICES}
    live = _live_cmds(pids.values())
    return {name: _ps_pid(pids.get(name)) is not None and
            MARKERS[name] in live.get(str(pids[name]), "")
            for name in SERVICES}


def _popen(cmd, **kw):
    defaults = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True)
    defaults.update(kw)
    return subprocess.Popen(cmd, **defaults)


def _web_env(repo_root):
    """The UI proxies' environment: the repo importable, and never the QoS
    switch klippy alone asked for (compat.shim_env does the same for moonraker).
    A user who exported KLIPPERFORMAC_QOS in their shell would otherwise boost
    every child of the stack, which is not the mitigation and not measurable."""
    env = dict(os.environ)
    env.pop(QOS_ENV_VAR, None)
    env["PYTHONPATH"] = repo_root + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def _under_install(token):
    """True when `token` is a path inside this install's own folders."""
    return any(token == str(root) or token.startswith(str(root) + os.sep)
               for root in (paths.APP_HOME, paths.DATA))


def _service_kind(cmd):
    """Which service this command line IS, or None if it is not ours.

    Judged on the argv shape start() spawns plus a path belonging to this
    install. A substring test is not enough in either direction: the bare word
    "moonraker" also turns up in `tail -f .../moonraker.log`, an editor on
    moonraker.conf or `klipperformac logs moonraker` -- all the user's own
    tools -- while a *different* install's argv is shaped exactly like ours, so
    only the home prefix tells the two apart.
    """
    toks = cmd.split()
    if len(toks) < 2 or not _under_install(toks[0]):
        return None
    if toks[1] == str(paths.KLIPPER / "klippy" / "klippy.py"):
        return "klipper"
    if toks[1:3] == ["-m", "moonraker"]:
        return "moonraker"
    if toks[1:3] == ["-m", "klipperformac.proxy"] and len(toks) > 3:
        if toks[3].isdigit():  # our proxy always takes its port first
            return "web"
    return None


def _scan_service_pids(with_caffeinate=False):
    """Pids of running klipperformac services of THIS install, tracked or not."""
    try:
        out = subprocess.run(["ps", "-eo", "pid=,command="],
                             capture_output=True).stdout.decode()
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        cmd = parts[1]
        if pid == os.getpid():
            continue
        if with_caffeinate and cmd.strip() == "caffeinate -dis":
            pids.append(pid)  # exact match: ours, started with -dis
            continue
        if _service_kind(cmd) is None:
            continue  # caffeinate included: generic name, may belong to others
        pids.append(pid)
    return pids


def _reap_orphans():
    """Kill service processes that exist but are not tracked by a live state
    file (crash, reboot, or stale state from a previous stack). Left alone
    they squat the ports/serial and the fresh children die on bind."""
    pids = _scan_service_pids()
    for pid in pids:
        _signal(pid, signal.SIGTERM)
    if pids:
        deadline = time.time() + 3.0
        live = set(pids)
        while live and time.time() < deadline:
            time.sleep(0.2)
            live = {p for p in live if _live_cmds([p])}
        for pid in live:
            _signal(pid, signal.SIGKILL)


# ---- caffeinate: our own sleep prevention, kept owned -------------------
#
# `caffeinate -dis` has no path in its argv, so _service_kind() cannot tell
# this install's caffeinate from anyone else's: a machine-wide scan would let a
# second KLIPPERFORMAC_HOME SIGKILL the primary instance's sleep guard (and the
# other way round lets a stray keep the Mac awake forever, AGENTS.md 5). So
# every caffeinate we start is written down next to the stack record, and only
# what we wrote down - re-validated by its exact command line - is ever ours to
# signal. state.json alone is not enough: it keeps one pid per service, so a
# stack that was started twice (before the cli lock) or whose record was
# cleared leaves a caffeinate nobody can see.
CAFFEINATE_CMD = "caffeinate -dis"
_CAFFEINATE_LIMIT = 16


def _caffeinate_record_path():
    return paths.RUN / "caffeinate.json"


def _read_caffeinate_record():
    try:
        with open(_caffeinate_record_path()) as f:
            rec = json.load(f)
    except Exception:
        return {}
    return rec if isinstance(rec, dict) else {}


def _note_caffeinate(pid):
    """Add a pid we just started to the caffeinate record (write-then-replace,
    like every other file a running stack depends on)."""
    pids = _recorded_caffeinate_pids(boot_check=False)
    if pid not in pids:
        pids.append(pid)
    data = {"boot_time": _boot_time(), "app_home": str(paths.APP_HOME),
            "pids": pids[-_CAFFEINATE_LIMIT:]}
    tmp = _caffeinate_record_path().with_name("caffeinate.json.tmp")
    try:
        paths.RUN.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(_caffeinate_record_path())
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


def _recorded_caffeinate_pids(boot_check=True):
    """Raw pids from the record, same-install same-boot only (boot_check off for
    read-modify-write, where a live check would drop the very pids we keep)."""
    rec = _read_caffeinate_record()
    pids = rec.get("pids")
    if not isinstance(pids, list):
        return []
    if rec.get("app_home") != str(paths.APP_HOME):
        return []  # another instance's list is not our process ledger
    if boot_check and rec.get("boot_time") not in (_boot_time(), None):
        return []  # a record from an earlier boot: those pids are gone
    out = []
    for value in pids:
        pid = _own_pid(value)
        if pid is not None and pid not in out:
            out.append(pid)
    return out


def _live_caffeinate_pids():
    """Recorded pids that are still an exact `caffeinate -dis` (a recycled pid
    belongs to somebody else and is left alone)."""
    pids = _recorded_caffeinate_pids()
    if not pids:
        return []
    live = _live_cmds(pids)
    return [p for p in pids if live.get(str(p), "").strip() == CAFFEINATE_CMD]


def reap_stale_caffeinate():
    """Signal the sleep guards this install started but no live stack record
    owns (previous session, cleared state file, double start). Called from
    start() and stop(); returns the pids it stopped."""
    pids = _live_caffeinate_pids()
    tracked = _load() or {}
    keep = _own_pid(tracked.get("caffeinate")) if tracked else None
    targets = [p for p in pids if p != keep]
    for pid in targets:
        _signal(pid, signal.SIGTERM)
    if targets:
        deadline = time.time() + 2.0
        live = set(targets)
        while live and time.time() < deadline:
            time.sleep(0.2)
            live = {p for p in live if _live_cmds([p])}
        for pid in live:
            _signal(pid, signal.SIGKILL)
    return [p for p in targets]


def _clear_caffeinate_record():
    try:
        _caffeinate_record_path().unlink()
    except OSError:
        pass


# ---- battery guard: an optional tracked child, kept owned ----------------
#
# The low-battery watchdog (klipperformac/batteryguard.py) is a detached child
# the user turns on and off, like the camera - deliberately NOT a member of
# SERVICES/MARKERS: `status`, state.json and the GUI stack list describe the
# stack, and an optional guard would read as a service that is always DOWN.
# Because its argv carries no path this install can point at (`-m
# klipperformac.batteryguard run`), _service_kind() cannot tell it from another
# checkout's guard, so - exactly like caffeinate - every pid we start is written
# down in its own ledger and only what we wrote down, re-validated against the
# exact argv shape, is ever ours to signal. That is also what makes `down` and
# `killall` able to reap it: an untracked watchdog is a process nobody can turn
# off, and this one posts to the printer on battery.
GUARD_ARGV = ["-m", "klipperformac.batteryguard", "run"]
_GUARD_LIMIT = 16


def guard_record_path():
    return paths.RUN / "batteryguard.json"


def _read_guard_record():
    try:
        with open(guard_record_path()) as f:
            rec = json.load(f)
    except Exception:
        return {}
    return rec if isinstance(rec, dict) else {}


def is_guard_argv(cmd):
    """The argv tail our guard is spawned with, in the argv positions it lands
    in: `<interpreter> -m klipperformac.batteryguard run ...`."""
    toks = cmd.split()
    return len(toks) >= 4 and toks[1:4] == GUARD_ARGV


# A python interpreter, in either spelling macOS gives you: the path that was
# exec'd (`.../venv/bin/python`) or the one a framework build re-exec's itself
# into (`.../Python.framework/Versions/x.y/Resources/Python.app/Contents/MacOS/
# Python`). Measured on this Mac: the second one is what `ps` reports for a
# venv python that is a symlink into a framework build, so an identity rule that
# only accepts the first would make this install's own guard unkillable.
_PYTHONISH = re.compile(r"(?:^|/)(?:python[\d.]*)$|(?:/|\b)MacOS/Python$")


def is_guard_cmd(cmd):
    """Does this command line read as OUR guard?

    Asked both of a pid this install's ledger names (the ledger already vouches
    for app_home and boot_time) and of every line of the machine-wide orphan
    scan - so this is the whole kill-authority rule, and the argv tail alone is
    not it. A *different* install's guard is spawned with a byte-identical
    argv, and it is not ours to signal: another account's watchdog dying means
    a print nobody authorised losing its battery protection (AGENTS.md: only
    what is provably ours is ever touched). The interpreter therefore has to
    be either under this install's folders, or a python binary that is NOT
    claimed by a foreign install - the framework path `ps` reports for our own
    venv symlink after its re-exec is exactly that (measured on this Mac)."""
    toks = cmd.split()
    if not is_guard_argv(cmd):
        return False
    if _under_install(toks[0]):
        return True
    return bool(_PYTHONISH.search(toks[0])) and not _foreign_install(toks[0])


# Two pieces of evidence that a python path belongs to SOMEONE ELSE's install:
# the default app-home marker (.klipperformac) in a folder that is not ours -
# _under_install() already returned True for ours before we are asked - and a
# /Users/<account> prefix that names a different account's home. A system
# framework python (/usr/local/Cellar/.../MacOS/Python) carries neither and is
# what this install's own guard looks like once `ps` renders the re-exec.
_APPHOME_MARKER = re.compile(r"(?:^|/)\.klipperformac(?:/|$)")
_USERS_ACCOUNT = re.compile(r"^/Users/([^/]+)")


def _foreign_install(token):
    """True when this interpreter path names another install's or another
    account's folders, so whatever argv follows it is not ours to reap."""
    if _APPHOME_MARKER.search(token):
        return True
    m = _USERS_ACCOUNT.match(token)
    if not m:
        return False
    for probe in (os.path.expanduser("~"), str(paths.APP_HOME),
                  str(paths.DATA)):
        own = _USERS_ACCOUNT.match(probe or "")
        if own and own.group(1) == m.group(1):
            return False  # this account's own folder (e.g. a repo checkout)
    return True


def note_guard(pid):
    """Write a guard pid we just started down (tmp+replace). False when the
    record cannot be written - the caller must then kill the child, because an
    untracked guard cannot be turned off by `down`/`killall`."""
    pids = _recorded_guard_pids(boot_check=False)
    if pid not in pids:
        pids.append(pid)
    data = {"boot_time": _boot_time(), "app_home": str(paths.APP_HOME),
            "pids": pids[-_GUARD_LIMIT:],
            "argv": GUARD_ARGV}
    tmp = guard_record_path().with_name("batteryguard.json.tmp")
    try:
        paths.RUN.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(guard_record_path())
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


def _recorded_guard_pids(boot_check=True):
    """Raw pids from the guard record, same-install (and same-boot) only.

    boot_check is off for the read-modify-write inside note_guard(), where a
    live check would drop the very pids we are keeping."""
    rec = _read_guard_record()
    pids = rec.get("pids")
    if not isinstance(pids, list):
        return []
    if rec.get("app_home") != str(paths.APP_HOME):
        return []  # another instance's list is not our process ledger
    if boot_check and rec.get("boot_time") not in (_boot_time(), None):
        return []  # a record from an earlier boot: those pids are gone
    out = []
    for value in pids:
        pid = _own_pid(value)
        if pid is not None and pid not in out:
            out.append(pid)
    return out


def live_guard_pids():
    """Recorded pids that are still our guard's exact argv (a recycled pid
    belongs to somebody else and is left alone)."""
    pids = _recorded_guard_pids()
    if not pids:
        return []
    live = _live_cmds(pids)
    return [p for p in pids if is_guard_cmd(live.get(str(p), ""))]


def guard_record_ours():
    """True when the guard ledger is this install's own (or absent).

    A record naming ANOTHER app home belongs to another instance's watchdog:
    its pids are already excluded from live_guard_pids(), and its file is not
    ours to delete either - otherwise a second checkout's `down` would blind the
    first one to its own guard."""
    rec = _read_guard_record()
    if not rec:
        return True
    return rec.get("app_home") == str(paths.APP_HOME)


def launch_guard(interval, log_file):
    """Spawn the detached watchdog and write it to the ledger.

    (pid, None) on success, (None, reason) when it cannot be started or cannot
    be tracked - in the latter the child is already signalled, so a caller can
    never end up with an untracked guard. The environment is _web_env()'s: the
    repo importable (that is what makes `-m klipperformac.batteryguard` resolve)
    and never klippy's QoS switch; argv stays exactly the shape is_guard_cmd()
    matches, so identification needs no extra flag."""
    if not paths.VENV_PY.exists():
        return None, "no venv python at {} (run: klipperformac setup)".format(
            paths.VENV_PY)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    argv = [str(paths.VENV_PY)] + GUARD_ARGV + ["--interval", str(interval)]
    try:
        proc = _popen(argv, cwd=repo_root, env=_web_env(repo_root),
                      stdout=log_file, stderr=log_file)
    except (OSError, ValueError) as e:
        # ValueError too: exec refuses an argv element with a NUL byte.
        return None, "could not start the guard: {}".format(e)
    if not note_guard(proc.pid):
        # Unwritable ledger: refuse the child we just made rather than leak a
        # watchdog nobody can turn off (see AGENTS.md on invisible strays).
        _signal(proc.pid, signal.SIGTERM)
        return None, "cannot write {} - the guard was started and then " \
                     "stopped again, because an untracked guard cannot be " \
                     "turned off by `down`".format(guard_record_path())
    return proc.pid, None


def reap_children(targets, timeout=2.0):
    """waitpid what we spawned: a killed child is gone from `ps` but stays a
    zombie in OUR process table until waited for, and kill(pid, 0) answers a
    zombie - so "is it really dead?" (the child tests' wait_dead, `down`'s
    reporting) must not be decided before the reap. A pid that is not our child
    answers ECHILD: nothing to reap (the guard is usually reparented to
    launchd; this loop only bites when the killer is the parent itself)."""
    pending = [pid for pid in targets if _own_pid(pid) is not None]
    deadline = time.time() + timeout
    while pending and time.time() < deadline:
        for pid in list(pending):
            try:
                done = os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError, ValueError):
                pending.remove(pid)
                continue
            if done[0]:
                pending.remove(pid)
        if pending:
            time.sleep(0.05)


def reap_guard(pids, timeout=2.0):
    """TERM, a short grace, then KILL the stragglers; reaps our own children so
    a zombie does not read as alive. Returns the pids signalled."""
    targets = [pid for pid in (_own_pid(p) for p in pids) if pid is not None]
    for pid in targets:
        _signal(pid, signal.SIGTERM)
    if not targets:
        return []
    deadline = time.time() + timeout
    live = set(str(p) for p in targets)
    while live and time.time() < deadline:
        time.sleep(0.1)
        live = {str(p) for p in targets if str(p) in _live_cmds([p])}
    for pid in targets:
        if str(pid) in live:
            _signal(pid, signal.SIGKILL)
    # Reap what we spawned (see reap_children): ps has already forgotten a
    # killed child by now, but it stays a zombie in OUR process table until
    # waitpid() takes it, and `wait_dead` (or the next `_live_cmds`) must not
    # be the thing that decides whether the ledger can be cleared -
    # process.stop() reaps for the same reason.
    reap_children(targets, timeout=2.0)
    return targets


def clear_guard_record():
    try:
        guard_record_path().unlink()
    except OSError:
        pass


def stop_guard(timeout=3.0):
    """Signal the guard(s) this install recorded and forget the ledger.
    Returns the pids stopped."""
    pids = live_guard_pids()
    stopped = reap_guard(pids, timeout=timeout)
    clear_guard_record()
    return stopped


def scan_guard_orphans():
    """Pids of a guard of THIS install that the ledger lost (hand-deleted
    record, crash between spawn and note). Machine-wide `ps`, but only argv
    is_guard_cmd() vouches for is ever returned."""
    try:
        out = subprocess.run(["ps", "-eo", "pid=,command="],
                             capture_output=True).stdout.decode(
                                 errors="replace")
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            number = int(parts[0])
        except ValueError:
            continue
        pid = _own_pid(number)
        if pid is None or pid == os.getpid():
            continue
        if is_guard_cmd(parts[1]):
            pids.append(pid)
    return pids


# ---- ports: what is actually listening ---------------------------------
def stack_port(name):
    return {"moonraker": paths.MOONRAKER_PORT, "web_mainsail": paths.WEB_PORT,
            "web_fluidd": paths.FLUIDD_PORT}[name]


def our_listeners():
    """[(port, pid)] on our stack ports held by a process whose command line is
    one of THIS install's services. A squatter is deliberately not one of
    these, so `down` can report "something we started is still there" without
    blaming somebody else's web server."""
    out = []
    for name in STACK_PORTS:
        port = stack_port(name)
        pids = _listener_pids(port)
        if pids is None:
            continue
        cmds = _live_cmds(pids)
        for pid in pids:
            if _service_kind(cmds.get(str(pid), "")) is not None:
                out.append((port, pid))
    return out


def lan_bind_in_effect():
    """'open'/'closed' as the RUNNING UI proxies' own argv says, or None when no
    proxy of ours is running.

    The bind address is an argument start() puts in the proxy's command line, so
    this is evidence of what is actually listening - unlike settings.json, which
    only records what the user asked for and is applied at the next start()."""
    state = running()
    if not state:
        return None
    pids = [state.get(name) for name in ("web_mainsail", "web_fluidd")
            if state.get(name)]
    seen = [pid for pid in pids if pid]
    if not seen:
        return None
    cmds = _live_cmds(seen)
    answers = []
    for pid in seen:
        toks = cmds.get(str(pid), "").split()
        if len(toks) >= 7 and toks[3].isdigit():
            answers.append(_bind_is_open(toks[6]))
    if not answers:
        return None
    return "open" if any(answers) else "closed"


def _bind_is_open(bind):
    return bind not in ("127.0.0.1", "localhost", "::1", "")


def _listener_pids(port):
    """Pids with a tcp LISTEN socket on `port`, or None when this Mac cannot
    tell (no lsof): callers must not read None as "free"."""
    lsof = shutil.which("lsof")
    if not lsof:
        return None
    try:
        out = subprocess.run(
            [lsof, "-nP", "-iTCP:{}".format(port), "-sTCP:LISTEN", "-Fp"],
            capture_output=True).stdout.decode(errors="replace")
    except Exception:
        return None
    pids = []
    for line in out.splitlines():
        if line.startswith("p") and line[1:].strip().isdigit():
            pid = int(line[1:].strip())
            if pid > 1 and pid not in pids:
                pids.append(pid)
    return pids


def _port_free(port):
    """Bind probe (SO_REUSEADDR so a TIME_WAIT socket we left behind does not
    read as "taken"). Only an active listener makes this False."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


def port_conflicts():
    """[(port, pid_or_None, description)] of stack ports held by a process that
    is NOT one of ours. Empty list means up may spawn."""
    conflicts = []
    for name in STACK_PORTS:
        port = stack_port(name)
        pids = _listener_pids(port)
        if pids is None:  # cannot inspect: fall back to the bind probe
            if not _port_free(port):
                conflicts.append((port, None, "held by another program "
                                              "(no lsof to identify it)"))
            continue
        cmds = _live_cmds(pids)
        for pid in pids:
            cmd = cmds.get(str(pid), "")
            if _service_kind(cmd) is not None:
                continue  # ours: _reap_orphans() owns that one
            conflicts.append((port, pid, cmd or "unknown command"))
    return conflicts


def _lan_opted_in():
    """LAN exposure needs an explicit true in settings.json (AGENTS.md: never
    enabled by default). paths.lan_enabled() coerces with bool(), where a
    string like "false" or "0" is truthy, so the bind decision reads the field
    itself; anything but the boolean true stays on loopback."""
    try:
        return paths.load_settings().get("lan") is True
    except Exception:
        return False


def _can_persist():
    """Write the stack record before any child exists.

    A stack that cannot be written down cannot be stopped: `down` and `status`
    would not see it and only a manual pkill would free 7125/8080/8081 and the
    serial port, so start() refuses to spawn instead of leaking it."""
    try:
        _save({})
    except OSError:
        return False
    return True


def start():
    state = running()
    if state is not None:
        if any(state.values()):
            return False
        _clear()  # crashed/stale: self-heal, then start fresh
    paths.ensure_dirs()
    if not _can_persist():
        print("[!] Cannot write {} - nothing was started. Fix that folder's "
              "permissions and run: klipperformac up".format(paths.STATE))
        return False
    _reap_orphans()
    reap_stale_caffeinate()
    conflicts = port_conflicts()
    if conflicts:
        _clear()
        for port, pid, desc in conflicts:
            print("[!] Port {} is held by {}{}".format(
                port, "pid {} ({})".format(pid, desc) if pid else desc,
                " - it is not part of this stack"))
        print("[!] Nothing was started: free the port above (stop the other "
              "program, or: klipperformac killall) then run: klipperformac up")
        return False
    invalidate_stale_build(paths.KLIPPER)
    ensure_runtime_shims(paths.VENV_PY)

    py = str(paths.VENV_PY)
    # klippy alone may run on the Python 3.14 shaper venv (numpy is imported
    # in-process by shaper_calibrate); moonraker always stays on the main venv.
    kpy = shaper.interpreter_for_klipper()[0]
    klipper_klippy = paths.KLIPPER / "klippy"
    env = build_env()
    # klippy alone is timing-critical, so it alone carries the switch that makes
    # our generated sitecustomize ask for a USER_INTERACTIVE QoS class
    # (compat.SITECUSTOMIZE_SRC). Moonraker, the UI proxies and the camera never
    # see it: they are not on the step-timing path.
    #
    # There is deliberately no nice/renice/`taskpolicy` call here. macOS has no
    # SCHED_FIFO and a *negative* nice is root-only - measured on this Mac:
    # os.setpriority(PRIO_PROCESS, 0, -5) -> EACCES, os.nice(-1) -> EPERM,
    # `renice -n -5` -> Permission denied - and a positive nice would only push
    # klippy further down. `klipperformac timing` reports what the scheduler
    # actually gave us.
    #
    # argv is untouched: _service_kind() and running() identify the child by its
    # argv shape, so an environment-only change stays invisible to detection.
    kenv = dict(env)
    kenv[QOS_ENV_VAR] = QOS_ENV_VALUE
    # ...but the switch is only half of it: sitecustomize is PER INTERPRETER, and
    # klippy may be about to run on the 3.14 shaper venv (SHAPER_CALIBRATE needs
    # numpy). installer.setup() writes the file into the main venv only, so
    # without this the QoS ask - and the CLOCK_BOOTTIME/SOCK_* attributes -
    # would be read by nobody, in silence. Idempotent, and cheap when kpy is the
    # main venv (the file already matches and is left alone).
    if str(kpy) != py:
        ensure_runtime_shims(kpy)

    kproc = _popen(
        [kpy, str(klipper_klippy / "klippy.py"),
         "-a", str(paths.API_SOCKET), "-l", str(paths.KLIPPY_LOG),
         str(paths.PRINTER_CFG)],
        cwd=str(klipper_klippy), env=kenv,
    )
    mproc = _popen(
        [py, "-m", "moonraker",
         "-d", str(paths.DATA), "-c", str(paths.MOONRAKER_CONF),
         "-l", str(paths.MOONRAKER_LOG)],
        cwd=str(paths.MOONRAKER), env=shim_env(),
    )
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    web_env = _web_env(repo_root)
    bind = "0.0.0.0" if _lan_opted_in() else "127.0.0.1"

    wproc = _popen(
        [py, "-m", "klipperformac.proxy", str(paths.WEB_PORT),
         str(paths.WEB_MAINSAIL), str(paths.MOONRAKER_PORT), bind],
        cwd=repo_root, env=web_env,
    )
    fproc = _popen(
        [py, "-m", "klipperformac.proxy", str(paths.FLUIDD_PORT),
         str(paths.WEB_FLUIDD), str(paths.MOONRAKER_PORT), bind],
        cwd=repo_root, env=web_env,
    )
    cproc = _popen(["caffeinate", "-dis"])

    _note_caffeinate(cproc.pid)
    _save({"klipper": kproc, "moonraker": mproc, "web_mainsail": wproc,
           "web_fluidd": fproc, "caffeinate": cproc})
    # Open the power-loss session marker (and, in that same call, judge the
    # session the last crash or reboot left open). Deliberately last: nothing a
    # start() can no longer undo may depend on a marker write, and a marker this
    # folder refuses to accept is reported by `status`/`doctor`, not fatal here.
    powerloss.note_session_start()
    return True


def missing_services(state=None):
    """Names in SERVICES the tracked stack is missing (all of them when no
    stack is tracked at all)."""
    st = running() if state is None else state
    if not st:
        return []
    return [name for name in SERVICES if not st.get(name)]


def repair(state=None):
    """Bring back the services a live stack lost without disturbing the ones
    that answer: klipper and moonraker are left alone on purpose, because
    restarting them kills an active print - the caller tells the user to run
    `klipperformac restart` for those. Returns the names it started."""
    st = running() if state is None else state
    if not st:
        return []
    safe = [n for n in missing_services(st)
            if n in ("web_mainsail", "web_fluidd", "caffeinate")]
    if not safe:
        return []
    py = str(paths.VENV_PY)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    web_env = _web_env(repo_root)
    bind = "0.0.0.0" if _lan_opted_in() else "127.0.0.1"
    started = {}
    if "web_mainsail" in safe:
        started["web_mainsail"] = _popen(
            [py, "-m", "klipperformac.proxy", str(paths.WEB_PORT),
             str(paths.WEB_MAINSAIL), str(paths.MOONRAKER_PORT), bind],
            cwd=repo_root, env=web_env)
    if "web_fluidd" in safe:
        started["web_fluidd"] = _popen(
            [py, "-m", "klipperformac.proxy", str(paths.FLUIDD_PORT),
             str(paths.WEB_FLUIDD), str(paths.MOONRAKER_PORT), bind],
            cwd=repo_root, env=web_env)
    if "caffeinate" in safe:
        started["caffeinate"] = _popen(["caffeinate", "-dis"])
        _note_caffeinate(started["caffeinate"].pid)
    try:
        _save(started)
    except OSError:
        for proc in started.values():
            _signal(_own_pid(proc.pid), signal.SIGTERM)
        print("[!] Cannot write {} - the repaired services are running but "
              "`down` will not know them. Fix that folder, then run: "
              "klipperformac killall".format(paths.STATE))
        return []
    return sorted(started)


def _stop_targets(state):
    """(name, pid) pairs of state.json this code may signal.

    A pid ps still lists has to carry this service's own marker before we touch
    it (the same evidence running() demands): after a crash macOS recycles pids
    freely, and the recorded number can by then belong to the user's Finder
    window. A pid ps does not list is already gone, so signalling it is a
    harmless no-op and keeps `down` idempotent for a half-dead stack.
    """
    pairs = []
    for name in _STOP_ORDER:
        pid = _own_pid(state.get(name))
        if pid is not None:
            pairs.append((name, pid))
    live = _live_cmds([p for _, p in pairs])
    return [(name, pid) for name, pid in pairs
            if str(pid) not in live or MARKERS[name] in live[str(pid)]]


def stop(timeout=6.0):
    state = _load()
    if state is None:
        return False
    targets = _stop_targets(state)
    # The same install can hold more processes than state.json lists (a stack
    # started twice, a record cleared by hand, a crash between spawn and save).
    # `down` used to report "Stopped." while those kept the API and the UI
    # serving on the ports - with LAN on, serving the whole local network after
    # the user said stop. Anything answering to our argv shape, under THIS
    # install's folders, goes down with the stack.
    tracked = {pid for _, pid in targets}
    targets += [("stray", pid) for pid in _scan_service_pids()
                if pid not in tracked]
    reap_stale_caffeinate()
    if not targets:
        _clear()  # nothing here is ours to stop; let the next start() be clean
        return False
    # Read once, HERE, for the power-loss marker (klipperformac/powerloss.py):
    # "was this session still alive when the graceful path ran?" asked after the
    # kills always answers "no", and every honest `down` would be reported as a
    # crash. Only the marker is written from here; the journal is not.
    alive = running()

    def _alive():
        """str() pids of the targets that ps still lists."""
        live = _live_cmds([p for _, p in targets])
        return {str(p) for _, p in targets if str(p) in live}

    for _, pid in targets:
        _signal(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive():
            break
        time.sleep(0.2)
    stragglers = [(n, p) for n, p in targets if str(p) in _alive()]
    for _, p in stragglers:
        _signal(p, signal.SIGKILL)
    # Reap children we spawned so zombies don't look alive.
    deadline = time.time() + 2.0
    while time.time() < deadline:
        for _, p in stragglers:
            try:
                os.waitpid(p, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
        if not _alive():
            break
        time.sleep(0.2)
    still = [n for n, p in stragglers if str(p) in _alive()]
    if still:
        for n in _STOP_ORDER:
            if n in still:
                print("[!] {} (pid {}) refused to die; leaving state file for "
                      "inspection: {}".format(n, dict(targets).get(n), paths.STATE))
        return False
    # The clean-stop marker, written BEFORE the stack record is cleared: the
    # marker is the one thing that says "this session ended the way the user
    # asked", and it lives in data/powerloss/, never in run/state.json - because
    # _load() deletes that file the moment kern.boottime moves, so a marker a
    # reboot erases cannot testify about a crash (see klipperformac/powerloss.py).
    powerloss.note_clean_stop(state=alive)
    _clear()
    _clear_caffeinate_record()
    return True


def kill_all():
    """Force-stop everything: tracked services plus any strays (untracked
    proxies/klipper/moonraker, our caffeinate) that `stop` would leave behind.
    Returns the list of killed (name, pid)."""
    targets = {}
    state = _load()
    if state:
        for name in SERVICES:
            pid = _own_pid(state.get(name))
            if pid is not None:
                targets[pid] = name
    for pid in _scan_service_pids(with_caffeinate=True):
        targets.setdefault(pid, "stray")
    for pid in _live_caffeinate_pids():
        targets.setdefault(pid, "caffeinate")
    # Tracked pids need the same cmdline evidence stop() uses; the scanned
    # strays were already identified by their argv.
    tracked = [pid for pid, name in targets.items() if name != "stray"]
    live = _live_cmds(tracked)
    killed = []
    for pid, name in targets.items():
        cmd = live.get(str(pid))
        if name != "stray" and cmd is not None and MARKERS[name] not in cmd:
            continue  # pid recycled by another app: not ours to kill
        if _signal(pid, signal.SIGKILL):
            killed.append((name, pid))
    deadline = time.time() + 3.0
    pending = [pid for _, pid in killed]
    while pending and time.time() < deadline:
        time.sleep(0.2)
        still = _live_cmds(pending)
        for pid in [p for p in pending if str(p) not in still]:
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
            pending.remove(pid)
    _clear()
    _clear_caffeinate_record()
    if killed:
        # A SIGKILL sweep is never a graceful stop: mark the session unclean with
        # the user's own command as the reason, so `status`/`doctor` can tell a
        # `killall` apart from a power cut instead of calling both "clean".
        powerloss.note_forced_stop()
    return sorted(killed)


def _signal(pid, sig):
    """Send `sig` to a validated pid; True when the process accepted it.

    ESRCH/EPERM are tolerable (already gone / not ours to kill) but must never
    read as a success: killall and uninstall report this answer to the user.
    """
    pid = _own_pid(pid)
    if pid is None:
        return False
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    except (OSError, TypeError, OverflowError, ValueError):
        return False
    return True
