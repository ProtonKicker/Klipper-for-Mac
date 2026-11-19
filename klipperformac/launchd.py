"""Login integration: a macOS LaunchAgent plus the supervisor it keeps alive.

The design in one sentence: launchd owns *the supervisor*, and only the
supervisor owns *the stack* - because a launchd job cannot tell "the user said
down" apart from "it crashed", and only this tool can.

    launchd (gui/<uid>)  --KeepAlive{SuccessfulExit=false}-->  supervise loop
        supervise loop   --flock(run/cli.lock) each sweep---->  the stack
        user intent      run/autostart.want                    the arbiter

`run/autostart.want` is the single tie-breaker and it is only ever written by a
*user-initiated* command (`up`, `restart`, `login on` set it; `down`, `killall`,
`login off` clear it). The supervisor starts the stack when the want file is
there and the stack is down, and does nothing at all when it is not: a manual
`down` therefore wins over launchd forever, which is the whole reason this
feature is not simply `KeepAlive` on klipper itself.

Rejected alternatives (kept here so nobody re-litigates them):
  * top-level or `KeepAlive.WatchPaths` - they are *start triggers* ("start the
    job if any listed path is modified"), so every hold/want write would kick the
    job itself and they carry no crash policy at all.
  * `KeepAlive = true` - resurrects the stack right after the user said `down`.
  * the job program being `klipperformac up` - fork-and-exit: launchd would
    "restart" a stack it cannot see, and `SuccessfulExit` cannot be honoured.
  * a LaunchDaemon (system domain) - needs root to install, and runs outside the
    Aqua session: no `caffeinate -d` effect for the console user, no user Home
    resolution, no camera. Out of scope.

Upstream stays sacred: nothing here touches klipper/moonraker. The agent only
runs our own CLI, which in turn uses the existing `process` primitives and the
localhost REST API.
"""
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import paths, process

LABEL_BASE = "com.klipperformac.supervisor"

# The sweep interval the supervisor sleeps between passes, and the floor a human
# can ask for: below this the flock fight with a running `klipperformac up`
# becomes the interesting part of the day.
DEFAULT_TICK = 10.0
MIN_TICK = 1.0

# launchd's own minimum is 10 s for a daemon and effectively 10-30 s for an
# agent; 30 keeps a crash loop from hammering a printer that is refusing to
# start, and is the documented floor Apple keeps repeating.
THROTTLE_INTERVAL = 30
EXIT_TIMEOUT = 60          # SIGTERM -> SIGKILL grace for `launchctl bootout`

WANT_NAME = "autostart.want"
SUPERVISOR_STATE_NAME = "supervisor.json"
SUPERVISOR_LOG_NAME = "supervisor.log"
AGENT_OUT_NAME = "autostart.out.log"
AGENT_ERR_NAME = "autostart.err.log"
LOG_ROTATE_BYTES = 1024 * 1024

# klipper is the one restart that can hurt (a serial board that will never come
# back is the common case), so it is budgeted: MAX restarts inside WINDOW seconds,
# then the supervisor latches and says so instead of re-inventing a crash loop.
MAX_KLIPPER_RESTARTS = 2
RESTART_WINDOW = 600.0
# After we start the stack ourselves, klippy needs a few seconds to load
# printer.cfg before "no REST answer" is evidence rather than noise.
START_GRACE = 15.0
START_TIMEOUT = 60.0

CORE_SERVICES = ("klipper", "moonraker")
# The env the agent gets. An allowlist, never a copy of os.environ: a shell with
# an API key, a proxy URL or a token exported in it must not be written into a
# plist that is world-readable by design (0644 under ~/Library/LaunchAgents).
ENV_KEYS = ("PATH", "PYTHONPATH", "LANG", "HOME", "KLIPPERFORMAC_HOME",
            "KLIPPERFORMAC_DATA", "KLIPPERFORMAC_PYTHON")

_STOP = [False]
_NOT_LOADED = re.compile(r"could not find (this )?service|no such service|"
                         r"cannot find service|already booted out|"
                         r"no such process|nothing matched", re.I)
_ALREADY_LOADED = re.compile(r"already loaded|service is already|"
                             r"bootstrap failed: 5", re.I)
_DISABLED_LINE = re.compile(r"\"([^\"]+)\"\s*(?:=>|=)\s*([A-Za-z0-9]+)")


# ------------------------------------------------------------- identities ---
def label(home=None):
    """This install's job label: per-app-home, so two homes never collide.

    A second checkout with its own KLIPPERFORMAC_HOME otherwise bootstraps under
    the same label and the first install's agent silently points at the second
    one's folders. The default home keeps the clean label everyone expects."""
    home = str(paths.APP_HOME if home is None else Path(home).expanduser())
    if home == str(Path("~/.klipperformac").expanduser()):
        return LABEL_BASE
    return "{}-{}".format(LABEL_BASE, hashlib.sha1(home.encode()).hexdigest()[:8])


def agents_dir():
    """~/Library/LaunchAgents - the gui/<uid> domain's on-disk home.

    KLIPPERFORMAC_LAUNCH_AGENTS relocates it, which is what lets the whole
    offline suite exercise install/remove without ever writing into a real
    user's login items."""
    override = os.environ.get("KLIPPERFORMAC_LAUNCH_AGENTS")
    if override and override.strip():
        return Path(override.strip()).expanduser()
    return Path.home() / "Library" / "LaunchAgents"


def plist_path():
    return agents_dir() / "{}.plist".format(label())


def uid():
    return os.getuid()


