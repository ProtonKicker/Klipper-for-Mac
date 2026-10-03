"""macOS compatibility shims for upstream components.

Build-time: two headers missing on macOS (<linux/can.h>, <sys/prctl.h>) are
supplied via CPATH, so pristine Klipper sources compile bit-for-bit unchanged
(the GNU make manual documents that CPATH dirs are searched ahead of system
dirs for #include <...>; verified empirically with `gcc -E -v`).

Run-time: a `sitecustomize.py` in the venv we create (CPython auto-imports
it) supplies Linux-only attributes Moonraker touches, and an `ip` shim on
Moonraker's PATH emulates `ip -json -det address` from `ifconfig`.

Upstream Klipper/Moonraker checkouts are never edited by any of this.
"""
import os
import stat
import subprocess
from pathlib import Path

INCLUDE = str((Path(__file__).resolve().parent / "include"))

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
Generated file; re-created by setup/up.
"""
import socket
import time

if not hasattr(time, "CLOCK_BOOTTIME"):
    time.CLOCK_BOOTTIME = time.CLOCK_MONOTONIC

for _flag, _name in ((0x800, "SOCK_CLOEXEC"), (0x4000, "SOCK_NONBLOCK")):
    if not hasattr(socket, _name):
        setattr(socket, _name, 0)
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
    from .. import paths
    env = dict(os.environ if base is None else base)
    shims = str(paths.APP_HOME / SHIMS_DIR_NAME)
    env["PATH"] = shims + os.pathsep + env.get("PATH", os.defpath)
    return env
