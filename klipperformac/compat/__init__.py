"""macOS compatibility shims for upstream components.

Build-time: two headers missing on macOS (<linux/can.h>, <sys/prctl.h>) are
supplied via CPATH, so pristine Klipper sources compile bit-for-bit unchanged
(the GNU make manual documents that CPATH dirs are searched ahead of system
dirs for #include <...>; verified empirically with `gcc -E -v`).

Run-time: a `sitecustomize.py` in the venv we create (CPython auto-imports
it) supplies Linux-only attributes Moonraker touches, asks for a USER_INTERACTIVE
QoS class for the klippy child alone (the closest macOS gets to real-time
scheduling without root), and an `ip` shim on Moonraker's PATH emulates
`ip -json -det address` from `ifconfig`.

Upstream Klipper/Moonraker checkouts are never edited by any of this.
"""
import os
import stat
import subprocess
from pathlib import Path

INCLUDE = str((Path(__file__).resolve().parent / "include"))

# The single switch the sitecustomize QoS block reads. Kept here so
# process.start() (which sets it) and shim_env()/web_env (which must not carry
# it) cannot drift apart, and so `klipperformac timing` can look for the exact
# string the shim looks for.
QOS_ENV_VAR = "KLIPPERFORMAC_QOS"
QOS_ENV_VALUE = "ui"

SITECUSTOMIZE_SRC = '''\
"""klipperformac runtime shim (auto-imported by the venv's Python).

macOS Python lacks a few Linux-only attributes Moonraker touches:
- time.CLOCK_BOOTTIME: proc_stats calls time.clock_gettime() on it
  unconditionally; a 500 here strands Mainsail on "initializing".
  CLOCK_MONOTONIC is the closest macOS equivalent.
- socket.SOCK_NONBLOCK / SOCK_CLOEXEC: machine._find_public_ip() probes the
  local address with a UDP socket; the flags only make it nonblocking/
  close-on-exec, and the probe never reads or writes, so defining them as 0
  keeps behavior correct (socket type stays a plain SOCK_DGRAM).

And a scheduler shim for klippy alone: macOS has no SCHED_FIFO and a *negative*
nice is root-only (measured on this Mac: os.setpriority(PRIO_PROCESS, 0, -5)
-> EACCES, os.nice(-1) -> EPERM, renice -n -5 -> Permission denied), so the one
posture still available without root is a QoS class request
(pthread_set_qos_class_self_np, sys/qos.h). It runs only when KLIPPERFORMAC_QOS
is "ui", which process.start() puts in the klippy child's environment and in
nobody else's; every failure path here is silent, because a machine where the
symbol is missing must still boot klippy. sys/qos.h:51 - this "does not allow
threads to supersede any limits that may be applied" to the process, so it is a
request, not a guarantee.
Generated file; re-created by setup/up.
"""
import os as _os
import socket
import time

if not hasattr(time, "CLOCK_BOOTTIME"):
    time.CLOCK_BOOTTIME = time.CLOCK_MONOTONIC

for _flag, _name in ((0x800, "SOCK_CLOEXEC"), (0x4000, "SOCK_NONBLOCK")):
    if not hasattr(socket, _name):
        setattr(socket, _name, 0)

_QOS_UI = 0x21                # QOS_CLASS_USER_INTERACTIVE (sys/qos.h:131)


def _klipperformac_set_qos():
    """Ask for USER_INTERACTIVE on THIS thread; False when not available."""
    try:
        import ctypes
        lib = ctypes.CDLL(None, use_errno=True)
        fn = lib.pthread_set_qos_class_self_np   # AttributeError -> bail
        fn.argtypes = [ctypes.c_uint, ctypes.c_int]
        # relative priority 0: the range is 0..QOS_MIN_RELATIVE_PRIORITY (-15,
        # sys/qos.h:153) and a negative one only ever loosens the request.
        return fn(ctypes.c_uint(_QOS_UI), ctypes.c_int(0)) == 0
    except Exception:
        return False


if _os.environ.get("KLIPPERFORMAC_QOS", "") == "ui":
    _klipperformac_set_qos()
    import threading as _th
    # Threads inherit the QoS class of the thread that created them, so
    # klippy's reactor/serial/helpers each have to ask again ON the new thread
    # itself. _bootstrap_inner is the first code that runs there, and every
    # library here is a caller of threading.Thread, so the patch is guarded to
    # be applied once - re-importing this file must not stack wrappers.
    if not getattr(_th.Thread, "_klipperformac_qos", False):
        _orig_boot = _th.Thread._bootstrap_inner

        def _qos_boot(self, *a, **kw):      # runs ON the new thread
            _klipperformac_set_qos()
            return _orig_boot(self, *a, **kw)

        _th.Thread._bootstrap_inner = _qos_boot
        _th.Thread._klipperformac_qos = True
'''

