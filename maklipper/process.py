"""Process lifecycle for the single Klipper instance (klippy -> moonraker -> web).

Modeled on the startup sequence of the `instances` app, adapted for macOS:
  * c_helper.so compiles at first launch with CPATH pointed at our compat
    shims; upstream Klipper sources stay untouched.
  * Process identity is re-validated on every read (pid + cmdline marker +
    boot time), so stale state files after crashes/reboots are tolerated.
  * caffeinate runs alongside the print so macOS sleep can't kill it.
"""
import json
import os
import re
import signal
import subprocess
import time

from . import paths
from .compat import build_env

SERVICES = ("web", "moonraker", "klipper", "caffeinate")
MARKERS = {
    "klipper": "klippy.py",
    "moonraker": "moonraker",
    "web": "http.server",
    "caffeinate": "caffeinate",
}


def _boot_time():
    out = subprocess.run(
        ["sysctl", "-n", "kern.boottime"], capture_output=True
    ).stdout.decode()
    m = re.search(r"sec = (\d+)", out)
    return m.group(1) if m else ""


def _load():
    if not paths.STATE.exists():
        return None
    try:
        with open(paths.STATE) as f:
            state = json.load(f)
    except Exception:
        return None
    if state.get("boot_time") != _boot_time():
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


def alive(name, pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    out = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True
    ).stdout.decode()
    return MARKERS[name] in out


def running():
    """{name: bool} from persisted state, or None if not started at all."""
    state = _load()
    if state is None:
        return None
    return {name: alive(name, state.get(name)) for name in SERVICES}


def start():
    if running() is not None:
        return False
    paths.ensure_dirs()

    py = str(paths.VENV_PY)
    klipper_klippy = paths.KLIPPER / "klippy"
    env = build_env()

    klog = open(paths.KLIPPY_LOG, "ab")
    mlog = open(paths.MOONRAKER_LOG, "ab")
    try:
        kproc = subprocess.Popen(
            [py, str(klipper_klippy / "klippy.py"), "-a", str(paths.API_SOCKET),
             str(paths.PRINTER_CFG)],
            cwd=str(klipper_klippy), env=env, stdout=klog, stderr=klog,
        )
        mproc = subprocess.Popen(
            [py, "-m", "moonraker",
             "-d", str(paths.DATA), "-c", str(paths.MOONRAKER_CONF)],
            cwd=str(paths.MOONRAKER), stdout=mlog, stderr=mlog,
        )
        wproc = subprocess.Popen(
            [py, "-m", "http.server", str(paths.WEB_PORT),
             "--directory", str(paths.WEB)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        cproc = subprocess.Popen(
            ["caffeinate", "-dis"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    finally:
        klog.close()
        mlog.close()

    _save({"klipper": kproc, "moonraker": mproc, "web": wproc, "caffeinate": cproc})
    return True


def stop(timeout=6.0):
    state = _load()
    if state is None:
        return False
    for name in ("web", "moonraker", "klipper", "caffeinate"):
        pid = state.get(name)
        if pid and alive(name, pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(alive(n, state.get(n)) for n in SERVICES):
            break
        time.sleep(0.2)
    for name in SERVICES:
        pid = state.get(name)
        if pid and alive(name, pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    _clear()
    return True