def domain():
    """The per-user GUI domain launchd(1) bootstraps agents into."""
    return "gui/{}".format(uid())


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def interpreter():
    """The python running this tool. NOT realpath'd: Homebrew's
    /usr/local/bin/python3 is a symlink whose target moves on every upgrade,
    and an agent must survive the upgrade that moved it."""
    return sys.executable or str(paths.VENV_PY)


def launcher_path():
    """The `klipperformac` executable the agent should run, or None.

    realpath first: launchd exec's this path directly, and the installer's
    ~/.local/bin entry is a symlink into APP_HOME/src."""
    found = shutil.which("klipperformac")
    if found:
        return Path(os.path.realpath(found))
    local = Path("~/.local/bin/klipperformac").expanduser()
    for candidate in (local, Path(repo_root()) / "bin" / "klipperformac"):
        if candidate.is_file():
            return Path(os.path.realpath(str(candidate)))
    return None


def job_argv(tick=None):
    """What launchd exec's. Falls back to `-m klipperformac` when no launcher
    exists (a source checkout with no ~/.local/bin symlink), because a plist
    pointing at a missing ProgramArguments[0] is a job launchd marks as
    "insecure/failed" and then disables in that session."""
    tick = DEFAULT_TICK if tick is None else _clean_tick(tick)
    launch = launcher_path()
    if launch is not None:
        return [str(launch), "supervise", "--tick", _tick_text(tick)]
    return [interpreter(), "-m", "klipperformac", "supervise", "--tick",
            _tick_text(tick)]