# Emits what Moonraker parses: operstate, link_type, address, addr_info with
# family/local/scope (scope "link" is treated as link-local by Moonraker).
IP_SHIM_SRC = '''\
#!/usr/bin/python3
"""klipperformac shim: minimal `ip -json -det address` on macOS (ifconfig)."""
import json
import re
import subprocess
import sys

out = subprocess.run(["/sbin/ifconfig", "-a"], capture_output=True).stdout.decode(
    errors="replace")
addrs = []
cur = None
for line in out.splitlines():
    m = re.match(r"^(\\w[\\w.]*?):(\\s|$)", line)
    if m:
        if cur:
            addrs.append(cur)
        cur = {"ifname": m.group(1), "addr_info": [], "operstate": "DOWN",
               "link_type": "ether", "address": "00:00:00:00:00:00",
               "txqlen": 0}
        if re.search(r"flags=\\d+<[^>]*\\bLOOPBACK\\b", line):
            cur["link_type"] = "loopback"
        continue
    if cur is None:
        continue
    line = line.strip()
    if line.startswith("status:"):
        cur["operstate"] = "UP" if line.split(":", 1)[1].strip() == "active" \\
            else "DOWN"
    elif line.startswith("ether "):
        cur["address"] = line.split(None, 1)[1].split()[0]
    elif line.startswith("inet6 "):
        local = line.split()[1].split("%", 1)[0]
        scope = ("host" if local == "::1" else
                 "link" if local.startswith("fe80") else "global")
        cur["addr_info"].append(
            {"family": "inet6", "local": local, "scope": scope})
    elif line.startswith("inet "):
        p = line.split()
        local = p[1]
        cur["addr_info"].append(
            {"family": "inet", "local": local, "label": cur["ifname"],
             "scope": "host" if local.startswith("127.") else "global"})
if cur:
    addrs.append(cur)
if sys.argv[1:]:
    joined = " ".join(sys.argv[1:])
    if "route" in joined:
        print("[]")
        sys.exit(0)
print(json.dumps([a for a in addrs if a["addr_info"] or a["link_type"] ==
                  "loopback"]))
'''

SHIMS_DIR_NAME = "shims"


def build_env(base=None):
    env = dict(os.environ if base is None else base)
    existing = env.get("CPATH")
    env["CPATH"] = INCLUDE if not existing else INCLUDE + os.pathsep + existing
    return env


def invalidate_stale_build(klipper_dir):
    """Klipper's mtime check cannot see CPATH headers; force a rebuild when a
    shim is newer than the compiled c_helper.so."""
    so = Path(klipper_dir) / "klippy" / "chelper" / "c_helper.so"
    if not so.exists():
        return False
    newest = max(
        (f.stat().st_mtime for f in Path(INCLUDE).rglob("*.h")), default=0)
    if newest > so.stat().st_mtime:
        so.unlink()
        return True
    return False


def _write_if_changed(path, content, executable=False):
    try:
        if path.exists() and path.read_text() == content:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if executable:
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP |
                       stat.S_IXOTH)
        return True
    except OSError:
        return False


def ensure_runtime_shims(venv_python):
    """Idempotent: sitecustomize in the venv + `ip` shim in APP_HOME/shims.
    Returns True when anything changed (caller may want a restart)."""
    from .. import paths
    changed = _write_if_changed(
        Path(paths.APP_HOME) / SHIMS_DIR_NAME / "ip", IP_SHIM_SRC,
        executable=True)
    try:
        purelib = subprocess.run(
            [str(venv_python), "-c",
             "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
            capture_output=True, check=True).stdout.decode().strip()
    except (OSError, subprocess.CalledProcessError):
        return changed
    if purelib:
        sitecustomize = Path(purelib) / "sitecustomize.py"
        if sitecustomize.exists() and \
                "klipperformac" not in sitecustomize.read_text():
            pass  # user-provided sitecustomize: do not clobber
        else:
            changed = _write_if_changed(sitecustomize,
                                        SITECUSTOMIZE_SRC) or changed
    return changed


def shim_env(base=None):
    """Moonraker's environment: our `ip` shim on PATH, and never the QoS switch.

    KLIPPERFORMAC_QOS is klippy's alone (process.start() sets it for the klippy
    child only). Stripping it here is what keeps that true when a user exports it
    in their shell: moonraker is not on the step-timing path, and a blanket
    boost is not what the mitigation asks for.
    """
    from .. import paths
    env = dict(os.environ if base is None else base)
    env.pop(QOS_ENV_VAR, None)
    shims = str(paths.APP_HOME / SHIMS_DIR_NAME)
    env["PATH"] = shims + os.pathsep + env.get("PATH", os.defpath)
    return env
