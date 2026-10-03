"""Process lifecycle for the single Klipper instance (klipper -> moonraker -> UIs).

Modeled on the startup sequence of the `instances` app, adapted for macOS:
  * c_helper.so compiles at first launch with CPATH pointed at our compat
    shims; upstream Klipper sources stay untouched.
  * Process identity is re-validated on every read (pid + cmdline marker +
    boot time), so stale state files after crashes/reboots are tolerated;
    a fully-dead state self-heals on next start().
  * Children get their own sessions (start_new_session) so closing the
    terminal cannot kill a running print, and caffeinate guards sleep.
"""
import json
import os
import re
import signal
import subprocess
import time

from . import paths
from .compat import build_env, invalidate_stale_build

SERVICES = ("web_mainsail", "web_fluidd", "moonraker", "klipper", "caffeinate")
MARKERS = {
    "klipper": "klippy.py",
    "moonraker": "moonraker",
    "web_mainsail": "klipperformac.proxy {} ".format(paths.WEB_PORT),
    "web_fluidd": "klipperformac.proxy {} ".format(paths.FLUIDD_PORT),
    "caffeinate": "caffeinate",
}
_STOP_ORDER = ("web_mainsail", "web_fluidd", "moonraker", "klipper", "caffeinate")


_boot_cache = [0.0, None]  # [monotonic, value]; sysctl is slow per keystroke


def _boot_time():
    now = time.monotonic()
    if now - _boot_cache[0] < 5.0:
        return _boot_cache[1]
    try:
        out = subprocess.run(
            ["sysctl", "-n", "kern.boottime"], capture_output=True,
            check=True).stdout.decode()
    except Exception:
        bt = None
    else:
        m = re.search(r"sec = (\d+)", out)
        bt = m.group(1) if m else None
    _boot_cache[0] = now
    _boot_cache[1] = bt
    return bt


def _load():
    if not paths.STATE.exists():
        return None
    try:
        with open(paths.STATE) as f:
            state = json.load(f)
    except Exception:
        return None
    bt = _boot_time()
    if bt is None or state.get("boot_time") != bt:
        _clear()
        return None
    return state


def _clear():
    try:
        paths.STATE.unlink()
    except OSError:
        pass


def _save(procs):
    paths.RUN.mkdir(parents=True, exist_ok=True)
    data = {"boot_time": _boot_time(), "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    for name, proc in procs.items():
        data[name] = proc.pid
    with open(paths.STATE, "w") as f:
        json.dump(data, f, indent=2)


def _live_cmds(pids):
    """pid -> command in one `ps` call; zombie entries are dropped."""
    pids = [str(p) for p in pids if p]
    if not pids:
        return {}
    try:
        out = subprocess.run(
            ["ps", "-p", ",".join(pids), "-o", "pid=,state=,command="],
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
    return {name: bool(pids.get(name)) and
            MARKERS[name] in live.get(str(pids[name]), "")
            for name in SERVICES}


def _popen(cmd, **kw):
    defaults = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True)
    defaults.update(kw)
    return subprocess.Popen(cmd, **defaults)


def start():
    state = running()
    if state is not None:
        if any(state.values()):
            return False
        _clear()  # crashed/stale: self-heal, then start fresh
    paths.ensure_dirs()
    invalidate_stale_build(paths.KLIPPER)

    py = str(paths.VENV_PY)
    klipper_klippy = paths.KLIPPER / "klippy"
    env = build_env()

    kproc = _popen(
        [py, str(klipper_klippy / "klippy.py"),
         "-a", str(paths.API_SOCKET), "-l", str(paths.KLIPPY_LOG),
         str(paths.PRINTER_CFG)],
        cwd=str(klipper_klippy), env=env,
    )
    mproc = _popen(
        [py, "-m", "moonraker",
         "-d", str(paths.DATA), "-c", str(paths.MOONRAKER_CONF),
         "-l", str(paths.MOONRAKER_LOG)],
        cwd=str(paths.MOONRAKER),
    )
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    web_env = dict(os.environ)
    web_env["PYTHONPATH"] = repo_root + (
        os.pathsep + web_env["PYTHONPATH"] if web_env.get("PYTHONPATH") else "")

    wproc = _popen(
        [py, "-m", "klipperformac.proxy", str(paths.WEB_PORT),
         str(paths.WEB_MAINSAIL), str(paths.MOONRAKER_PORT)],
        cwd=repo_root, env=web_env,
    )
    fproc = _popen(
        [py, "-m", "klipperformac.proxy", str(paths.FLUIDD_PORT),
         str(paths.WEB_FLUIDD), str(paths.MOONRAKER_PORT)],
        cwd=repo_root, env=web_env,
    )
    cproc = _popen(["caffeinate", "-dis"])

    _save({"klipper": kproc, "moonraker": mproc, "web_mainsail": wproc,
           "web_fluidd": fproc, "caffeinate": cproc})
    return True


def stop(timeout=6.0):
    state = _load()
    if state is None:
        return False
    pids = [(n, state.get(n)) for n in _STOP_ORDER if state.get(n)]

    def _alive_pids():
        allp = [p for _, p in pids]
        live = _live_cmds(allp)
        return [p for _, p in pids if str(p) in live]

    for _, pid in pids:
        _signal(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive_pids():
            break
        time.sleep(0.2)
    stragglers = [(n, p) for n, p in pids if str(p) in _alive_pids()]
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
        if not _alive_pids():
            break
        time.sleep(0.2)
    still = [n for n, p in stragglers if str(p) in _alive_pids()]
    if still:
        for n in _STOP_ORDER:
            if n in still:
                print("[!] {} (pid {}) refused to die; leaving state file for "
                      "inspection: {}".format(n, dict(pids).get(n), paths.STATE))
        return False
    _clear()
    return True


def _signal(pid, sig):
    try:
        os.kill(pid, sig)
    except OSError:
        pass