def env_block():
    """EnvironmentVariables for the job: plain strings only.

    launchd silently drops a non-string value (plist(5)), which used to look
    like "the agent starts but cannot find python", so every value is str()'d
    here and the plist test asserts exactly that."""
    py = interpreter()
    parts = [os.path.dirname(py), "/usr/local/bin", "/opt/homebrew/bin",
             "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    home = os.environ.get("HOME") or str(Path.home())
    path_env = os.environ.get("PATH") or ""
    for entry in path_env.split(os.pathsep):
        if entry and entry not in parts:
            parts.append(entry)
    values = {
        "PATH": os.pathsep.join(parts),
        "PYTHONPATH": repo_root(),
        "LANG": os.environ.get("LANG") or "en_US.UTF-8",
        "HOME": home,
        "KLIPPERFORMAC_HOME": str(paths.APP_HOME),
        "KLIPPERFORMAC_DATA": str(paths.DATA),
        "KLIPPERFORMAC_PYTHON": py,
    }
    # ENV_KEYS is the contract: the test asserts exactly these keys, so a future
    # "just pass os.environ through" change has to fail loudly here.
    return {key: str(values[key]) for key in ENV_KEYS}


def job_dict(tick=None):
    """The whole LaunchAgent contract as one dict (see the module docstring)."""
    logs = paths.LOGS
    return {
        "Label": label(),
        "ProgramArguments": job_argv(tick),
        # Login is the trigger; KeepAlive on a *failed* exit is the crash
        # trigger. A 0 exit (our SIGTERM handler) is deliberately not restarted,
        # so `login off`/logout/bootout cannot be read as a crash.
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": THROTTLE_INTERVAL,
        "ExitTimeOut": EXIT_TIMEOUT,
        # The stack's children are in their own sessions (process._popen
        # start_new_session) and must outlive a `bootout` of the supervisor:
        # without this launchd SIGKILLs the whole process group on its way out.
        "AbandonProcessGroup": True,
        "LimitLoadToSessionType": "Aqua",
        "WorkingDirectory": str(paths.DATA),
        "StandardOutPath": str(logs / AGENT_OUT_NAME),
        "StandardErrorPath": str(logs / AGENT_ERR_NAME),
        "EnvironmentVariables": env_block(),
    }


def plist_bytes(tick=None):
    return plistlib.dumps(job_dict(tick), fmt=plistlib.FMT_XML)


def _clean_tick(tick):
    try:
        value = float(tick)
    except (TypeError, ValueError):
        return DEFAULT_TICK
    if not value or value < MIN_TICK:
        return MIN_TICK
    return min(value, 3600.0)


def _tick_text(tick):
    value = _clean_tick(tick)
    return str(int(value)) if value == int(value) else "{:.1f}".format(value)


# ------------------------------------------------------- the plist on disk ---
def _read_bytes(path=None):
    """(raw, None) or (None, reason): a directory, a symlink loop or a refusal
    must read as "cannot tell", never as "not ours"."""
    path = plist_path() if path is None else Path(path)
    if not path.exists():
        return None, "not installed"
    if path.is_dir():
        return None, "{} is a folder, not a property list".format(path)
    try:
        return path.read_bytes(), None
    except OSError as e:
        return None, "cannot read {} ({})".format(path,
                                                  getattr(e, "strerror", None) or e)


def read_plist(path=None):
    """The agent's payload, {} when it is missing, unreadable or not a plist.

    Never raises: `login status`, `doctor` and the supervisor's own sweep all
    ask this question about a file a human may have half-edited in Xcode."""
    raw, _reason = _read_bytes(path)
    if raw is None:
        return {}
    try:
        data = plistlib.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def program_is_ours(data):
    """Does this payload's argv name a program THIS install can own?

    Three accepted shapes: the launcher we resolve right now, a launcher-shaped
    path inside this app home (the *stale* case - a moved or reinstalled tree
    still has to be ours to rewrite and to report), and the documented
    `-m klipperformac` fallback for a checkout with no symlink. Anything else
    that merely contains the word "supervise" is somebody else's job."""
    args = [str(a) for a in (data.get("ProgramArguments") or [])]
    if not args or "supervise" not in args:
        return False
    launch = launcher_path()
    if launch is not None and args[0] == str(launch):
        return True
    if args[0].endswith("klipperformac") and args[0].startswith(
            (str(paths.APP_HOME) + os.sep, str(paths.DATA) + os.sep)):
        return True
    return args[0] == interpreter() and args[1:3] == ["-m", "klipperformac"]


def payload_is_ours(data):
    """Is this payload THIS install's supervisor agent?

    Three proofs, all required: our label (which is a hash of the app home), our
    supervise argv, and our app home in its environment. What is deliberately
    NOT a proof here is the data folder: `klipperformac data --set` moves that
    on purpose, and an agent that names the old one is *ours and stale* (see
    plist_stale), not somebody else's - refusing to rewrite it would strand the
    user on "already loaded" with an agent pointing at a deleted folder."""
    if not isinstance(data, dict) or not data:
        return False
    if data.get("Label") != label():
        return False
    if not program_is_ours(data):
        return False
    env = data.get("EnvironmentVariables")
    env = env if isinstance(env, dict) else {}
    return str(env.get("KLIPPERFORMAC_HOME") or "") == str(paths.APP_HOME)


def is_ours(path=None):
    return payload_is_ours(read_plist(path))


def installed():
    """True when our agent is on disk and provably ours."""
    return is_ours(plist_path())


def plist_stale():
    """Why the on-disk agent no longer matches this install (None if it does)."""
    path = plist_path()
    raw, reason = _read_bytes(path)
    if reason == "not installed":
        return None  # absence is not staleness: `login status` says "off"
    if reason:
        return reason
    data = read_plist(path)
    if not data:
        return ("{} is not a property list (edit it, or re-run: "
                "klipperformac login on)".format(path))
    if not payload_is_ours(data):
        return "it belongs to a different install or folder set"
    env = data.get("EnvironmentVariables")
    env = env if isinstance(env, dict) else {}
    if str(env.get("KLIPPERFORMAC_DATA") or "") != str(paths.DATA):
        return ("it points at the data folder {} but this install now uses {} "
                "- re-run: klipperformac login on".format(
                    env.get("KLIPPERFORMAC_DATA"), paths.DATA))
    program = str(data.get("ProgramArguments", [""])[0])
    if program.endswith("klipperformac") and not Path(program).is_file():
        return "its program {} is gone".format(program)
    return None


def _backup_path(path):
    """A name that does not exist yet: two recoveries must keep two pieces of
    evidence, and a second-resolution timestamp alone collides."""
    base = "{}.corrupt-{}".format(path.name, int(time.time()))
    candidate = path.with_name(base)
    n = 1
    while candidate.exists():
        n += 1
        candidate = path.with_name("{}-{}".format(base, n))
    return candidate


def install_plist(tick=None, dry_run=False):
    """Write the LaunchAgent. (ok, lines) - cam.start()'s shape.

    Never overwrites something we cannot prove is ours, and never deletes an
    unreadable file: a foreign agent with our filename is somebody else's login
    item. A malformed file at our exact path can only be ours (the name is our
    label), so it is backed up and replaced - that recovery is the difference
    between `login on` working again and a user stuck on "already loaded"."""
    path = plist_path()
    payload = plist_bytes(tick)
    lines = ["LaunchAgent: {}".format(path)]
    raw, reason = _read_bytes(path)
    if reason and reason != "not installed":
        return False, ["[!] Refusing to overwrite: {}".format(reason)]
    if raw is not None:
        data = read_plist(path)
        if data and not payload_is_ours(data):
            return False, ["[{}] is not this install's LaunchAgent, so it was "
                           "left exactly as it is.".format(path),
                           "Move or rename that file, then run: klipperformac "
                           "login on"]
        if data and payload_is_ours(data) and _same_payload(data, tick):
            lines.append("Already installed and current (label {}, "
                         "ThrottleInterval {}s).".format(label(),
                                                         THROTTLE_INTERVAL))
            return True, lines
        if not data:
            backup = _backup_path(path)
            if dry_run:
                lines.append("Would keep the unreadable file as {} and write "
                             "a fresh agent.".format(backup))
                return True, lines
            try:
                path.replace(backup)
                lines.append("[!] {} was not a readable plist: kept as {}".format(
                    path, backup))
            except OSError as e:
                return False, ["[!] Cannot move the unreadable {} aside ({}): "
                               "nothing was written.".format(
                                   path, getattr(e, "strerror", None) or e)]
    if dry_run:
        lines.append("Would write label {} running: {}".format(
            label(), " ".join(job_argv(tick))))
        return True, lines
    try:
        agents_dir().mkdir(parents=True, exist_ok=True)
        paths.LOGS.mkdir(parents=True, exist_ok=True)
        paths.DATA.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, ["[!] Cannot prepare {}: {} (nothing was written)".format(
            agents_dir(), getattr(e, "strerror", None) or e)]
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, path)
        os.chmod(path, 0o644)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False, ["[!] Could not write {} ({}) - the login item was not "
                       "installed.".format(path, getattr(e, "strerror", None) or e)]
    lines.append("label {}  program {}  tick {}".format(
        label(), job_argv(tick)[0], _tick_text(tick)))
    return True, lines


def _same_payload(data, tick):
    """Does the file on disk already say exactly what we would write now?"""
    try:
        return plistlib.loads(plist_bytes(tick)) == data
    except Exception:
        return False


def remove_plist():
    """Delete our agent's file. Refuses anything that is not provably ours."""
    path = plist_path()
    if not path.exists():
        return True, ["No LaunchAgent at {}.".format(path)]
    if not is_ours(path):
        return False, ["[!] Left {} in place: it is not this install's "
                       "LaunchAgent.".format(path)]
    try:
        path.unlink()
    except OSError as e:
        return False, ["[!] Could not remove {} ({})".format(
            path, getattr(e, "strerror", None) or e)]
    return True, ["Removed LaunchAgent: {}".format(path)]


# ----------------------------------------------------------- launchctl -----
def _launchctl(args, timeout=15):
    """One launchctl call: (rc, out, err); rc None when launchctl is unusable.

    The seam the offline suite replaces wholesale: nothing here may touch this
    Mac's real login items, so every test fakes argv and the reply."""
    argv = [shutil.which("launchctl") or "/bin/launchctl"] + list(args)
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    except Exception as e:  # missing binary, EPERM, a hung launchctl
        return None, "", "{}: {}".format(argv[0], e)
    return proc.returncode, _decode(proc.stdout), _decode(proc.stderr)


def _decode(data):
    try:
        return (data or b"").decode(errors="replace")
    except AttributeError:
        return str(data or "")


def bootstrap(path=None):
    """Load the agent into gui/<uid>. (ok, detail).

    "Already loaded" is not a failure of intent, but a stale job in launchd's
    own table would keep the OLD ProgramArguments, so in that case we boot out
    our own label (never anybody else's: the label is ours by construction) and
    bootstrap the new file."""
    path = Path(plist_path() if path is None else path)
    rc, out, err = _launchctl(["bootstrap", domain(), str(path)])
    detail = (out + " " + err).strip()
    if rc == 0:
        return True, "loaded"
    if _ALREADY_LOADED.search(detail):
        ok, _detail = bootout()
        if not ok:
            return False, "already loaded and could not be reloaded: " + detail
        rc2, out2, err2 = _launchctl(["bootstrap", domain(), str(path)])
        if rc2 == 0:
            return True, "reloaded"
        return False, (out2 + " " + err2).strip() or "bootstrap rc {}".format(rc2)
    if rc is None:
        return False, "no usable launchctl on this Mac: " + detail
    return False, detail or "bootstrap rc {}".format(rc)


def bootout():
    """Unload the agent. (ok, detail) - idempotent on purpose.

    `login off` and `uninstall` both call this, and "it was not loaded" (the
    user turned it off in System Settings, or a reboot cleared it) is exactly
    the state they want: it must not read as a failure."""
    rc, out, err = _launchctl(["bootout", "{}/{}".format(domain(), label())])
    detail = (out + " " + err).strip()
    if rc == 0:
        return True, "unloaded"
    if _NOT_LOADED.search(detail) or "could not find service" in detail.lower():
        return True, "was not loaded"
    if rc is None:
        return False, "no usable launchctl: " + detail
    return False, detail or "bootout rc {}".format(rc)


def kickstart():
    """Ask launchd to run the job now (used after `login on` when the stack is
    already up and no reload should have to wait for the next login)."""
    rc, out, err = _launchctl(["kickstart", "-k",
                               "{}/{}".format(domain(), label())])
    detail = (out + " " + err).strip()
    if rc == 0 or _NOT_LOADED.search(detail):
        return True, detail or "started"
    return False, detail or "kickstart rc {}".format(rc)


def job_info():
    """{loaded, state, pid, detail} from `launchctl print gui/<uid>/<label>`."""
    target = "{}/{}".format(domain(), label())
    rc, out, err = _launchctl(["print", target], timeout=10)
    text = out or err or ""
    if rc is None:
        return {"loaded": None, "state": "unknown", "pid": None,
                "detail": "no launchctl found: " + text.strip()}
    if rc != 0:
        return {"loaded": False, "state": "not loaded", "pid": None,
                "detail": text.splitlines()[0] if text.strip() else
                          "print rc {}".format(rc)}
    state = None
    pid = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("state ="):
            state = line.split("=", 1)[1].strip()
        elif line.startswith("pid ="):
            try:
                pid = int(line.split("=", 1)[1].strip())
            except ValueError:
                pid = None
    return {"loaded": True, "state": state or "loaded", "pid": pid,
            "detail": "gui domain"}


def domain_disabled():
    """True/False when `launchctl print-disabled gui/<uid>` names our label,
    None when this Mac cannot say. System Settings > Login Items writes here.

    Both spellings of the reply are accepted (`"x" = true` and `"x" => 1`): the
    format has moved across macOS versions, and a status line that silently
    reads "not disabled" because the parser missed a form is worse than "unknown".
    """
    rc, out, _err = _launchctl(["print-disabled", domain()], timeout=10)
    if rc is None or rc != 0:
        return None
    for line in out.splitlines():
        m = _DISABLED_LINE.search(line)
        if not m or m.group(1) != label():
            continue
        value = m.group(2).lower()
        if value in ("true", "1", "yes"):
            return True
        if value in ("false", "0", "no", "enabled"):
            return False
    return False


# ------------------------------------------------- user-intent + state ------
def want_path():
    return paths.RUN / WANT_NAME


def want_set(reason="cli"):
    """Record "the user wants this stack running".

    A user-initiated call also clears the crash budget: whatever the supervisor
    gave up on, an explicit `up`/`restart` means look again. The supervisor
    never calls this - only a human can raise the intent."""
    try:
        paths.RUN.mkdir(parents=True, exist_ok=True)
        paths.write_config(want_path(),
                           "wanted by: {}\nset at: {}\n".format(
                               reason, time.strftime("%Y-%m-%dT%H:%M:%S")))
        reset_restarts()
        return True
    except OSError as e:
        print("[!] Could not record the autostart intent in {} ({}): the stack "
              "still starts now, but a background supervisor cannot see that "
              "you want it up.".format(want_path(),
                                       getattr(e, "strerror", None) or e))
        return False


def want_clear(reason="cli"):
    """Record "the user said down". Absence of the file IS the record, so an
    already-absent want reads as success: idempotence is what makes `down`
    twice, or `down` with login off, still mean down."""
    path = want_path()
    try:
        if path.is_dir():
            path.rmdir()  # a folder where the record belongs is not a record
        else:
            path.unlink(missing_ok=True)
        return True
    except OSError as e:
        print("[!] Could not clear the autostart intent in {} ({}): a running "
              "supervisor may bring the stack back up.".format(
                  path, getattr(e, "strerror", None) or e))
        return False


def want_state():
    """The want record, or None for "no intent".

    Absence, an unreadable file and a file we cannot parse all read the same
    way: this file is the only authority that lets a background job start a
    printer, so a guess about junk is never allowed to be that authority. The
    user's last explicit command is; a mangled file is not."""
    path = want_path()
    if not path.is_file():
        return None
    try:
        text = paths.read_config_text(path)
    except OSError:
        return None
    out = {}
    for line in text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            out[key.strip()] = value.strip()
    return out if out.get("wanted by") else None


def sup_state_path():
    return paths.RUN / SUPERVISOR_STATE_NAME


def _boot_time():
    try:
        return process._boot_time()
    except Exception:
        return None


def read_sup_state():
    """Our supervisor's counter, {} when it is absent, corrupt, from an earlier
    boot or from another install. A latch must never survive a reboot: the
    board that was unplugged yesterday may be plugged in today."""
    try:
        data = json.loads(sup_state_path().read_text())
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    if data.get("app_home") != str(paths.APP_HOME):
        return {}
    if data.get("boot_time") not in (_boot_time(), None, "unknown"):
        return {}
    return data


def _write_sup_state(data):
    data = dict(data)
    data["app_home"] = str(paths.APP_HOME)
    data["boot_time"] = _boot_time()
    try:
        paths.RUN.mkdir(parents=True, exist_ok=True)
        paths.write_config(sup_state_path(),
                           json.dumps(data, indent=2, sort_keys=True) + "\n")
        return True
    except OSError:
        return False


def reset_restarts():
    """Forget the crash budget and the latch (a user spoke)."""
    _write_sup_state({"restarts": [], "latched": False})


def note_restart(now=None):
    """Record one klipper restart; returns the pruned count."""
    state = read_sup_state()
    now = time.time() if now is None else float(now)
    restarts = recent_restarts(state, now)
    restarts.append(now)
    _write_sup_state({"restarts": restarts,
                      "latched": bool(state.get("latched"))})
    return len(restarts)


def clear_sup_state():
    try:
        sup_state_path().unlink()
    except OSError:
        pass


def recent_restarts(state=None, now=None):
    """The klipper restarts still inside the budget window, junk-free: a ledger
    a human edited is not a reason to latch or to lie in `login status`."""
    state = read_sup_state() if state is None else state
    now = time.time() if now is None else float(now)
    out = []
    for value in (state.get("restarts") or []):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if now - float(value) <= RESTART_WINDOW:
                out.append(float(value))
    return out


def latched():
    return bool(read_sup_state().get("latched"))


# ------------------------------------------------------ the supervisor log --
def supervisor_log_path():
    return paths.LOGS / SUPERVISOR_LOG_NAME


def note(message, _path=None):
    """One line in logs/supervisor.log, timestamped. Never raises: a supervisor
    that cannot write its diary still supervises."""
    path = supervisor_log_path() if _path is None else Path(_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > LOG_ROTATE_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        with open(path, "a") as handle:
            handle.write("{} {}\n".format(time.strftime("%Y-%m-%d %H:%M:%S"),
                                          message))
    except OSError:
        pass


def log_tail(count=6):
    try:
        lines = supervisor_log_path().read_text(errors="replace").splitlines()
    except OSError:
        return []
    return lines[-int(count):]


# ------------------------------------------------------------- the policy ---
def decide(wanted, state, restarts=None, now=None, limit=MAX_KLIPPER_RESTARTS,
           window=RESTART_WINDOW):
    """The whole supervisor policy as data: (wanted, running dict or None) ->
    actions. No I/O, no signals, so the table below is testable as a table.

    wanted is falsy  -> nothing, ever. This is "down wins".
    state is None    -> cold start (a login, or a cleared state file).
    klipper down     -> a budgeted full restart: klippy is the printer, so no
                        print can be running and nothing live is disturbed.
    klipper up,
     moonraker down -> REPORT only. Restarting here would signal a live klipper
                        and kill a print over a web-API outage.
    only UIs/
     caffeinate down-> repair() - the free, already-existing path.
    """
    if not wanted:
        return []
    now = time.time() if now is None else float(now)
    if state is None:
        return [("start", None)]
    missing = [name for name in process.SERVICES if not state.get(name)]
    if not missing:
        return []
    if not state.get("klipper"):
        # bools are ints in Python, and `true` in a hand-edited ledger is not a
        # timestamp: process._ps_pid() draws the same line for the same reason.
        restarts = [float(t) for t in (restarts or [])
                    if isinstance(t, (int, float)) and not isinstance(t, bool)]
        fresh = [t for t in restarts if now - t <= window]
        if len(fresh) >= limit:
            return [("latch", None)]
        return [("core_restart", None)]
    if not state.get("moonraker"):
        return [("report_moonraker", None)]
    return [("repair", [n for n in missing if n not in CORE_SERVICES])]


def rest_answers(timeout=2.0):
    """Does our own Moonraker answer on localhost? Corroboration only: a stack
    whose processes are alive but that never serves is not worth a print."""
    url = "http://localhost:{}/server/info".format(paths.MOONRAKER_PORT)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as reply:
            body = json.loads(reply.read().decode(errors="replace"))
    except Exception:
        return False
    return isinstance(body, dict) and isinstance(body.get("result"), dict)


# The lock, taken without waiting (the batteryguard._cli_lock_nb pattern): a
# supervisor that queues behind a `setup` blocks a human's own command, and one
# that barges in without the lock can resurrect a stack `down` is mid-way
# through stopping. Busy means "come back on the next sweep".
def _cli_lock_nb():
    for _ in range(3):
        try:
            paths.RUN.mkdir(parents=True, exist_ok=True)
            handle = open(paths.CLI_LOCK, "a+")
        except OSError:
            return None
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            time.sleep(0.2)
            continue
        return handle
    return None


def _unlock(handle):
    if handle is None:
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        pass
    handle.close()


def single_instance(lock_path=None):
    """flock(run/supervise.lock) for the supervisor itself.

    launchd will not start two (it serialises its own jobs), but `login on`
    kickstarts, a second install of the same home can bootstrap, and a human can
    always run `klipperformac supervise` in a terminal. (handle, None) when this
    process is now the only supervisor, (None, reason) when another one is."""
    path = paths.RUN / "supervise.lock" if lock_path is None else Path(lock_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+")
    except OSError as e:
        return None, "cannot open {} ({})".format(path,
                                                  getattr(e, "strerror", None) or e)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None, "another supervisor already holds {}".format(path)
    return handle, None


def sweep(now=None, rest=None, state_reader=None):
    """One supervisor pass. Returns (outcome, actions) for the log/tests.

    `rest` and `state_reader` are seams: the offline suite hands in recorders so
    "it never signals a service that answers" is a fact about recorded calls,
    not a claim in a comment."""
    now = time.time() if now is None else float(now)
    rest = rest_answers if rest is None else rest
    state_reader = process.running if state_reader is None else state_reader
    if not installed():
        # Someone removed the agent under us (hand-deleted, `login off` raced the
        # bootout). Leaving the stack exactly as the user left it is the only
        # honest move for a background job, then exit.
        return "agent gone", []
    wanted = want_state()
    if not wanted:
        return "held down", []
    handle = _cli_lock_nb()
    if handle is None:
        return "busy", []  # a human command owns the stack right now
    try:
        state = state_reader()
        recorded = read_sup_state()
        restarts = recorded.get("restarts") or []
        pending = float(recorded.get("pending_start") or 0.0)
        actions = decide(wanted, state, restarts, now)
        if not actions and state and all(state.get(n) for n in CORE_SERVICES):
            if not pending:
                return "up", []        # steady state: nothing we started, nothing owed
            if now - pending <= START_GRACE:
                return "starting", []  # klippy is still loading printer.cfg
            if rest():
                _write_sup_state({"restarts": list(restarts), "latched": False,
                                  "pending_start": 0.0})
                note("stack ready: moonraker answers on port {}".format(
                    paths.MOONRAKER_PORT))
                return "up", []
            if now - pending < START_TIMEOUT:
                return "starting", []
            # Processes alive, API silent for a full START_TIMEOUT after a start
            # we ordered: that is a wedged klippy, not a slow one, and the only
            # repair we have is the same budgeted restart.
            actions = [("core_restart", None)]
        for action, value in apply_actions(actions, state, now):
            note("sweep: {} {}".format(action, value or "").strip())
        return "acted", actions
    finally:
        _unlock(handle)


def _mark_pending(now, latched=False):
    """Remember that the stack we just started owns an unproven claim: the
    sweep after this one checks the API before it believes the pids."""
    _write_sup_state({"restarts": list(read_sup_state().get("restarts") or []),
                      "latched": bool(latched), "pending_start": float(now)})


def apply_actions(actions, state, now=None):
    """Do what `decide` said. Returns the (action, detail) list it performed.

    A klipper/moonraker restart is `stop()` + `start()`: this module owns no
    per-service spawn helpers (process.py's start() builds every argv inline),
    and that is exactly why decide() refuses to act when klipper is up - the
    only path we have also touches a live klipper, so it is taken only when
    klippy itself is the thing that is gone."""
    now = time.time() if now is None else float(now)
    done = []
    for action, value in actions:
        if action == "start":
            if not process.start():
                note_restart(now)
                done.append((action, "refused: a port is held or the record "
                                     "could not be written"))
                continue
            _mark_pending(now)
            done.append((action, "stack started"))
        elif action == "core_restart":
            count = note_restart(now)
            process.stop()
            if process.start():
                _mark_pending(now)
                done.append((action, "klipper was down: restarted the stack "
                                     "({} of {} in {}s)".format(
                                         count, MAX_KLIPPER_RESTARTS,
                                         int(RESTART_WINDOW))))
            else:
                done.append((action, "restart refused; look at "
                                     "klipperformac logs"))
        elif action == "repair":
            started = process.repair(state)
            done.append((action, ",".join(started) if started else "nothing"))
        elif action == "report_moonraker":
            done.append((action, "moonraker down while klipper is up: NOT "
                                 "restarting (a print may be running) - run: "
                                 "klipperformac restart"))
        elif action == "latch":
            _write_sup_state({"restarts": list(
                read_sup_state().get("restarts") or []), "latched": True,
                "pending_start": 0.0})
            done.append((action, "klipper died {} times in {}s - NOT "
                                 "restarting. Check `klipperformac logs klipper`,"
                                 " then `klipperformac up`.".format(
                                     MAX_KLIPPER_RESTARTS, int(RESTART_WINDOW))))
    return done


def _on_signal(signum, _frame):
    """SIGTERM/SIGINT = launchd bootout, logout, or a human's Ctrl-C. Exit 0:
    a deliberate stop must never look like a crash to KeepAlive, and must never
    take the stack down with it (AbandonProcessGroup + start_new_session)."""
    _STOP[0] = True


def _install_handlers():
    for number in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(number, _on_signal)
        except (ValueError, OSError):
            return  # not the main thread (a test, or the TUI calling us)


def _sleep_slices(total, slice_length=0.5):
    """Sleep in slices so a bootout is obeyed within half a second."""
    deadline = time.time() + max(float(total), 0.0)
    while not _STOP[0] and time.time() < deadline:
        remaining = deadline - time.time()
        time.sleep(min(slice_length, max(remaining, 0.0)) or 0.05)


def run(tick=None, sweeps=None, handlers=True, rest=None, state_reader=None,
        sleeper=None):
    """`klipperformac supervise`: the resident loop launchd keeps alive.

    Returns the exit code: always 0, because every way this can end is a
    decision (SIGTERM, the agent removed, another supervisor already running)
    rather than a crash - a nonzero return would make launchd respawn it against
    the user's intent."""
    _STOP[0] = False
    tick = _clean_tick(paths.load_settings().get("autostart_tick")
                       if tick is None else tick)
    if handlers:
        _install_handlers()
    sleeper = _sleep_slices if sleeper is None else sleeper
    handle, why = single_instance()
    if handle is None:
        note("supervisor not started: {}".format(why))
        print("Another supervisor is already running ({}).".format(why))
        return 0
    note("supervisor start pid={} tick={} label={}".format(
        os.getpid(), _tick_text(tick), label()))
    count = 0
    outcome = "idle"
    try:
        while not _STOP[0]:
            try:
                outcome, _actions = sweep(now=None, rest=rest,
                                          state_reader=state_reader)
            except Exception as e:  # a sweep must never be able to kill itself
                note("sweep failed: {}".format(e))
                outcome = "failed"
            count += 1
            if outcome == "agent gone":
                break
            if sweeps is not None and count >= int(sweeps):
                break
            if _STOP[0]:
                break
            sleeper(tick)
    finally:
        _unlock(handle)
        note("supervisor exiting after {} sweep(s) ({})".format(count, outcome))
    return 0


# ------------------------------------------------------- the CLI surface ----
def home_dir():
    """The account's home, as launchd resolves ~ for an agent.

    A function rather than a Path.home() call in the warning below, so the
    offline suite can put a data folder under a fake ~/Documents without
    creating - or reading - anything inside the real one."""
    return Path.home()


def data_dir_warning():
    """A data folder macOS may hide from a background job (privacy)."""
    data = str(paths.DATA)
    for name in ("Documents", "Desktop", "Downloads"):
        if data.startswith(str(home_dir() / name) + os.sep):
            return ("[!] Your data folder is inside ~/{}: macOS can deny a "
                    "background job access to those folders. If the stack does "
                    "not come up at login, move it: klipperformac data --set "
                    "~/.klipperformac/data".format(name))
    return None


def login_on(tick=None, dry_run=False):
    """`klipperformac login on`: install + bootstrap + want.

    The load is deliberately the last step, and the want record is written
    before it, so a supervisor launchd starts at bootstrap already sees the
    user's intent instead of reading an empty run/ folder as "leave it down"."""
    lines = []
    if not paths.VENV_PY.exists():
        return False, ["[!] This install has no venv python at {} yet: run "
                       "klipperformac setup first.".format(paths.VENV_PY)]
    ok, plist_lines = install_plist(tick=tick, dry_run=dry_run)
    lines.extend(plist_lines)
    if not ok:
        return False, lines
    warn = data_dir_warning()
    if warn:
        lines.append(warn)
    if dry_run:
        lines.append("Would load it into {} and ask launchd to keep the "
                     "supervisor alive.".format(domain()))
        want = want_state()
        lines.append("Stack intent: {}".format(
            "up (kept)" if want else "down - a login would start nothing until "
                                     "you run: klipperformac up"))
        return True, lines
    if not want_state():
        want_set("login on")
    ok, detail = bootstrap()
    info = job_info()
    if not ok:
        lines.append("[!] Could not load the agent into {}: {}".format(
            domain(), detail))
        lines.append("    Try by hand: launchctl bootstrap {} {}".format(
            domain(), plist_path()))
        return False, lines
    lines.append("Login start: ON  ({} {})".format(domain(), detail))
    if info.get("state") == "running":
        lines.append("  supervisor: running (pid {})".format(info.get("pid")))
    else:
        lines.append("  supervisor: {} - launchd starts it at your next "
                     "login; nudge it now with: launchctl kickstart -k {}/{}"
                     .format(info.get("state"), domain(), label()))
    if info.get("loaded") is None:
        lines.append("  [!] Cannot confirm the job is loaded: " +
                     str(info.get("detail")))
    lines.append("  stack intent: up (a background supervisor brings the stack "
                 "up when it is down)")
    lines.append("  restart policy: launchd restarts the supervisor only if it "
                 "exits nonzero, at most once every {}s".format(
                     THROTTLE_INTERVAL))
    lines.append("  logs: {}".format(supervisor_log_path()))
    lines.append("  note: macOS starts this at your next GUI login; the camera "
                 "is never started automatically.")
    lines.append("  Switch the stack off without switching this off: "
                 "klipperformac down (the supervisor will not fight it).")
    return True, lines


def login_off():
    """`klipperformac login off`: bootout, then remove the file.

    Order matters: booting the job out SIGTERMs the supervisor, which exits 0
    and leaves the stack alone. Removing the plist first would leave a loaded
    job whose file is gone, which launchd remembers for the rest of the session.
    No read-only probe comes first either: bootout() is idempotent, so asking
    whether the job is loaded before unloading it would only add a question."""
    lines = []
    ok, detail = bootout()
    if ok:
        lines.append("LaunchAgent unloaded from {} ({}).".format(domain(), detail))
    else:
        lines.append("[!] launchctl bootout did not confirm: {}".format(detail))
    removed, rm_lines = remove_plist()
    lines.extend(rm_lines)
    if not removed:
        lines.append("    Login start is still ON as far as the file on disk is "
                     "concerned. Clear it by hand: launchctl bootout {}/{} && "
                     "rm {}".format(domain(), label(), plist_path()))
        return False, lines
    want_clear("login off")
    lines.append("Login start: OFF. The stack keeps running until you run: "
                 "klipperformac down")
    if not ok and "no usable launchctl" in detail:
        lines.append("  [!] launchd could not be asked about the job: no "
                     "usable launchctl found.")
    return True, lines


def login_status():
    """One dict for both `login status` and `login status --json`: the GUI's
    future autostart row reads this and nothing else."""
    info = job_info()
    want = want_state()
    state = read_sup_state()
    restarts = recent_restarts(state)
    stale = plist_stale()
    return {
        "label": label(),
        "domain": domain(),
        "plist": str(plist_path()),
        "installed": installed(),
        "stale": stale,
        "job_loaded": info.get("loaded"),
        "job_state": info.get("state"),
        "job_pid": info.get("pid"),
        "disabled_in_settings": domain_disabled(),
        "tick": _current_tick(),
        "throttle_interval": THROTTLE_INTERVAL,
        "keep_alive": read_plist().get("KeepAlive"),
        "stack_intent": "up" if want else "down",
        "intent_reason": (want or {}).get("wanted by"),
        "latched": bool(state.get("latched")),
        "klipper_restarts_in_window": len(restarts),
        "supervisor_log": str(supervisor_log_path()),
        "agent_log_out": str(paths.LOGS / AGENT_OUT_NAME),
        "agent_log_err": str(paths.LOGS / AGENT_ERR_NAME),
        "launchctl_detail": info.get("detail"),
    }


def _current_tick():
    args = read_plist().get("ProgramArguments") or []
    text = [str(a) for a in args]
    if "--tick" in text:
        try:
            return float(text[text.index("--tick") + 1])
        except (IndexError, ValueError):
            return DEFAULT_TICK
    return DEFAULT_TICK


def status_lines(status=None):
    st = login_status() if status is None else status
    head = "Login start: ON" if st["installed"] else "Login start: off"
    lines = [head + "  ({})".format(st["plist"])]
    if not st["installed"]:
        if st["stale"]:
            lines.append("  [!] an agent file is there but it is not this "
                         "install's: {}".format(st["stale"]))
        lines.append("  Turn it on: klipperformac login on")
        return lines
    if st["job_loaded"] is None:
        lines.append("  launchd: unknown ({})".format(st["launchctl_detail"]))
    elif st["job_loaded"]:
        lines.append("  launchd: {}{}".format(
            st["job_state"], " (pid {})".format(st["job_pid"])
            if st["job_pid"] else ""))
    else:
        lines.append("  [!] launchd: not loaded{}".format(
            " - macOS has it switched off (System Settings > General > Login "
            "Items)" if st["disabled_in_settings"] else
            "; re-run: klipperformac login on"))
    if st["stale"]:
        lines.append("  [!] the agent is stale: {} - re-run: klipperformac "
                     "login on".format(st["stale"]))
    lines.append("  stack intent: {}{}".format(
        st["stack_intent"],
        " ({})".format(st["intent_reason"]) if st["intent_reason"] else ""))
    if st["latched"]:
        lines.append("  [!] latched: klipper crashed too often, the supervisor "
                     "stopped restarting it. `klipperformac up` clears this.")
    if st["klipper_restarts_in_window"]:
        lines.append("  klipper restarts this boot/window: {} of {}".format(
            st["klipper_restarts_in_window"], MAX_KLIPPER_RESTARTS))
    lines.append("  sweep {}s, launchd throttle {}s".format(
        _tick_text(st["tick"]), st["throttle_interval"]))
    lines.append("  logs: {}".format(st["supervisor_log"]))
    return lines


def uninstall_teardown():
    """`uninstall` step 0: the agent must be gone before the tree it points at
    is. (ok, lines)

    Without this, launchd respawns a supervisor whose interpreter and data
    folder were just rmtree'd, and the user's next login is a crash loop with no
    log files. bootout happens before the unlink for the same reason
    `login off` orders them that way."""
    path = plist_path()
    loaded = job_info().get("loaded")
    if loaded is False and not path.is_file():
        return True, []  # nothing to tear down: uninstall stays silent
    lines = []
    ok, detail = bootout()
    if ok:
        lines.append("LaunchAgent unloaded from {}{}.".format(
            domain(), "" if detail == "unloaded" else " ({})".format(detail)))
    else:
        lines.append("[!] Could not unload the agent: {}".format(detail))
    if not path.is_file():
        return ok, lines
    removed, rm_lines = remove_plist()
    lines.extend(rm_lines)
    if not removed:
        lines.append("    Refusing to delete a login item this tool did not "
                     "write: {}".format(path))
        return False, lines
    if not ok:
        lines.append("    The file is gone but launchd still has the job for "
                     "this session: launchctl bootout {}/{}".format(
                         domain(), label()))
        return False, lines
    clear_sup_state()
    return True, lines
